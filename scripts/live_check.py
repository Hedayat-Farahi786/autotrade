#!/usr/bin/env python3
"""Live connectivity check — run this on YOUR machine with real keys.

Proves the whole chain works against the real services before you trust it:

  • AI parser   — does a real parse via Gemini/Anthropic (if a key is set)
  • Telegram    — logs in, resolves the channel, reads the last few messages and
                  shows exactly how each would be parsed (listening works)
  • MT5         — connects, logs in, resolves the symbol, reads the live tick &
                  spread, and validates a 0.01-lot order via order_check WITHOUT
                  placing it (the order pipeline works, zero risk)

Optional:
  --trade       place a REAL 0.01-lot market order and immediately close it
                (use only on a DEMO account; asks for confirmation)
  --messages N  how many recent channel messages to sample (default 8)

Usage:
    python scripts/live_check.py
    python scripts/live_check.py --trade        # real open+close round-trip
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

G, Y, R, D, X = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
_fails = 0


def line(status: str, label: str, detail: str = "") -> None:
    global _fails
    mark = {"ok": f"{G}✓{X}", "warn": f"{Y}!{X}", "fail": f"{R}✗{X}"}[status]
    if status == "fail":
        _fails += 1
    print(f"  {mark} {label}" + (f"  {D}{detail}{X}" if detail else ""))


def head(t: str) -> None:
    print(f"\n\033[1m{t}\033[0m")


async def check_ai(cfg) -> None:
    head("AI parser")
    from bot.parser import build_parser
    p = build_parser(cfg.parser.mode, provider=cfg.parser.provider,
                     gemini_api_key=cfg.parser.gemini_api_key,
                     anthropic_api_key=cfg.parser.anthropic_api_key)
    name = type(p).__name__
    if cfg.parser.mode == "regex" or name == "RegexParserAsync":
        line("warn", "AI parser", f"{name} (regex — no AI call; set a key + PARSER_MODE)")
        return
    try:
        intents = await p.parse("Gold buy now 4470 - 4467\nSL: 4464\nTP: 4472", 0)
        ok = any(i.type.value == "ENTRY" for i in intents)
        line("ok" if ok else "fail", f"{cfg.parser.provider} live parse",
             f"{[i.type.value for i in intents]}")
    except Exception as exc:  # noqa: BLE001
        line("fail", f"{cfg.parser.provider} live parse", str(exc)[:120])


async def check_telegram(cfg, n_messages: int) -> None:
    head("Telegram")
    try:
        from bot.parser.signal_parser import SignalParser
        from bot.telegram.listener import TelegramListener
    except Exception as exc:  # noqa: BLE001
        line("fail", "import telethon", f"pip install telethon ({exc})")
        return
    listener = TelegramListener(cfg.telegram, on_message=lambda t, m: None)
    try:
        entity = await listener.connect_only()
        me = await listener.client.get_me()
        line("ok", "login", f"as {getattr(me, 'username', None) or getattr(me, 'first_name', '?')}")
        title = getattr(entity, "title", str(entity))
        line("ok", "channel resolved", title)
        parser = SignalParser(default_symbol=cfg.mt5.symbol)
        msgs = await listener.client.get_messages(entity, limit=n_messages)
        actionable = 0
        print(f"  {D}— last {len(msgs)} message(s):{X}")
        for m in reversed(msgs):
            text = (m.message or "").strip()
            if not text:
                continue
            intents = parser.parse(text, m.id)
            act = [i.type.value for i in intents if i.type.value != "NOISE"]
            if act:
                actionable += 1
            tag = (",".join(act)) if act else "noise"
            snippet = text.replace("\n", " ⏎ ")[:54]
            print(f"     {D}#{m.id}{X} {snippet:<56} → {tag}")
        line("ok", "message read + parse", f"{actionable} actionable of {len(msgs)}")
    except Exception as exc:  # noqa: BLE001
        line("fail", "Telegram connection", str(exc)[:160])
    finally:
        try:
            await listener.stop()
        except Exception:  # noqa: BLE001
            pass


async def check_mt5(cfg, do_trade: bool) -> None:
    head("MetaTrader 5")
    from bot.mt5.executor import _MT5_AVAILABLE, MT5Executor
    if not _MT5_AVAILABLE:
        line("fail", "MetaTrader5 package", "not installed (Windows-only). "
             "Run the bot on a Windows host/VPS with MT5.")
        return
    ex = MT5Executor(cfg.mt5, dry_run=False)  # force the real path for the check
    try:
        if not await ex.connect():
            line("fail", "connect/login", "see logs (check login/password/server)")
            return
        bal = await ex.account_balance()
        eq = await ex.account_equity()
        line("ok", "connect + login", f"balance={bal:.2f} equity={eq:.2f}")
        line("ok", "symbol resolved", ex.symbol)
        tick = await ex.current_price()
        spread = (tick["ask"] - tick["bid"]) / cfg.risk.pip_size
        line("ok", "live quote", f"bid={tick['bid']} ask={tick['ask']} spread={spread:.1f} pips")

        chk = await ex.check_order(direction="BUY", volume=cfg.risk.min_lot)
        line("ok" if chk["ok"] else "fail", "order_check (no order placed)",
             f"retcode={chk['retcode']} {chk.get('comment','')}")

        if do_trade:
            if input("\n  Place a REAL 0.01-lot test order now? [y/N] ").strip().lower() != "y":
                line("warn", "real round-trip", "skipped by user")
            else:
                res = await ex.open_position(direction="BUY", volume=cfg.risk.min_lot,
                                             price=None, sl=None, tp=None,
                                             order_kind="MARKET",
                                             magic=cfg.mt5.magic_base,
                                             comment="gtmo-livecheck")
                if res.ok and res.ticket:
                    line("ok", "real order opened", f"ticket={res.ticket} @ {res.price}")
                    close = await ex.close_position(res.ticket)
                    line("ok" if close.ok else "fail", "real order closed",
                         f"pl={close.profit}")
                else:
                    line("fail", "real order", res.comment)
    except Exception as exc:  # noqa: BLE001
        line("fail", "MT5 check", str(exc)[:160])
    finally:
        await ex.shutdown()


async def _main(n_messages: int, do_trade: bool) -> int:
    from bot.config import ConfigError, get_config
    from bot.logger import setup_logging
    try:
        cfg = get_config(require_secrets=True)
    except ConfigError as exc:
        print(f"\n{R}Configuration incomplete:{X} {exc}\n"
              f"  Copy .env.example → .env and fill in Telegram + MT5 (+ AI key),\n"
              f"  then re-run.  See also: python main.py --doctor\n")
        return 1
    setup_logging(cfg.log_level, cfg.log_dir)

    print(f"\n\033[1mGTMO live connectivity check\033[0m  "
          f"{D}(mode={'LIVE' if not cfg.dry_run else 'dry-run'}, "
          f"parser={cfg.parser.mode}/{cfg.parser.provider}){X}")

    await check_ai(cfg)
    await check_telegram(cfg, n_messages)
    await check_mt5(cfg, do_trade)

    print()
    if _fails:
        print(f"{R}{_fails} check(s) FAILED — fix these before going live.{X}\n")
        return 1
    print(f"{G}All live checks passed — real Telegram + MT5 + parsing work.{X}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Live connectivity check")
    ap.add_argument("--trade", action="store_true",
                    help="Place a REAL 0.01-lot order and close it (DEMO only).")
    ap.add_argument("--messages", type=int, default=8,
                    help="How many recent channel messages to sample.")
    args = ap.parse_args()
    try:
        return asyncio.run(_main(args.messages, args.trade))
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
