"""FastIdentity — first-party, consent-first identity resolution (FastHTML), multi-tenant SaaS."""
import re

from fasthtml.common import *
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

import collect
from db import execute, fetchone, insert_row, scalar, update_row
from domain import matching, privacy, tenancy
from domain.activation import run_all, sync_profile
from domain.audit import audit
from domain.consent import record_consent
from domain.identity import unmerge
from domain.segments import refresh as refresh_segment
from seed import bootstrap
from settings import PORT, PUBLIC_BASE_URL, SECRET_KEY
from ui import context, google_auth, graph, ops
from ui import settings as settings_ui
from ui.auth import finish_login, google_user, login_page, password_user
from ui.layout import CSS, page
from util import dumps, new_id, new_write_key, now_iso

bootstrap()

PUBLIC = ("/collect", "/v1/", "/static/", "/login", "/health", "/auth/google", "/invite/")
NO_WORKSPACE_OK = ("/onboarding", "/logout", "/workspace/switch", "/platform/", "/invite/")

# Minimum role per route (first match wins). Unlisted: GET -> viewer, other methods -> admin.
ROLE_RULES = [
    ("GET", r"^/profiles/[^/]+/export$", "admin"),
    ("POST", r"^/profiles/[^/]+/(consent|erase|sync)$", "admin"),
    ("POST", r"^/merges/", "analyst"),
    ("POST", r"^/candidates", "analyst"),
    ("POST", r"^/segments", "analyst"),
    ("POST", r"^/settings/workspace$", "owner"),
    ("*", r"^/settings/(members|invites|api-keys)", "admin"),
]
# Objects addressed by id must belong to the current workspace.
OWNERSHIP = [
    (r"^/profiles/([^/]+)", "profile"),
    (r"^/merges/([^/]+)", "merge_log"),
    (r"^/segments/([^/]+)", "segment"),
    (r"^/candidates/([^/]+)/(accept|reject)$", "match_candidate"),
    (r"^/flows/([^/]+)/toggle$", "flow"),
]


def _needed_role(method: str, path: str) -> str:
    for m, pattern, role in ROLE_RULES:
        if (m == "*" or m == method) and re.match(pattern, path):
            return role
    return "viewer" if method in ("GET", "HEAD") else "admin"


def _forbidden(msg: str, status: int = 403):
    return HTMLResponse(to_xml(page("Not allowed", P(msg))), status_code=status)


def _before(req, sess):
    path = req.url.path
    if path.startswith(PUBLIC):
        return
    ctx = req.scope.get("fi") or {}
    if not ctx.get("user"):
        sess.clear()
        return RedirectResponse("/login", status_code=303)
    if path.startswith("/platform/"):
        return None if ctx.get("platform_admin") else _forbidden("Platform admins only.")
    if not ctx.get("tenant_id"):
        return None if path.startswith(NO_WORKSPACE_OK) else RedirectResponse("/onboarding", status_code=303)
    if path.startswith(NO_WORKSPACE_OK):
        return
    needed = _needed_role(req.method, path)
    if not tenancy.role_at_least(ctx["role"], needed):
        return _forbidden(f"This needs the {needed} role; you are {ctx['role']}.")
    for pattern, table in OWNERSHIP:
        m = re.match(pattern, path)
        if m and m.group(1) not in ("run",):
            row = fetchone(f"select tenant_id from {table} where id=?", [m.group(1)])
            if not row or row["tenant_id"] != ctx["tenant_id"]:
                return _forbidden("Not found in this workspace.", 404)


app, rt = fast_app(
    secret_key=SECRET_KEY,
    session_cookie="fastidentity_session",
    before=Beforeware(_before, skip=[r"/collect.*", r"/v1/.*", r"/static/.*", r"/login", r"/health",
                                     r"/auth/google.*"]),
    hdrs=(Style(CSS),),
)
# Resolve user/workspace/role once per request, inside the session middleware.
app.user_middleware.append(Middleware(context.ContextMiddleware))
app.middleware_stack = None


def T(sess) -> str:
    """Current workspace (already validated against membership by the context middleware)."""
    return context.tenant()


def ME(sess) -> str:
    return context.current().get("email") or "system"


def back(url):
    return RedirectResponse(url, status_code=303)


# --- auth / health ---------------------------------------------------------------------

@rt("/health")
def get():
    return JSONResponse({"ok": True, "db": bool(fetchone("select 1 as x"))})


@rt("/login")
def get(sess):
    inv = tenancy.invite_by_token(sess.get("invite_token") or "") if sess.get("invite_token") else None
    note = f"Sign in as {inv['email']} to join the workspace." if inv else ""
    return login_page(note=note)


