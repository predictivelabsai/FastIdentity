"""Look-alike propensity model: score every profile by how much it resembles a seed.

This is the *seed* half of look-alike marketing (see README "Activation" and the
platform-guide appendix). FastIdentity builds a clean, consented seed audience and
ranks the rest of the base by resemblance to it; the ad platform (Meta Lookalike,
Google Similar) does the population-level expansion from its own graph. We do **not**
try to model strangers — only profiles already in the first-party graph are scored.

Method (deliberately basic, PU-learning baseline):

* Build one feature vector per profile from first-party signals only — status,
  recency/tenure, event volume and mix, resolved interests, device classes and
  household membership. Raw PII and raw identifiers are never features.
* Treat the seed as the positive class and a random sample of the rest as negatives
  (the standard "unlabelled-as-negative" PU baseline). Train a logistic-regression
  classifier (scikit-learn) over the sparse feature space and read off the predicted
  probability as the look-alike score in [0, 1].
* Rank the non-seed profiles by score. Consent is reported per profile but **not**
  used as a feature — it is an activation gate, not a similarity signal.

Engines mirror ``domain.matching``: ``sklearn`` (default, needs scikit-learn) and a
dependency-free ``builtin`` cosine-similarity-to-centroid fallback so the module
always runs. Nothing here sends anything; activation still passes the consent,
suppression and frequency gates.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import datetime, timezone

from db import fetchall
from domain.audit import audit
from domain.consent import has_purpose
from settings import (
    LOOKALIKE_DEFAULT_LIMIT,
    LOOKALIKE_ENGINE,
    LOOKALIKE_MIN_SEED,
    LOOKALIKE_NEG_RATIO,
)
from util import loads

RANDOM_SEED = 42
# A few recognisable intent events get their own feature so the model can lean on
# stage, not just volume. Everything else still contributes via ``ev=<name>``.
INTENT_EVENTS = ("product_viewed", "booking_started", "booking_completed",
                 "order_completed", "cart_started")


def sklearn_available() -> bool:
    try:
        import sklearn  # noqa: F401
    except Exception:
        return False
    return True


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def build_features(tenant_id: str) -> tuple[list[str], dict[str, dict], dict[str, dict]]:
    """One pass over the live graph → (profile_ids, features_by_id, meta_by_id).

    ``features_by_id`` maps a profile id to a flat ``{name: float}`` dict (numeric
    fields plus sparse ``int=<key>:<value>`` and ``ev=<event>`` indicators) ready
    for scikit-learn's :class:`DictVectorizer` or the builtin cosine scorer.
    ``meta_by_id`` carries the display/gating fields (status, consent) kept out of
    the feature space.
    """
    profiles = fetchall(
        "select id, status, interests_json, first_seen, last_seen from profile "
        "where tenant_id=? and merged_into is null and erased_at is null",
        [tenant_id],
    )
    ids = [p["id"] for p in profiles]
    if not ids:
        return [], {}, {}

    events = fetchall(
        "select profile_id, event, count(*) as n from event "
        "where tenant_id=? and profile_id is not null group by profile_id, event",
        [tenant_id],
    )
    ev_by_pid: dict[str, dict[str, int]] = defaultdict(dict)
    for r in events:
        ev_by_pid[r["profile_id"]][r["event"]] = int(r["n"] or 0)

    devices = fetchall(
        "select profile_id, device_class, device_key from device where tenant_id=?",
        [tenant_id],
    )
    dev_classes: dict[str, set] = defaultdict(set)
    dev_keys: dict[str, set] = defaultdict(set)
    for r in devices:
        dev_classes[r["profile_id"]].add((r["device_class"] or "unknown").lower())
        dev_keys[r["profile_id"]].add(r["device_key"])

    hh = fetchall(
        "select m.profile_id, h.members from household_member m "
        "join household h on h.id = m.household_id where m.tenant_id=?",
        [tenant_id],
    )
    hh_by_pid = {r["profile_id"]: int(r["members"] or 0) for r in hh}

    now = datetime.now(timezone.utc)
    features: dict[str, dict] = {}
    meta: dict[str, dict] = {}
    for p in profiles:
        pid = p["id"]
        f: dict[str, float] = {}
        f["known"] = 1.0 if p["status"] == "known" else 0.0

        first, last = _parse(p["first_seen"]), _parse(p["last_seen"])
        f["recency_days"] = min((now - last).days, 365.0) if last else 365.0
        f["tenure_days"] = max((last - first).days, 0.0) if first and last else 0.0

        ev = ev_by_pid.get(pid, {})
        total = sum(ev.values())
        f["log_events"] = math.log1p(total)
        f["event_types"] = float(len(ev))
        for name in INTENT_EVENTS:
            if ev.get(name):
                f[f"ev={name}"] = float(ev[name])
        for name, n in ev.items():
            if name not in INTENT_EVENTS:
                f[f"ev={name}"] = float(n)

        interests = loads(p.get("interests_json"))
        weight = 0.0
        keys = 0
        for key, bucket in interests.items():
            if not isinstance(bucket, dict):
                continue
            keys += 1
            for value, count in bucket.items():
                try:
                    c = float(count)
                except (TypeError, ValueError):
                    c = 1.0
                weight += c
                f[f"int={key}:{str(value).lower()}"] = 1.0
        f["n_interest_keys"] = float(keys)
        f["interest_weight"] = weight

        classes = dev_classes.get(pid, set())
        f["n_devices"] = float(len(dev_keys.get(pid, set())))
        for cls in ("mobile", "desktop", "tablet", "tv"):
            if cls in classes:
                f[f"dev={cls}"] = 1.0

        size = hh_by_pid.get(pid, 0)
        f["in_household"] = 1.0 if size else 0.0
        f["household_size"] = float(size)

        features[pid] = f
        meta[pid] = {
            "status": p["status"],
            "marketing_email_consent": has_purpose(pid, "marketing_email"),
            "identity_resolution_consent": has_purpose(pid, "identity_resolution"),
        }
    return ids, features, meta


# --- scoring engines ---------------------------------------------------------------

def _score_sklearn(pos_ids, cand_ids, features, neg_ratio):
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import MaxAbsScaler

    rng = random.Random(RANDOM_SEED)
    n_neg = min(len(cand_ids), max(len(pos_ids) * max(neg_ratio, 1), 50))
    neg_ids = cand_ids if len(cand_ids) <= n_neg else rng.sample(cand_ids, n_neg)

    train_ids = pos_ids + neg_ids
    y = [1] * len(pos_ids) + [0] * len(neg_ids)
    pipe = make_pipeline(
        DictVectorizer(sparse=True),
        MaxAbsScaler(),
        LogisticRegression(max_iter=1000, class_weight="balanced", random_state=RANDOM_SEED),
    )
    pipe.fit([features[p] for p in train_ids], y)

    train_proba = pipe.predict_proba([features[p] for p in train_ids])[:, 1]
    try:
        auc = round(float(roc_auc_score(y, train_proba)), 3)
    except ValueError:
        auc = None

    scores = {pid: float(pr) for pid, pr in
              zip(cand_ids, pipe.predict_proba([features[p] for p in cand_ids])[:, 1])}

    vec = pipe.named_steps["dictvectorizer"]
    coef = pipe.named_steps["logisticregression"].coef_[0]
    names = vec.get_feature_names_out()
    top = sorted(zip(names, coef), key=lambda kv: kv[1], reverse=True)[:12]
    top_features = [{"feature": str(n), "weight": round(float(w), 3)} for n, w in top]
    return scores, {"train_auc": auc, "top_features": top_features, "negatives": len(neg_ids)}


def _score_builtin(pos_ids, cand_ids, features):
    """Dependency-free fallback: cosine similarity to the L2-normalised seed centroid."""
    def norm(vec: dict) -> dict:
        mag = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {k: v / mag for k, v in vec.items()}

    centroid: dict[str, float] = defaultdict(float)
    for pid in pos_ids:
        for k, v in norm(features[pid]).items():
            centroid[k] += v / len(pos_ids)
    c = norm(dict(centroid))
    scores = {}
    for pid in cand_ids:
        v = norm(features[pid])
        dot = sum(v.get(k, 0.0) * cv for k, cv in c.items())
        scores[pid] = max(0.0, min(1.0, dot))  # cosine ≥ 0 here (features are non-negative)
    return scores, {"train_auc": None, "top_features": [], "negatives": 0}


def score_lookalike(
    tenant_id: str,
    seed_ids,
    *,
    limit: int | None = None,
    min_score: float = 0.0,
    engine: str | None = None,
    neg_ratio: int | None = None,
    min_seed: int | None = None,
    actor: str = "system",
) -> dict:
    """Rank non-seed profiles by resemblance to ``seed_ids``.

    Returns ``{"ok", "engine", "seed_size", "scored", "train_auc", "top_features",
    "audience": [...] }`` where each audience row is a profile id, its score and its
    status + consent flags, highest score first. ``ok`` is False (with a ``reason``)
    when the seed is too small to model — the single most common real failure.
    """
    limit = LOOKALIKE_DEFAULT_LIMIT if limit is None else limit
    neg_ratio = LOOKALIKE_NEG_RATIO if neg_ratio is None else neg_ratio
    min_seed = LOOKALIKE_MIN_SEED if min_seed is None else min_seed
    want = (engine or LOOKALIKE_ENGINE or "sklearn").strip().lower()

    ids, features, meta = build_features(tenant_id)
    present = set(ids)
    seed_set = {s for s in seed_ids if s in present}
    cand_ids = [p for p in ids if p not in seed_set]

    base = {"ok": False, "engine": want, "seed_size": len(seed_set),
            "scored": 0, "train_auc": None, "top_features": [], "audience": []}
    if len(seed_set) < max(min_seed, 1):
        base["reason"] = f"seed too small ({len(seed_set)} < {min_seed})"
        return base
    if not cand_ids:
        base["ok"] = True
        base["reason"] = "no candidates outside the seed"
        return base

    used = want
    if want == "sklearn" and not sklearn_available():
        used = "builtin"  # degrade, don't break
    if used == "sklearn":
        scores, diag = _score_sklearn(sorted(seed_set), cand_ids, features, neg_ratio)
    else:
        scores, diag = _score_builtin(sorted(seed_set), cand_ids, features)

    ranked = sorted(cand_ids, key=lambda p: scores[p], reverse=True)
    audience = []
    for pid in ranked:
        if scores[pid] < min_score:
            continue
        m = meta[pid]
        audience.append({
            "profile_id": pid,
            "score": round(scores[pid], 4),
            "status": m["status"],
            "marketing_email_consent": m["marketing_email_consent"],
            "identity_resolution_consent": m["identity_resolution_consent"],
        })
        if len(audience) >= limit:
            break

    result = {
        "ok": True,
        "engine": used,
        "engine_requested": want,
        "seed_size": len(seed_set),
        "scored": len(cand_ids),
        "train_auc": diag["train_auc"],
        "top_features": diag["top_features"],
        "audience": audience,
    }
    audit(tenant_id, actor, "lookalike.scored", "tenant", tenant_id,
          engine=used, seed_size=len(seed_set), scored=len(cand_ids),
          returned=len(audience), train_auc=diag["train_auc"])
    return result


def lookalike_for_segment(tenant_id: str, segment_id: str, **kwargs) -> dict:
    """Score a look-alike audience using a saved segment's members as the seed."""
    from db import fetchone
    from domain.segments import evaluate

    seg = fetchone("select * from segment where id=? and tenant_id=?", [segment_id, tenant_id])
    if not seg:
        return {"ok": False, "reason": "segment not found", "seed_size": 0, "audience": []}
    members = evaluate(tenant_id, loads(seg["definition_json"]))
    return score_lookalike(tenant_id, [p["id"] for p in members], **kwargs)
