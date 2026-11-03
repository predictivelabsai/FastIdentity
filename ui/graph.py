"""Dashboard, profiles, events, merge candidates, match quality."""
from fasthtml.common import *

from db import fetchall, fetchone
from domain.audit import recent
from domain.consent import PURPOSES, current_consent
from domain import households as hh
from domain.identity import get_profile, identities_of, merge_history
from domain.matching import quality_metrics
from ui.layout import kpi, kv_table, page, pct, pill, short
from util import days_ago_iso, loads


def _n(sql, args):
    return (fetchone(sql, args) or {}).get("n") or 0


def dashboard(tenant_id: str):
    m = quality_metrics(tenant_id)
    ev24 = _n("select count(*) as n from event where tenant_id=? and ts>=?", [tenant_id, days_ago_iso(1)])
    drops = _n("select coalesce(sum(dropped),0) as n from consent_drop where tenant_id=? and day>=?",
               [tenant_id, days_ago_iso(7)[:10]])
    msgs = _n("select count(*) as n from message where tenant_id=? and status in ('sent','dry_run') and ts>=?",
              [tenant_id, days_ago_iso(7)])
    email_ok = _n("select count(distinct profile_id) as n from consent_record c where tenant_id=? and purpose='marketing_email' "
                  "and granted=1 and ts=(select max(ts) from consent_record c2 where c2.profile_id=c.profile_id and c2.purpose='marketing_email')",
                  [tenant_id])
    return page(
        "Dashboard",
        Div(
            kpi("Live profiles", m["profiles"]),
            kpi("Known", m["known"], pct(m["known_rate"]) + " of profiles"),
            kpi("Email-contactable", email_ok, "marketing_email consent"),
            kpi("Events (24h)", ev24),
            kpi("Consent drops (7d)", drops, "events not collected"),
            kpi("Pending merges", m["pending"]),
            kpi("Messages (7d)", msgs),
            cls="kpis",
        ),
        H3("Recent activity"),
        audit_table(recent(tenant_id, 15)),
        active="dash",
    )


def audit_table(rows):
    if not rows:
        return P(Em("No activity yet."), cls="muted")
    return Table(
        Thead(Tr(Th("When"), Th("Actor"), Th("Action"), Th("Target"), Th("Detail"))),
        Tbody(*[Tr(Td(r["ts"][:19]), Td(r["actor"]), Td(Code(r["action"])),
                   Td(A(short(r["target_id"]), href=f"/profiles/{r['target_id']}") if r["target_type"] == "profile" else short(r["target_id"])),
                   Td(Small(r["detail_json"][:140]))) for r in rows]),
    )


def profile_list(tenant_id: str, q: str = "", status: str = ""):
    where, args = ["tenant_id=?", "merged_into is null", "erased_at is null"], [tenant_id]
    if q:
        where.append("(email like ? or phone like ? or user_id like ? or id like ?)")
        args += [f"%{q}%"] * 4
    if status:
        where.append("status=?")
        args.append(status)
    rows = fetchall(f"select * from profile where {' and '.join(where)} order by last_seen desc limit 200", args)
    return page(
        "Profiles",
        Form(Div(Input(name="q", value=q, placeholder="email, phone, user id, profile id"),
                 Select(Option("any status", value=""), *[Option(s, value=s, selected=s == status) for s in ("known", "anonymous")],
                        name="status"),
                 Button("Search"), cls="row"), method="get"),
        Table(
            Thead(Tr(Th("Profile"), Th("Status"), Th("Email"), Th("Interests"), Th("Events"), Th("Last seen"))),
            Tbody(*[Tr(
                Td(A(short(p["id"]), href=f"/profiles/{p['id']}")),
                Td(pill(p["status"], "ok" if p["status"] == "known" else "")),
                Td(p.get("email") or "—"),
                Td(Small(", ".join(f"{v}" for b in loads(p["interests_json"]).values() for v in list(b)[:2])[:80])),
                Td(_n("select count(*) as n from event where profile_id=?", [p["id"]])),
                Td(Small((p["last_seen"] or "")[:16])),
            ) for p in rows]),
        ),
        active="profiles",
    )


