"""SaaS settings pages: onboarding, workspace, members & invites, API keys, platform."""
from fasthtml.common import *

from db import scalar
from domain import tenancy
from settings import PUBLIC_BASE_URL
from ui import context
from ui.layout import page, pill


def onboarding(msg: str = ""):
    ctx = context.current()
    return Title("Create a workspace · FastIdentity"), Main(
        Article(
            H2("Create your workspace"),
            P("A workspace holds one company's identity graph, consent ledger, keys and team. "
              "You become its owner and can invite colleagues.", cls="muted"),
            P(msg, style="color:#b00") if msg else None,
            Form(
                Label("Company or site name", Input(name="name", required=True, placeholder="e.g. MyMedicalGateway")),
                Label("Company email domain (optional)",
                      Input(name="allowed_domains", placeholder="e.g. mymedicalgateway.com"),
                      Small("Colleagues who sign in with Google from this domain join automatically as analysts.")),
                Button("Create workspace", type="submit"),
                method="post", action="/onboarding",
            ),
            P(Small(f"Signed in as {ctx.get('email', '')}. Waiting for an invite instead? Ask a workspace admin to "
                    "invite this email, then sign in again. ", A("Log out", href="/logout")), cls="muted"),
        ),
        cls="container", style="max-width:34rem;margin-top:3rem",
    )


def workspace_page(msg: str = ""):
    ctx = context.current()
    ws = ctx["workspace"]
    editable = context.can("owner")
    profiles = scalar("select count(*) from profile where tenant_id=? and merged_into is null", [ws["id"]]) or 0
    sample = (Form(Button("Load sample data", cls="secondary"), method="post", action="/settings/sample-data",
                   onsubmit="return confirm('Load synthetic demo visitors into this workspace?')")
              if context.can("admin") and profiles == 0 else None)
    return page(
        "Workspace",
        P(msg, cls="muted") if msg else None,
        Table(Tbody(
            Tr(Th("Workspace ID"), Td(Code(ws["id"]))),
            Tr(Th("Plan"), Td(pill(ws.get("plan") or "free"))),
            Tr(Th("Created"), Td(ws.get("created_at") or "—")),
            Tr(Th("Your role"), Td(pill(ctx["role"]), " ", Small(tenancy.ROLE_HELP.get(ctx["role"], ""), cls="muted"))),
            Tr(Th("Live profiles"), Td(str(profiles))),
        )),
        Form(
            Label("Name", Input(name="name", value=ws["name"], disabled=not editable)),
            Label("Auto-join email domains",
                  Input(name="allowed_domains", value=ws.get("allowed_domains") or "", disabled=not editable,
                        placeholder="example.com, example.co.uk"),
                  Small("Google sign-ins from these domains join this workspace automatically.")),
            Label("Role for auto-joined users",
                  Select(*[Option(r, selected=r == (ws.get("join_role") or "analyst")) for r in tenancy.ROLES[:-1]],
                         name="join_role", disabled=not editable)),
            Button("Save", type="submit") if editable else P(Small("Only owners can change these settings."), cls="muted"),
            method="post", action="/settings/workspace",
        ),
        H3("Sample data") if sample else None,
        P("Populate an empty workspace with synthetic visitors to explore the product.", cls="muted") if sample else None,
        sample,
        active="ws",
    )


def members_page(msg: str = "", invite_link: str = ""):
    tid = context.tenant()
    is_owner = context.can("owner")
    rows = []
    for m in tenancy.members(tid):
        can_edit = is_owner or m["role"] != "owner"
        role_form = Form(
            Select(*[Option(r, selected=r == m["role"]) for r in tenancy.ROLES if is_owner or r != "owner"],
                   name="role", onchange="this.form.submit()", style="margin:0;padding:.2rem .5rem;height:auto"),
            method="post", action=f"/settings/members/{m['id']}/role", cls="inline") if can_edit else pill(m["role"])
        remove = Form(Button("Remove", cls="small outline secondary"), method="post",
                      action=f"/settings/members/{m['id']}/remove", cls="inline",
                      onsubmit="return confirm('Remove this member?')") if can_edit else ""
        rows.append(Tr(Td(m["email"]), Td(role_form), Td(Small(m["created_at"] or "")), Td(remove)))
    invites = [Tr(Td(i["email"]), Td(pill(i["role"])), Td(Small(i["created_at"])), Td(Small(i["invited_by"] or "")),
                  Td(Form(Button("Revoke", cls="small outline secondary"), method="post",
                          action=f"/settings/invites/{i['id']}/revoke", cls="inline")))
               for i in tenancy.pending_invites(tid)]
    return page(
        "Members",
        P(msg, cls="muted") if msg else None,
        (Article(P(B("Invite link — share it with the invitee (shown once):")), Div(invite_link, cls="secret"),
                 Small("They sign in with Google using the invited email and land in this workspace."))
         if invite_link else None),
        Table(Thead(Tr(Th("Email"), Th("Role"), Th("Joined"), Th(""))), Tbody(*rows)),
        H3("Invite a teammate"),
        Form(Div(Input(name="email", type="email", placeholder="name@company.com", required=True),
                 Select(*[Option(r, selected=r == "analyst") for r in tenancy.ROLES if is_owner or r != "owner"],
                        name="role"),
                 Button("Invite"), cls="row"),
             method="post", action="/settings/members/invite"),
        Ul(*[Li(B(r), " — ", tenancy.ROLE_HELP[r]) for r in reversed(tenancy.ROLES)], cls="muted"),
        H3("Pending invites") if invites else None,
        Table(Thead(Tr(Th("Email"), Th("Role"), Th("Sent"), Th("By"), Th(""))), Tbody(*invites)) if invites else None,
        active="members",
    )


