from fasthtml.common import *

from settings import DATA_REGION
from ui import context

NAV = [
    ("Overview", [("/", "Dashboard", "dash")]),
    ("Identity graph", [("/profiles", "Profiles", "profiles"), ("/events", "Events", "events"),
                        ("/households", "Devices & households", "households"),
                        ("/candidates", "Merge candidates", "candidates"), ("/quality", "Match quality", "quality")]),
    ("Activation", [("/segments", "Segments", "segments"), ("/flows", "Flows", "flows"),
                    ("/destinations", "Destinations", "destinations")]),
    ("Compliance", [("/consent", "Consent", "consent"), ("/privacy", "Privacy requests", "privacy"),
                    ("/audit", "Audit log", "audit")]),
    ("Setup", [("/sources", "Sources & pixel", "sources")]),
]
# (href, label, key, minimum role)
SETTINGS_NAV = [("/settings/workspace", "Workspace", "ws", "viewer"), ("/settings/members", "Members", "members", "admin"),
                ("/settings/api-keys", "API keys", "keys", "admin")]

CSS = """
:root { --pico-font-size: 15px; }
body { margin:0; }
.shell { display:grid; grid-template-columns: 220px 1fr; min-height:100vh; }
.side { background: var(--pico-card-background-color); border-right: 1px solid var(--pico-muted-border-color); padding: 1rem; }
.side h2 { font-size: 1.15rem; margin-bottom: .25rem; }
.side .group { font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; color: var(--pico-muted-color); margin: 1rem 0 .25rem; }
.side a { display:block; padding: .25rem .5rem; border-radius: 6px; text-decoration:none; font-size:.92rem; }
.side a.on { background: var(--pico-primary-background); color: var(--pico-primary-inverse); }
.content { padding: 1.25rem 2rem; max-width: 1200px; overflow-x:auto; }
.kpis { display:grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr)); gap: .75rem; margin-bottom: 1.25rem; }
.kpi { border:1px solid var(--pico-muted-border-color); border-radius: 10px; padding: .75rem 1rem; }
.kpi b { display:block; font-size: 1.5rem; }
.kpi small { color: var(--pico-muted-color); }
.muted { color: var(--pico-muted-color); }
.pill { display:inline-block; padding: 0 .5rem; border-radius: 999px; font-size: .78rem; border:1px solid var(--pico-muted-border-color); }
.pill.ok { background:#e6f4ea; color:#1e6b34; border-color:#b7dfc2; }
.pill.no { background:#fdecea; color:#8a1c12; border-color:#f3c1bb; }
.pill.warn { background:#fff4e0; color:#7a4b00; border-color:#f1d29a; }
table { font-size: .88rem; }
td code, pre { font-size: .8rem; }
pre { max-height: 22rem; overflow:auto; }
.row { display:flex; gap: .5rem; align-items: end; flex-wrap: wrap; }
.row > * { margin-bottom: 0; }
form.inline { display:inline; }
.ws-name { font-weight: 600; font-size: .95rem; }
.ws-switch select { font-size: .82rem; padding: .25rem .5rem; margin: .35rem 0 0; height: auto; }
.secret { font-family: monospace; word-break: break-all; padding: .6rem .8rem; border-radius: 8px; border: 1px dashed var(--pico-primary); }
button.small, a.small[role=button] { padding: .2rem .6rem; font-size: .8rem; }
@media (max-width: 760px) { .shell { grid-template-columns: 1fr; } .side { border-right:0; } .content { padding: 1rem; } }
"""


def _switcher(ctx):
    opts = {m["tenant_id"]: m["name"] for m in ctx.get("member_of", [])}
    if ctx.get("tenant_id") and ctx["tenant_id"] not in opts and ctx.get("workspace"):
        opts[ctx["tenant_id"]] = ctx["workspace"]["name"] + " (platform)"
    if len(opts) < 2:
        return None
    return Form(Select(*[Option(name, value=tid, selected=tid == ctx.get("tenant_id")) for tid, name in opts.items()],
                       name="tenant_id", onchange="this.form.submit()", aria_label="Switch workspace"),
                method="post", action="/workspace/switch", cls="ws-switch")


def sidebar(active: str):
    ctx = context.current()
    ws = ctx.get("workspace") or {}
    items = [H2(A("FastIdentity", href="/", style="padding:0")),
             Div(ws.get("name") or "No workspace", cls="ws-name"),
             Small(f"{ws.get('id', '—')} · region {DATA_REGION.upper()}", cls="muted"),
             _switcher(ctx)]
    if ws:
        for group, links in NAV:
            items.append(Div(group, cls="group"))
            for href, label, key in links:
                items.append(A(label, href=href, cls="on" if key == active else ""))
        items.append(Div("Settings", cls="group"))
        for href, label, key, need in SETTINGS_NAV:
            if context.can(need):
                items.append(A(label, href=href, cls="on" if key == active else ""))
    if ctx.get("platform_admin"):
        items.append(Div("Platform", cls="group"))
        items.append(A("All workspaces", href="/platform/workspaces", cls="on" if active == "platform" else ""))
    items.append(Div("Account", cls="group"))
    if ctx.get("email"):
        items.append(Small(f"{ctx['email']} · {ctx.get('role') or 'no role'}", cls="muted",
                           style="display:block;padding:0 .5rem;word-break:break-all"))
    items.append(A("New workspace", href="/onboarding"))
    items.append(A("Log out", href="/logout"))
    return Aside(*items, cls="side")


def page(title: str, *body, active: str = ""):
    return Title(f"{title} · FastIdentity"), Div(
        sidebar(active),
        Main(H1(title), *body, cls="content"),
        cls="shell",
    )


def kpi(label, value, hint=""):
    return Div(Small(label), B(value), Small(hint) if hint else None, cls="kpi")


def pill(text, kind=""):
    return Span(text, cls=f"pill {kind}")


def kv_table(data: dict):
    if not data:
        return P(Em("none"), cls="muted")
    return Table(Tbody(*[Tr(Th(str(k)), Td(str(v))) for k, v in data.items()]))


def pct(x):
    return "—" if x is None else f"{x*100:.1f}%"


def short(pid: str | None):
    return (pid or "")[:12]