def _consent_block(p):
    state = current_consent(p["id"])
    rows = []
    for purpose in PURPOSES:
        s = state.get(purpose)
        badge = pill("granted", "ok") if s and s["granted"] else pill("denied", "no") if s else pill("unknown")
        rows.append(Tr(
            Td(Code(purpose)), Td(badge), Td(Small(s["legal_basis"] if s else "")), Td(Small(s["source"] if s else "")),
            Td(Small((s or {}).get("ts", "")[:16])),
            Td(Form(Input(type="hidden", name="purpose", value=purpose),
                    Input(type="hidden", name="granted", value="0" if s and s["granted"] else "1"),
                    Button("Withdraw" if s and s["granted"] else "Record opt-in", cls="small outline"),
                    method="post", action=f"/profiles/{p['id']}/consent", cls="inline")),
        ))
    return Table(Thead(Tr(Th("Purpose"), Th("State"), Th("Basis"), Th("Source"), Th("At"), Th(""))), Tbody(*rows))


def profile_detail(tenant_id: str, pid: str):
    p = get_profile(pid)
    if not p or p["tenant_id"] != tenant_id:
        return page("Not found", P("No such profile."))
    if p.get("merged_into"):
        return page("Merged profile", P("This profile was merged into ", A(p["merged_into"], href=f"/profiles/{p['merged_into']}"), "."),
                    _merges(pid), active="profiles")
    events = fetchall("select * from event where profile_id=? order by ts desc limit 200", [pid])
    sigs = fetchall("select signal_type, count(*) as n, max(last_seen) as last from signal where profile_id=? group by signal_type", [pid])
    msgs = fetchall("select * from message where profile_id=? order by ts desc limit 50", [pid])
    dests = fetchall("select * from destination where tenant_id=? and enabled=1", [tenant_id])
    title = p.get("email") or p.get("user_id") or f"Anonymous {short(pid)}"
    return page(
        title,
        P(pill(p["status"], "ok" if p["status"] == "known" else ""), " ", Code(pid), " · first seen ", (p["first_seen"] or "")[:16],
          " · last seen ", (p["last_seen"] or "")[:16], cls="muted"),
        Div(
            A("Export (DSAR)", href=f"/profiles/{pid}/export", role="button", cls="small outline"), " ",
            *[Form(Input(type="hidden", name="destination_id", value=d["id"]), Button(f"Sync to {d['name']}", cls="small outline"),
                   method="post", action=f"/profiles/{pid}/sync", cls="inline") for d in dests], " ",
            Form(Button("Erase (right to erasure)", cls="small", style="background:#b3261e;border-color:#b3261e"),
                 method="post", action=f"/profiles/{pid}/erase", cls="inline",
                 onsubmit="return confirm('Permanently erase this person\\'s data? A hashed suppression entry is kept.')"),
        ),
        Div(
            Div(H3("Identifiers"), Table(Tbody(*[Tr(Td(Code(i["id_type"])), Td(i["id_value"]), Td(Small(i["created_at"][:16])))
                                              for i in identities_of(pid)]))),
            Div(H3("Hashed signals"), Table(Tbody(*[Tr(Td(Code(s["signal_type"])), Td(f"{s['n']} value(s)"), Td(Small((s['last'] or '')[:16])))
                                                 for s in sigs])) if sigs else P(Em("None (no identity_resolution consent or no signals)"), cls="muted")),
            cls="grid",
        ),
        _devices_block(p),
        H3("Consent"), _consent_block(p),
        Div(Div(H3("Interests"), kv_table({k: ", ".join(f"{v} ×{n}" for v, n in b.items()) for k, b in loads(p["interests_json"]).items()})),
            Div(H3("Traits"), kv_table(loads(p["traits_json"]))), cls="grid"),
        H3(f"Timeline ({len(events)})"),
        Table(Thead(Tr(Th("When"), Th("Event"), Th("Properties"))),
              Tbody(*[Tr(Td(Small(e["ts"][:19])), Td(Code(e["event"])), Td(Small(e["properties_json"][:160]))) for e in events])),
        H3("Messages"),
        Table(Tbody(*[Tr(Td(Small(m["ts"][:16])), Td(pill(m["status"], "ok" if m["status"] in ("sent", "dry_run") else "warn")),
                         Td(Small(m["reason"][:100]))) for m in msgs])) if msgs else P(Em("None"), cls="muted"),
        _merges(pid),
        active="profiles",
    )


