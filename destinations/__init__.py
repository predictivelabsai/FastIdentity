"""Activation destinations.

Each destination implements ``send_event`` (fire a flow trigger such as
"Cart Abandoned" so the ESP's own template/journey sends the message) and
``sync_profile`` (upsert the resolved contact). Both return ``(status, detail)``
where status is ``sent`` | ``failed`` | ``dry_run``.

Credentials come from env (see settings) and can be overridden per destination
row via ``config_json``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx

import settings

TIMEOUT = 10.0
KINDS = ["log", "webhook", "klaviyo", "customerio", "braze"]


def _cfg(dest: dict) -> dict:
    try:
        return json.loads(dest.get("config_json") or "{}")
    except json.JSONDecodeError:
        return {}


def _result(resp: httpx.Response) -> tuple[str, str]:
    if 200 <= resp.status_code < 300:
        return "sent", f"{resp.status_code}"
    return "failed", f"{resp.status_code} {resp.text[:300]}"


class Destination:
    kind = "log"
    required = ()

    def __init__(self, row: dict):
        self.row = row
        self.cfg = _cfg(row)

    def missing(self) -> list[str]:
        return [k for k in self.required if not self.cfg.get(k)]

    def send_event(self, name: str, profile: dict, properties: dict) -> tuple[str, str]:
        return "dry_run", f"would send {name!r} to {profile.get('email')}"

    def sync_profile(self, profile: dict, traits: dict) -> tuple[str, str]:
        return "dry_run", f"would sync {profile.get('id')}"

    def _guard(self, fn):
        missing = self.missing()
        if missing:
            return "failed", f"missing config: {', '.join(missing)}"
        try:
            return fn()
        except httpx.HTTPError as exc:
            return "failed", f"{type(exc).__name__}: {exc}"


class WebhookDestination(Destination):
    kind = "webhook"
    required = ("url",)

    def __init__(self, row):
        super().__init__(row)
        self.cfg.setdefault("url", settings.WEBHOOK_DEFAULT_URL)
        self.cfg.setdefault("secret", settings.WEBHOOK_SIGNING_SECRET)

    def _post(self, body: dict):
        raw = json.dumps(body, default=str).encode()
        headers = {"content-type": "application/json"}
        if self.cfg.get("secret"):
            sig = hmac.new(self.cfg["secret"].encode(), raw, hashlib.sha256).hexdigest()
            headers["x-fastidentity-signature"] = f"sha256={sig}"
        return _result(httpx.post(self.cfg["url"], content=raw, headers=headers, timeout=TIMEOUT))

    def send_event(self, name, profile, properties):
        return self._guard(lambda: self._post({"type": "event", "event": name, "profile": profile, "properties": properties}))

    def sync_profile(self, profile, traits):
        return self._guard(lambda: self._post({"type": "profile", "profile": profile, "traits": traits}))


class KlaviyoDestination(Destination):
    kind = "klaviyo"
    required = ("api_key",)
    base = "https://a.klaviyo.com/api"

    def __init__(self, row):
        super().__init__(row)
        self.cfg.setdefault("api_key", settings.KLAVIYO_API_KEY)

    def _headers(self):
        return {
            "Authorization": f"Klaviyo-API-Key {self.cfg['api_key']}",
            "revision": self.cfg.get("revision", "2024-10-15"),
            "content-type": "application/vnd.api+json",
            "accept": "application/vnd.api+json",
        }

    @staticmethod
    def _profile_attrs(profile):
        attrs = {"external_id": profile["id"]}
        if profile.get("email"):
            attrs["email"] = profile["email"]
        if profile.get("phone"):
            attrs["phone_number"] = profile["phone"]
        return attrs

    def send_event(self, name, profile, properties):
        body = {"data": {"type": "event", "attributes": {
            "properties": properties,
            "metric": {"data": {"type": "metric", "attributes": {"name": name}}},
            "profile": {"data": {"type": "profile", "attributes": self._profile_attrs(profile)}},
        }}}
        return self._guard(lambda: _result(httpx.post(f"{self.base}/events/", json=body, headers=self._headers(), timeout=TIMEOUT)))

    def sync_profile(self, profile, traits):
        attrs = {**self._profile_attrs(profile), "properties": traits}
        body = {"data": {"type": "profile", "attributes": attrs}}
        return self._guard(lambda: _result(httpx.post(f"{self.base}/profile-import/", json=body, headers=self._headers(), timeout=TIMEOUT)))


class CustomerIODestination(Destination):
    kind = "customerio"
    required = ("site_id", "api_key")

    def __init__(self, row):
        super().__init__(row)
        self.cfg.setdefault("site_id", settings.CUSTOMERIO_SITE_ID)
        self.cfg.setdefault("api_key", settings.CUSTOMERIO_API_KEY)
        self.cfg.setdefault("region", settings.CUSTOMERIO_REGION)

    @property
    def base(self):
        host = "track-eu.customer.io" if self.cfg.get("region") == "eu" else "track.customer.io"
        return f"https://{host}/api/v1"

    def _auth(self):
        token = base64.b64encode(f"{self.cfg['site_id']}:{self.cfg['api_key']}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def send_event(self, name, profile, properties):
        url = f"{self.base}/customers/{profile['id']}/events"
        return self._guard(lambda: _result(httpx.post(url, json={"name": name, "data": properties}, headers=self._auth(), timeout=TIMEOUT)))

    def sync_profile(self, profile, traits):
        url = f"{self.base}/customers/{profile['id']}"
        body = {**traits, "email": profile.get("email")}
        return self._guard(lambda: _result(httpx.put(url, json=body, headers=self._auth(), timeout=TIMEOUT)))


class BrazeDestination(Destination):
    kind = "braze"
    required = ("api_key", "rest_endpoint")

    def __init__(self, row):
        super().__init__(row)
        self.cfg.setdefault("api_key", settings.BRAZE_API_KEY)
        self.cfg.setdefault("rest_endpoint", settings.BRAZE_REST_ENDPOINT)

    def _post(self, body):
        return _result(httpx.post(f"{self.cfg['rest_endpoint'].rstrip('/')}/users/track", json=body,
                                  headers={"Authorization": f"Bearer {self.cfg['api_key']}"}, timeout=TIMEOUT))

    def send_event(self, name, profile, properties):
        from util import now_iso
        body = {"events": [{"external_id": profile["id"], "name": name, "time": now_iso(), "properties": properties}]}
        return self._guard(lambda: self._post(body))

    def sync_profile(self, profile, traits):
        body = {"attributes": [{"external_id": profile["id"], "email": profile.get("email"), **traits}]}
        return self._guard(lambda: self._post(body))


REGISTRY = {
    "log": Destination,
    "webhook": WebhookDestination,
    "klaviyo": KlaviyoDestination,
    "customerio": CustomerIODestination,
    "braze": BrazeDestination,
}


def build(row: dict | None) -> Destination:
    if not row:
        return Destination({"kind": "log"})
    return REGISTRY.get(row.get("kind"), Destination)(row)
