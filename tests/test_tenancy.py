"""SaaS layer: workspace isolation, roles, secret API keys, invites, domain auto-join."""
import pytest
from starlette.testclient import TestClient

from collect import process
from db import fetchone, scalar
from domain import tenancy
from tests.conftest import ctx
from util import hash_password

IP = "86.1.2.3"


@pytest.fixture
def app_client():
    from app import app
    return TestClient(app)


def _user(email, password="pw"):
    return tenancy.create_user(email, email, hash_password(password))


def _login(c, email, password="pw"):
    r = c.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return r


def _workspace(name, owner_email):
    owner = _user(owner_email)
    tid = tenancy.create_workspace(name, actor=owner_email, owner=owner)
    return tid, owner


def _profile_in(tid, email):
    src = fetchone("select * from source where tenant_id=? and kind='js'", [tid])
    return process(src, {"type": "identify", "anonymousId": f"a-{email}", "context": ctx(),
                         "traits": {"email": email}}, IP)["profile_id"]


def test_new_workspace_gets_starter_kit():
    tid, _ = _workspace("MyMedicalGateway", "cto@mmg.test")
    assert tid == "mymedicalgateway"
    assert scalar("select count(*) from source where tenant_id=?", [tid]) == 2
    assert scalar("select count(*) from flow where tenant_id=?", [tid]) == 3
    assert scalar("select count(*) from destination where tenant_id=? and kind='log'", [tid]) == 1


def test_member_cannot_see_or_touch_other_workspace(app_client):
    a, _ = _workspace("Acme", "owner@acme.test")
    b, _ = _workspace("Beta", "owner@beta.test")
    pa = _profile_in(a, "alice@example.com")
    pb = _profile_in(b, "bob@example.com")
    _login(app_client, "owner@acme.test")
    assert "alice@example.com" in app_client.get("/profiles").text
    assert "bob@example.com" not in app_client.get("/profiles").text
    assert app_client.get(f"/profiles/{pa}").status_code == 200
    assert app_client.get(f"/profiles/{pb}").status_code == 404
    assert app_client.get(f"/profiles/{pb}/export").status_code == 404
    assert app_client.post(f"/profiles/{pb}/erase").status_code == 404
    assert fetchone("select status from profile where id=?", [pb])["status"] != "erased"
    # Switching into a workspace you are not a member of is refused.
    assert app_client.post("/workspace/switch", data={"tenant_id": b}).status_code == 403


def test_roles_are_enforced(app_client):
    tid, owner = _workspace("Acme", "owner@acme.test")
    viewer = _user("viewer@acme.test")
    tenancy.add_member(tid, viewer, "viewer", actor="test")
    pid = _profile_in(tid, "carol@example.com")
    _login(app_client, "viewer@acme.test")
    assert app_client.get("/profiles").status_code == 200
    assert app_client.get(f"/profiles/{pid}/export").status_code == 403
    assert app_client.post(f"/profiles/{pid}/erase").status_code == 403
    assert app_client.get("/settings/api-keys").status_code == 403
    assert app_client.post("/sources", data={"name": "x"}).status_code == 403


def test_secret_key_is_scoped_to_its_workspace(app_client):
    a, _ = _workspace("Acme", "owner@acme.test")
    b, _ = _workspace("Beta", "owner@beta.test")
    _profile_in(b, "bob@example.com")
    key = tenancy.create_api_key(a, "backend", ["ingest", "privacy"], "test")
    assert key.startswith("fi_sk_") and not fetchone("select id from api_key where key_hash=?", [key])
    hdr = {"authorization": f"Bearer {key}"}
    r = app_client.post("/v1/identify", json={"userId": "12345", "traits": {"email": "pat@example.com"}}, headers=hdr)
    assert r.status_code == 200 and r.json()["accepted"]
    assert fetchone("select tenant_id from profile where id=?", [r.json()["profile_id"]])["tenant_id"] == a
    # The same userId in another workspace is a different person.
    assert not fetchone("select id from identity where tenant_id=? and id_value='12345'", [b])
    # Erasure with Acme's key cannot reach Beta's data.
    r = app_client.post("/v1/erasure", json={"email": "bob@example.com"}, headers=hdr)
    assert r.json()["found"] is False


