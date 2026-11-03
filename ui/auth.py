from fasthtml.common import *

from db import fetchone, insert_row
from settings import ADMIN_EMAIL, DEFAULT_TENANT
from ui import google_auth
from util import new_id, verify_password

GOOGLE_ONLY = "!google"  # password_hash marker: account can only sign in via Google


def login_page(msg=""):
    return Title("Sign in · FastIdentity"), Main(
        Article(
            H1("FastIdentity"),
            P("First-party, consent-first identity resolution.", cls="muted"),
            P(msg, style="color:#b00") if msg else None,
            Form(
                Label("Email", Input(name="email", type="email", value=ADMIN_EMAIL, required=True)),
                Label("Password", Input(name="password", type="password", required=True)),
                Button("Sign in", type="submit"),
                method="post", action="/login",
            ),
            (Div(P("or", cls="muted", style="text-align:center;margin:.5rem 0"),
                 A("Sign in with Google", href="/auth/google", role="button", cls="outline",
                   style="width:100%")) if google_auth.enabled() else None),
        ),
        cls="container", style="max-width:28rem;margin-top:4rem",
    )


def _start_session(row, sess):
    sess["auth"] = row["email"]
    sess["tenant_id"] = row["tenant_id"]
    sess["role"] = row["role"]


def try_login(email: str, password: str, sess) -> bool:
    row = fetchone("select * from staff_user where email=?", [email.strip().lower()])
    if not row or not verify_password(password, row["password_hash"]):
        return False
    _start_session(row, sess)
    return True


def google_login(email: str, sess) -> tuple[bool, bool]:
    """Sign in a verified Google identity. Returns (ok, newly_provisioned).

    Existing staff may always use Google; unknown accounts are created only when
    their email or domain is allow-listed (GOOGLE_ALLOWED_EMAILS / _DOMAINS).
    """
    email = email.strip().lower()
    row = fetchone("select * from staff_user where email=?", [email])
    created = False
    if not row:
        if not google_auth.allow_listed(email):
            return False, False
        insert_row("staff_user", id=new_id("u_"), tenant_id=DEFAULT_TENANT, email=email,
                   password_hash=GOOGLE_ONLY, role="admin")
        row = fetchone("select * from staff_user where email=?", [email])
        created = True
    _start_session(row, sess)
    return True, created
