"""Workspaces (tenants), members, roles, invites and API keys — the SaaS layer.

* A **user** (``staff_user``) is global and identified by email.
* A **workspace** is a ``tenant``; all customer data is partitioned by ``tenant_id``.
* **Membership** grants a user a role in a workspace:
  ``owner`` > ``admin`` > ``analyst`` > ``viewer``.
* **Platform admins** (FastSME staff) can open any workspace; every switch is audited.
* **Publishable write keys** (``wk_…``) live on sources and are safe in a browser.
* **Secret API keys** (``fi_sk_…``) authenticate server-to-server calls for one
  workspace. Only a SHA-256 hash is stored; the key is shown once at creation.
"""
from __future__ import annotations

import hashlib
import re
import secrets

from db import execute, fetchall, fetchone, insert_row, update_row
from domain.audit import audit
from settings import (ADMIN_EMAIL, DEFAULT_TENANT, PLATFORM_ADMIN_DOMAINS, PLATFORM_ADMIN_EMAILS,
                      SIGNUP_OPEN)
from util import new_id, now_iso

ROLES = ["viewer", "analyst", "admin", "owner"]
ROLE_HELP = {
    "viewer": "read-only access to the workspace",
    "analyst": "+ review merges, run matching, build segments",
    "admin": "+ flows, destinations, sources, API keys, members, privacy actions",
    "owner": "+ workspace settings and ownership",
}
SCOPES = ["ingest", "privacy"]
KEY_PREFIX = "fi_sk_"


def role_at_least(role: str | None, needed: str) -> bool:
    return role in ROLES and ROLES.index(role) >= ROLES.index(needed)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --- users -------------------------------------------------------------------------------

def get_user(email: str):
    return fetchone("select * from staff_user where email=?", [email.strip().lower()])


def is_platform_email(email: str) -> bool:
    email = email.strip().lower()
    return (email == ADMIN_EMAIL.lower() or email in PLATFORM_ADMIN_EMAILS
            or email.rsplit("@", 1)[-1] in PLATFORM_ADMIN_DOMAINS)


def create_user(email: str, name: str = "", password_hash: str = "!google") -> dict:
    email = email.strip().lower()
    insert_row("staff_user", id=new_id("u_"), tenant_id=None, email=email, name=name or email,
               password_hash=password_hash, role="admin",
               is_platform_admin=1 if is_platform_email(email) else 0, created_at=now_iso())
    return get_user(email)


# --- workspaces -----------------------------------------------------------------------------

def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "workspace"
    base, n = slug, 2
    while fetchone("select id from tenant where id=?", [slug]):
        slug, n = f"{base}-{n}", n + 1
    return slug


def create_workspace(name: str, actor: str, owner: dict | None = None, slug: str | None = None,
                     allowed_domains: str = "") -> str:
    """Create a workspace with the starter kit. ``owner`` (if given) becomes its owner."""
    from seed import bootstrap_workspace  # local import: seed imports domain modules

    tid = slugify(slug or name)
    insert_row("tenant", id=tid, name=name.strip() or tid, created_at=now_iso(), plan="free",
               allowed_domains=allowed_domains.strip().lower(), join_role="analyst", created_by=actor)
    if owner:
        add_member(tid, owner, "owner", actor=actor)
    bootstrap_workspace(tid)
    audit(tid, actor, "workspace.created", "tenant", tid, name=name)
    return tid


def get_workspace(tid: str):
    return fetchone("select * from tenant where id=?", [tid])


def all_workspaces():
    return fetchall(
        "select t.*, (select count(*) from membership m where m.tenant_id=t.id) as members, "
        "(select count(*) from profile p where p.tenant_id=t.id and p.merged_into is null) as profiles "
        "from tenant t order by t.created_at", [])


def update_workspace(tid: str, actor: str, **fields):
    update_row("tenant", tid, **fields)
    audit(tid, actor, "workspace.updated", "tenant", tid, **fields)


# --- membership ------------------------------------------------------------------------------