@rt("/login")
def post(email: str, password: str, sess):
    user = password_user(email, password)
    if not user:
        return login_page("Invalid email or password")
    return back(finish_login(user, sess, "password"))


@rt("/auth/google")
def get(request, sess):
    if not google_auth.enabled():
        return back("/login")
    state = google_auth.new_state()
    sess["google_state"] = state
    return RedirectResponse(google_auth.authorize_url(request, state), status_code=303)


@rt("/auth/google/callback")
def get(request, sess, code: str = "", state: str = "", error: str = ""):
    expected = sess.pop("google_state", None)
    if error or not code or not expected or state != expected:
        return login_page("Google sign-in was cancelled or expired. Please try again.")
    ident = google_auth.exchange(request, code)
    if not ident:
        return login_page("Google sign-in failed.")
    user = google_user(ident["email"], ident.get("name", ""))
    if not user:
        audit(None, ident["email"], "staff.login_denied", method="google")
        return login_page(f"{ident['email']} has no access yet. Ask a workspace admin for an invite.")
    return back(finish_login(user, sess, "google"))


@rt("/invite/{token}")
def get(token: str, sess):
    inv = tenancy.invite_by_token(token)
    if not inv:
        return login_page("This invite link is invalid, expired or already used.")
    ctx = context.current()
    if ctx.get("user"):
        if ctx["email"] != inv["email"]:
            return login_page(f"This invite is for {inv['email']}; you are signed in as {ctx['email']}. "
                              "Log out and sign in with the invited account.")
        return back(finish_login(ctx["user"], sess, "invite"))
    sess["invite_token"] = token
    return back("/login")


# --- workspaces & settings -----------------------------------------------------------------

@rt("/onboarding")
def get():
    return settings_ui.onboarding()


@rt("/onboarding")
def post(sess, name: str, allowed_domains: str = ""):
    ctx = context.current()
    if not name.strip():
        return settings_ui.onboarding("Please name your workspace.")
    tid = tenancy.create_workspace(name, actor=ctx["email"], owner=ctx["user"], allowed_domains=allowed_domains)
    sess["tenant_id"] = tid
    update_row("staff_user", ctx["user"]["id"], last_tenant_id=tid)
    return back("/")


@rt("/workspace/switch")
def post(sess, tenant_id: str):
    ctx = context.current()
    role = tenancy.role_in(ctx["user"], tenant_id)
    if not role:
        return _forbidden("You are not a member of that workspace.")
    sess["tenant_id"] = tenant_id
    update_row("staff_user", ctx["user"]["id"], last_tenant_id=tenant_id)
    if not any(m["tenant_id"] == tenant_id for m in ctx["member_of"]):
        audit(tenant_id, ctx["email"], "platform.workspace_opened", "tenant", tenant_id)
    return back("/")


@rt("/settings/workspace")
def get():
    return settings_ui.workspace_page()


@rt("/settings/workspace")
def post(sess, name: str, allowed_domains: str = "", join_role: str = "analyst"):
    domains = ",".join(d.strip().lower().lstrip("@") for d in allowed_domains.split(",") if d.strip())
    tenancy.update_workspace(T(sess), ME(sess), name=name.strip() or T(sess), allowed_domains=domains,
                             join_role=join_role if join_role in tenancy.ROLES[:-1] else "analyst")
    return back("/settings/workspace")


@rt("/settings/sample-data")
def post(sess):
    from seed import demo

    if scalar("select count(*) from profile where tenant_id=?", [T(sess)]):
        return settings_ui.workspace_page("Sample data can only be loaded into an empty workspace.")
    demo(T(sess))
    matching.run_matching(T(sess), actor=ME(sess))
    audit(T(sess), ME(sess), "workspace.sample_data_loaded")
    return back("/")


@rt("/settings/members")
def get():
    return settings_ui.members_page()


@rt("/settings/members/invite")
def post(sess, email: str, role: str = "analyst"):
    if role == "owner" and not context.can("owner"):
        return settings_ui.members_page("Only owners can invite owners.")
    if role not in tenancy.ROLES:
        return settings_ui.members_page("Unknown role.")
    token = tenancy.create_invite(T(sess), email, role, ME(sess))
    return settings_ui.members_page(f"Invited {email.strip().lower()} as {role}.",
                                    invite_link=f"{PUBLIC_BASE_URL}/invite/{token}")


