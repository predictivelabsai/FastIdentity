"""Consent ledger, legal-basis gates and suppression.

Purposes (what a profile has agreed to):

* ``analytics``            – collect behavioural events at all (CMP "analytics")
* ``personalisation``      – use events to tailor the site (CMP "functional"/"preferences")
* ``identity_resolution``  – link this browser/device to other sessions probabilistically
                             (CMP "marketing"/"advertisement")
* ``marketing_email``      – send marketing email (explicit form opt-in or PECR soft opt-in)
* ``marketing_sms``        – send marketing SMS (explicit opt-in)

Cookie-banner consent never implies email consent: ``marketing_*`` purposes are
only set from an explicit opt-in captured with the identify call, an admin action
or the consent API.
"""
from __future__ import annotations

from db import fetchall, fetchone, insert_row
from domain.audit import audit
from util import dumps, keyed_hash, new_id, now_iso, now_iso_us

PURPOSES = ["analytics", "personalisation", "identity_resolution", "marketing_email", "marketing_sms"]
LEGAL_BASES = ["consent", "soft_opt_in", "legitimate_interest", "contract"]

# CMP category -> purposes. Covers CookieYes, OneTrust (C000x groups) and Google
# Consent Mode keys; extend per CMP.
CMP_CATEGORY_MAP = {
    "analytics": ["analytics"],
    "statistics": ["analytics"],
    "performance": ["analytics"],
    "analytics_storage": ["analytics"],
    "C0002": ["analytics"],
    "functional": ["personalisation"],
    "preferences": ["personalisation"],
    "personalization_storage": ["personalisation"],
    "C0003": ["personalisation"],
    "marketing": ["identity_resolution"],
    "advertisement": ["identity_resolution"],
    "targeting": ["identity_resolution"],
    "ad_storage": ["identity_resolution"],
    "C0004": ["identity_resolution"],
}

CHANNEL_PURPOSE = {"email": "marketing_email", "sms": "marketing_sms"}


def purposes_from_cmp(cmp: dict | None) -> dict[str, bool]:
    """Translate a CMP consent object ({"analytics": true, ...}) into purposes."""
    out: dict[str, bool] = {}
    for key, value in (cmp or {}).items():
        granted = value is True or str(value).lower() in {"1", "true", "yes", "granted", "accepted"}
        for purpose in CMP_CATEGORY_MAP.get(key, []):
            out[purpose] = out.get(purpose, False) or granted
    return out


def current_consent(profile_id: str) -> dict[str, dict]:
    rows = fetchall(
        "select * from consent_record where profile_id=? order by ts asc, id asc", [profile_id]
    )
    state: dict[str, dict] = {}
    for r in rows:
        state[r["purpose"]] = {
            "granted": bool(r["granted"]),
            "legal_basis": r["legal_basis"],
            "source": r["source"],
            "ts": r["ts"],
        }
    return state


def has_purpose(profile_id: str, purpose: str) -> bool:
    row = fetchone(
        "select granted from consent_record where profile_id=? and purpose=? order by ts desc, id desc limit 1",
        [profile_id, purpose],
    )
    return bool(row and row["granted"])


def record_consent(
    tenant_id: str,
    profile_id: str,
    purpose: str,
    granted: bool,
    legal_basis: str = "consent",
    source: str = "",
    evidence: dict | None = None,
    actor: str = "system",
    only_if_changed: bool = True,
):
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}")
    if legal_basis not in LEGAL_BASES:
        raise ValueError(f"unknown legal basis {legal_basis!r}")
    if only_if_changed:
        cur = current_consent(profile_id).get(purpose)
        if cur and cur["granted"] == bool(granted) and cur["legal_basis"] == legal_basis:
            return False
    insert_row(
        "consent_record",
        id=new_id("c_"),
        tenant_id=tenant_id,
        profile_id=profile_id,
        purpose=purpose,
        granted=1 if granted else 0,
        legal_basis=legal_basis,
        source=source,
        evidence_json=dumps(evidence or {}),
        ts=now_iso_us(),
    )
    audit(tenant_id, actor, "consent.recorded", "profile", profile_id,
          purpose=purpose, granted=bool(granted), legal_basis=legal_basis, source=source)

    if not granted and purpose in CHANNEL_PURPOSE.values():
        p = fetchone("select email, phone from profile where id=?", [profile_id])
        channel = "email" if purpose == "marketing_email" else "sms"
        addr = (p or {}).get("email" if channel == "email" else "phone")
        if addr:
            suppress(tenant_id, channel, addr, reason="consent_withdrawn", actor=actor)
    return True


def record_cmp(tenant_id: str, profile_id: str, cmp: dict | None, source: str = "cmp"):
    for purpose, granted in purposes_from_cmp(cmp).items():
        record_consent(tenant_id, profile_id, purpose, granted, "consent", source, evidence={"cmp": cmp})


def copy_consent(tenant_id: str, from_profile: str, to_profile: str):
    """On merge: carry the loser's consent forward where the survivor has no record.

    Withdrawals always win (a withdrawal on either side is applied), so a merge can
    never silently re-enable marketing for someone who opted out.
    """
    a, b = current_consent(to_profile), current_consent(from_profile)
    for purpose, rec in b.items():
        mine = a.get(purpose)
        if mine is None or (mine["granted"] and not rec["granted"]):
            record_consent(tenant_id, to_profile, purpose, rec["granted"], rec["legal_basis"],
                           source=f"merge:{from_profile}")


# --- suppression ----------------------------------------------------------------

def _addr_hash(channel: str, address: str) -> str:
    return keyed_hash(address.strip().lower(), purpose=f"suppress:{channel}")


def suppress(tenant_id: str, channel: str, address: str, reason: str = "unsubscribe", actor: str = "system"):
    h = _addr_hash(channel, address)
    if fetchone("select id from suppression where tenant_id=? and channel=? and address_hash=?", [tenant_id, channel, h]):
        return
    insert_row("suppression", id=new_id("s_"), tenant_id=tenant_id, channel=channel,
               address_hash=h, reason=reason, created_at=now_iso())
    audit(tenant_id, actor, "suppression.added", "suppression", h, channel=channel, reason=reason)


def is_suppressed(tenant_id: str, channel: str, address: str | None) -> bool:
    if not address:
        return False
    return bool(fetchone(
        "select id from suppression where tenant_id=? and channel=? and address_hash=?",
        [tenant_id, channel, _addr_hash(channel, address)],
    ))


def can_message(profile: dict, channel: str) -> tuple[bool, str]:
    """Gate every outbound message. Returns (allowed, reason)."""
    if not profile or profile.get("erased_at") or profile.get("merged_into"):
        return False, "profile_inactive"
    addr = profile.get("email") if channel == "email" else profile.get("phone")
    if not addr:
        return False, "no_address"
    purpose = CHANNEL_PURPOSE.get(channel)
    if not purpose:
        return False, "unknown_channel"
    state = current_consent(profile["id"]).get(purpose)
    if not state or not state["granted"]:
        return False, "no_consent"
    if state["legal_basis"] not in ("consent", "soft_opt_in"):
        # PECR: consumer electronic marketing needs consent or the soft opt-in.
        return False, "invalid_legal_basis"
    if is_suppressed(profile["tenant_id"], channel, addr):
        return False, "suppressed"
    return True, "ok"


def can_link_probabilistically(profile_ids: list[str]) -> bool:
    """Probabilistic (non-deterministic) links need identity_resolution on every side."""
    return all(has_purpose(pid, "identity_resolution") for pid in profile_ids)
