"""Identity graph: profiles, deterministic resolution, merge / unmerge.

Deterministic identifiers (``email``, ``phone``, ``user_id``, ``crm_id``,
``anonymous_id``) live in ``identity`` with a unique index per tenant, so one
value can belong to one live profile. Any identify call that touches identifiers
on two different profiles merges them (method ``deterministic``).

Every merge writes ``merge_log`` with a snapshot of the survivor; rows keep an
immutable ``origin_profile_id`` so ``unmerge`` can move identities, signals and
events back to the loser exactly.
"""
from __future__ import annotations

from db import execute, fetchall, fetchone, insert_row, update_row
from domain.audit import audit
from domain import consent as consent_mod
from settings import INTEREST_KEYS
from util import dumps, loads, new_id, normalize_email, normalize_phone, now_iso

ID_TYPES = ["user_id", "email", "phone", "crm_id", "anonymous_id"]
PROFILE_COLS = ["status", "email", "phone", "user_id", "traits_json", "interests_json", "first_seen", "last_seen"]


# --- lookups ---------------------------------------------------------------------

def get_profile(profile_id: str | None):
    if not profile_id:
        return None
    return fetchone("select * from profile where id=?", [profile_id])


def canonical_id(profile_id: str | None) -> str | None:
    """Follow merged_into pointers to the live profile."""
    seen = set()
    pid = profile_id
    while pid and pid not in seen:
        seen.add(pid)
        p = get_profile(pid)
        if not p:
            return None
        if not p.get("merged_into"):
            return pid
        pid = p["merged_into"]
    return pid


def find_identity(tenant_id: str, id_type: str, id_value: str | None):
    if not id_value:
        return None
    return fetchone(
        "select * from identity where tenant_id=? and id_type=? and id_value=?",
        [tenant_id, id_type, id_value],
    )


def identities_of(profile_id: str):
    return fetchall("select * from identity where profile_id=? order by created_at", [profile_id])


def _normalize(id_type: str, value):
    if value in (None, ""):
        return None
    if id_type == "email":
        return normalize_email(value)
    if id_type == "phone":
        return normalize_phone(value)
    return str(value).strip() or None


def extract_ids(payload: dict) -> dict[str, str]:
    traits = payload.get("traits") or {}
    raw = {
        "user_id": payload.get("userId") or payload.get("user_id"),
        "email": traits.get("email") or payload.get("email"),
        "phone": traits.get("phone") or payload.get("phone"),
        "crm_id": traits.get("crm_id") or payload.get("crm_id"),
        "anonymous_id": payload.get("anonymousId") or payload.get("anonymous_id"),
    }
    return {k: v for k, v in ((k, _normalize(k, v)) for k, v in raw.items()) if v}


# --- create / attach ----------------------------------------------------------------

def new_profile(tenant_id: str) -> str:
    pid = new_id("p_")
    ts = now_iso()
    insert_row("profile", id=pid, tenant_id=tenant_id, status="anonymous", first_seen=ts, last_seen=ts)
    return pid


def _attach_identity(tenant_id: str, profile_id: str, id_type: str, id_value: str, actor: str):
    existing = find_identity(tenant_id, id_type, id_value)
    if existing:
        owner = canonical_id(existing["profile_id"])
        if owner and owner != profile_id:
            merge(tenant_id, survivor_id=profile_id, loser_id=owner, method="deterministic",
                  reason=f"shared {id_type}", actor=actor)
        return
    insert_row("identity", id=new_id("i_"), tenant_id=tenant_id, profile_id=profile_id,
               origin_profile_id=profile_id, id_type=id_type, id_value=id_value, created_at=now_iso())


def _refresh_profile_columns(profile_id: str):
    """Denormalise primary identifiers + status from the identity table."""
    p = get_profile(profile_id)
    if not p:
        return
    ids = identities_of(profile_id)
    first = {}
    for i in ids:
        first.setdefault(i["id_type"], i["id_value"])
    email = p.get("email") if p.get("email") in [i["id_value"] for i in ids if i["id_type"] == "email"] else first.get("email")
    phone = p.get("phone") if p.get("phone") in [i["id_value"] for i in ids if i["id_type"] == "phone"] else first.get("phone")
    user_id = first.get("user_id")
    known = bool(email or phone or user_id or first.get("crm_id"))
    update_row("profile", profile_id, email=email, phone=phone, user_id=user_id,
               status="known" if known else "anonymous")


def resolve(tenant_id: str, payload: dict, actor: str = "ingest") -> str:
    """Deterministic resolution for an identify-style payload. Returns profile_id.

    Finds every live profile any supplied identifier points at; the oldest
    known profile survives and the rest merge into it. With no hits, a new
    (anonymous) profile is created.
    """
    ids = extract_ids(payload)
    hits: list[str] = []
    for id_type in ID_TYPES:
        row = find_identity(tenant_id, id_type, ids.get(id_type))
        if row:
            cid = canonical_id(row["profile_id"])
            if cid and cid not in hits:
                hits.append(cid)

    if hits:
        profiles = [get_profile(h) for h in hits]
        # Prefer a known profile, then the oldest, as survivor.
        profiles.sort(key=lambda p: (p["status"] != "known", p["first_seen"] or ""))
        profile_id = profiles[0]["id"]
        for other in profiles[1:]:
            merge(tenant_id, profile_id, other["id"], method="deterministic",
                  reason="identify linked identifiers", actor=actor)
    else:
        profile_id = new_profile(tenant_id)

    for id_type in ID_TYPES:
        if ids.get(id_type):
            _attach_identity(tenant_id, profile_id, id_type, ids[id_type], actor)

    traits = {k: v for k, v in (payload.get("traits") or {}).items()
              if v not in (None, "") and k not in ("email", "phone", "consent")}
    p = get_profile(profile_id)
    merged_traits = {**loads(p.get("traits_json")), **traits}
    update_row("profile", profile_id, traits_json=dumps(merged_traits), last_seen=now_iso())
    _refresh_profile_columns(profile_id)
    return profile_id


