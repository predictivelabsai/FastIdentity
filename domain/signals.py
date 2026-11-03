"""Hashed device / network signals for probabilistic matching.

Raw values (user agent, IP, screen…) are never stored: each signal is an HMAC
with ``IDENTITY_HASH_SECRET``. Signals are only recorded when the visitor has
granted ``identity_resolution`` (fingerprinting falls under PECR reg. 6 just
like cookies).
"""
from __future__ import annotations

import ipaddress
import re

from db import execute, fetchone, insert_row
from util import days_ago_iso, keyed_hash, new_id, now_iso, to_iso

SIGNAL_TYPES = ["device", "device_id", "net", "hh_ip", "ua_family", "tz", "lang"]
# device     fingerprint (UA + screen + tz + locale + platform)
# device_id  explicit id from an app/SDK (IDFV, Android app-set id, fi.js first-party id)
# net        /24 (IPv4) or /48 (IPv6) network — person matching
# hh_ip      full IPv4 or IPv6 /64 — household grouping only, never person matching


def client_ip(headers, fallback: str | None) -> str | None:
    fwd = headers.get("x-forwarded-for") or headers.get("cf-connecting-ip") or ""
    ip = fwd.split(",")[0].strip() or (fallback or "")
    return ip or None


def network_prefix(ip: str | None) -> str | None:
    """IPv4 /24 or IPv6 /48 — coarse enough to not identify a household alone."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.is_loopback or addr.is_private:
        return None
    bits = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{ip}/{bits}", strict=False))


def household_ip(ip: str | None) -> str | None:
    """The address a home router presents: IPv4 as-is, IPv6 /64 (one subnet per home)."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_multicast:
        return None
    if addr.version == 4:
        return str(addr)
    return str(ipaddress.ip_network(f"{ip}/64", strict=False))


def describe_device(ua: str | None, screen=None) -> dict:
    """Coarse, non-identifying descriptors for display and segmentation."""
    ua = ua or ""
    low = ua.lower()
    if any(b in low for b in ("bot", "crawler", "spider", "headless", "lighthouse")):
        cls = "bot"
    elif "ipad" in low or "tablet" in low or ("android" in low and "mobile" not in low and "mozilla" in low):
        cls = "tablet"
    elif "mobi" in low or "iphone" in low or "android" in low:
        cls = "mobile"
    elif "smart-tv" in low or "smarttv" in low or "tizen" in low or "web0s" in low:
        cls = "tv"
    elif ua:
        cls = "desktop"
    else:
        cls = "unknown"
    # iPadOS reports a Mac UA; a touch-sized screen gives it away.
    if cls == "desktop" and "macintosh" in low and isinstance(screen, dict) and screen.get("width") in (768, 810, 820, 834, 1024):
        cls = "tablet"
    os_name = next((name for token, name in (("iphone", "iOS"), ("ipad", "iPadOS"), ("android", "Android"),
                                             ("cros", "ChromeOS"), ("windows", "Windows"), ("mac os x", "macOS"),
                                             ("linux", "Linux")) if token in low), "")
    browser = next((name for token, name in (("edg/", "Edge"), ("opr/", "Opera"), ("samsungbrowser", "Samsung Internet"),
                                             ("firefox", "Firefox"), ("fxios", "Firefox"), ("crios", "Chrome"),
                                             ("chrome", "Chrome"), ("safari", "Safari")) if token in low), "")
    if not browser and ua and ("okhttp" in low or "cfnetwork" in low or "dalvik" in low):
        browser = "App"
    return {"device_class": cls, "os": os_name, "browser": browser}


def explicit_device_id(context: dict | None, msg: dict | None = None) -> str | None:
    """``context.device.id`` (Segment spec), ``context.device.advertisingId`` is ignored on purpose."""
    dev = (context or {}).get("device") or {}
    raw = dev.get("id") or (msg or {}).get("deviceId")
    raw = str(raw).strip() if raw not in (None, "") else ""
    # Reject obvious placeholders (all-zero IDFA/IDFV when tracking is limited).
    if not raw or set(raw.replace("-", "")) <= {"0"} or len(raw) < 8:
        return None
    return raw


def ua_family(ua: str | None) -> str | None:
    if not ua:
        return None
    browser = next((b for b in ("Edg", "OPR", "Firefox", "Chrome", "Safari") if b in ua), "Other")
    os_name = next((o for o in ("Android", "iPhone", "iPad", "Windows", "Mac OS X", "Linux") if o in ua), "Other")
    return f"{browser}/{os_name}"


