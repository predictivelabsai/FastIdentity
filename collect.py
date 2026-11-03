"""Ingest: browser pixel (/collect), no-JS pixel (/collect/px), server API (/v1/*).

Pipeline per message: authenticate write key → consent gate → deterministic
resolution → consent ledger → hashed signals (only with identity_resolution) →
append event → fold interests.
"""
from __future__ import annotations

import base64
import hmac
import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from db import execute, fetchone, insert_row
from domain import signals as signals_mod
from domain.audit import audit
from domain.consent import PURPOSES, has_purpose, purposes_from_cmp, record_cmp, record_consent
from domain.identity import extract_ids, find_identity, resolve, touch
from domain.privacy import erase, export, lookup
from settings import ADMIN_API_TOKEN, ALLOWED_ORIGINS, DEFAULT_TENANT, REQUIRE_ANALYTICS_CONSENT
from util import dumps, new_id, now_iso, to_iso

PII_PROPERTY_KEYS = {"email", "phone", "password", "name", "first_name", "last_name", "address", "dob"}
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
MAX_BATCH = 100


# --- helpers -----------------------------------------------------------------------

def _source_for_key(key: str | None):
    if not key:
        return None
    return fetchone("select * from source where write_key=?", [key])


def _key_from(request: Request, body: dict | None = None) -> str | None:
    header = request.headers.get("authorization") or ""
    key = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    return (key or (body or {}).get("writeKey") or request.query_params.get("writeKey")
            or request.query_params.get("k"))


def _origin_allowed(source: dict, origin: str | None) -> bool:
    allowed = [o.strip() for o in (source.get("allowed_origins") or "").split(",") if o.strip()] or ALLOWED_ORIGINS
    return not origin or "*" in allowed or origin in allowed


def cors_headers(request: Request) -> dict:
    origin = request.headers.get("origin")
    if not origin:
        return {}
    return {
        "access-control-allow-origin": origin,
        "access-control-allow-methods": "POST, GET, OPTIONS",
        "access-control-allow-headers": "content-type, authorization",
        "access-control-max-age": "86400",
        "vary": "Origin",
    }


async def _json(request: Request) -> dict:
    try:
        raw = await request.body()
        data = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _count_drop(tenant_id: str):
    day = now_iso()[:10]
    if fetchone("select 1 as x from consent_drop where tenant_id=? and day=?", [tenant_id, day]):
        execute("update consent_drop set dropped=dropped+1 where tenant_id=? and day=?", [tenant_id, day])
    else:
        insert_row("consent_drop", tenant_id=tenant_id, day=day, dropped=1)


def _explicit_consent(tenant_id: str, profile_id: str, consent: dict, source: str, context: dict):
    """Explicit opt-ins sent with identify / consent calls (e.g. a form checkbox)."""
    for purpose, val in (consent or {}).items():
        if purpose not in PURPOSES:
            continue
        if isinstance(val, dict):
            granted, basis, wording = bool(val.get("granted")), val.get("legal_basis", "consent"), val.get("wording")
        else:
            granted, basis, wording = bool(val), "consent", None
        record_consent(tenant_id, profile_id, purpose, granted, basis, source=source,
                       evidence={"wording": wording, "page": (context.get("page") or {}).get("url")})


# --- core pipeline -----------------------------------------------------------------

def process(source: dict, msg: dict, ip: str | None = None) -> dict:
    tenant_id = source.get("tenant_id") or DEFAULT_TENANT
    mtype = (msg.get("type") or "track").lower()
    context = msg.get("context") or {}
    cmp = context.get("consent")
    cmp_purposes = purposes_from_cmp(cmp)

    if REQUIRE_ANALYTICS_CONSENT and source.get("kind") != "server" and mtype != "consent":
        if not cmp_purposes.get("analytics"):
            _count_drop(tenant_id)
            return {"ok": True, "accepted": False, "reason": "no_analytics_consent"}

    ids = extract_ids(msg)
    if not ids:
        return {"ok": False, "accepted": False, "reason": "no_identifier"}
    profile_id = resolve(tenant_id, msg, actor=f"source:{source['id']}")

    if cmp:
        record_cmp(tenant_id, profile_id, cmp, source=f"cmp:{source['id']}")
    if msg.get("consent"):
        _explicit_consent(tenant_id, profile_id, msg["consent"], f"{mtype}:{source['id']}", context)

    anon = ids.get("anonymous_id")
    ident = find_identity(tenant_id, "anonymous_id", anon) if anon else None
    origin = ident["origin_profile_id"] if ident else profile_id

    if cmp_purposes.get("identity_resolution") or has_purpose(profile_id, "identity_resolution"):
        sig = signals_mod.extract(context, ip)
        if sig:
            signals_mod.record(tenant_id, profile_id, origin, sig)

    if mtype == "consent":
        return {"ok": True, "accepted": True, "profile_id": profile_id}

    name = {"page": "page_view", "identify": "identify"}.get(mtype) or msg.get("event") or "track"
    props = dict(msg.get("properties") or {})
    if mtype == "page":
        page = context.get("page") or {}
        props.setdefault("path", page.get("path"))
        props.setdefault("title", msg.get("name") or page.get("title"))
        props.setdefault("referrer", page.get("referrer"))
    props = {k: v for k, v in props.items() if k.lower() not in PII_PROPERTY_KEYS}
    safe_context = {k: v for k, v in context.items() if k in ("page", "campaign", "consent", "library", "timeOnPage")}

    eid = str(msg.get("messageId") or new_id("e_"))
    if not fetchone("select id from event where id=?", [eid]):
        insert_row("event", id=eid, tenant_id=tenant_id, event=name, ts=to_iso(msg.get("timestamp")),
                   profile_id=profile_id, origin_profile_id=origin, anonymous_id=anon, source_id=source["id"],
                   properties_json=dumps(props), context_json=dumps(safe_context))
        touch(profile_id, props)
    return {"ok": True, "accepted": True, "profile_id": profile_id, "event_id": eid}


