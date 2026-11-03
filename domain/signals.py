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
from util import keyed_hash, new_id, now_iso

SIGNAL_TYPES = ["device", "net", "ua_family", "tz", "lang"]


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


def ua_family(ua: str | None) -> str | None:
    if not ua:
        return None
    browser = next((b for b in ("Edg", "OPR", "Firefox", "Chrome", "Safari") if b in ua), "Other")
    os_name = next((o for o in ("Android", "iPhone", "iPad", "Windows", "Mac OS X", "Linux") if o in ua), "Other")
    return f"{browser}/{os_name}"


def extract(context: dict, ip: str | None) -> dict[str, str]:
    """Return {signal_type: hash} for whatever the context provides."""
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
    net = network_prefix(ip)
    if net:
        out["net"] = keyed_hash(net, "net")
    fam = ua_family(ua)
    if fam:
        out["ua_family"] = keyed_hash(fam, "ua_family")
    if tz:
        out["tz"] = keyed_hash(str(tz), "tz")
    if lang:
        out["lang"] = keyed_hash(re.split(r"[-_]", str(lang))[0].lower(), "lang")
    return out


def record(tenant_id: str, profile_id: str, origin_profile_id: str, signals: dict[str, str]):
    ts = now_iso()
    for stype, h in signals.items():
        row = fetchone(
            "select id from signal where tenant_id=? and origin_profile_id=? and signal_type=? and value_hash=?",
            [tenant_id, origin_profile_id, stype, h],
        )
        if row:
            execute("update signal set last_seen=?, seen_count=seen_count+1, profile_id=? where id=?",
                    [ts, profile_id, row["id"]])
        else:
            insert_row("signal", id=new_id("g_"), tenant_id=tenant_id, profile_id=profile_id,
                       origin_profile_id=origin_profile_id, signal_type=stype, value_hash=h,
                       first_seen=ts, last_seen=ts, seen_count=1)