def api_keys_page(msg: str = "", new_key: str = ""):
    tid = context.tenant()
    rows = [Tr(Td(k["name"]), Td(Code(k["prefix"] + "…")), Td(", ".join((k["scopes"] or "").split(","))),
               Td(Small(k["created_by"] or "")), Td(Small(k["created_at"] or "")), Td(Small(k["last_used_at"] or "never")),
               Td(pill("revoked", "no") if k["revoked_at"] else
                  Form(Button("Revoke", cls="small outline secondary"), method="post",
                       action=f"/settings/api-keys/{k['id']}/revoke", cls="inline",
                       onsubmit="return confirm('Revoke this key? Calls using it will start failing.')")))
            for k in tenancy.api_keys(tid)]
    created = Article(
        P(B("Your new secret key — copy it now. It will not be shown again.")),
        Div(new_key, cls="secret"),
        Pre(Code(f"FASTIDENTITY_URL={PUBLIC_BASE_URL}\nFASTIDENTITY_SECRET_KEY={new_key}")),
        Pre(Code(f"curl -X POST {PUBLIC_BASE_URL}/v1/identify \\\n"
                 f"  -H 'Authorization: Bearer {new_key[:14]}…' -H 'Content-Type: application/json' \\\n"
                 "  -d '{\"userId\": \"12345\", \"traits\": {\"email\": \"patient@example.com\"}}'")),
    ) if new_key else None
    return page(
        "API keys",
        P(msg, cls="muted") if msg else None,
        created,
        P("Secret keys authenticate your backend to this workspace (",
          Code("Authorization: Bearer fi_sk_…"), "). Use them for server-side ", Code("/v1/identify"), ", ",
          Code("/v1/track"), " and the privacy API (", Code("/v1/erasure"), ", ", Code("/v1/export"),
          "). Never put a secret key in a browser — the pixel uses the publishable write key on ",
          A("Sources & pixel", href="/sources"), ".", cls="muted"),
        Table(Thead(Tr(Th("Name"), Th("Key"), Th("Scopes"), Th("Created by"), Th("Created"), Th("Last used"), Th(""))),
              Tbody(*rows)) if rows else P(Em("No keys yet."), cls="muted"),
        H3("Create a secret key"),
        Form(Div(Input(name="name", placeholder="e.g. MMG production backend", required=True),
                 Label(Input(type="checkbox", name="scope_ingest", checked=True), "ingest (identify / track)"),
                 Label(Input(type="checkbox", name="scope_privacy", checked=True), "privacy (erasure / export)"),
                 Button("Create key"), cls="row"),
             method="post", action="/settings/api-keys"),
        active="keys",
    )


def platform_page(msg: str = ""):
    rows = [Tr(Td(B(w["name"]), Br(), Small(Code(w["id"]))), Td(w.get("allowed_domains") or "—"),
               Td(str(w["members"])), Td(str(w["profiles"])), Td(Small(w.get("created_at") or "")),
               Td(Form(Input(type="hidden", name="tenant_id", value=w["id"]), Button("Open", cls="small"),
                       method="post", action="/workspace/switch", cls="inline")))
            for w in tenancy.all_workspaces()]
    return page(
        "All workspaces",
        P(msg, cls="muted") if msg else None,
        P("Platform admins can open any workspace; each switch is written to that workspace's audit log.", cls="muted"),
        Table(Thead(Tr(Th("Workspace"), Th("Auto-join domains"), Th("Members"), Th("Profiles"), Th("Created"), Th(""))),
              Tbody(*rows)),
        H3("Create a customer workspace"),
        Form(Div(Input(name="name", placeholder="Customer name", required=True),
                 Input(name="slug", placeholder="workspace id (optional)"),
                 Input(name="allowed_domains", placeholder="customer.com (auto-join)"),
                 Input(name="owner_email", type="email", placeholder="owner email (invited)"),
                 Button("Create"), cls="row"),
             method="post", action="/platform/workspaces"),
        active="platform",
    )
