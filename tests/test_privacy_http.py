from starlette.testclient import TestClient

from collect import process
from db import fetchone
from domain.consent import can_message
from domain.identity import get_profile
from tests.conftest import ctx

IP = "86.10.20.30"


def _client():
    from app import app
    return TestClient(app)


def test_erasure_api_deletes_and_keeps_hashed_suppression(web):
    pid = process(web, {"type": "identify", "anonymousId": "a1", "context": ctx(), "traits": {"email": "gone@example.com"},
                        "consent": {"marketing_email": True}}, IP)["profile_id"]
    c = _client()
    assert c.post("/v1/erasure", json={"email": "gone@example.com"}).status_code == 401
    r = c.post("/v1/erasure", json={"email": "gone@example.com"}, headers={"authorization": "Bearer tok"})
    assert r.json()["found"] is True
    assert fetchone("select count(*) as n from event")["n"] == 0
    assert fetchone("select count(*) as n from identity")["n"] == 0
    assert fetchone("select email, status from profile where id=?", [pid]) == {"email": None, "status": "erased"}
    sup = fetchone("select * from suppression")
    assert sup and "gone@" not in sup["address_hash"]
    # Comes back and opts in again: still suppressed.
    pid2 = process(web, {"type": "identify", "anonymousId": "a9", "context": ctx(), "traits": {"email": "gone@example.com"},
                         "consent": {"marketing_email": True}}, IP)["profile_id"]
    assert can_message(get_profile(pid2), "email") == (False, "suppressed")


def test_export_api(web):
    process(web, {"type": "identify", "anonymousId": "a1", "context": ctx(), "traits": {"email": "me@example.com"}}, IP)
    r = _client().post("/v1/export", json={"email": "me@example.com"}, headers={"authorization": "Bearer tok"})
    data = r.json()["data"]
    assert any(i["value"] == "me@example.com" for i in data["identifiers"])
    assert data["consent"]["analytics"]["granted"] is True


def test_collect_endpoint_and_cors(web):
    c = _client()
    body = {"writeKey": web["write_key"], "type": "page", "anonymousId": "x1", "context": ctx()}
    r = c.post("/collect", content=__import__("json").dumps(body), headers={"origin": "https://site.example", "content-type": "text/plain"})
    assert r.status_code == 200 and r.json()["accepted"] == 1
    assert r.headers["access-control-allow-origin"] == "https://site.example"
    assert "profile_id" not in r.json()
    assert c.post("/collect", json={"writeKey": "nope"}).status_code == 401
    assert c.options("/collect", headers={"origin": "https://site.example"}).status_code == 204


def test_pixel_gif(web):
    r = _client().get(f"/collect/px?k={web['write_key']}&aid=px1&c=analytics")
    assert r.headers["content-type"] == "image/gif"
    assert fetchone("select count(*) as n from event")["n"] == 1


def test_admin_requires_login():
    r = _client().get("/profiles", follow_redirects=False)
    assert r.status_code == 303


def test_admin_pages_render(web):
    process(web, {"type": "identify", "anonymousId": "a1", "context": ctx(), "traits": {"email": "ui@example.com"}}, IP)
    c = _client()
    c.post("/login", data={"email": "admin@fastsme.com", "password": "admin"})
    pid = fetchone("select id from profile")["id"]
    for path in ["/", "/profiles", f"/profiles/{pid}", "/events", "/candidates", "/quality", "/segments", "/flows",
                 "/destinations", "/consent", "/privacy", "/audit", "/sources"]:
        r = c.get(path)
        assert r.status_code == 200, path
        assert "FastIdentity" in r.text, path
