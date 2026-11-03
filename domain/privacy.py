"""Data-subject rights (erasure, access/export) and retention.

Erasure keeps a tombstone profile row (no PII) so merge history stays coherent,
and by default keeps a *hashed* suppression entry so the person is never
re-contacted (ICO guidance permits retaining a suppression record).
"""
from __future__ import annotations

from db import execute, fetchall, fetchone
from domain.audit import audit
from domain.consent import current_consent, suppress
from domain.identity import ID_TYPES, _normalize, canonical_id, find_identity, get_profile
from settings import ANON_PROFILE_RETENTION_DAYS, EVENT_RETENTION_DAYS
from util import days_ago_iso, loads, now_iso


def lookup(tenant_id: str, *, profile_id=None, email=None, phone=None, user_id=None) -> str | None:
    if profile_id:
        p = get_profile(profile_id)
        return canonical_id(profile_id) if p and p["tenant_id"] == tenant_id else None
    for id_type, val in (("email", email), ("phone", phone), ("user_id", user_id)):
        row = find_identity(tenant_id, id_type, _normalize(id_type, val))
        if row:
            return canonical_id(row["profile_id"])
    return None


def _family(profile_id: str) -> list[str]:
    fam, frontier = {profile_id}, [profile_id]
    while frontier:
        nxt = []
        for pid in frontier:
            for r in fetchall("select id from profile where merged_into=?", [pid]):
                if r["id"] not in fam:
                    fam.add(r["id"])
                    nxt.append(r["id"])
        frontier = nxt
    return sorted(fam)


def export(tenant_id: str, profile_id: str) -> dict:
    """Subject access request bundle."""
    fam = _family(profile_id)
    marks = ",".join("?" for _ in fam)
    p = get_profile(profile_id)
    return {
        "profile": {k: v for k, v in p.items() if not k.endswith("_json")} | {
            "traits": loads(p.get("traits_json")), "interests": loads(p.get("interests_json"))},
        "linked_profiles": fam,
        "identifiers": [{"type": r["id_type"], "value": r["id_value"], "since": r["created_at"]}
                        for r in fetchall(f"select * from identity where profile_id in ({marks})", fam)],
        "consent": current_consent(profile_id),
        "consent_history": fetchall(
            f"select purpose, granted, legal_basis, source, ts from consent_record where profile_id in ({marks}) order by ts", fam),
        "events": [{"event": e["event"], "ts": e["ts"], "properties": loads(e["properties_json"])}
                   for e in fetchall(f"select * from event where profile_id in ({marks}) order by ts", fam)],
        "messages": fetchall(
            f"select flow_id, channel, status, reason, ts from message where profile_id in ({marks}) order by ts", fam),
        "generated_at": now_iso(),
    }


def erase(tenant_id: str, profile_id: str, actor: str, keep_suppression: bool = True) -> dict:
    fam = _family(profile_id)
    marks = ",".join("?" for _ in fam)
    counts = {"profiles": len(fam)}
    if keep_suppression:
        for r in fetchall(f"select id_type, id_value from identity where profile_id in ({marks}) "
                          f"and id_type in ('email','phone')", fam):
            suppress(tenant_id, "email" if r["id_type"] == "email" else "sms", r["id_value"],
                     reason="erasure", actor=actor)
    for table, col in (("event", "profile_id"), ("event", "origin_profile_id"), ("identity", "profile_id"),
                       ("signal", "profile_id"), ("consent_record", "profile_id"), ("message", "profile_id")):
        n = fetchone(f"select count(*) as n from {table} where {col} in ({marks})", fam)["n"]
        counts[table] = counts.get(table, 0) + n
        execute(f"delete from {table} where {col} in ({marks})", fam)
    execute(f"delete from match_candidate where left_id in ({marks}) or right_id in ({marks})", [*fam, *fam])
    execute(f"update merge_log set survivor_snapshot_json='{{}}' where survivor_id in ({marks}) or loser_id in ({marks})",
            [*fam, *fam])
    ts = now_iso()
    execute(f"update profile set status='erased', email=null, phone=null, user_id=null, traits_json='{{}}', "
            f"interests_json='{{}}', erased_at=? where id in ({marks})", [ts, *fam])
    audit(tenant_id, actor, "profile.erased", "profile", profile_id, **counts)
    return counts


def apply_retention(tenant_id: str, actor: str = "worker") -> dict:
    ev_cut = days_ago_iso(EVENT_RETENTION_DAYS)
    anon_cut = days_ago_iso(ANON_PROFILE_RETENTION_DAYS)
    n_events = fetchone("select count(*) as n from event where tenant_id=? and ts<?", [tenant_id, ev_cut])["n"]
    execute("delete from event where tenant_id=? and ts<?", [tenant_id, ev_cut])
    stale = [r["id"] for r in fetchall(
        "select id from profile where tenant_id=? and status='anonymous' and merged_into is null "
        "and erased_at is null and last_seen<?", [tenant_id, anon_cut])]
    for pid in stale:
        for table in ("event", "identity", "signal", "consent_record"):
            execute(f"delete from {table} where profile_id=?", [pid])
        execute("delete from match_candidate where left_id=? or right_id=?", [pid, pid])
        execute("delete from profile where id=?", [pid])
    result = {"events_deleted": n_events, "anonymous_profiles_deleted": len(stale)}
    audit(tenant_id, actor, "retention.applied", "tenant", tenant_id, **result)
    return result
