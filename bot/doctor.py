"""Preflight self-check (``python main.py --doctor``).

Validates configuration and connectivity *before* you trust the bot with money:
config sanity, AI provider key, MT5 connection (and resolved symbol/balance),
parser construction, and Telegram credential presence. Prints a PASS/WARN/FAIL
report and exits non-zero if anything critical fails.
"""
from __future__ import annotations

import asyncio
from typing import List, Tuple

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


class _Report:
    def __init__(self) -> None:
        self.rows: List[Tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))

    def render(self) -> int:
        color = {PASS: GREEN, WARN: YELLOW, FAIL: RED}
        print("\n  GTMO bot — preflight check\n")
        for status, name, detail in self.rows:
            mark = {PASS: "✓", WARN: "!", FAIL: "✗"}[status]
            line = f"  {color[status]}{mark} {status:<4}{RESET} {name}"
            if detail:
                line += f"  {DIM}{detail}{RESET}"
            print(line)
        fails = sum(1 for s, _, _ in self.rows if s == FAIL)
        warns = sum(1 for s, _, _ in self.rows if s == WARN)
        print(f"\n  {len(self.rows)} checks · {fails} failed · {warns} warnings\n")
        return 1 if fails else 0


async def _run() -> int:
    rep = _Report()

    # --- Config ---------------------------------------------------------
    try:
        from .config import get_config

        cfg = get_config(require_secrets=False)
        rep.add(PASS, "Configuration loaded & validated")
    except Exception as exc:  # noqa: BLE001
        rep.add(FAIL, "Configuration", str(exc))
        return rep.render()

    # --- Telegram creds -------------------------------------------------
    tg = cfg.telegram
    if tg.api_id and tg.api_hash and tg.channel:
        rep.add(PASS, "Telegram credentials present", f"channel={tg.channel}")
    else:
        rep.add(WARN, "Telegram credentials", "missing API_ID/HASH/CHANNEL")

    # --- AI provider ----------------------------------------------------
    p = cfg.parser
    if p.mode == "regex":
        rep.add(PASS, "Parser mode", "regex (no AI key needed)")
    elif p.provider == "gemini":
        rep.add(PASS if p.gemini_api_key else WARN, "Gemini API key",
                "set" if p.gemini_api_key else f"missing (mode={p.mode})")
    else:
        rep.add(PASS if p.anthropic_api_key else WARN, "Anthropic API key",
                "set" if p.anthropic_api_key else f"missing (mode={p.mode})")

    # --- Parser builds --------------------------------------------------
    try:
        from .parser import build_parser

        parser = build_parser(p.mode, provider=p.provider,
                              gemini_api_key=p.gemini_api_key,
                              anthropic_api_key=p.anthropic_api_key)
        intents = await parser.parse("Gold buy now 4470 - 4467\nSL: 4464\nTP: 4472", 0)
        ok = any(i.type.value == "ENTRY" for i in intents)
        rep.add(PASS if ok else WARN, "Parser self-test",
                f"{parser.__class__.__name__} → {[i.type.value for i in intents]}")
    except Exception as exc:  # noqa: BLE001
        rep.add(FAIL, "Parser", str(exc))

    # --- MT5 connection -------------------------------------------------
    try:
        from .mt5.executor import MT5Executor, _MT5_AVAILABLE

        ex = MT5Executor(cfg.mt5, dry_run=cfg.dry_run)
        connected = await ex.connect()
        if ex.simulate:
            why = "DRY_RUN" if cfg.dry_run else "MetaTrader5 package not installed"
            rep.add(WARN, "MT5 connection", f"simulator ({why})")
        elif connected:
            bal = await ex.account_balance()
            rep.add(PASS, "MT5 connection", f"symbol={ex.symbol} balance={bal:.2f}")
        else:
            rep.add(FAIL, "MT5 connection", "initialize/login failed")
        await ex.shutdown()
    except Exception as exc:  # noqa: BLE001
        rep.add(FAIL, "MT5", str(exc))

    # --- Risk / safety sanity ------------------------------------------
    rep.add(WARN if not cfg.dry_run else PASS, "Trading mode",
            "LIVE — real orders!" if not cfg.dry_run else "dry-run (safe)")
    rep.add(PASS, "Risk per signal", f"{cfg.risk.risk_per_signal*100:.1f}%")
    rep.add(PASS, "Daily loss halt", f"{cfg.risk.max_daily_loss*100:.1f}%")

    return rep.render()


def run() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 1
