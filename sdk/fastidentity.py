"""Tiny server-side SDK (stdlib + httpx). Use a write key from a *server* source.

    fi = FastIdentity("wk_...", host="https://identity.fastsme.com")
    fi.identify(user_id="u_1", anonymous_id=cookie_aid, traits={"email": "a@b.com"},
                consent={"marketing_email": {"granted": True, "wording": "Email me offers"}})
    fi.track("booking_completed", user_id="u_1", properties={"treatment": "cataract"})
    fi.erase(email="a@b.com", admin_token="...")   # right to erasure
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx


class FastIdentity:
    def __init__(self, write_key: str, host: str = "http://localhost:5023", timeout: float = 5.0):
        self.write_key = write_key
        self.host = host.rstrip("/")
        self.client = httpx.Client(timeout=timeout)

    def _post(self, path: str, body: dict, token: str | None = None) -> dict:
        body.setdefault("messageId", str(uuid.uuid4()))
        body.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        r = self.client.post(f"{self.host}{path}", json=body,
                             headers={"authorization": f"Bearer {token or self.write_key}"})
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _ids(user_id, anonymous_id):
        out = {}
        if user_id:
            out["userId"] = user_id
        if anonymous_id:
            out["anonymousId"] = anonymous_id
        return out

    def identify(self, user_id=None, anonymous_id=None, traits=None, consent=None, context=None):
        return self._post("/v1/identify", {**self._ids(user_id, anonymous_id), "traits": traits or {},
                                           "consent": consent or {}, "context": context or {}})

    def track(self, event, user_id=None, anonymous_id=None, properties=None, context=None):
        return self._post("/v1/track", {**self._ids(user_id, anonymous_id), "event": event,
                                        "properties": properties or {}, "context": context or {}})

    def page(self, path, user_id=None, anonymous_id=None, context=None):
        ctx = {**(context or {}), "page": {"path": path}}
        return self._post("/v1/page", {**self._ids(user_id, anonymous_id), "context": ctx})

    def consent(self, purposes: dict, user_id=None, anonymous_id=None, email=None):
        body = {**self._ids(user_id, anonymous_id), "consent": purposes}
        if email:
            body["traits"] = {"email": email}
        return self._post("/v1/consent", body)

    def erase(self, admin_token: str, **who):
        return self._post("/v1/erasure", dict(who), token=admin_token)

    def export(self, admin_token: str, **who):
        return self._post("/v1/export", dict(who), token=admin_token)
