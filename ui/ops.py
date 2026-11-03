"""Segments, flows, destinations, consent dashboard, privacy, audit, sources."""
import json

from fasthtml.common import *

import destinations as dest_mod
from db import fetchall, fetchone
from domain.activation import FLOW_TEMPLATES
from domain.audit import recent
from domain.consent import PURPOSES
from domain.segments import RULE_TYPES, evaluate
from settings import (ANON_PROFILE_RETENTION_DAYS, DATA_REGION, EVENT_RETENTION_DAYS, PUBLIC_BASE_URL,
                      REQUIRE_ANALYTICS_CONSENT)
from ui.graph import audit_table
from ui.layout import kpi, page, pill, short
from util import days_ago_iso, loads


# --- segments --------------------------------------------------------------------

def segment_list(tenant_id: str):
    rows = fetchall("select * from segment where tenant_id=? order by name", [tenant_id])
    return page(
        "Segments",
        Table(Thead(Tr(Th("Name"), Th("Rules"), Th("Members"), Th("Computed"), Th(""))),
              Tbody(*[Tr(Td(A(s["name"], href=f"/segments/{s['id']}")), Td(Small(s["definition_json"][:120])),
                         Td(s["materialized_count"]), Td(Small((s["last_computed_at"] or "never")[:16])),
                         Td(Form(Button("Refresh", cls="small outline"), method="post", action=f"/segments/{s['id']}/refresh", cls="inline")))
                      for s in rows])),
        H3("New segment"),
        P("Pick up to three rules (all must match). Leave a rule's type empty to skip it.", cls="muted"),
        Form(
            Input(name="name", placeholder="Segment name", required=True),
            *[Div(Select(Option("—", value=""), *[Option(t, value=t) for t in RULE_TYPES], name=f"type{i}"),
                  Input(name=f"a{i}", placeholder="field / key / event / purpose / status"),
                  Input(name=f"b{i}", placeholder="value (or min count for event)"),
                  Input(name=f"c{i}", placeholder="days (event only)", style="max-width:10rem"), cls="row")
              for i in range(3)],
            Button("Create"), method="post", action="/segments",
        ),
        active="segments",
    )


def segment_from_form(form: dict) -> dict:
    rules = []
    for i in range(3):
        t, a, b, c = (form.get(f"{k}{i}", "").strip() for k in ("type", "a", "b", "c"))
        if not t:
            continue
        if t == "status":
            rules.append({"type": t, "value": a or b or "known"})
        elif t == "trait":
            rules.append({"type": t, "field": a, "value": b})
        elif t == "interest":
            rules.append({"type": t, "key": a, "value": b})
        elif t == "event":
            rules.append({"type": t, "event": a, "min": int(b or 1), "days": int(c or 30)})
        elif t == "consent":
            rules.append({"type": t, "purpose": a or "marketing_email"})
    return {"all": rules}


def segment_detail(tenant_id: str, sid: str):
    s = fetchone("select * from segment where id=? and tenant_id=?", [sid, tenant_id])
    if not s:
        return page("Not found", P("No such segment."))
    members = evaluate(tenant_id, loads(s["definition_json"]))
    return page(
        s["name"], Pre(json.dumps(loads(s["definition_json"]), indent=2)), H3(f"{len(members)} members"),
        Table(Tbody(*[Tr(Td(A(m.get("email") or short(m["id"]), href=f"/profiles/{m['id']}")), Td(pill(m["status"])))
                      for m in members[:500]])),
        active="segments",
    )


# --- flows -----------------------------------------------------------------------

