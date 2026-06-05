#!/usr/bin/env python3
"""Capture dashboard screenshots (desktop + mobile) with no API keys required.

Boots the FastAPI dashboard against freshly-seeded demo data, drives a headless
Chromium (Playwright), and writes PNGs to ``docs/screenshots/``.

Usage:
    python scripts/screenshot.py [--port 8099] [--out docs/screenshots]

Browser resolution order:
  1. $CHROME_PATH if set
  2. a Puppeteer-installed Chrome under ~/.cache/puppeteer
  3. Playwright's bundled chromium (if installed)
"""
from __future__ import annotations

import argparse
import glob
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Headless software-rendering args that work in a container.
CHROME_ARGS = [
    "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
    "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
    "--disable-software-rasterizer", "--hide-scrollbars",
    "--force-color-profile=srgb",
]


def find_chrome() -> str | None:
    if os.getenv("CHROME_PATH"):
        return os.getenv("CHROME_PATH")
    cands = glob.glob(os.path.expanduser(
        "~/.cache/puppeteer/chrome/*/chrome-linux64/chrome"))
    cands += glob.glob(os.path.expanduser(
        "~/.cache/puppeteer/chrome/*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"))
    return cands[0] if cands else None


def wait_for_port(host: str, port: int, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(1.0)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.25)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    ap.add_argument("--keep-data", action="store_true",
                    help="Don't re-seed demo data (use whatever is on disk).")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # 1) Seed realistic demo data (no API keys needed).
    if not args.keep_data:
        from web.demo import seed
        seed()

    # 2) Boot the dashboard in a background thread.
    import uvicorn

    from web.server import app

    config = uvicorn.Config(app, host="127.0.0.1", port=args.port,
                            log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    if not wait_for_port("127.0.0.1", args.port):
        print("ERROR: dashboard did not start", file=sys.stderr)
        return 1
    base = f"http://127.0.0.1:{args.port}"

    # 3) Drive a headless browser.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: pip install playwright", file=sys.stderr)
        return 1

    chrome = find_chrome()
    shots = []
    with sync_playwright() as p:
        launch_kw = {"headless": True, "args": CHROME_ARGS}
        if chrome:
            launch_kw["executable_path"] = chrome
        browser = p.chromium.launch(**launch_kw)

        views = [
            ("dashboard-desktop", 1280, 900, 2, False),
            ("dashboard-tablet", 834, 1112, 2, False),
            ("dashboard-mobile", 390, 844, 3, True),
        ]
        for name, w, h, dsr, full in views:
            page = browser.new_page(viewport={"width": w, "height": h},
                                    device_scale_factor=dsr)
            page.goto(base, wait_until="networkidle")
            page.wait_for_timeout(1400)  # let charts/animations settle
            path = out / f"{name}.png"
            page.screenshot(path=str(path), full_page=full)
            shots.append(path)
            print(f"  captured {path}")
            page.close()

        browser.close()

    server.should_exit = True
    print(f"\nDone — {len(shots)} screenshot(s) in {out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
