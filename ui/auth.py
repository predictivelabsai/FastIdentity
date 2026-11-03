from fasthtml.common import *

from domain import tenancy
from domain.audit import audit
from ui import google_auth
from util import now_iso, verify_password

GOOGLE_ONLY = "!google"  # password_hash marker: account can only sign in via Google


def login_page(msg: str = "", note: str = ""):
    return Title("Sign in · FastIdentity"), Main(
        Article(
            H1("FastIdentity"),
            P("First-party, consent-first identity resolution.", cls="muted"),
            P(note, cls="muted") if note else None,
            P(msg, style="color:#b00") if msg else None,
            (A("Continue with Google", href="/auth/google", role="button", style="width:100%")
             if google_auth.enabled() else None),
            Details(
                Summary("Sign in with email and password", cls="muted"),
                Form(
                    Label("Email", Input(name="email", type="email", required=True)),
                    Label("Password", Input(name="password", type="password", required=True)),
                    Button("Sign in", type="submit", cls="secondary"),
                    method="post", action="/login",
                ),
                open=not google_auth.enabled(),
            ),
            P(Small("New here? Continue with Google to create your workspace." if google_auth.enabled() else ""),
              cls="muted"),
        ),
        cls="container", style="max-width:28rem;margin-top:4rem",
    )


def password_user(email: str, password: str):
    user = tenancy.get_user(email)
    if not user or not verify_password(password, user["password_hash"]):
        return None
    return user


def google_user(email: str, name: str):
    """Existing user, or a new one when sign-up is allowed for this email. None = denied."""
    user = tenancy.get_user(email)
    if user:
        return user
    if not tenancy.may_sign_up(email):
        return None
    user = tenancy.create_user(email, name, GOOGLE_ONLY)
    audit(None, email, "user.signed_up", "user", email, method="google")
    return user


def finish_login(user: dict, sess, method: str) -> str:
    """Start the session, join invited/allowed-domain workspaces, pick a workspace.

    Returns where to redirect: the dashboard, or onboarding when the user has no workspace.
    """
    sess.clear()
    sess["auth"] = user["email"]
    joined = tenancy.accept_pending_invites(user) + tenancy.auto_join_domains(user)
    tid = joined[0] if joined else tenancy.default_workspace(user)
    if tid:
        sess["tenant_id"] = tid
        from db import update_row

        update_row("staff_user", user["id"], last_tenant_id=tid)
    audit(tid, user["email"], "staff.login", method=method, at=now_iso())
    return "/" if tid else "/onboarding"
