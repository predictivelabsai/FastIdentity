from datetime import datetime, timedelta, timezone

from collect import process
from db import execute, fetchall, fetchone
from domain.activation import run_all
from tests.conftest import ctx

IP = "86.10.20.30"
NOW = datetime.now(timezone.utc)


def _ago(h):
    return (NOW - timedelta(hours=h)).isoformat()


def _booker(web, aid, email, optin=True, complete=False):
    process(web, {"type": "identify", "anonymousId": aid, "context": ctx(), "timestamp": _ago(5),
                  "traits": {"email": email}, "consent": {"marketing_email": optin}}, IP)
    process(web, {"type": "track", "event": "booking_started", "anonymousId": aid, "context": ctx(),
                  "timestamp": _ago(4), "properties": {"treatment": "cataract surgery"}}, IP)
    if complete:
        process(web, {"type": "track", "event": "booking_completed", "anonymousId": aid, "context": ctx(),
                      "timestamp": _ago(3)}, IP)


def _msgs(status=None):
    rows = fetchall("select m.*, f.name from message m join flow f on f.id=m.flow_id where f.name='Booking abandonment'")
    return [r for r in rows if status is None or r["status"] == status]


def test_booking_abandonment_respects_consent_and_conversion(web):
    _booker(web, "a1", "yes@example.com", optin=True)
    _booker(web, "a2", "no@example.com", optin=False)
    _booker(web, "a3", "done@example.com", optin=True, complete=True)
    run_all("demo", now=NOW)
    sent = _msgs("dry_run")
    skipped = _msgs("skipped")
    assert len(sent) == 1 and "cataract" in sent[0]["payload_json"]
    assert len(skipped) == 1 and skipped[0]["reason"] == "no_consent"


def test_flow_is_idempotent(web):
    _booker(web, "a1", "yes@example.com")
    run_all("demo", now=NOW)
    run_all("demo", now=NOW)
    assert len(_msgs("dry_run")) == 1


def test_frequency_cap(web):
    execute("update flow set frequency_cap=1, frequency_window_days=7")
    _booker(web, "a1", "yes@example.com")
    process(web, {"type": "track", "event": "cart_started", "anonymousId": "a1", "context": ctx(), "timestamp": _ago(4)}, IP)
    run_all("demo", now=NOW)
    statuses = [r["status"] for r in fetchall("select status from message")]
    assert statuses.count("dry_run") == 1
    assert fetchone("select count(*) as n from message where reason='frequency_cap'")["n"] == 1


def test_anonymous_intent_not_messaged(web):
    process(web, {"type": "track", "event": "booking_started", "anonymousId": "zz", "context": ctx(), "timestamp": _ago(4)}, IP)
    run_all("demo", now=NOW)
    assert fetchone("select count(*) as n from message")["n"] == 0
