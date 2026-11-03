"""Minimal server-side Google OpenID Connect flow (same pattern as the other Fast* apps).

Enabled when GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are set. The FastSME apps
share one OAuth client; each app registers its own redirect URI
(https://<host>/auth/google/callback).
"""
from __future__ import annotations

import json
import secrets
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from settings import (GOOGLE_ALLOWED_DOMAINS, GOOGLE_ALLOWED_EMAILS, GOOGLE_CLIENT_ID,
                      GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def new_state() -> str:
    return secrets.token_urlsafe(32)


def callback_uri(request) -> str:
    if GOOGLE_REDIRECT_URI:
        return GOOGLE_REDIRECT_URI
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/auth/google/callback"


def authorize_url(request, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": GOOGLE_CLIENT_ID, "redirect_uri": callback_uri(request),
        "response_type": "code", "scope": "openid email profile",
        "state": state, "access_type": "online", "prompt": "select_account",
    })


def _json(url: str, *, data: dict | None = None, token: str | None = None) -> dict:
    body = urlencode(data).encode() if data else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(url, data=body, headers=headers), timeout=20) as resp:
        return json.loads(resp.read())


def exchange(request, code: str) -> dict | None:
    """Swap the auth code for the verified Google identity, or None."""
    try:
        token = _json(TOKEN_URL, data={
            "code": code, "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": callback_uri(request), "grant_type": "authorization_code",
        })
        info = _json(USERINFO_URL, token=token.get("access_token"))
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None
    email = (info.get("email") or "").strip().lower()
    if not email or info.get("email_verified") is False:
        return None
    return {"email": email, "name": info.get("name") or email}


def allow_listed(email: str) -> bool:
    """May this Google account be auto-provisioned as staff?"""
    email = email.lower()
    return email in GOOGLE_ALLOWED_EMAILS or email.rsplit("@", 1)[-1] in GOOGLE_ALLOWED_DOMAINS
