from collect import process
from db import fetchall, fetchone
from domain.identity import canonical_id, get_profile, merge_history, unmerge
from tests.conftest import ctx

IP = "86.10.20.30"


def test_anonymous_then_identify_links_same_profile(web):
    r1 = process(web, {"type": "track", "event": "product_viewed", "anonymousId": "a1", "context": ctx(),
                       "properties": {"treatment": "MRI scan"}}, IP)
    r2 = process(web, {"type": "identify", "anonymousId": "a1", "context": ctx(), "traits": {"email": "Ada@Example.com"}}, IP)
    assert r1["profile_id"] == r2["profile_id"]
    p = get_profile(r2["profile_id"])
    assert p["status"] == "known" and p["email"] == "ada@example.com"
    assert "MRI scan" in p["interests_json"]


def test_two_devices_merge_deterministically_on_email_and_unmerge(web):
    a = process(web, {"type": "track", "event": "product_viewed", "anonymousId": "a1", "context": ctx()}, IP)["profile_id"]
    process(web, {"type": "identify", "anonymousId": "a1", "context": ctx(), "traits": {"email": "ben@example.com"}}, IP)
    b = process(web, {"type": "track", "event": "cart_started", "anonymousId": "b1", "context": ctx()}, IP)["profile_id"]
    assert a != b
    process(web, {"type": "identify", "anonymousId": "b1", "context": ctx(), "traits": {"email": "ben@example.com"}}, IP)
    assert canonical_id(b) == a
    assert fetchone("select count(*) as n from event where profile_id=?", [a])["n"] == 4

    log = [m for m in merge_history(a) if m["loser_id"] == b][0]
    assert log["method"] == "deterministic"
    assert unmerge("demo", log["id"], actor="test")
    assert canonical_id(b) == b
    # events that originated on device b (incl. the identify that linked it) go back to b
    assert {e["event"] for e in fetchall("select event from event where profile_id=?", [b])} == {"cart_started", "identify"}
    assert "cart_started" not in {e["event"] for e in fetchall("select event from event where profile_id=?", [a])}
    # b1 anonymous id went back with b
    assert fetchone("select profile_id from identity where id_value='b1'")["profile_id"] == b


def test_phone_normalisation_links_profiles(server):
    p1 = process(server, {"type": "identify", "userId": "u1", "traits": {"phone": "07700 900123"}})["profile_id"]
    p2 = process(server, {"type": "identify", "traits": {"phone": "+44 7700 900123"}})["profile_id"]
    assert p1 == p2


def test_idempotent_message_id(web):
    m = {"type": "track", "event": "x", "anonymousId": "a1", "messageId": "m-1", "context": ctx()}
    process(web, m, IP)
    process(web, m, IP)
    assert fetchone("select count(*) as n from event where id='m-1'")["n"] == 1


def test_pii_stripped_from_properties(web):
    process(web, {"type": "track", "event": "x", "anonymousId": "a1", "context": ctx(),
                  "properties": {"email": "x@y.com", "treatment": "MRI"}}, IP)
    props = fetchone("select properties_json from event")["properties_json"]
    assert "x@y.com" not in props and "MRI" in props