def test_scopes_and_revocation(app_client):
    tid, _ = _workspace("Acme", "owner@acme.test")
    ingest_only = tenancy.create_api_key(tid, "ingest", ["ingest"], "test")
    hdr = {"authorization": f"Bearer {ingest_only}"}
    assert app_client.post("/v1/export", json={"email": "x@example.com"}, headers=hdr).status_code == 401
    assert app_client.post("/v1/track", json={"userId": "1", "event": "x"}, headers=hdr).status_code == 200
    kid = fetchone("select id from api_key where tenant_id=?", [tid])["id"]
    tenancy.revoke_api_key(tid, kid, "test")
    assert app_client.post("/v1/track", json={"userId": "1", "event": "x"}, headers=hdr).status_code == 401


def test_api_key_ui_shows_key_once(app_client):
    _workspace("Acme", "owner@acme.test")
    _login(app_client, "owner@acme.test")
    r = app_client.post("/settings/api-keys", data={"name": "MMG backend", "scope_ingest": "on"})
    assert "fi_sk_" in r.text and "will not be shown again" in r.text
    listing = app_client.get("/settings/api-keys").text
    raw = r.text.split('class="secret">')[1].split("<")[0]
    assert raw not in listing and raw[:14] in listing


def test_invite_flow_joins_workspace(app_client):
    tid, _ = _workspace("Acme", "owner@acme.test")
    _login(app_client, "owner@acme.test")
    r = app_client.post("/settings/members/invite", data={"email": "new@acme.test", "role": "analyst"})
    link = r.text.split('class="secret">')[1].split("<")[0]
    token = link.rsplit("/", 1)[-1]
    app_client.get("/logout")
    newbie = _user("new@acme.test")
    assert app_client.get(f"/invite/{token}", follow_redirects=False).headers["location"] == "/login"
    _login(app_client, "new@acme.test")
    assert tenancy.role_in(newbie, tid) == "analyst"
    assert app_client.get("/settings/members").status_code == 403  # analysts can't manage members


def test_domain_auto_join():
    tid, _ = _workspace("MyMedicalGateway", "owner@mmg.test")
    tenancy.update_workspace(tid, "test", allowed_domains="mymedicalgateway.com")
    dev = _user("joonatan@mymedicalgateway.com")
    assert tenancy.auto_join_domains(dev) == [tid]
    assert tenancy.role_in(dev, tid) == "analyst"
    outsider = _user("eve@evil.test")
    assert tenancy.auto_join_domains(outsider) == []


def test_last_owner_cannot_be_removed():
    tid, owner = _workspace("Acme", "owner@acme.test")
    mid = fetchone("select id from membership where tenant_id=? and user_id=?", [tid, owner["id"]])["id"]
    assert tenancy.remove_member(tid, mid, "test") == "A workspace needs at least one owner."
    assert tenancy.set_role(tid, mid, "viewer", "test") == "A workspace needs at least one owner."


def test_platform_admin_can_open_any_workspace(app_client):
    tid, _ = _workspace("Acme", "owner@acme.test")
    _login(app_client, "admin@fastsme.com", "admin")
    assert "Acme" in app_client.get("/platform/workspaces").text
    assert app_client.post("/workspace/switch", data={"tenant_id": tid}, follow_redirects=False).status_code == 303
    assert fetchone("select id from audit_log where tenant_id=? and action='platform.workspace_opened'", [tid])


def test_non_platform_user_cannot_reach_platform(app_client):
    _workspace("Acme", "owner@acme.test")
    _login(app_client, "owner@acme.test")
    assert app_client.get("/platform/workspaces").status_code == 403


def test_onboarding_creates_owned_workspace(app_client):
    _user("solo@startup.test")
    r = _login(app_client, "solo@startup.test")
    assert r.headers["location"] == "/onboarding"
    app_client.post("/onboarding", data={"name": "Startup Ltd"})
    user = tenancy.get_user("solo@startup.test")
    assert tenancy.role_in(user, "startup-ltd") == "owner"
    assert app_client.get("/").status_code == 200
