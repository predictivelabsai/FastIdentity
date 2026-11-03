"""FastIdentity — first-party, consent-first identity resolution (FastHTML)."""
from fasthtml.common import *
from starlette.responses import JSONResponse

import collect
from db import execute, fetchone, insert_row, update_row
from domain import matching, privacy
from domain.activation import run_all, sync_profile
from domain.audit import audit
from domain.consent import record_consent
from domain.identity import unmerge
from domain.segments import refresh as refresh_segment
from seed import bootstrap
from settings import DEFAULT_TENANT, PORT, PUBLIC_BASE_URL, SECRET_KEY
from ui import graph, ops
from ui.auth import login_page, try_login
from ui.layout import CSS
from util import dumps, new_id, new_write_key, now_iso

bootstrap()

PUBLIC = ("/collect", "/v1/", "/static/", "/login", "/health")


def _before(req, sess):
    if req.url.path.startswith(PUBLIC):
        return
    if not sess.get("auth"):
        return RedirectResponse("/login", status_code=303)


app, rt = fast_app(
    secret_key=SECRET_KEY,
    session_cookie="fastidentity_session",
    before=Beforeware(_before, skip=[r"/collect.*", r"/v1/.*", r"/static/.*", r"/login", r"/health"]),
    hdrs=(Style(CSS),),
)


def T(sess) -> str:
    return sess.get("tenant_id") or DEFAULT_TENANT


def ME(sess) -> str:
    return sess.get("auth") or "admin"


def back(url):
    return RedirectResponse(url, status_code=303)


# --- auth / health ---------------------------------------------------------------------

@rt("/health")
def get():
    return JSONResponse({"ok": True, "db": bool(fetchone("select 1 as x"))})


@rt("/login")
def get():
    return login_page()


@rt("/login")
def post(email: str, password: str, sess):
    if try_login(email, password, sess):
        audit(T(sess), email, "staff.login")
        return back("/")
    return login_page("Invalid email or password")


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