def flow_list(tenant_id: str):
    flows = fetchall("select * from flow where tenant_id=? order by name", [tenant_id])
    dests = fetchall("select * from destination where tenant_id=?", [tenant_id])
    dname = {d["id"]: d["name"] for d in dests}
    msgs = fetchall("select m.*, f.name as flow_name from message m left join flow f on f.id=m.flow_id "
                    "where m.tenant_id=? order by m.ts desc limit 50", [tenant_id])
    return page(
        "Flows",
        P("Abandonment flows fire for known profiles that did the trigger event and not the exit event. Every send "
          "passes consent, suppression and frequency-cap checks; skipped sends are logged with the reason.", cls="muted"),
        Form(Button("Run all flows now"), method="post", action="/flows/run"),
        Table(Thead(Tr(Th("Flow"), Th("Trigger → exit"), Th("Delay"), Th("Destination"), Th("Cap"), Th("Enabled"), Th(""))),
              Tbody(*[Tr(Td(f["name"]), Td(Code(f["trigger_event"]), " → ", Code(f["exit_event"])),
                         Td(f"{f['delay_minutes']} min / {f['lookback_hours']} h"),
                         Td(dname.get(f["destination_id"], "log (dry run)")),
                         Td(f"{f['frequency_cap'] or 'default'} / {f['frequency_window_days'] or 'default'} d"),
                         Td(pill("on", "ok") if f["enabled"] else pill("off")),
                         Td(Form(Button("Disable" if f["enabled"] else "Enable", cls="small outline"),
                                 method="post", action=f"/flows/{f['id']}/toggle", cls="inline")))
                      for f in flows])),
        H3("New flow"),
        Form(
            Div(Input(name="name", placeholder="Name", required=True),
                Input(name="trigger_event", placeholder="trigger event, e.g. booking_started", required=True),
                Input(name="exit_event", placeholder="exit event, e.g. booking_completed", required=True), cls="row"),
            Div(Input(name="delay_minutes", type="number", value="60", title="delay minutes"),
                Input(name="lookback_hours", type="number", value="72", title="lookback hours"),
                Select(Option("log (dry run)", value=""), *[Option(d["name"], value=d["id"]) for d in dests], name="destination_id"),
                Input(name="template", placeholder="ESP template / journey id"), cls="row"),
            Button("Create flow"), method="post", action="/flows",
        ),
        P("Templates: ", *[Span(Code(f"{t['name']}: {t['trigger_event']} → {t['exit_event']}"), " ") for t in FLOW_TEMPLATES], cls="muted"),
        H3("Recent messages"),
        Table(Thead(Tr(Th("When"), Th("Flow"), Th("Profile"), Th("Status"), Th("Detail"))),
              Tbody(*[Tr(Td(Small(m["ts"][:16])), Td(m["flow_name"] or ""), Td(A(short(m["profile_id"]), href=f"/profiles/{m['profile_id']}")),
                         Td(pill(m["status"], "ok" if m["status"] in ("sent", "dry_run") else "warn")), Td(Small(m["reason"][:120])))
                      for m in msgs])),
        active="flows",
    )


def destination_list(tenant_id: str):
    rows = fetchall("select * from destination where tenant_id=? order by name", [tenant_id])
    return page(
        "Destinations",
        P("Credentials default to environment variables (", Code("KLAVIYO_API_KEY"), ", ", Code("CUSTOMERIO_*"), ", ",
          Code("BRAZE_*"), ", ", Code("WEBHOOK_DEFAULT_URL"), "). Per-destination JSON config overrides them.", cls="muted"),
        Table(Thead(Tr(Th("Name"), Th("Kind"), Th("Ready"), Th("Config keys"))),
              Tbody(*[Tr(Td(d["name"]), Td(Code(d["kind"])),
                         Td(pill("ready", "ok") if not dest_mod.build(d).missing() else pill("missing " + ", ".join(dest_mod.build(d).missing()), "warn")),
                         Td(Small(", ".join(loads(d["config_json"]).keys()) or "env"))) for d in rows])),
        H3("Add destination"),
        Form(Div(Input(name="name", placeholder="Name", required=True),
                 Select(*[Option(k, value=k) for k in dest_mod.KINDS], name="kind"), cls="row"),
             Textarea(name="config_json", placeholder='{"url": "https://..."}  (optional; secrets are better in env)', rows=3),
             Button("Add"), method="post", action="/destinations"),
        active="destinations",
    )


# --- compliance --------------------------------------------------------------------

def consent_dashboard(tenant_id: str):
    latest = fetchall(
        "select purpose, granted, count(*) as n from consent_record c where tenant_id=? and ts = "
        "(select max(ts) from consent_record c2 where c2.profile_id=c.profile_id and c2.purpose=c.purpose) "
        "group by purpose, granted", [tenant_id])
    by = {(r["purpose"], r["granted"]): r["n"] for r in latest}
    drops = fetchall("select day, dropped from consent_drop where tenant_id=? and day>=? order by day desc",
                     [tenant_id, days_ago_iso(30)[:10]])
    supp = fetchall("select channel, reason, count(*) as n from suppression where tenant_id=? group by channel, reason", [tenant_id])
    records = fetchall("select * from consent_record where tenant_id=? order by ts desc limit 50", [tenant_id])
    return page(
        "Consent",
        Div(kpi("Region", DATA_REGION.upper(), "data residency"),
            kpi("Analytics gate", "on" if REQUIRE_ANALYTICS_CONSENT else "off", "events need consent"),
            kpi("Event retention", f"{EVENT_RETENTION_DAYS} d"),
            kpi("Anon retention", f"{ANON_PROFILE_RETENTION_DAYS} d"), cls="kpis"),
        H3("Current state by purpose"),
        Table(Thead(Tr(Th("Purpose"), Th("Granted"), Th("Denied / withdrawn"))),
              Tbody(*[Tr(Td(Code(p)), Td(by.get((p, 1), 0)), Td(by.get((p, 0), 0))) for p in PURPOSES])),
        Div(Div(H3("Dropped for no consent (30d)"), Table(Tbody(*[Tr(Td(d["day"]), Td(d["dropped"])) for d in drops])) if drops else P(Em("None"), cls="muted")),
            Div(H3("Suppression list"), Table(Tbody(*[Tr(Td(s["channel"]), Td(s["reason"]), Td(s["n"])) for s in supp])) if supp else P(Em("Empty"), cls="muted")),
            cls="grid"),
        H3("Latest consent records"),
        Table(Thead(Tr(Th("When"), Th("Profile"), Th("Purpose"), Th("State"), Th("Basis"), Th("Source"))),
              Tbody(*[Tr(Td(Small(r["ts"][:16])), Td(A(short(r["profile_id"]), href=f"/profiles/{r['profile_id']}")),
                         Td(Code(r["purpose"])), Td(pill("granted", "ok") if r["granted"] else pill("denied", "no")),
                         Td(Small(r["legal_basis"])), Td(Small(r["source"]))) for r in records])),
        active="consent",
    )


