"""Probabilistic matching over hashed signals.

Default engine is a small built-in Fellegi-Sunter scorer (m/u probabilities per
comparison, Bayes factors summed in log space). ``MATCH_ENGINE=splink`` swaps in
Splink (DuckDB backend) for scoring when it's installed and there's enough data;
blocking, rules and the merge policy stay the same.

Policy (see README "Resolution rules"):

* Pairs are only formed by blocking on a shared device, device id or network hash.
* The household IP (``hh_ip``) is deliberately not evidence: sharing a home
  router says two profiles live together, not that they are one person.
  Candidates inside one household are flagged so reviewers see it.
* Both profiles must have ``identity_resolution`` consent, or the pair is skipped.
* Auto-merge only anonymous → known, and only at ``AUTO_MERGE_THRESHOLD``.
* An anonymous profile matching more than one known person is never auto-merged.
* Known ↔ known pairs (e.g. a shared family laptop) only ever go to human review.
"""
from __future__ import annotations

import math
from collections import defaultdict

from db import fetchall, fetchone, insert_row, update_row
from domain.audit import audit
from domain.consent import can_link_probabilistically
from domain.identity import canonical_id, get_profile, merge
from settings import AUTO_MERGE_THRESHOLD, MATCH_ENGINE, REVIEW_THRESHOLD
from util import dumps, loads, new_id, now_iso

PRIOR = 0.001  # P(match) for a random blocked pair before evidence

# (m, u): P(agree | match), P(agree | non-match). Disagreement uses (1-m, 1-u).
COMPARISONS = {
    "device_id": (0.95, 0.0001),  # explicit app/SDK id: one physical device (may still be shared)
    "device": (0.90, 0.0005),
    "net": (0.70, 0.01),
    "ua_family": (0.95, 0.30),
    "tz": (0.98, 0.40),
    "lang": (0.97, 0.60),
    "interests": (0.60, 0.10),
}


def _signal_sets(tenant_id: str) -> dict[str, dict[str, set]]:
    out: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    rows = fetchall(
        "select s.profile_id, s.signal_type, s.value_hash from signal s "
        "join profile p on p.id = s.profile_id "
        "where s.tenant_id=? and p.merged_into is null and p.erased_at is null",
        [tenant_id],
    )
    for r in rows:
        out[r["profile_id"]][r["signal_type"]].add(r["value_hash"])
    return out


def _interest_set(profile: dict) -> set:
    return {f"{k}:{v}" for k, bucket in loads(profile.get("interests_json")).items() for v in bucket}


def blocked_pairs(tenant_id: str) -> set[tuple[str, str]]:
    rows = fetchall(
        "select distinct a.profile_id as l, b.profile_id as r from signal a "
        "join signal b on a.tenant_id=b.tenant_id and a.signal_type=b.signal_type "
        "and a.value_hash=b.value_hash and a.profile_id < b.profile_id "
        "join profile pa on pa.id=a.profile_id join profile pb on pb.id=b.profile_id "
        "where a.tenant_id=? and a.signal_type in ('device','device_id','net') "
        "and pa.merged_into is null and pb.merged_into is null "
        "and pa.erased_at is null and pb.erased_at is null",
        [tenant_id],
    )
    return {(r["l"], r["r"]) for r in rows}


def score_pair(sig_a: dict, sig_b: dict, prof_a: dict, prof_b: dict) -> tuple[float, dict]:
    """Return (match probability, per-comparison features with Bayes factors)."""
    log_odds = math.log(PRIOR / (1 - PRIOR))
    features = {}
    for name, (m, u) in COMPARISONS.items():
        if name == "interests":
            a, b = _interest_set(prof_a), _interest_set(prof_b)
            if not a or not b:
                continue
            agree = len(a & b) / len(a | b) >= 0.3
        else:
            a, b = sig_a.get(name, set()), sig_b.get(name, set())
            if not a or not b:
                continue  # missing -> neutral
            if name == "device" and sig_a.get("device_id") and sig_b.get("device_id"):
                continue  # the explicit id already answers "same device?"; don't count it twice
            agree = bool(a & b)
        bf = (m / u) if agree else ((1 - m) / (1 - u))
        log_odds += math.log(bf)
        features[name] = {"agree": agree, "bf": round(bf, 4)}
    prob = 1 / (1 + math.exp(-log_odds))
    return prob, features