def add_member(tid: str, user: dict, role: str, actor: str):
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    if fetchone("select id from membership where tenant_id=? and user_id=?", [tid, user["id"]]):
        return
    insert_row("membership", id=new_id("mb_"), tenant_id=tid, user_id=user["id"], role=role, created_at=now_iso())
    audit(tid, actor, "member.added", "user", user["email"], role=role)


def memberships(user: dict) -> list[dict]:
    return fetchall(
        "select m.role, t.id as tenant_id, t.name from membership m join tenant t on t.id=m.tenant_id "
        "where m.user_id=? order by t.name", [user["id"]])


def role_in(user: dict, tid: str) -> str | None:
    row = fetchone("select role from membership where tenant_id=? and user_id=?", [tid, user["id"]])
    if row:
        return row["role"]
    return "owner" if user.get("is_platform_admin") and get_workspace(tid) else None


def members(tid: str) -> list[dict]:
    return fetchall(
        "select m.id, m.role, m.created_at, u.email, u.name, u.id as user_id from membership m "
        "join staff_user u on u.id=m.user_id where m.tenant_id=? order by m.created_at", [tid])


def owners(tid: str) -> int:
    return len(fetchall("select id from membership where tenant_id=? and role='owner'", [tid]))


def set_role(tid: str, membership_id: str, role: str, actor: str) -> str | None:
    m = fetchone("select * from membership where id=? and tenant_id=?", [membership_id, tid])
    if not m or role not in ROLES:
        return "No such member."
    if m["role"] == "owner" and role != "owner" and owners(tid) <= 1:
        return "A workspace needs at least one owner."
    update_row("membership", membership_id, role=role)
    audit(tid, actor, "member.role_changed", "membership", membership_id, role=role)
    return None


def remove_member(tid: str, membership_id: str, actor: str) -> str | None:
    m = fetchone("select * from membership where id=? and tenant_id=?", [membership_id, tid])
    if not m:
        return "No such member."
    if m["role"] == "owner" and owners(tid) <= 1:
        return "A workspace needs at least one owner."
    execute("delete from membership where id=?", [membership_id])
    audit(tid, actor, "member.removed", "membership", membership_id)
    return None


# --- invites ---------------------------------------------------------------------------------

def create_invite(tid: str, email: str, role: str, actor: str) -> str:
    """Returns the raw invite token (put it in the invite link); only its hash is stored."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    token = secrets.token_urlsafe(24)
    insert_row("invite", id=new_id("inv_"), tenant_id=tid, email=email.strip().lower(), role=role,
               token_hash=_sha(token), invited_by=actor, created_at=now_iso())
    audit(tid, actor, "member.invited", "invite", email.strip().lower(), role=role)
    return token


def pending_invites(tid: str) -> list[dict]:
    return fetchall("select * from invite where tenant_id=? and accepted_at is null and revoked_at is null "
                    "order by created_at desc", [tid])


def revoke_invite(tid: str, invite_id: str, actor: str):
    execute("update invite set revoked_at=? where id=? and tenant_id=?", [now_iso(), invite_id, tid])
    audit(tid, actor, "invite.revoked", "invite", invite_id)


def invite_by_token(token: str):
    return fetchone("select * from invite where token_hash=? and accepted_at is null and revoked_at is null",
                    [_sha(token)])


def accept_pending_invites(user: dict) -> list[str]:
    """Join every workspace this email was invited to."""
    joined = []
    for inv in fetchall("select * from invite where email=? and accepted_at is null and revoked_at is null",
                        [user["email"]]):
        add_member(inv["tenant_id"], user, inv["role"], actor=user["email"])
        update_row("invite", inv["id"], accepted_at=now_iso())
        joined.append(inv["tenant_id"])
    return joined


def auto_join_domains(user: dict) -> list[str]:
    """Join workspaces whose allowed_domains include the user's email domain."""
    domain = user["email"].rsplit("@", 1)[-1]
    joined = []
    for t in fetchall("select * from tenant where allowed_domains is not null and allowed_domains != ''", []):
        domains = {d.strip() for d in (t["allowed_domains"] or "").split(",") if d.strip()}
        if domain in domains and not fetchone("select id from membership where tenant_id=? and user_id=?",
                                              [t["id"], user["id"]]):
            add_member(t["id"], user, t.get("join_role") or "analyst", actor="domain-join")
            joined.append(t["id"])
    return joined


