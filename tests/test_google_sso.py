"""Google SSO: state check, sign-up rules, existing users, Google-only accounts."""
import importlib

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    import settings
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "csecret")
    from ui import google_auth
    importlib.reload(google_auth)
    import app as app_mod
    import ui.auth as auth_mod
    monkeypatch.setattr(app_mod, "google_auth", google_auth)
    monkeypatch.setattr(auth_mod, "google_auth", google_auth)
    return TestClient(app_mod.app), google_auth, monkeypatch


def google_login(c, google_auth, monkeypatch, email):
    r = c.get("/auth/google", follow_redirects=False)
    assert r.status_code == 303 and "accounts.google.com" in r.headers["location"]
    state = r.headers["location"].split("state=")[1].split("&")[0]
    monkeypatch.setattr(google_auth, "exchange", lambda req, code: {"email": email, "name": email})
    return c.get(f"/auth/google/callback?code=x&state={state}", follow_redirects=False)


def test_login_page_shows_google_button(client):
    c, *_ = client
    assert "Continue with Google" in c.get("/login").text


def test_bad_state_rejected(client):
    c, *_ = client
    c.get("/auth/google", follow_redirects=False)
    r = c.get("/auth/google/callback?code=x&state=forged", follow_redirects=False)
    assert r.status_code == 200 and "expired" in r.text


def test_open_signup_lands_in_onboarding_with_no_data_access(client):
    c, google_auth, mp = client
    r = google_login(c, google_auth, mp, "founder@startup.io")
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    assert c.get("/profiles", follow_redirects=False).headers["location"] == "/onboarding"


def test_closed_signup_denies_strangers(client):
    c, google_auth, mp = client
    import domain.tenancy as tenancy
    mp.setattr(tenancy, "SIGNUP_OPEN", False)
    r = google_login(c, google_auth, mp, "stranger@gmail.com")
    assert r.status_code == 200 and "no access" in r.text
    from db import fetchone
    assert not fetchone("select id from staff_user where email=?", ["stranger@gmail.com"])


def test_google_only_account_cannot_password_login(client):
    c, google_auth, mp = client
    google_login(c, google_auth, mp, "owner@gmail.com")
    c.get("/logout")
    r = c.post("/login", data={"email": "owner@gmail.com", "password": "!google"}, follow_redirects=False)
    assert r.status_code == 200 and "Invalid" in r.text