def touch(profile_id: str, properties: dict | None = None):
    """Update last_seen and fold domain interests (treatment, hospital…) into the profile."""
    p = get_profile(profile_id)
    if not p:
        return
    interests = loads(p.get("interests_json"))
    for key in INTEREST_KEYS:
        val = (properties or {}).get(key)
        if val not in (None, ""):
            bucket = interests.setdefault(key, {})
            bucket[str(val)] = bucket.get(str(val), 0) + 1
    update_row("profile", profile_id, interests_json=dumps(interests), last_seen=now_iso())


# --- merge / unmerge ---------------------------------------------------------------

def merge(tenant_id: str, survivor_id: str, loser_id: str, method: str = "manual",
          score: float | None = None, reason: str = "", actor: str = "system") -> str | None:
    survivor_id, loser_id = canonical_id(survivor_id), canonical_id(loser_id)
    if not survivor_id or not loser_id or survivor_id == loser_id:
        return None
    survivor, loser = get_profile(survivor_id), get_profile(loser_id)
    if survivor.get("erased_at") or loser.get("erased_at"):
        return None

    snapshot = {c: survivor.get(c) for c in PROFILE_COLS}
    log_id = new_id("m_")
    insert_row("merge_log", id=log_id, tenant_id=tenant_id, survivor_id=survivor_id, loser_id=loser_id,
               method=method, score=score, reason=reason, survivor_snapshot_json=dumps(snapshot),
               actor=actor, ts=now_iso())

    traits = {**loads(loser.get("traits_json")), **loads(survivor.get("traits_json"))}
    interests = loads(survivor.get("interests_json"))
    for key, bucket in loads(loser.get("interests_json")).items():
        mine = interests.setdefault(key, {})
        for val, n in bucket.items():
            mine[val] = mine.get(val, 0) + n
    firsts = [x for x in (survivor.get("first_seen"), loser.get("first_seen")) if x]
    lasts = [x for x in (survivor.get("last_seen"), loser.get("last_seen")) if x]
    update_row("profile", survivor_id, traits_json=dumps(traits), interests_json=dumps(interests),
               first_seen=min(firsts) if firsts else now_iso(), last_seen=max(lasts) if lasts else now_iso(),
               email=survivor.get("email") or loser.get("email"),
               phone=survivor.get("phone") or loser.get("phone"))
    update_row("profile", loser_id, merged_into=survivor_id)

    for table in ("identity", "signal", "event", "device"):
        execute(f"update {table} set profile_id=? where profile_id=?", [survivor_id, loser_id])
    consent_mod.copy_consent(tenant_id, loser_id, survivor_id)
    execute("update match_candidate set status='superseded' where status='pending' and (left_id=? or right_id=?)",
            [loser_id, loser_id])
    _refresh_profile_columns(survivor_id)
    audit(tenant_id, actor, "profile.merged", "profile", survivor_id,
          loser_id=loser_id, method=method, score=score, merge_id=log_id)
    return log_id


def unmerge(tenant_id: str, merge_id: str, actor: str = "system") -> bool:
    """Split a loser back out of its survivor (newest-first is safest).

    Rows whose ``origin_profile_id`` is the loser — or any profile that had
    itself been merged into the loser before — return to the loser. The
    survivor's profile columns are restored from the snapshot taken at merge.
    """
    log = fetchone("select * from merge_log where id=? and tenant_id=?", [merge_id, tenant_id])
    if not log or log.get("undone_at"):
        return False
    survivor_id, loser_id = log["survivor_id"], log["loser_id"]
    current_owner = canonical_id(survivor_id)

    # Profiles that live "inside" the loser: itself + anything merged into it earlier.
    family = {loser_id}
    frontier = [loser_id]
    while frontier:
        nxt = []
        for pid in frontier:
            for r in fetchall("select id from profile where merged_into=?", [pid]):
                if r["id"] not in family:
                    family.add(r["id"])
                    nxt.append(r["id"])
        frontier = nxt
    marks = ",".join("?" for _ in family)
    for table in ("identity", "signal", "event", "device"):
        execute(f"update {table} set profile_id=? where origin_profile_id in ({marks})", [loser_id, *family])

    update_row("profile", loser_id, merged_into=None)
    snap = loads(log.get("survivor_snapshot_json"))
    if snap:
        update_row("profile", survivor_id, **{k: snap.get(k) for k in PROFILE_COLS if k in snap})
    update_row("merge_log", merge_id, undone_at=now_iso(), undone_by=actor)
    _refresh_profile_columns(survivor_id)
    _refresh_profile_columns(loser_id)
    if current_owner and current_owner != survivor_id:
        _refresh_profile_columns(current_owner)
    audit(tenant_id, actor, "profile.unmerged", "profile", survivor_id, loser_id=loser_id, merge_id=merge_id)
    return True


def merge_history(profile_id: str):
    return fetchall(
        "select * from merge_log where survivor_id=? or loser_id=? order by ts desc", [profile_id, profile_id]
    )