def _score_with_splink(tenant_id, pairs, sigs, profs):  # pragma: no cover - optional dep
    """Score with Splink if available. Returns {(l, r): prob} or None to fall back."""
    try:
        import pandas as pd
        import splink.comparison_library as cl
        from splink import DuckDBAPI, Linker, SettingsCreator, block_on
    except ImportError:
        return None
    ids = sorted({x for p in pairs for x in p})
    if len(ids) < 50:
        return None  # EM is unstable on tiny graphs; built-in priors are better

    def first(s):
        return sorted(s)[0] if s else None

    df = pd.DataFrame([
        {"unique_id": pid, **{t: first(sigs[pid].get(t, set())) for t in ("device", "net", "ua_family", "tz", "lang")}}
        for pid in ids
    ])
    settings = SettingsCreator(
        link_type="dedupe_only",
        comparisons=[cl.ExactMatch(c) for c in ("device", "net", "ua_family", "tz", "lang")],
        blocking_rules_to_generate_predictions=[block_on("device"), block_on("net")],
    )
    linker = Linker(df, settings, db_api=DuckDBAPI())
    linker.training.estimate_probability_two_random_records_match([block_on("device")], recall=0.7)
    linker.training.estimate_u_using_random_sampling(max_pairs=1e5)
    linker.training.estimate_parameters_using_expectation_maximisation(block_on("net"))
    preds = linker.inference.predict(threshold_match_probability=REVIEW_THRESHOLD).as_pandas_dataframe()
    out = {}
    for _, row in preds.iterrows():
        a, b = sorted([row["unique_id_l"], row["unique_id_r"]])
        out[(a, b)] = float(row["match_probability"])
    return out


def run_matching(tenant_id: str, actor: str = "system") -> dict:
    stats = {"pairs": 0, "no_consent": 0, "candidates": 0, "auto_merged": 0, "engine": "builtin"}
    sigs = _signal_sets(tenant_id)
    pairs = blocked_pairs(tenant_id)
    household_of = {r["profile_id"]: r["household_id"] for r in fetchall(
        "select profile_id, household_id from household_member where tenant_id=?", [tenant_id])}
    profs = {}

    def prof(pid):
        if pid not in profs:
            profs[pid] = get_profile(pid)
        return profs[pid]

    splink_scores = None
    if MATCH_ENGINE == "splink":
        splink_scores = _score_with_splink(tenant_id, pairs, sigs, profs)
        if splink_scores is not None:
            stats["engine"] = "splink"

    scored = []  # (cid, prob, engine, left, right, a, b)
    for left, right in sorted(pairs):
        stats["pairs"] += 1
        a, b = prof(left), prof(right)
        if not a or not b or (a["status"] == "anonymous" and b["status"] == "anonymous"):
            continue  # anon<->anon links add nothing actionable
        if not can_link_probabilistically([left, right]):
            stats["no_consent"] += 1
            continue
        prob, features = score_pair(sigs[left], sigs[right], a, b)
        engine = "builtin"
        if splink_scores is not None and (left, right) in splink_scores:
            prob, engine = splink_scores[(left, right)], "splink"
        if prob < REVIEW_THRESHOLD:
            continue
        if household_of.get(left) and household_of.get(left) == household_of.get(right):
            features["household"] = {"agree": True, "bf": 1.0, "note": "same household - may be two people"}
        if fetchone("select id from match_candidate where tenant_id=? and left_id=? and right_id=?",
                    [tenant_id, left, right]):
            continue
        cid = new_id("mc_")
        insert_row("match_candidate", id=cid, tenant_id=tenant_id, left_id=left, right_id=right,
                   score=round(prob, 6), features_json=dumps(features), engine=engine,
                   status="pending", created_at=now_iso())
        stats["candidates"] += 1
        scored.append((cid, prob, engine, a, b))

    # Ambiguity guard: an anonymous profile that plausibly matches more than one
    # known person (shared/common devices) is never auto-merged.
    known_matches: dict[str, set] = defaultdict(set)
    for _, _, _, a, b in scored:
        if (a["status"] == "anonymous") != (b["status"] == "anonymous"):
            anon, known = (a, b) if a["status"] == "anonymous" else (b, a)
            known_matches[anon["id"]].add(known["id"])
    for row in fetchall("select left_id, right_id from match_candidate where tenant_id=? and status='pending'", [tenant_id]):
        for x, y in ((row["left_id"], row["right_id"]), (row["right_id"], row["left_id"])):
            if x in known_matches:
                known_matches[x].add(y)

    stats["ambiguous"] = 0
    for cid, prob, engine, a, b in scored:
        one_anon = (a["status"] == "anonymous") != (b["status"] == "anonymous")
        if not one_anon or prob < AUTO_MERGE_THRESHOLD:
            continue
        known, anon = (a, b) if a["status"] == "known" else (b, a)
        if len(known_matches[anon["id"]]) > 1:
            stats["ambiguous"] += 1
            continue
        mid = merge(tenant_id, known["id"], anon["id"], method="probabilistic_auto",
                    score=prob, reason=f"{engine} auto-merge", actor=actor)
        if mid:
            update_row("match_candidate", cid, status="auto_merged", reviewed_at=now_iso(), reviewed_by=actor)
            stats["auto_merged"] += 1
    audit(tenant_id, actor, "matching.run", "tenant", tenant_id, **stats)
    return stats


