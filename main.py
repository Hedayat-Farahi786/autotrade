#!/usr/bin/env python3
"""Entry point for the GTMO VIP XAUUSD Telegram → MT5 trading bot.

Usage:
    python main.py            # run the bot (mode/provider from .env)
    python main.py --parse "Gold buy now 4470 - 4467\\nSL: 4464\\nTP: 4472"
                              # offline: parse a message and print intents
    python main.py --backfill 3        # read-only: replay last 3 days of the
                              # channel and show how each message parses
    python main.py --backfill 3 --show-noise   # include ignored messages
"""
from __future__ import annotations

import argparse
import asyncio
import sys


def _run_parse(text: str) -> None:
    """Offline helper to inspect how a message is parsed (no MT5/Telegram)."""

    from bot.config import get_config
    from bot.logger import setup_logging
    from bot.parser import build_parser

    cfg = get_config(require_secrets=False)
    setup_logging(cfg.log_level, cfg.log_dir)
    parser = build_parser(
        cfg.parser.mode,
        provider=cfg.parser.provider,
        default_symbol=cfg.mt5.symbol,
        anthropic_api_key=cfg.parser.anthropic_api_key,
        gemini_api_key=cfg.parser.gemini_api_key,
        anthropic_model=cfg.parser.anthropic_model,
        gemini_model=cfg.parser.gemini_model,
    )

    async def _go() -> None:
        intents = await parser.parse(text, message_id=0)
        print(f"\nMessage:\n{text}\n")
        print(f"Parsed {len(intents)} intent(s):")
        for i in intents:
            print(f"  • {i!r}")

    asyncio.run(_go())


def main() -> None:
    ap = argparse.ArgumentParser(description="GTMO XAUUSD Telegram → MT5 bot")
    ap.add_argument("--parse", metavar="TEXT",
                    help="Parse a single message offline and print the intents.")
    ap.add_argument("--backfill", metavar="DAYS", type=int,
                    help="Replay the last DAYS of channel history (read-only, "
                         "no trades) and report how each message parses.")
    ap.add_argument("--show-noise", action="store_true",
                    help="With --backfill, also list ignored/noise messages.")
    ap.add_argument("--web", action="store_true",
                    help="Launch the monitoring dashboard (FastAPI).")
    ap.add_argument("--demo", action="store_true",
                    help="With --web, seed sample data for a UI preview.")
    ap.add_argument("--host", default="127.0.0.1", help="Dashboard host.")
    ap.add_argument("--port", type=int, default=8000, help="Dashboard port.")
    args = ap.parse_args()

    if args.parse is not None:
        _run_parse(args.parse.replace("\\n", "\n"))
        return

    if args.backfill is not None:
        from bot.replay import run_backfill

        run_backfill(days=args.backfill, show_noise=args.show_noise)
        return

    if args.web:
        if args.demo:
            from web.demo import seed

            seed()
        from web.server import run as run_web

        print(f"\n  Dashboard → http://{args.host}:{args.port}\n")
        run_web(host=args.host, port=args.port)
        return

    from bot.app import main as run_bot

    run_bot()


if __name__ == "__main__":
    sys.exit(main())
