#!/usr/bin/env python3
"""Capture platform-guide screenshots and the animated walkthrough GIF.

Drives headless Chromium through the FastIdentity admin, saves one screenshot
per sidebar page into docs/img/, then stitches them into
docs/demo/fastidentity-walkthrough.gif.

Usage (server must be running, demo data seeded with `python seed.py --demo`
and one worker tick `python -m workers.run --once`):

    DEMO_BASE_URL=http://localhost:5023 python scripts/capture_guide_screenshots.py

Env: ADMIN_EMAIL / ADMIN_PASSWORD (read from .env when present),
     DEMO_PROFILE_ID (profile shown on the detail slide; default = busiest known profile).
Dev-only deps: pip install playwright pillow && python -m playwright install chromium
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
VIEWPORT = {"width": 1440, "height": 900}
GIF_WIDTH = 1100
GIF_FRAME_MS = 2200

# (filename, caption, route) — sidebar order: Overview, Identity graph, Activation, Compliance, Setup.
SHOTS = [
    ("00-login.png", "Sign in", "/login"),
    ("01-dashboard.png", "Overview — dashboard", "/"),
    ("02-profiles.png", "Identity graph — profiles", "/profiles"),
    ("03-profile.png", "Identity graph — unified profile", "/profiles/{profile}"),
    ("04-events.png", "Identity graph — event stream", "/events"),
    ("05-candidates.png", "Identity graph — merge candidates", "/candidates"),
    ("06-quality.png", "Identity graph — match quality", "/quality"),
    ("07-segments.png", "Activation — segments", "/segments"),
    ("08-flows.png", "Activation — abandonment flows", "/flows"),
    ("09-destinations.png", "Activation — destinations", "/destinations"),
    ("10-consent.png", "Compliance — consent ledger", "/consent"),
    ("11-privacy.png", "Compliance — privacy requests", "/privacy"),
    ("12-audit.png", "Compliance — audit log", "/audit"),
    ("13-sources.png", "Setup — sources & pixel", "/sources"),
    ("14-households.png", "Identity graph — devices & households", "/households"),
    ("15-household.png", "Identity graph — one household", "/households/{household}"),
    ("16-api-keys.png", "Setup — workspaces, members & API keys", "/settings/api-keys"),
]


def busiest_profile() -> str:
    sys.path.insert(0, str(ROOT))
    import db  # noqa: PLC0415

    row = db.fetchone(
        "select p.id, count(e.id) n from profile p join event e on e.profile_id=p.id "
        "where p.status='known' and p.merged_into is null group by p.id order by n desc limit 1"
    )
    return row["id"] if row else ""


def biggest_household() -> str:
    sys.path.insert(0, str(ROOT))
    import db  # noqa: PLC0415

    row = db.fetchone("select id from household order by members desc, devices desc limit 1")
    return row["id"] if row else ""


def settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(900)


def capture(base: str, out_dir: Path, only: set[str] | None) -> list[tuple[str, Path]]:
    email = os.getenv("ADMIN_EMAIL", "admin@fastsme.com")
    password = os.getenv("ADMIN_PASSWORD", "admin")
    profile = os.getenv("DEMO_PROFILE_ID") or busiest_profile()
    household = os.getenv("DEMO_HOUSEHOLD_ID") or biggest_household()
    out_dir.mkdir(parents=True, exist_ok=True)
    done: list[tuple[str, Path]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport=VIEWPORT).new_page()
        page.goto(f"{base}/login")
        settle(page)
        if not only or "00-login.png" in only:
            page.screenshot(path=out_dir / "00-login.png")
            done.append(("Sign in", out_dir / "00-login.png"))
        page.fill('input[name="email"]', email)
        page.fill('input[name="password"]', password)
        page.click('button[type="submit"]')
        settle(page)
        for fname, caption, route in SHOTS[1:]:
            if only and fname not in only:
                continue
            try:
                page.goto(base + route.format(profile=profile, household=household))
                settle(page)
                page.screenshot(path=out_dir / fname)
                done.append((caption, out_dir / fname))
                print(f"  ✓ {fname}")
            except Exception as exc:  # keep going; report the miss
                print(f"  ✗ {fname}: {exc}", file=sys.stderr)
        browser.close()
    return done


def make_gif(frames: list[tuple[str, Path]], gif_path: Path):
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    images = []
    for caption, path in frames:
        im = Image.open(path).convert("RGB")
        im = im.resize((GIF_WIDTH, int(im.height * GIF_WIDTH / im.width)), Image.LANCZOS)
        bar = Image.new("RGB", (im.width, 44), (15, 23, 42))
        ImageDraw.Draw(bar).text((16, 10), caption, fill=(255, 255, 255), font=font)
        canvas = Image.new("RGB", (im.width, im.height + bar.height))
        canvas.paste(bar, (0, 0))
        canvas.paste(im, (0, bar.height))
        images.append(canvas.quantize(colors=128, method=Image.Quantize.MEDIANCUT))
    gif_path.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(gif_path, save_all=True, append_images=images[1:], duration=GIF_FRAME_MS,
                   loop=0, optimize=True)
    print(f"✓ GIF {gif_path.relative_to(ROOT)} ({gif_path.stat().st_size // 1024} KB, {len(images)} frames)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.getenv("DEMO_BASE_URL", "http://localhost:5023"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "img"))
    ap.add_argument("--only", default="", help="comma-separated filenames to (re)capture")
    ap.add_argument("--no-gif", action="store_true")
    a = ap.parse_args()
    only = {s.strip() for s in a.only.split(",") if s.strip()} or None
    shots = capture(a.base_url.rstrip("/"), Path(a.out), only)
    if not a.no_gif and not only:
        make_gif(shots, ROOT / "docs" / "demo" / "fastidentity-walkthrough.gif")
