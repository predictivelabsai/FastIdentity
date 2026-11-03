"""Devices (explicit ids vs fingerprints) and household grouping."""
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from collect import process
from db import execute, fetchall, fetchone, scalar
from domain import households, privacy
from domain.matching import run_matching
from domain.segments import evaluate
from domain.signals import describe_device, explicit_device_id, household_ip
from tests.conftest import FULL

HOME = "86.10.20.30"
PHONE = {"userAgent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
         "screen": {"width": 390, "height": 844, "density": 3}, "timezone": "Europe/London", "locale": "en-GB"}
LAPTOP = {"userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Version/17.5 Safari/605.1.15",
          "screen": {"width": 1512, "height": 982, "density": 2}, "timezone": "Europe/London", "locale": "en-GB"}


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def visit(source, anon, device, ip=HOME, hours=1, email=None, cmp=FULL, **extra):
    msg = {"type": "identify" if email else "page", "anonymousId": anon, "timestamp": ago(hours),
           "context": {"consent": cmp, **device}, **extra}
    if email:
        msg["traits"] = {"email": email}
    return process(source, msg, ip)["profile_id"]


@pytest.fixture
def hh_on():
    execute("update tenant set households_enabled=1 where id='demo'")


def test_device_descriptors():
    assert describe_device(PHONE["userAgent"]) == {"device_class": "mobile", "os": "iOS", "browser": "Safari"}
    assert describe_device(LAPTOP["userAgent"])["device_class"] == "desktop"
    assert describe_device("Mozilla/5.0 (Linux; Android 14; SM-X710) Chrome/128.0 Safari/537.36")["device_class"] == "tablet"
    assert describe_device("Googlebot/2.1")["device_class"] == "bot"
    assert household_ip("10.0.0.4") is None and household_ip("2a00:23c5:1:2:3:4:5:6") == "2a00:23c5:1:2::/64"
    assert explicit_device_id({"device": {"id": "00000000-0000-0000-0000-000000000000"}}) is None


def test_devices_recorded_per_person_and_kind(web):
    pid = visit(web, "a1", PHONE, email="ada@example.com")
    visit(web, "a1", LAPTOP)
    rows = fetchall("select * from device where profile_id=? order by device_class", [pid])
    assert [r["device_class"] for r in rows] == ["desktop", "mobile"]
    assert {r["key_kind"] for r in rows} == {"fingerprint"}
    # No identity_resolution consent -> no device rows.
    other = visit(web, "a2", PHONE, cmp={"analytics": True, "marketing": False})
    assert not fetchone("select id from device where profile_id=?", [other])


def test_explicit_device_id_replaces_fingerprint_and_links_logout(web):
    did = {**PHONE, "device": {"id": "fi-did-1234567890"}}
    pid = visit(web, "before", PHONE, email="ben@example.com")
    visit(web, "before", did)
    rows = fetchall("select key_kind from device where profile_id=?", [pid])
    assert [r["key_kind"] for r in rows] == ["device_id"]
    # After logout (new anonymous id, same device id) the visitor is a strong candidate.
    # Browser window resized (fingerprint changes) but the device id is stable.
    anon = visit(web, "after-logout", {**did, "screen": {"width": 1, "height": 1}})
    stats = run_matching("demo")
    assert stats["auto_merged"] == 1
    assert fetchone("select merged_into from profile where id=?", [anon])["merged_into"] == pid


def test_household_groups_people_without_merging(web, hh_on):
    a = visit(web, "a", LAPTOP, hours=40, email="ada@example.com")
    visit(web, "a", LAPTOP, hours=2)
    b = visit(web, "b", PHONE, hours=30, email="ben@example.com")
    visit(web, "b", PHONE, hours=3)
    stats = households.rebuild("demo")
    assert stats["households"] == 1 and stats["members"] == 2
    h = households.household_of(a)
    assert h and households.household_of(b)["id"] == h["id"] and h["devices"] == 2
    assert fetchone("select merged_into from profile where id=?", [b])["merged_into"] is None
    assert [p["id"] for p in evaluate("demo", {"all": [{"type": "household", "min": 2}]})] != []
    assert {p["id"] for p in evaluate("demo", {"all": [{"type": "device", "value": "mobile"}]})} == {b}