def review(tenant_id: str, candidate_id: str, accept: bool, actor: str) -> bool:
    c = fetchone("select * from match_candidate where id=? and tenant_id=?", [candidate_id, tenant_id])
    if not c or c["status"] != "pending":
        return False
    if accept:
        left, right = canonical_id(c["left_id"]), canonical_id(c["right_id"])
        if not can_link_probabilistically([left, right]):
            return False
        a, b = get_profile(left), get_profile(right)
        if a["status"] != b["status"]:
            survivor, loser = (a, b) if a["status"] == "known" else (b, a)
        else:
            survivor, loser = (a, b) if (a["first_seen"] or "") <= (b["first_seen"] or "") else (b, a)
        merge(tenant_id, survivor["id"], loser["id"], method="manual", score=c["score"],
              reason="reviewed candidate", actor=actor)
    update_row("match_candidate", candidate_id, status="accepted" if accept else "rejected",
               reviewed_at=now_iso(), reviewed_by=actor)
    audit(tenant_id, actor, "candidate.reviewed", "match_candidate", candidate_id, accepted=accept)
    return True


def quality_metrics(tenant_id: str) -> dict:
    def n(sql, args=()):
        return (fetchone(sql, [tenant_id, *args]) or {}).get("n") or 0

    live = "tenant_id=? and merged_into is null and erased_at is null"
    total = n(f"select count(*) as n from profile where {live}")
    known = n(f"select count(*) as n from profile where {live} and status='known'")
    accepted = n("select count(*) as n from match_candidate where tenant_id=? and status='accepted'")
    rejected = n("select count(*) as n from match_candidate where tenant_id=? and status='rejected'")
    undone = n("select count(*) as n from merge_log where tenant_id=? and method='probabilistic_auto' and undone_at is not null")
    auto = n("select count(*) as n from merge_log where tenant_id=? and method='probabilistic_auto'")
    buckets = fetchall(
        "select case when score>=0.99 then '0.99+' when score>=0.95 then '0.95-0.99' "
        "when score>=0.85 then '0.85-0.95' else '<0.85' end as bucket, count(*) as n "
        "from match_candidate where tenant_id=? group by 1 order by 1 desc",
        [tenant_id],
    )
    by_method = fetchall(
        "select method, count(*) as n from merge_log where tenant_id=? and undone_at is null group by method",
        [tenant_id],
    )
    return {
        "profiles": total,
        "known": known,
        "known_rate": (known / total) if total else 0.0,
        "pending": n("select count(*) as n from match_candidate where tenant_id=? and status='pending'"),
        "accepted": accepted,
        "rejected": rejected,
        "review_precision": (accepted / (accepted + rejected)) if (accepted + rejected) else None,
        "auto_merges": auto,
        "auto_merges_undone": undone,
        "auto_precision_proxy": (1 - undone / auto) if auto else None,
        "score_buckets": buckets,
        "merges_by_method": by_method,
    }
