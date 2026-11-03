"""Per-request SaaS context: signed-in user, current workspace, role.

Resolved once per request by an ASGI middleware that sits *inside* the session
middleware, and exposed through a ContextVar so layout code (sidebar, guards)
can read it without threading ``sess`` through every view.
"""
from __future__ import annotations

from contextvars import ContextVar

from domain import tenancy

_CTX: ContextVar[dict] = ContextVar("fi_ctx", default={})


def resolve(session: dict) -> dict:
    email = (session or {}).get("auth")
    if not email:
        return {}
    user = tenancy.get_user(email)
    if not user:
        return {}
    tid = session.get("tenant_id")
    role = tenancy.role_in(user, tid) if tid else None
    if not role:
        tid = tenancy.default_workspace(user)
        role = tenancy.role_in(user, tid) if tid else None
        if tid:
            session["tenant_id"] = tid
    ws = tenancy.get_workspace(tid) if tid else None
    return {
        "user": user,
        "email": user["email"],
        "platform_admin": bool(user.get("is_platform_admin")),
        "tenant_id": tid if role else None,
        "workspace": ws if role else None,
        "role": role,
        "member_of": tenancy.memberships(user),
    }


def current() -> dict:
    return _CTX.get()


def tenant() -> str | None:
    return _CTX.get().get("tenant_id")


def role() -> str | None:
    return _CTX.get().get("role")


def can(needed: str) -> bool:
    return tenancy.role_at_least(role(), needed)


class ContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            ctx = resolve(scope.get("session") or {})
            scope["fi"] = ctx
            _CTX.set(ctx)
        await self.app(scope, receive, send)
