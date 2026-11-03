"""Rule segments, evaluated in Python (fine at prototype / SME scale).

Definition: ``{"all": [rule, ...]}`` where a rule is one of

* ``{"type": "status", "value": "known"}``
* ``{"type": "trait", "field": "plan", "value": "pro"}``
* ``{"type": "interest", "key": "treatment", "value": "hip replacement"}``
* ``{"type": "event", "event": "booking_started", "min": 1, "days": 30}``
* ``{"type": "consent", "purpose": "marketing_email"}``
"""
from __future__ import annotations

from db import fetchall, fetchone, update_row
from domain.consent import has_purpose
from util import days_ago_iso, loads, now_iso

RULE_TYPES = ["status", "trait", "interest", "event", "consent"]


def _match(rule: dict, p: dict) -> bool:
    t = rule.get("type")
    if t == "status":
        return p["status"] == rule.get("value")
    if t == "trait":
        return str(loads(p.get("traits_json")).get(rule.get("field"), "")).lower() == str(rule.get("value", "")).lower()
    if t == "interest":
        bucket = loads(p.get("interests_json")).get(rule.get("key"), {})
        want = str(rule.get("value", "")).lower()
        return any(want in k.lower() for k in bucket) if want else bool(bucket)
    if t == "event":
        since = days_ago_iso(float(rule.get("days") or 30))
        n = fetchone("select count(*) as n from event where profile_id=? and event=? and ts>=?",
                     [p["id"], rule.get("event"), since])["n"]
        return n >= int(rule.get("min") or 1)
    if t == "consent":
        return has_purpose(p["id"], rule.get("purpose"))
    return False


def evaluate(tenant_id: str, definition: dict) -> list[dict]:
    rules = definition.get("all") or []
    profiles = fetchall(
        "select * from profile where tenant_id=? and merged_into is null and erased_at is null order by last_seen desc",
        [tenant_id],
    )
    return [p for p in profiles if all(_match(r, p) for r in rules)]


def refresh(segment_id: str) -> int:
    seg = fetchone("select * from segment where id=?", [segment_id])
    if not seg:
        return 0
    members = evaluate(seg["tenant_id"], loads(seg["definition_json"]))
    update_row("segment", segment_id, materialized_count=len(members), last_computed_at=now_iso())
    return len(members)