def privacy_page(tenant_id: str, msg=None):
    return page(
        "Privacy requests",
        P("Look up a data subject by email, phone or user id to export (Art. 15 / portability) or erase (Art. 17) "
          "their data. Erasure deletes events, identifiers, signals, consent and messages for the profile and "
          "everything merged into it, leaves a PII-free tombstone, and keeps a hashed suppression entry.", cls="muted"),
        Article(msg) if msg else None,
        Form(Div(Input(name="email", placeholder="email"), Input(name="phone", placeholder="phone"),
                 Input(name="user_id", placeholder="user id"), cls="row"),
             Div(Button("Find & export", name="action", value="export"),
                 Button("Find & erase", name="action", value="erase", style="background:#b3261e;border-color:#b3261e",
                        onclick="return confirm('Erase all data for this person?')"),
                 Button("Apply retention policy now", name="action", value="retention", cls="outline"), cls="row"),
             method="post", action="/privacy"),
        H3("API"),
        Pre(f"""curl -X POST {PUBLIC_BASE_URL}/v1/erasure \\
  -H "authorization: Bearer $ADMIN_API_TOKEN" -H "content-type: application/json" \\
  -d '{{"email":"someone@example.com"}}'

curl -X POST {PUBLIC_BASE_URL}/v1/export  ... same body ..."""),
        H3("Recent privacy actions"),
        audit_table([a for a in recent(tenant_id, 300) if a["action"].startswith(("profile.erased", "profile.exported", "privacy.", "retention."))][:30]),
        active="privacy",
    )


def audit_page(tenant_id: str):
    return page("Audit log", audit_table(recent(tenant_id, 300)), active="audit")


def sources_page(tenant_id: str, base: str):
    rows = fetchall("select * from source where tenant_id=? order by created_at", [tenant_id])
    blocks = []
    for s in rows:
        snippet = f"""<!-- Load AFTER your CMP (CookieYes / OneTrust). Nothing is sent until analytics consent. -->
<script>
  window.FastIdentityConfig = {{ key: "{s['write_key']}", host: "{base}", cmp: "auto" }};
</script>
<script async src="{base}/static/fi.js"></script>

<!-- Booking / lead form: identify with an explicit, recorded opt-in -->
<script>
  fastidentity.identify(null, {{ email: form.email.value, first_name: form.first_name.value }}, {{
    consent: {{ marketing_email: {{ granted: form.optin.checked, wording: "Email me about my enquiry and offers" }} }}
  }});
  fastidentity.track("booking_started", {{ treatment: "hip replacement", hospital: "London Bridge" }});
</script>"""
        blocks.append(Article(
            Header(B(s["name"]), " ", pill(s["kind"]), " ", Small("origins: " + (s["allowed_origins"] or "env ALLOWED_ORIGINS"), cls="muted")),
            P("Write key: ", Code(s["write_key"])),
            Pre(snippet) if s["kind"] == "js" else Pre(f"""from sdk.fastidentity import FastIdentity
fi = FastIdentity("{s['write_key']}", host="{base}")
fi.identify(user_id="u_123", traits={{"email": "a@b.com"}}, consent={{"marketing_email": True}})
fi.track("booking_completed", user_id="u_123", properties={{"treatment": "cataract"}})"""),
        ))
    return page(
        "Sources & pixel",
        P("For first-party collection, point a subdomain of the client site (e.g. ", Code("t.mymedicalgateway.com"),
          ") at this service via CNAME and use that as ", Code("host"), ".", cls="muted"),
        *blocks,
        H3("Add source"),
        Form(Div(Input(name="name", placeholder="Site name", required=True),
                 Select(Option("js (browser)", value="js"), Option("server (SDK)", value="server"), name="kind"),
                 Input(name="allowed_origins", placeholder="https://www.example.com, https://example.com"), cls="row"),
             Button("Create"), method="post", action="/sources"),
        active="sources",
    )
