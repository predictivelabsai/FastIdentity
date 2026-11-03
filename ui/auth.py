from fasthtml.common import *

from db import fetchone
from settings import ADMIN_EMAIL
from util import verify_password


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
        ),
        cls="container", style="max-width:28rem;margin-top:4rem",
    )


def try_login(email: str, password: str, sess) -> bool:
    row = fetchone("select * from staff_user where email=?", [email.strip().lower()])
    if not row or not verify_password(password, row["password_hash"]):
        return False
    sess["auth"] = row["email"]
    sess["tenant_id"] = row["tenant_id"]
    sess["role"] = row["role"]
    return True