def test_household_needs_two_days_and_ignores_busy_networks(web, hh_on):
    visit(web, "a", LAPTOP, hours=1, email="ada@example.com")
    visit(web, "b", PHONE, hours=2, email="ben@example.com")
    assert households.rebuild("demo")["too_new"] == 1
    office = "81.2.3.4"
    for i in range(8):
        visit(web, f"o{i}", {**LAPTOP, "screen": {"width": 1000 + i, "height": 800}}, ip=office, hours=30 + i,
              email=f"staff{i}@example.com")
        visit(web, f"o{i}", {**LAPTOP, "screen": {"width": 1000 + i, "height": 800}}, ip=office, hours=1)
    stats = households.rebuild("demo")
    assert stats["shared_networks"] == 1 and stats["households"] == 0


def test_household_ip_only_collected_when_enabled(web):
    visit(web, "a", LAPTOP, email="ada@example.com")
    assert not scalar("select count(*) from signal where signal_type='hh_ip'")
    assert households.rebuild("demo") == {"enabled": False}


def test_same_household_flag_on_candidates(web, hh_on):
    a = visit(web, "a", LAPTOP, hours=40, email="ada@example.com")
    visit(web, "a", LAPTOP, hours=2)
    b = visit(web, "b", LAPTOP, hours=30, email="ben@example.com")  # same laptop model + home: looks alike
    visit(web, "b", LAPTOP, hours=3)
    households.rebuild("demo")
    run_matching("demo")
    c = fetchone("select * from match_candidate where tenant_id='demo'")
    assert c and c["status"] == "pending" and '"household"' in c["features_json"]  # known<->known: review only
    assert {c["left_id"], c["right_id"]} == {a, b}


def test_erasure_and_export_cover_devices_and_households(web, hh_on):
    a = visit(web, "a", LAPTOP, hours=40, email="ada@example.com")
    visit(web, "a", LAPTOP, hours=2)
    b = visit(web, "b", PHONE, hours=30, email="ben@example.com")
    visit(web, "b", PHONE, hours=3)
    households.rebuild("demo")
    bundle = privacy.export("demo", a)
    assert bundle["devices"][0]["class"] == "desktop" and bundle["household"]
    assert "ben@example.com" not in str(bundle)  # other members are not disclosed
    privacy.erase("demo", a, actor="test")
    assert not fetchone("select id from device where profile_id=?", [a])
    assert not fetchone("select * from household_member where profile_id=?", [a])
    assert households.rebuild("demo")["households"] == 0  # one person left is not a household


def test_households_ui_and_owner_toggle(web):
    from app import app
    from domain import tenancy
    from util import hash_password

    c = TestClient(app)
    c.post("/login", data={"email": "admin@fastsme.com", "password": "admin"})
    visit(web, "a", PHONE, email="ada@example.com")
    r = c.get("/households")
    assert r.status_code == 200 and "Devices by type" in r.text and "mobile" in r.text
    c.post("/households/toggle", data={"enabled": "1"})
    assert households.enabled("demo")
    visit(web, "a", PHONE)
    assert scalar("select count(*) from signal where signal_type='hh_ip'") == 1
    c.post("/households/toggle", data={"enabled": "0"})
    assert not households.enabled("demo") and not scalar("select count(*) from signal where signal_type='hh_ip'")
    # Analysts can look but not switch it on.
    analyst = tenancy.create_user("an@acme.test", "an", hash_password("pw"))
    tenancy.add_member("demo", analyst, "analyst", actor="test")
    c2 = TestClient(app)
    c2.post("/login", data={"email": "an@acme.test", "password": "pw"})
    assert c2.get("/households").status_code == 200
    assert c2.post("/households/toggle", data={"enabled": "1"}).status_code == 403
