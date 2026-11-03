"""Google SSO: state check, allow-list provisioning, existing staff, denial."""
import importlib

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    import settings
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(settings, "GOOGLE_ALLOWED_DOMAINS", {"predictivelabs.ai"})
    monkeypatch.setattr(settings, "GOOGLE_ALLOWED_EMAILS", {"owner@gmail.com"})
    from ui import google_auth
    importlib.reload(google_auth)
    import app as app_mod
    monkeypatch.setattr(app_mod, "google_auth", google_auth)
    import ui.auth as auth_mod
    monkeypatch.setattr(auth_mod, "google_auth", google_auth)
    return TestClient(app_mod.app), google_auth, monkeypatch


def _login_via_google(c, google_auth, monkeypatch, email):
    r = c.get("/auth/google", follow_redirects=False)
    assert r.status_code == 303 and "accounts.google.com" in r.headers["location"]
    state = r.headers["location"].split("state=")[1].split("&")[0]
    monkeypatch.setattr(google_auth, "exchange", lambda req, code: {"email": email, "name": email})
    return c.get(f"/auth/google/callback?code=x&state={state}", follow_redirects=False)


def test_login_page_shows_google_button(client):
    c, *_ = client
    assert "Sign in with Google" in c.get("/login").text


def test_bad_state_rejected(client):
    c, *_ = client
    c.get("/auth/google", follow_redirects=False)
    r = c.get("/auth/google/callback?code=x&state=forged", follow_redirects=False)
    assert r.status_code == 200 and "expired" in r.text


def test_allow_listed_domain_is_provisioned(client):
    c, google_auth, mp = client
    r = _login_via_google(c, google_auth, mp, "new.person@predictivelabs.ai")
    assert r.status_code == 303 and r.headers["location"] == "/"
    from db import fetchone
    row = fetchone("select * from staff_user where email=?", ["new.person@predictivelabs.ai"])
    assert row and row["password_hash"] == "!google"
    assert c.get("/profiles").status_code == 200


def test_unknown_account_denied(client):
    c, google_auth, mp = client
    r = _login_via_google(c, google_auth, mp, "stranger@gmail.com")
    assert r.status_code == 200 and "not authorised" in r.text
    from db import fetchone
    assert not fetchone("select id from staff_user where email=?", ["stranger@gmail.com"])
    assert c.get("/profiles", follow_redirects=False).status_code == 303


def test_google_only_account_cannot_password_login(client):
    c, google_auth, mp = client
    _login_via_google(c, google_auth, mp, "owner@gmail.com")
    c.get("/logout")
    r = c.post("/login", data={"email": "owner@gmail.com", "password": "!google"}, follow_redirects=False)
    assert r.status_code == 200 and "Invalid" in r.text
