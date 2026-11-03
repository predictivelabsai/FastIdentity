"""Household grouping: distinct people who repeatedly share a residential IP.

A household is a *grouping*, never a merge — two members stay two profiles.
Rules (see README "Devices & households"):

* Opt-in per workspace (``tenant.households_enabled``); ``hh_ip`` signals are
  only collected while it is on, and only with ``identity_resolution`` consent.
* Only IPs seen in the last ``HOUSEHOLD_WINDOW_DAYS`` count (home IPs rotate).
* An IP must be seen on ``HOUSEHOLD_MIN_DAYS`` distinct days to be "home".
* An IP shared by more than ``HOUSEHOLD_MAX_MEMBERS`` profiles is an office,
  mobile carrier (CGNAT), café or VPN — it is ignored, not grouped.
* Profiles linked through several qualifying IPs (the router got a new
  address) form one household; a chain that grows past the cap is dropped.

Rebuilt from scratch each worker pass, so withdrawn consent, erasure and
expiry take effect on the next run.
"""
from __future__ import annotations

from collections import defaultdict

from db import execute, fetchall, fetchone, insert_row
from domain.audit import audit
from domain.consent import has_purpose
from settings import HOUSEHOLD_MAX_MEMBERS, HOUSEHOLD_MIN_DAYS, HOUSEHOLD_WINDOW_DAYS
from util import days_ago_iso, keyed_hash, now_iso


def enabled(tenant_id: str) -> bool:
    return bool((fetchone("select households_enabled from tenant where id=?", [tenant_id]) or {})
                .get("households_enabled"))


def clear(tenant_id: str):
    execute("delete from household_member where tenant_id=?", [tenant_id])
    execute("delete from household where tenant_id=?", [tenant_id])


def rebuild(tenant_id: str, actor: str = "worker") -> dict:
    if not enabled(tenant_id):
        clear(tenant_id)
        return {"enabled": False}
    rows = fetchall(
        "select s.value_hash, s.profile_id, s.first_seen, s.last_seen from signal s "
        "join profile p on p.id = s.profile_id "
        "where s.tenant_id=? and s.signal_type='hh_ip' and s.last_seen>=? "
        "and p.merged_into is null and p.erased_at is null",
        [tenant_id, days_ago_iso(HOUSEHOLD_WINDOW_DAYS)],
    )
    consent_ok: dict[str, bool] = {}

    def ok(pid):
        if pid not in consent_ok:
            consent_ok[pid] = has_purpose(pid, "identity_resolution")
        return consent_ok[pid]

    by_ip: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    days: dict[str, set] = defaultdict(set)
    for r in rows:
        if not ok(r["profile_id"]):
            continue
        prev = by_ip[r["value_hash"]].get(r["profile_id"])
        first, last = r["first_seen"] or "", r["last_seen"] or ""
        if prev:
            first, last = min(first, prev[0]), max(last, prev[1])
        by_ip[r["value_hash"]][r["profile_id"]] = (first, last)
        days[r["value_hash"]].update(d[:10] for d in (r["first_seen"], r["last_seen"]) if d)

    stats = {"ips": len(by_ip), "shared_networks": 0, "too_new": 0, "households": 0, "members": 0}
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    qualifying = {}
    for ip, members in by_ip.items():
        if len(members) > HOUSEHOLD_MAX_MEMBERS:
            stats["shared_networks"] += 1
            continue
        if len(members) < 2:
            continue
        if len(days[ip]) < HOUSEHOLD_MIN_DAYS:
            stats["too_new"] += 1
            continue
        qualifying[ip] = members
        pids = sorted(members)
        for other in pids[1:]:
            parent[find(other)] = find(pids[0])

    groups: dict[str, dict] = defaultdict(lambda: {"members": {}, "ips": set()})
    for ip, members in qualifying.items():
        for pid, (first, last) in members.items():
            g = groups[find(pid)]
            g["ips"].add(ip)
            prev = g["members"].get(pid)
            g["members"][pid] = (min(first, prev[0]), max(last, prev[1])) if prev else (first, last)

    clear(tenant_id)
    ts = now_iso()
    for g in groups.values():
        members = g["members"]
        if len(members) > HOUSEHOLD_MAX_MEMBERS:
            stats["shared_networks"] += 1
            continue
        hid = "hh_" + keyed_hash(f"{tenant_id}|{min(members)}", "household")[:16]
        marks = ",".join("?" for _ in members)
        n_devices = (fetchone(f"select count(distinct device_key) as n from device where profile_id in ({marks})",
                              list(members)) or {}).get("n") or 0
        insert_row("household", id=hid, tenant_id=tenant_id, status="household", members=len(members),
                   devices=n_devices, networks=len(g["ips"]),
                   first_seen=min(v[0] for v in members.values()), last_seen=max(v[1] for v in members.values()),
                   updated_at=ts)
        for pid, (first, last) in members.items():
            insert_row("household_member", household_id=hid, tenant_id=tenant_id, profile_id=pid,
                       first_seen=first, last_seen=last)
        stats["households"] += 1
        stats["members"] += len(members)
    audit(tenant_id, actor, "households.rebuilt", "tenant", tenant_id, **stats)
    return stats


def household_of(profile_id: str) -> dict | None:
    return fetchone("select h.* from household h join household_member m on m.household_id=h.id "
                    "where m.profile_id=?", [profile_id])


def members(household_id: str) -> list[dict]:
    return fetchall("select p.*, m.first_seen as hh_first, m.last_seen as hh_last from household_member m "
                    "join profile p on p.id=m.profile_id where m.household_id=? order by p.status desc, p.first_seen",
                    [household_id])


def devices_of(profile_id: str) -> list[dict]:
    rows = fetchall("select * from device where profile_id=? order by last_seen desc", [profile_id])
    for d in rows:
        d["shared_with"] = (fetchone(
            "select count(distinct profile_id) as n from device d2 join profile p on p.id=d2.profile_id "
            "where d2.tenant_id=? and d2.device_key=? and d2.profile_id != ? and p.merged_into is null "
            "and p.erased_at is null", [d["tenant_id"], d["device_key"], profile_id]) or {}).get("n") or 0
    return rows