DEVICE_ICON = {"mobile": "📱", "tablet": "📲", "desktop": "🖥", "tv": "📺", "bot": "🤖"}


def device_label(d: dict) -> str:
    parts = [d.get("device_class") or "unknown", d.get("os"), d.get("browser")]
    return f"{DEVICE_ICON.get(d.get('device_class'), '❔')} " + " · ".join(x for x in parts if x)


def _devices_block(p: dict):
    devs = hh.devices_of(p["id"])
    house = hh.household_of(p["id"])
    house_line = (P("Household ", A(short(house["id"]), href=f"/households/{house['id']}"), f" — {house['members']} people, "
                    f"{house['devices']} devices", cls="muted") if house else None)
    if not devs:
        return Div(H3("Devices"), P(Em("None recorded (needs identity_resolution consent and a user agent or device id)."),
                                    cls="muted"), house_line)
    return Div(H3(f"Devices ({len(devs)})"), house_line, Table(
        Thead(Tr(Th("Device"), Th("Identified by"), Th("First seen"), Th("Last seen"), Th("Hits"), Th("Shared"))),
        Tbody(*[Tr(Td(device_label(d)),
                   Td(pill("device id", "ok") if d["key_kind"] == "device_id" else pill("fingerprint")),
                   Td(Small((d["first_seen"] or "")[:16])), Td(Small((d["last_seen"] or "")[:16])), Td(d["seen_count"]),
                   Td(pill(f"+{d['shared_with']} other profile(s)", "warn") if d["shared_with"] else Small("—", cls="muted")))
                for d in devs])))


def _who(pid):
    p = get_profile(pid) or {}
    return A(p.get("email") or p.get("user_id") or f"anon {short(pid)}", href=f"/profiles/{pid}")


def households_page(tenant_id: str, enabled: bool, can_toggle: bool):
    by_class = fetchall("select d.device_class, count(*) as n, count(distinct d.profile_id) as people from device d "
                        "join profile p on p.id=d.profile_id where d.tenant_id=? and p.merged_into is null "
                        "and p.erased_at is null group by d.device_class order by n desc", [tenant_id])
    shared = fetchall("select d.device_key, max(d.device_class) as device_class, max(d.os) as os, max(d.browser) as browser, "
                      "max(d.key_kind) as key_kind, count(distinct d.profile_id) as n from device d "
                      "join profile p on p.id=d.profile_id where d.tenant_id=? and p.merged_into is null "
                      "and p.erased_at is null group by d.device_key having count(distinct d.profile_id) > 1 "
                      "order by n desc limit 25", [tenant_id])
    multi = (fetchone("select count(*) as n from (select profile_id from device where tenant_id=? "
                      "group by profile_id having count(*) > 1) x", [tenant_id]) or {}).get("n") or 0
    houses = fetchall("select * from household where tenant_id=? order by members desc, last_seen desc limit 100",
                      [tenant_id])
    toggle = Form(Input(type="hidden", name="enabled", value="0" if enabled else "1"),
                  Button("Turn household grouping off" if enabled else "Turn household grouping on",
                         cls="small outline" if enabled else "small"),
                  method="post", action="/households/toggle", cls="inline",
                  onsubmit=None if enabled else "return confirm('Start collecting hashed household IPs for "
                                                "visitors with identity_resolution consent?')") if can_toggle else None
    return page(
        "Devices & households",
        Div(kpi("Devices", sum(r["n"] for r in by_class)), kpi("Multi-device people", multi),
            kpi("Shared devices", len(shared)), kpi("Households", len(houses)), cls="kpis"),
        H3("Devices by type"),
        Table(Thead(Tr(Th("Type"), Th("Devices"), Th("People"))),
              Tbody(*[Tr(Td(device_label({"device_class": r["device_class"]})), Td(r["n"]), Td(r["people"]))
                      for r in by_class])) if by_class else P(Em("No devices yet."), cls="muted"),
        P("Each device is keyed by an explicit device id when an app or fi.js sends one (", Code("context.device.id"),
          "), otherwise by a hashed browser fingerprint. Only the device type, OS and browser family are kept in clear.",
          cls="muted"),
        H3("Shared devices"),
        P("One device used by several profiles — a family iPad, a reception desktop. Anonymous visitors on these "
          "are never auto-merged into a known person.", cls="muted"),
        Table(Thead(Tr(Th("Device"), Th("Identified by"), Th("Profiles"), Th("Who"))),
              Tbody(*[Tr(Td(device_label(r)), Td(Code(r["key_kind"])), Td(r["n"]),
                         Td(*[Span(_who(x["profile_id"]), " ") for x in fetchall(
                             "select distinct profile_id from device where tenant_id=? and device_key=? limit 6",
                             [tenant_id, r["device_key"]])]))
                      for r in shared])) if shared else P(Em("None."), cls="muted"),
        H3("Households"),
        Div(pill("on", "ok") if enabled else pill("off"), " ", toggle, cls="row"),
        P("Profiles that repeatedly share a home IP address (IPv4, or IPv6 /64) over the last 30 days. A household groups "
          "different people; it never merges them. IPs shared by many profiles (offices, mobile carriers, VPNs) are "
          "ignored. Only the keyed hash of the IP is stored, and it expires after 30 days.", cls="muted"),
        (Table(Thead(Tr(Th("Household"), Th("People"), Th("Devices"), Th("Networks"), Th("Members"), Th("Last seen"))),
               Tbody(*[Tr(Td(A(short(h["id"]), href=f"/households/{h['id']}")), Td(h["members"]), Td(h["devices"]),
                          Td(h["networks"]),
                          Td(*[Span(_who(m["profile_id"]), " ") for m in fetchall(
                              "select profile_id from household_member where household_id=? limit 6", [h["id"]])]),
                          Td(Small((h["last_seen"] or "")[:16])))
                       for h in houses]))
         if houses else P(Em("No households yet." if enabled else "Household grouping is off for this workspace."),
                          cls="muted")),
        active="households",
    )