# --- HTTP handlers ---------------------------------------------------------------

async def collect(request: Request):
    """Browser endpoint. Accepts a single message or {"batch": [...]}. Always 200s
    to the browser on accepted-or-dropped so the pixel never retries consent drops."""
    body = await _json(request)
    source = _source_for_key(_key_from(request, body))
    headers = cors_headers(request)
    if not source:
        return JSONResponse({"ok": False, "error": "invalid write key"}, status_code=401, headers=headers)
    if not _origin_allowed(source, request.headers.get("origin")):
        return JSONResponse({"ok": False, "error": "origin not allowed"}, status_code=403, headers=headers)
    ip = signals_mod.client_ip(request.headers, request.client.host if request.client else None)
    batch = body.get("batch") if isinstance(body.get("batch"), list) else [body]
    results = [process(source, m, ip) for m in batch[:MAX_BATCH] if isinstance(m, dict)]
    # Don't leak internal profile ids to the browser.
    return JSONResponse({"ok": True, "accepted": sum(1 for r in results if r.get("accepted"))}, headers=headers)


async def collect_preflight(request: Request):
    return Response(status_code=204, headers=cors_headers(request))


async def pixel(request: Request):
    """No-JS / email-open pixel: /collect/px?k=KEY&aid=ANON&e=page_view&c=analytics,marketing&p=/path"""
    q = request.query_params
    source = _source_for_key(q.get("k"))
    if source and q.get("aid"):
        cats = {c: True for c in (q.get("c") or "").split(",") if c}
        msg = {"type": "page" if q.get("e", "page_view") == "page_view" else "track", "event": q.get("e"),
               "anonymousId": q.get("aid"), "context": {"consent": cats, "page": {"path": q.get("p")}}}
        process(source, msg, signals_mod.client_ip(request.headers, request.client.host if request.client else None))
    return Response(GIF, media_type="image/gif", headers={"cache-control": "no-store"})


async def server_api(request: Request, mtype: str):
    """Segment-compatible server API: /v1/identify|track|page|consent."""
    body = await _json(request)
    source = _source_for_key(_key_from(request, body))
    if not source:
        return JSONResponse({"error": "invalid write key"}, status_code=401)
    body["type"] = mtype
    ip = body.get("context", {}).get("ip")
    return JSONResponse(process(source, body, ip))


def _admin_ok(request: Request) -> bool:
    header = request.headers.get("authorization") or ""
    token = header.removeprefix("Bearer ").strip()
    return bool(ADMIN_API_TOKEN) and hmac.compare_digest(token, ADMIN_API_TOKEN)


async def privacy_api(request: Request, action: str):
    """POST /v1/erasure or /v1/export with {email|phone|user_id|profile_id}. Admin token."""
    if not _admin_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await _json(request)
    tenant_id = body.get("tenant_id") or DEFAULT_TENANT
    pid = lookup(tenant_id, profile_id=body.get("profile_id"), email=body.get("email"),
                 phone=body.get("phone"), user_id=body.get("user_id"))
    if not pid:
        audit(tenant_id, "api", f"privacy.{action}.not_found", "request", "")
        return JSONResponse({"ok": True, "found": False})
    if action == "export":
        audit(tenant_id, "api", "profile.exported", "profile", pid)
        return JSONResponse({"ok": True, "found": True, "data": export(tenant_id, pid)})
    counts = erase(tenant_id, pid, actor="api", keep_suppression=body.get("keep_suppression", True))
    return JSONResponse({"ok": True, "found": True, "erased": counts})
