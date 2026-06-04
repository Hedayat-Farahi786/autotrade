"""Offline backfill / replay tool.

Pulls the last *N* days of messages from the configured Telegram channel and
runs each through the parser **without executing any trades**, printing a
report of how every message is interpreted. This is the easiest way to validate
parsing against real recent channel history — no need to forward messages
anywhere.

Run via:  ``python main.py --backfill 3``
"""
from __future__ import annotations

import asyncio
from collections import Counter
from typing import List

from .config import get_config
from .logger import audit, get_logger, setup_logging
from .models import Intent, IntentType
from .parser import build_parser
from .telegram.listener import TelegramListener

log = get_logger("replay")


def _fmt_intent(i: Intent) -> str:
    if i.type is IntentType.ENTRY and i.entry:
        e = i.entry
        tps = ", ".join(str(tp.price or "open") for tp in e.take_profits)
        return (f"ENTRY {e.direction.value} {e.order_kind.value} "
                f"{e.entry_low}-{e.entry_high} SL={e.sl} TP=[{tps}]")
    if i.type is IntentType.MODIFY_SL:
        return f"MODIFY_SL → {i.new_sl}"
    if i.type is IntentType.PARTIAL_CLOSE:
        return f"PARTIAL_CLOSE ({i.matched_rule}, {int(i.close_fraction * 100)}%)"
    if i.type is IntentType.TP_HIT:
        return f"TP_HIT (tp={i.tp_index})"
    return i.type.value


async def _run(days: int, show_noise: bool) -> None:
    cfg = get_config(require_secrets=True)
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
    listener = TelegramListener(cfg.telegram, on_message=_noop)

    print(f"\nConnecting to Telegram and fetching the last {days} day(s)…\n")
    await listener.connect_only()

    counts: Counter = Counter()
    total = 0
    actionable = 0
    async for mid, date, text, has_media in listener.iter_history(days=days):
        total += 1
        if not text.strip():
            if show_noise:
                ts = date.strftime("%m-%d %H:%M") if date else "?"
                print(f"[{ts}] #{mid:<7} (media/no-text)")
            continue
        intents: List[Intent] = await parser.parse(text, mid)
        act = [i for i in intents if i.type is not IntentType.NOISE]
        for i in intents:
            counts[i.type.value] += 1
        ts = date.strftime("%m-%d %H:%M") if date else "?"
        snippet = text.replace("\n", " ⏎ ")
        snippet = (snippet[:70] + "…") if len(snippet) > 70 else snippet
        if act:
            actionable += 1
            print(f"[{ts}] #{mid:<7} {snippet}")
            for i in act:
                print(f"            └─► {_fmt_intent(i)}")
            audit("replay", message_id=mid, text=text,
                  intents=[_fmt_intent(i) for i in act])
        elif show_noise:
            print(f"[{ts}] #{mid:<7} {snippet}   ·noise·")

    await listener.stop()
    print("\n" + "=" * 60)
    print(f"Scanned {total} message(s); {actionable} actionable.")
    print("Intent breakdown:")
    for name, n in counts.most_common():
        print(f"  {name:<14} {n}")
    print("=" * 60)
    print("\nNOTE: replay is read-only — no trades were placed.\n")


async def _noop(text: str, message_id: int) -> None:  # listener callback (unused)
    return None


def run_backfill(days: int = 3, show_noise: bool = False) -> None:
    asyncio.run(_run(days, show_noise))