def may_sign_up(email: str) -> bool:
    email = email.strip().lower()
    if SIGNUP_OPEN or is_platform_email(email):
        return True
    if fetchone("select id from invite where email=? and accepted_at is null and revoked_at is null", [email]):
        return True
    domain = email.rsplit("@", 1)[-1]
    return any(domain in {d.strip() for d in (t["allowed_domains"] or "").split(",")}
               for t in fetchall("select allowed_domains from tenant where allowed_domains is not null", []))


def default_workspace(user: dict) -> str | None:
    ms = memberships(user)
    ids = [m["tenant_id"] for m in ms]
    if user.get("last_tenant_id") and (user["last_tenant_id"] in ids or user.get("is_platform_admin")):
        if get_workspace(user["last_tenant_id"]):
            return user["last_tenant_id"]
    if ids:
        return ids[0]
    if user.get("is_platform_admin") and get_workspace(DEFAULT_TENANT):
        return DEFAULT_TENANT
    return None


# --- API keys ----------------------------------------------------------------------------------

def create_api_key(tid: str, name: str, scopes: list[str], actor: str) -> str:
    """Create a secret key and return it — the only time it is ever visible."""
    scopes = [s for s in scopes if s in SCOPES] or ["ingest"]
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    insert_row("api_key", id=new_id("key_"), tenant_id=tid, name=name.strip() or "API key",
               prefix=key[:14], key_hash=_sha(key), scopes=",".join(scopes), created_by=actor,
               created_at=now_iso())
    audit(tid, actor, "api_key.created", "api_key", key[:14], scopes=",".join(scopes))
    return key


def api_keys(tid: str) -> list[dict]:
    return fetchall("select id, name, prefix, scopes, created_by, created_at, last_used_at, revoked_at "
                    "from api_key where tenant_id=? order by created_at desc", [tid])


def revoke_api_key(tid: str, key_id: str, actor: str):
    execute("update api_key set revoked_at=? where id=? and tenant_id=? and revoked_at is null",
            [now_iso(), key_id, tid])
    audit(tid, actor, "api_key.revoked", "api_key", key_id)


def authenticate_key(raw: str | None, scope: str) -> dict | None:
    """Resolve a secret key to its (live) row if it carries ``scope``."""
    if not raw or not raw.startswith(KEY_PREFIX):
        return None
    row = fetchone("select * from api_key where key_hash=? and revoked_at is null", [_sha(raw)])
    if not row or scope not in (row["scopes"] or "").split(","):
        return None
    if not row["last_used_at"] or row["last_used_at"][:13] != now_iso()[:13]:  # at most hourly writes
        execute("update api_key set last_used_at=? where id=?", [now_iso(), row["id"]])
    return row


def server_source(tid: str) -> dict:
    """The source that secret-key server calls are attributed to (created on demand)."""
    src = fetchone("select * from source where tenant_id=? and kind='server' order by created_at limit 1", [tid])
    if src:
        return src
    from util import new_write_key

    sid = new_id("src_")
    insert_row("source", id=sid, tenant_id=tid, name="Backend", write_key=new_write_key(), kind="server",
               allowed_origins="", created_at=now_iso())
    return fetchone("select * from source where id=?", [sid])


# --- migration from single-tenant installs ------------------------------------------------------

def migrate_legacy_users():
    """Give pre-SaaS staff rows (staff_user.tenant_id) a membership, and flag platform admins."""
    for u in fetchall("select * from staff_user", []):
        if u.get("tenant_id") and get_workspace(u["tenant_id"]) and not fetchone(
                "select id from membership where tenant_id=? and user_id=?", [u["tenant_id"], u["id"]]):
            insert_row("membership", id=new_id("mb_"), tenant_id=u["tenant_id"], user_id=u["id"],
                       role="owner", created_at=now_iso())
        if is_platform_email(u["email"]) and not u.get("is_platform_admin"):
            update_row("staff_user", u["id"], is_platform_admin=1)
