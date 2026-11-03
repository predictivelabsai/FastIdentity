"""Abandonment flows, frequency caps and ESP sync.

A flow fires when a profile did ``trigger_event`` (e.g. ``booking_started``)
at least ``delay_minutes`` ago, within ``lookback_hours``, and has not done
``exit_event`` (e.g. ``booking_completed``) since. Every candidate passes the
consent gate (``can_message``), suppression and the frequency cap before the
destination is called. One message row per (flow, trigger event) makes runs
idempotent.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import destinations
from db import fetchall, fetchone, insert_row
from domain.audit import audit
from domain.consent import can_message
from domain.identity import canonical_id, get_profile
from settings import DEFAULT_FREQUENCY_CAP, DEFAULT_FREQUENCY_WINDOW_DAYS
from util import dumps, loads, new_id, now_iso

FLOW_TEMPLATES = [
    {"name": "Browse abandonment", "trigger_event": "product_viewed", "exit_event": "booking_started",
     "delay_minutes": 120, "lookback_hours": 48},
    {"name": "Cart abandonment", "trigger_event": "cart_started", "exit_event": "order_completed",
     "delay_minutes": 60, "lookback_hours": 72},
    {"name": "Booking abandonment", "trigger_event": "booking_started", "exit_event": "booking_completed",
     "delay_minutes": 45, "lookback_hours": 72},
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _record(tenant_id, profile_id, flow, trigger_event_id, status, reason="", payload=None):
    if fetchone("select id from message where flow_id=? and trigger_event_id=?", [flow["id"], trigger_event_id]):
        return False
    insert_row("message", id=new_id("msg_"), tenant_id=tenant_id, profile_id=profile_id, flow_id=flow["id"],
               trigger_event_id=trigger_event_id, channel=flow["channel"], destination_id=flow.get("destination_id"),
               status=status, reason=reason, payload_json=dumps(payload or {}), ts=now_iso())
    return True


def frequency_ok(profile_id: str, cap: int, window_days: int, now: datetime) -> bool:
    since = _iso(now - timedelta(days=window_days))
    sent = fetchone(
        "select count(*) as n from message where profile_id=? and status in ('sent','dry_run') and ts>=?",
        [profile_id, since],
    )["n"]
    return sent < cap


def public_profile(p: dict) -> dict:
    traits = loads(p.get("traits_json"))
    return {"id": p["id"], "email": p.get("email"), "phone": p.get("phone"),
            "first_name": traits.get("first_name") or traits.get("firstName"),
            "interests": loads(p.get("interests_json"))}


def run_flow(tenant_id: str, flow: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stats = {"flow": flow["name"], "candidates": 0, "sent": 0, "skipped": 0, "failed": 0}
    window_start = _iso(now - timedelta(hours=flow["lookback_hours"] or 72))
    cutoff = _iso(now - timedelta(minutes=flow["delay_minutes"] or 0))
    triggers = fetchall(
        "select * from event where tenant_id=? and event=? and ts>=? and ts<=? and profile_id is not null order by ts desc",
        [tenant_id, flow["trigger_event"], window_start, cutoff],
    )
    latest: dict[str, dict] = {}
    for ev in triggers:
        pid = canonical_id(ev["profile_id"])
        if pid and pid not in latest:
            latest[pid] = ev

    cap = flow.get("frequency_cap") or DEFAULT_FREQUENCY_CAP
    window = flow.get("frequency_window_days") or DEFAULT_FREQUENCY_WINDOW_DAYS
    dest = destinations.build(fetchone("select * from destination where id=?", [flow.get("destination_id")])
                              if flow.get("destination_id") else None)

    for pid, ev in latest.items():
        if fetchone("select id from event where profile_id=? and event=? and ts>=?", [pid, flow["exit_event"], ev["ts"]]):
            continue  # converted
        if fetchone("select id from message where flow_id=? and profile_id=? and ts>=?", [flow["id"], pid, window_start]):
            continue  # already handled this episode
        profile = get_profile(pid)
        if not profile or profile["status"] != "known":
            continue  # nobody to message; anonymous intent stays anonymous
        stats["candidates"] += 1
        ok, reason = can_message(profile, flow["channel"])
        if ok and not frequency_ok(pid, cap, window, now):
            ok, reason = False, "frequency_cap"
        if not ok:
            if _record(tenant_id, pid, flow, ev["id"], "skipped", reason):
                stats["skipped"] += 1
            continue
        props = {**loads(ev["properties_json"]), "flow": flow["name"], "template": flow.get("template") or "",
                 "trigger_event": flow["trigger_event"], "trigger_ts": ev["ts"]}
        status, detail = dest.send_event(f"FastIdentity {flow['name']}", public_profile(profile), props)
        if _record(tenant_id, pid, flow, ev["id"], status, detail, props):
            stats["sent" if status in ("sent", "dry_run") else "failed"] += 1
    return stats


def run_all(tenant_id: str, now: datetime | None = None, actor: str = "worker") -> list[dict]:
    results = [run_flow(tenant_id, f, now) for f in
               fetchall("select * from flow where tenant_id=? and enabled=1", [tenant_id])]
    audit(tenant_id, actor, "flows.run", "tenant", tenant_id,
          sent=sum(r["sent"] for r in results), skipped=sum(r["skipped"] for r in results))
    return results


def sync_profile(tenant_id: str, profile_id: str, destination_id: str, actor: str = "system") -> tuple[str, str]:
    """Push a resolved, consented contact to an ESP."""
    profile = get_profile(canonical_id(profile_id))
    if not profile or profile["tenant_id"] != tenant_id:
        return "skipped", "not_found"
    row = fetchone("select * from destination where id=? and tenant_id=?", [destination_id, tenant_id])
    if not row:
        return "skipped", "unknown_destination"
    ok, reason = can_message(profile, "email")
    if not ok:
        return "skipped", reason
    dest = destinations.build(row)
    traits = {k: v for k, v in loads(profile.get("traits_json")).items() if isinstance(v, (str, int, float, bool))}
    status, detail = dest.sync_profile(public_profile(profile), traits)
    audit(tenant_id, actor, "profile.synced", "profile", profile["id"], destination=destination_id, status=status)
    return status, detail