def _member_guard(sess, mid: str, new_role: str | None = None) -> str | None:
    m = fetchone("select * from membership where id=? and tenant_id=?", [mid, T(sess)])
    if not m:
        return "No such member."
    if (m["role"] == "owner" or new_role == "owner") and not context.can("owner"):
        return "Only owners can change owners."
    return None


@rt("/settings/members/{mid}/role")
def post(sess, mid: str, role: str):
    err = _member_guard(sess, mid, role) or tenancy.set_role(T(sess), mid, role, ME(sess))
    return settings_ui.members_page(err) if err else back("/settings/members")


@rt("/settings/members/{mid}/remove")
def post(sess, mid: str):
    err = _member_guard(sess, mid) or tenancy.remove_member(T(sess), mid, ME(sess))
    return settings_ui.members_page(err) if err else back("/settings/members")


@rt("/settings/invites/{iid}/revoke")
def post(sess, iid: str):
    tenancy.revoke_invite(T(sess), iid, ME(sess))
    return back("/settings/members")


@rt("/settings/api-keys")
def get():
    return settings_ui.api_keys_page()


@rt("/settings/api-keys")
def post(sess, name: str, scope_ingest: str = "", scope_privacy: str = ""):
    scopes = [s for s, on in (("ingest", scope_ingest), ("privacy", scope_privacy)) if on]
    key = tenancy.create_api_key(T(sess), name, scopes, ME(sess))
    return settings_ui.api_keys_page("Key created.", new_key=key)


@rt("/settings/api-keys/{kid}/revoke")
def post(sess, kid: str):
    tenancy.revoke_api_key(T(sess), kid, ME(sess))
    return back("/settings/api-keys")


@rt("/platform/workspaces")
def get():
    return settings_ui.platform_page()


@rt("/platform/workspaces")
def post(sess, name: str, slug: str = "", allowed_domains: str = "", owner_email: str = ""):
    ctx = context.current()
    tid = tenancy.create_workspace(name, actor=ctx["email"], slug=slug.strip() or None,
                                   allowed_domains=allowed_domains)
    msg = f"Created workspace {tid}."
    if owner_email.strip():
        token = tenancy.create_invite(tid, owner_email, "owner", ctx["email"])
        msg += f" Owner invite link (shown once): {PUBLIC_BASE_URL}/invite/{token}"
    return settings_ui.platform_page(msg)


@rt("/logout")
def get(sess):
    sess.clear()
    return back("/login")


# --- collection (public) --------------------------------------------------------------

@app.post("/collect")
async def _collect(request):
    return await collect.collect(request)


@app.route("/collect", methods=["OPTIONS"])
async def _collect_options(request):
    return await collect.collect_preflight(request)


@app.get("/collect/px")
async def _pixel(request):
    return await collect.pixel(request)


for _t in ("identify", "track", "page", "consent"):
    def _make(mtype):
        async def handler(request):
            return await collect.server_api(request, mtype)
        return handler
    app.post(f"/v1/{_t}")(_make(_t))


@app.post("/v1/erasure")
async def _erasure(request):
    return await collect.privacy_api(request, "erase")


@app.post("/v1/export")
async def _export(request):
    return await collect.privacy_api(request, "export")


# --- identity graph ---------------------------------------------------------------------

@rt("/")
def get(sess):
    return graph.dashboard(T(sess))


@rt("/profiles")
def get(sess, q: str = "", status: str = ""):
    return graph.profile_list(T(sess), q, status)


@rt("/profiles/{pid}")
def get(sess, pid: str):
    return graph.profile_detail(T(sess), pid)


@rt("/profiles/{pid}/consent")
def post(sess, pid: str, purpose: str, granted: int):
    record_consent(T(sess), pid, purpose, bool(granted), "consent", source="admin", actor=ME(sess),
                   evidence={"note": "recorded by staff"})
    return back(f"/profiles/{pid}")


@rt("/profiles/{pid}/export")
def get(sess, pid: str):
    audit(T(sess), ME(sess), "profile.exported", "profile", pid)
    data = privacy.export(T(sess), pid)
    return JSONResponse(data, headers={"content-disposition": f'attachment; filename="dsar-{pid}.json"'})


@rt("/profiles/{pid}/erase")
def post(sess, pid: str):
    privacy.erase(T(sess), pid, actor=ME(sess))
    return back("/profiles")


@rt("/profiles/{pid}/sync")
def post(sess, pid: str, destination_id: str):
    sync_profile(T(sess), pid, destination_id, actor=ME(sess))
    return back(f"/profiles/{pid}")


@rt("/merges/{mid}/undo")
def post(sess, mid: str, req):
    unmerge(T(sess), mid, actor=ME(sess))
    return back(req.headers.get("referer") or "/profiles")


