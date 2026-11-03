"""Bootstrap (tenant, admin, sources, flows) and optional synthetic demo traffic.

    python seed.py          # bootstrap only (idempotent)
    python seed.py --demo   # + synthetic visitors run through the real pipeline

Demo data is entirely synthetic (example.com addresses, made-up people).
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone

from db import fetchone, insert_row
from domain.activation import FLOW_TEMPLATES
from settings import ADMIN_EMAIL, ADMIN_PASSWORD, DEFAULT_TENANT
from util import dumps, hash_password, new_id, new_write_key, now_iso


def bootstrap(tenant_id: str = DEFAULT_TENANT):
    """First boot: the demo workspace, the platform admin login, legacy migration."""
    from domain.tenancy import add_member, create_user, get_user, migrate_legacy_users

    if not fetchone("select id from tenant where id=?", [tenant_id]):
        insert_row("tenant", id=tenant_id, name=tenant_id.title(), created_at=now_iso(), plan="free",
                   join_role="analyst", created_by=ADMIN_EMAIL.lower())
    if not get_user(ADMIN_EMAIL):
        admin = create_user(ADMIN_EMAIL, "Platform admin", hash_password(ADMIN_PASSWORD))
        add_member(tenant_id, admin, "owner", actor="bootstrap")
    migrate_legacy_users()
    bootstrap_workspace(tenant_id)


def bootstrap_workspace(tenant_id: str):
    """Starter kit for a new workspace: sources + keys, dry-run destination, flow templates, segments."""
    if not fetchone("select id from source where tenant_id=?", [tenant_id]):
        insert_row("source", id=new_id("src_"), tenant_id=tenant_id, name="Website", write_key=new_write_key(),
                   kind="js", allowed_origins="", created_at=now_iso())
        insert_row("source", id=new_id("src_"), tenant_id=tenant_id, name="Backend", write_key=new_write_key(),
                   kind="server", allowed_origins="", created_at=now_iso())
    if not fetchone("select id from destination where tenant_id=?", [tenant_id]):
        insert_row("destination", id=new_id("d_"), tenant_id=tenant_id, name="Dry run log", kind="log",
                   config_json="{}", enabled=1)
    if not fetchone("select id from flow where tenant_id=?", [tenant_id]):
        dest = fetchone("select id from destination where tenant_id=? and kind='log'", [tenant_id])
        for t in FLOW_TEMPLATES:
            insert_row("flow", id=new_id("f_"), tenant_id=tenant_id, channel="email",
                       destination_id=dest["id"] if dest else None, template="", enabled=1, **t)
    if not fetchone("select id from segment where tenant_id=?", [tenant_id]):
        for name, rules in [
            ("Contactable", [{"type": "status", "value": "known"}, {"type": "consent", "purpose": "marketing_email"}]),
            ("Orthopaedic intent (30d)", [{"type": "interest", "key": "treatment", "value": "hip"}]),
            ("Booking starters (7d)", [{"type": "event", "event": "booking_started", "min": 1, "days": 7}]),
        ]:
            insert_row("segment", id=new_id("seg_"), tenant_id=tenant_id, name=name,
                       definition_json=dumps({"all": rules}), materialized_count=0)


TREATMENTS = ["hip replacement", "knee replacement", "cataract surgery", "MRI scan", "hernia repair",
              "IVF consultation", "dental implants", "physiotherapy"]
HOSPITALS = ["London Bridge", "Spire Manchester", "Nuffield Leeds", "Circle Bath", "HCA Wellington"]
FIRST = ["Ada", "Ben", "Chloe", "Dev", "Ella", "Finn", "Grace", "Hari", "Isla", "Jack", "Kara", "Liam",
         "Maya", "Noah", "Orla", "Priya", "Quinn", "Ravi", "Sara", "Tom", "Uma", "Vik", "Wren", "Yara", "Zac"]
UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Version/17.5 Mobile Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/128.0 Mobile Safari/537.36",
]
SCREENS = [(1512, 982, 2), (1920, 1080, 1), (390, 844, 3), (412, 915, 2.6), (1440, 900, 2)]


def demo(tenant_id: str = DEFAULT_TENANT, people: int = 25, seed: int = 7):
    from collect import process

    rnd = random.Random(seed)
    web = fetchone("select * from source where tenant_id=? and kind='js'", [tenant_id])
    now = datetime.now(timezone.utc)

    def ts(hours_ago):
        return (now - timedelta(hours=hours_ago)).isoformat()

    def ctx(dev, cmp):
        ua, scr = dev
        return {"consent": cmp, "userAgent": ua, "screen": {"width": scr[0], "height": scr[1], "density": scr[2]},
                "timezone": "Europe/London", "locale": "en-GB", "page": {"path": "/"}}

    for i in range(people):
        name = FIRST[i % len(FIRST)]
        email = f"{name.lower()}.{i}@example.com"
        dev = (rnd.choice(UAS), rnd.choice(SCREENS))
        ip = f"86.{rnd.randint(1, 250)}.{rnd.randint(1, 250)}.{rnd.randint(2, 250)}"
        roll = rnd.random()
        cmp = ({"analytics": True, "marketing": True, "functional": True} if roll < 0.6
               else {"analytics": True, "marketing": False} if roll < 0.85
               else {"analytics": False, "marketing": False})
        aid = f"anon-{i}-a"
        treatment, hospital = rnd.choice(TREATMENTS), rnd.choice(HOSPITALS)
        start = rnd.uniform(20, 70)
        c = ctx(dev, cmp)
        process(web, {"type": "page", "anonymousId": aid, "timestamp": ts(start), "context": c,
                      "properties": {"path": "/treatments"}}, ip)
        for k in range(rnd.randint(1, 4)):
            process(web, {"type": "track", "event": "product_viewed", "anonymousId": aid, "timestamp": ts(start - 0.1 * k),
                          "context": c, "properties": {"treatment": treatment, "hospital": hospital}}, ip)
        if rnd.random() < 0.6:
            process(web, {"type": "track", "event": "form_started", "anonymousId": aid, "timestamp": ts(start - 0.5),
                          "context": c, "properties": {"form": "enquiry"}}, ip)
            optin = rnd.random() < 0.7
            process(web, {"type": "identify", "anonymousId": aid, "timestamp": ts(start - 0.6), "context": c,
                          "traits": {"email": email, "first_name": name},
                          "consent": {"marketing_email": {"granted": optin, "wording": "Email me about my enquiry and offers"}}}, ip)
            process(web, {"type": "track", "event": "booking_started", "anonymousId": aid, "timestamp": ts(start - 0.7),
                          "context": c, "properties": {"treatment": treatment, "hospital": hospital}}, ip)
            if rnd.random() < 0.35:
                process(web, {"type": "track", "event": "booking_completed", "anonymousId": aid, "timestamp": ts(start - 1),
                              "context": c, "properties": {"treatment": treatment, "hospital": hospital, "value": 450}}, ip)
            # Returns later with cleared cookies on the same device: probabilistic match.
            if rnd.random() < 0.5:
                aid2 = f"anon-{i}-b"
                later = rnd.uniform(2, max(3, start - 2))
                # Same home network -> auto-merge; on mobile data -> human review.
                ip2 = ip if rnd.random() < 0.6 else f"92.{rnd.randint(1, 250)}.{rnd.randint(1, 250)}.{rnd.randint(2, 250)}"
                for t in (treatment, rnd.choice(TREATMENTS)):
                    process(web, {"type": "track", "event": "product_viewed", "anonymousId": aid2, "timestamp": ts(later),
                                  "context": c, "properties": {"treatment": t, "hospital": hospital}}, ip2)
            # A household member on the same network, different device: review candidate at most.
            if rnd.random() < 0.25:
                other = (rnd.choice(UAS), rnd.choice(SCREENS))
                process(web, {"type": "track", "event": "product_viewed", "anonymousId": f"anon-{i}-hh",
                              "timestamp": ts(rnd.uniform(2, 10)), "context": ctx(other, cmp),
                              "properties": {"treatment": rnd.choice(TREATMENTS)}}, ip)


if __name__ == "__main__":
    bootstrap()
    if "--demo" in sys.argv:
        demo()
        from domain.matching import run_matching
        print("matching:", run_matching(DEFAULT_TENANT, actor="seed"))
        from domain.segments import refresh
        from db import fetchall
        for s in fetchall("select id from segment where tenant_id=?", [DEFAULT_TENANT]):
            refresh(s["id"])
    print("ok")
