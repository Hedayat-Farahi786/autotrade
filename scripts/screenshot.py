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

    # Start with a clean slate so the connection/boot screen is captured first.
    cfg = _server_cfg()
    if not args.keep_data:
        for f in (cfg.status_file, cfg.state_file, cfg.trades_file, cfg.review_file):
            try:
                os.remove(f)
            except OSError:
                pass

    # Boot the dashboard in a background thread.
    import uvicorn

    from web.server import app

    config = uvicorn.Config(app, host="127.0.0.1", port=args.port,
                            log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    if not wait_for_port("127.0.0.1", args.port):
        print("ERROR: dashboard did not start", file=sys.stderr)
        return 1
    base = f"http://127.0.0.1:{args.port}"

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

        # 1) Connection / boot screen (bot offline).
        page = browser.new_page(viewport={"width": 1280, "height": 900},
                                device_scale_factor=2)
        page.goto(base, wait_until="networkidle")
        page.wait_for_timeout(1200)
        page.screenshot(path=str(out / "01-connect.png"))
        shots.append("01-connect.png")
        print("  captured 01-connect.png")

        # 2) Start the live demo and let it connect + stream.
        page.click("#startDemoBtn")
        page.wait_for_timeout(5000)  # boot sequence + a few live ticks
        page.screenshot(path=str(out / "dashboard-desktop.png"))
        shots.append("dashboard-desktop.png")
        print("  captured dashboard-desktop.png (live)")
        page.close()

        # 3) Tablet + mobile (demo already running → straight to live).
        for name, w, h, dsr, full in [
            ("dashboard-tablet", 834, 1112, 2, False),
            ("dashboard-mobile", 390, 844, 3, True),
        ]:
            pg = browser.new_page(viewport={"width": w, "height": h},
                                  device_scale_factor=dsr)
            pg.goto(base, wait_until="networkidle")
            pg.wait_for_timeout(2500)
            pg.screenshot(path=str(out / f"{name}.png"), full_page=full)
            shots.append(f"{name}.png")
            print(f"  captured {name}.png (live)")
            pg.close()

        browser.close()

    try:
        from web.sim_bot import get_demo_bot
        get_demo_bot().stop()
    except Exception:  # noqa: BLE001
        pass
    server.should_exit = True
    print(f"\nDone — {len(shots)} screenshot(s) in {out}/")
    return 0


def _server_cfg():
    from bot.config import get_config
    return get_config(require_secrets=False)


if __name__ == "__main__":
    raise SystemExit(main())