@rt("/events")
def get(sess, name: str = ""):
    return graph.event_list(T(sess), name)


@rt("/candidates")
def get(sess, status: str = "pending"):
    return graph.candidates(T(sess), status)


@rt("/candidates/run")
def post(sess):
    matching.run_matching(T(sess), actor=ME(sess))
    return back("/candidates")


@rt("/candidates/{cid}/{verdict}")
def post(sess, cid: str, verdict: str):
    matching.review(T(sess), cid, verdict == "accept", actor=ME(sess))
    return back("/candidates")


@rt("/quality")
def get(sess):
    return graph.quality(T(sess))


# --- activation ---------------------------------------------------------------------------

@rt("/segments")
def get(sess):
    return ops.segment_list(T(sess))


@rt("/segments")
async def post(sess, req):
    form = dict(await req.form())
    sid = new_id("seg_")
    insert_row("segment", id=sid, tenant_id=T(sess), name=form.get("name", "Untitled"),
               definition_json=dumps(ops.segment_from_form(form)), materialized_count=0)
    refresh_segment(sid)
    audit(T(sess), ME(sess), "segment.created", "segment", sid)
    return back("/segments")


@rt("/segments/{sid}")
def get(sess, sid: str):
    return ops.segment_detail(T(sess), sid)


@rt("/segments/{sid}/refresh")
def post(sess, sid: str):
    refresh_segment(sid)
    return back("/segments")


@rt("/flows")
def get(sess):
    return ops.flow_list(T(sess))


@rt("/flows")
def post(sess, name: str, trigger_event: str, exit_event: str, delay_minutes: int = 60,
         lookback_hours: int = 72, destination_id: str = "", template: str = ""):
    fid = new_id("f_")
    insert_row("flow", id=fid, tenant_id=T(sess), name=name, trigger_event=trigger_event.strip(),
               exit_event=exit_event.strip(), delay_minutes=delay_minutes, lookback_hours=lookback_hours,
               channel="email", destination_id=destination_id or None, template=template, enabled=1)
    audit(T(sess), ME(sess), "flow.created", "flow", fid)
    return back("/flows")


@rt("/flows/{fid}/toggle")
def post(sess, fid: str):
    execute("update flow set enabled = 1 - enabled where id=? and tenant_id=?", [fid, T(sess)])
    audit(T(sess), ME(sess), "flow.toggled", "flow", fid)
    return back("/flows")


@rt("/flows/run")
def post(sess):
    run_all(T(sess), actor=ME(sess))
    return back("/flows")


@rt("/destinations")
def get(sess):
    return ops.destination_list(T(sess))


@rt("/destinations")
def post(sess, name: str, kind: str, config_json: str = ""):
    did = new_id("d_")
    insert_row("destination", id=did, tenant_id=T(sess), name=name, kind=kind,
               config_json=config_json.strip() or "{}", enabled=1)
    audit(T(sess), ME(sess), "destination.created", "destination", did, kind=kind)
    return back("/destinations")


# --- compliance / setup ---------------------------------------------------------------------

@rt("/consent")
def get(sess):
    return ops.consent_dashboard(T(sess))


@rt("/privacy")
def get(sess):
    return ops.privacy_page(T(sess))


@rt("/privacy")
def post(sess, action: str, email: str = "", phone: str = "", user_id: str = ""):
    if action == "retention":
        r = privacy.apply_retention(T(sess), actor=ME(sess))
        return ops.privacy_page(T(sess), f"Retention applied: {r}")
    pid = privacy.lookup(T(sess), email=email or None, phone=phone or None, user_id=user_id or None)
    if not pid:
        return ops.privacy_page(T(sess), "No matching profile.")
    if action == "export":
        return back(f"/profiles/{pid}/export")
    counts = privacy.erase(T(sess), pid, actor=ME(sess))
    return ops.privacy_page(T(sess), f"Erased profile {pid}: {counts}")


@rt("/audit")
def get(sess):
    return ops.audit_page(T(sess))


@rt("/sources")
def get(sess):
    return ops.sources_page(T(sess), PUBLIC_BASE_URL)


@rt("/sources")
def post(sess, name: str, kind: str = "js", allowed_origins: str = ""):
    sid = new_id("src_")
    insert_row("source", id=sid, tenant_id=T(sess), name=name, write_key=new_write_key(), kind=kind,
               allowed_origins=allowed_origins.strip(), created_at=now_iso())
    audit(T(sess), ME(sess), "source.created", "source", sid)
    return back("/sources")


if __name__ == "__main__":
    serve(port=PORT, reload=False)
