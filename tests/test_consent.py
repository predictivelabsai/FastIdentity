from collect import process
from db import fetchone
from domain.consent import can_message, current_consent, purposes_from_cmp, record_consent
from domain.identity import canonical_id, get_profile
from tests.conftest import ANALYTICS_ONLY, ctx

IP = "86.10.20.30"


def test_no_analytics_consent_drops_event(web):
    r = process(web, {"type": "track", "event": "x", "anonymousId": "a1", "context": {"consent": {"analytics": False}}}, IP)
    assert r["accepted"] is False
    assert fetchone("select count(*) as n from profile")["n"] == 0
    assert fetchone("select dropped from consent_drop")["dropped"] == 1


def test_missing_cmp_state_drops_event(web):
    assert process(web, {"type": "track", "event": "x", "anonymousId": "a1"}, IP)["accepted"] is False


def test_server_source_not_gated(server):
    assert process(server, {"type": "track", "event": "booking_completed", "userId": "u1"})["accepted"]


def test_cmp_mapping():
    assert purposes_from_cmp({"C0002": True, "C0004": False}) == {"analytics": True, "identity_resolution": False}
    assert purposes_from_cmp({"analytics_storage": "granted", "ad_storage": "denied"}) == {
        "analytics": True, "identity_resolution": False}


def test_signals_only_with_identity_resolution(web):
    process(web, {"type": "page", "anonymousId": "a1", "context": ctx(ANALYTICS_ONLY)}, IP)
    assert fetchone("select count(*) as n from signal")["n"] == 0
    process(web, {"type": "page", "anonymousId": "a2", "context": ctx()}, IP)
    assert fetchone("select count(*) as n from signal")["n"] >= 3
    # never stores raw values
    assert fetchone("select count(*) as n from signal where value_hash like '%86.10%' or value_hash like '%Mozilla%'")["n"] == 0


def test_cookie_consent_never_implies_email_consent(web):
    pid = process(web, {"type": "identify", "anonymousId": "a1", "context": ctx(),
                        "traits": {"email": "a@example.com"}}, IP)["profile_id"]
    assert can_message(get_profile(pid), "email") == (False, "no_consent")


def test_explicit_optin_then_withdrawal_suppresses(web):
    pid = process(web, {"type": "identify", "anonymousId": "a1", "context": ctx(), "traits": {"email": "a@example.com"},
                        "consent": {"marketing_email": {"granted": True, "wording": "Email me"}}}, IP)["profile_id"]
    assert can_message(get_profile(pid), "email") == (True, "ok")
    record_consent("demo", pid, "marketing_email", False, source="unsubscribe")
    assert can_message(get_profile(pid), "email") == (False, "no_consent")
    # Re-granting consent does not bypass the suppression list.
    record_consent("demo", pid, "marketing_email", True, source="test")
    assert can_message(get_profile(pid), "email") == (False, "suppressed")


def test_legitimate_interest_cannot_send_email(server):
    pid = process(server, {"type": "identify", "userId": "u1", "traits": {"email": "b@example.com"}})["profile_id"]
    record_consent("demo", pid, "marketing_email", True, legal_basis="legitimate_interest")
    assert can_message(get_profile(pid), "email") == (False, "invalid_legal_basis")


def test_merge_keeps_withdrawal(web):
    a = process(web, {"type": "identify", "anonymousId": "a1", "context": ctx(), "traits": {"email": "c@example.com"},
                      "consent": {"marketing_email": True}}, IP)["profile_id"]
    b = process(web, {"type": "identify", "anonymousId": "b1", "context": ctx(), "traits": {"user_id": "x"},
                      "userId": "u9", "consent": {"marketing_email": False}}, IP)["profile_id"]
    process(web, {"type": "identify", "userId": "u9", "anonymousId": "a1", "context": ctx()}, IP)
    survivor = canonical_id(a)
    assert survivor == canonical_id(b)
    assert current_consent(survivor)["marketing_email"]["granted"] is False