def extract(context: dict, ip: str | None, device_id: str | None = None,
            households: bool = False) -> dict[str, str]:
    """Return {signal_type: hash} for whatever the context provides.

    ``hh_ip`` is only produced when the workspace has household grouping on.
    """
    ctx = context or {}
    device = ctx.get("device") or {}
    ua = ctx.get("userAgent") or device.get("userAgent")
    screen = ctx.get("screen") or device.get("screen")
    if isinstance(screen, dict):
        screen = f"{screen.get('width')}x{screen.get('height')}x{screen.get('density', '')}"
    tz = ctx.get("timezone")
    lang = ctx.get("locale") or ctx.get("language")
    out: dict[str, str] = {}
    if ua and screen:
        out["device"] = keyed_hash("|".join(str(x or "") for x in (ua, screen, tz, lang, device.get("platform"))), "device")
    if device_id:
        out["device_id"] = keyed_hash(device_id, "device_id")
    net = network_prefix(ip)
    if net:
        out["net"] = keyed_hash(net, "net")
    hh = household_ip(ip) if households else None
    if hh:
        out["hh_ip"] = keyed_hash(hh, "hh_ip")
    fam = ua_family(ua)
    if fam:
        out["ua_family"] = keyed_hash(fam, "ua_family")
    if tz:
        out["tz"] = keyed_hash(str(tz), "tz")
    if lang:
        out["lang"] = keyed_hash(re.split(r"[-_]", str(lang))[0].lower(), "lang")
    return out


def seen_at(timestamp) -> str:
    """Event time for first/last seen, clamped to the last 7 days so clients can't backdate or postdate."""
    ts, now = to_iso(timestamp), now_iso()
    return min(max(ts, days_ago_iso(7)), now)


def record(tenant_id: str, profile_id: str, origin_profile_id: str, signals: dict[str, str], ts: str | None = None):
    ts = ts or now_iso()
    for stype, h in signals.items():
        row = fetchone(
            "select id from signal where tenant_id=? and origin_profile_id=? and signal_type=? and value_hash=?",
            [tenant_id, origin_profile_id, stype, h],
        )
        if row:
            execute("update signal set first_seen=case when first_seen>? then ? else first_seen end, "
                    "last_seen=case when last_seen<? then ? else last_seen end, seen_count=seen_count+1, profile_id=? "
                    "where id=?", [ts, ts, ts, ts, profile_id, row["id"]])
        else:
            insert_row("signal", id=new_id("g_"), tenant_id=tenant_id, profile_id=profile_id,
                       origin_profile_id=origin_profile_id, signal_type=stype, value_hash=h,
                       first_seen=ts, last_seen=ts, seen_count=1)


def record_device(tenant_id: str, profile_id: str, origin_profile_id: str, context: dict | None,
                  signals: dict[str, str], ts: str | None = None):
    """Upsert the device this event came from. Keyed by explicit id, else fingerprint."""
    if signals.get("device_id"):
        key, kind = signals["device_id"], "device_id"
    elif signals.get("device"):
        key, kind = signals["device"], "fingerprint"
    else:
        return None
    ctx = context or {}
    dev = ctx.get("device") or {}
    desc = describe_device(ctx.get("userAgent") or dev.get("userAgent"), ctx.get("screen") or dev.get("screen"))
    if dev.get("type") in ("ios", "android") and desc["device_class"] in ("unknown", "desktop"):
        desc["device_class"] = "mobile"  # app SDKs send a type but no browser UA
    ts = ts or now_iso()
    row = fetchone("select id from device where tenant_id=? and origin_profile_id=? and device_key=?",
                   [tenant_id, origin_profile_id, key])
    if row:
        execute("update device set first_seen=case when first_seen>? then ? else first_seen end, "
                "last_seen=case when last_seen<? then ? else last_seen end, seen_count=seen_count+1, profile_id=? "
                "where id=?", [ts, ts, ts, ts, profile_id, row["id"]])
        return row["id"]
    did = new_id("d_")
    insert_row("device", id=did, tenant_id=tenant_id, profile_id=profile_id, origin_profile_id=origin_profile_id,
               device_key=key, key_kind=kind, first_seen=ts, last_seen=ts, seen_count=1, **desc)
    # A fingerprint seen before an explicit id arrived is the same device: fold it in.
    if kind == "device_id" and signals.get("device"):
        execute("delete from device where tenant_id=? and origin_profile_id=? and device_key=? and key_kind='fingerprint'",
                [tenant_id, origin_profile_id, signals["device"]])
    return did