def household_detail(tenant_id: str, hid: str):
    h = fetchone("select * from household where id=? and tenant_id=?", [hid, tenant_id])
    if not h:
        return page("Not found", P("No such household (households are rebuilt on every worker run)."))
    people = hh.members(hid)
    return page(
        f"Household {short(hid)}",
        P(f"{h['members']} people · {h['devices']} devices · {h['networks']} home network(s) · seen ",
          (h["first_seen"] or "")[:10], " → ", (h["last_seen"] or "")[:10], cls="muted"),
        P("Members are separate people. Do not use household membership to reveal one member's activity to another "
          "(e.g. health-related messages to a shared address).", cls="muted"),
        *[Article(H4(_who(p["id"]), " ", pill(p["status"], "ok" if p["status"] == "known" else "")),
                  Ul(*[Li(device_label(d), " ", Small(f"last seen {(d['last_seen'] or '')[:10]}", cls="muted"))
                       for d in hh.devices_of(p["id"])]) or P(Em("No devices"), cls="muted"))
          for p in people],
        active="households",
    )


def _merges(pid):
    rows = merge_history(pid)
    if not rows:
        return Div(H3("Merge history"), P(Em("No merges."), cls="muted"))
    return Div(H3("Merge history"), Table(
        Thead(Tr(Th("When"), Th("Survivor"), Th("Merged in"), Th("Method"), Th("Score"), Th("By"), Th(""))),
        Tbody(*[Tr(
            Td(Small(m["ts"][:16])), Td(A(short(m["survivor_id"]), href=f"/profiles/{m['survivor_id']}")),
            Td(A(short(m["loser_id"]), href=f"/profiles/{m['loser_id']}")), Td(Code(m["method"])),
            Td(f"{m['score']:.3f}" if m["score"] is not None else "—"), Td(Small(m["actor"])),
            Td(Small(f"undone {m['undone_at'][:16]}") if m["undone_at"] else
               Form(Button("Unmerge", cls="small outline"), method="post", action=f"/merges/{m['id']}/undo", cls="inline")),
        ) for m in rows])))


def event_list(tenant_id: str, name: str = ""):
    args, where = [tenant_id], "tenant_id=?"
    if name:
        where += " and event=?"
        args.append(name)
    rows = fetchall(f"select * from event where {where} order by ts desc limit 300", args)
    names = fetchall("select event, count(*) as n from event where tenant_id=? group by event order by n desc", [tenant_id])
    return page(
        "Events",
        P(*[Span(A(f"{r['event']} ({r['n']})", href=f"/events?name={r['event']}"), " ") for r in names]),
        Table(Thead(Tr(Th("When"), Th("Event"), Th("Profile"), Th("Properties"))),
              Tbody(*[Tr(Td(Small(e["ts"][:19])), Td(Code(e["event"])),
                         Td(A(short(e["profile_id"]), href=f"/profiles/{e['profile_id']}")),
                         Td(Small(e["properties_json"][:160]))) for e in rows])),
        active="events",
    )


def candidates(tenant_id: str, status: str = "pending"):
    rows = fetchall("select * from match_candidate where tenant_id=? and status=? order by score desc limit 200", [tenant_id, status])

    def who(pid):
        p = get_profile(pid) or {}
        return Div(A(p.get("email") or f"anon {short(pid)}", href=f"/profiles/{pid}"), " ", pill(p.get("status", "?")))

    return page(
        "Merge candidates",
        P("Pairs are formed only by blocking on a shared hashed device or network signal, and only when both "
          "sides granted ", Code("identity_resolution"), ". Anonymous→known pairs above the auto-merge threshold merge "
          "automatically; everything else waits here.", cls="muted"),
        Div(Form(Button("Run matching now"), method="post", action="/candidates/run", cls="inline"), " ",
            *[A(s, href=f"/candidates?status={s}", role="button", cls="small outline" if s != status else "small")
              for s in ("pending", "accepted", "rejected", "auto_merged", "superseded")], cls="row"),
        Table(
            Thead(Tr(Th("Score"), Th("Left"), Th("Right"), Th("Evidence"), Th("Engine"), Th(""))),
            Tbody(*[Tr(
                Td(B(f"{c['score']:.3f}")), Td(who(c["left_id"])), Td(who(c["right_id"])),
                Td(*[Span(pill("same household", "warn") if k == "household" else pill(k, "ok" if v["agree"] else "no"), " ") for k, v in loads(c["features_json"]).items()]),
                Td(Small(c["engine"])),
                Td(Form(Button("Merge", cls="small"), method="post", action=f"/candidates/{c['id']}/accept", cls="inline"), " ",
                   Form(Button("Reject", cls="small outline"), method="post", action=f"/candidates/{c['id']}/reject", cls="inline"))
                if c["status"] == "pending" else Td(Small(c["reviewed_by"] or "")),
            ) for c in rows]),
        ) if rows else P(Em(f"No {status} candidates."), cls="muted"),
        active="candidates",
    )


def quality(tenant_id: str):
    m = quality_metrics(tenant_id)
    return page(
        "Match quality",
        Div(kpi("Known rate", pct(m["known_rate"]), f"{m['known']} / {m['profiles']}"),
            kpi("Pending review", m["pending"]),
            kpi("Reviewer precision", pct(m["review_precision"]), f"{m['accepted']} accepted · {m['rejected']} rejected"),
            kpi("Auto-merges", m["auto_merges"], f"{m['auto_merges_undone']} undone"),
            kpi("Auto precision (proxy)", pct(m["auto_precision_proxy"]), "1 − undone/auto"),
            cls="kpis"),
        Div(Div(H3("Candidates by score"), Table(Tbody(*[Tr(Td(b["bucket"]), Td(b["n"])) for b in m["score_buckets"]]))),
            Div(H3("Active merges by method"), Table(Tbody(*[Tr(Td(Code(b["method"])), Td(b["n"])) for b in m["merges_by_method"]]))),
            cls="grid"),
        P("Reviewer precision is the share of human-reviewed candidates accepted; the auto proxy treats an unmerge "
          "as a false positive. Use both to tune ", Code("AUTO_MERGE_THRESHOLD"), " / ", Code("REVIEW_THRESHOLD"), ".", cls="muted"),
        active="quality",
    )
