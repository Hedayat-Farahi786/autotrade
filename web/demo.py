"""Seed realistic demo data so the dashboard can be previewed without a live bot.

Writes a status heartbeat, an active signal with legs, and a feed of audit
events modelled on the real GTMO VIP screenshots. Purely for UI preview.
"""
from __future__ import annotations

import json
import os
import time

from bot.config import get_config


def seed() -> None:
    cfg = get_config(require_secrets=False)
    os.makedirs(os.path.dirname(cfg.status_file) or ".", exist_ok=True)
    os.makedirs(cfg.log_dir, exist_ok=True)
    now = time.time()

    status = {
        "ts": round(now, 3),
        "dry_run": True,
        "parser_mode": "hybrid",
        "provider": "gemini",
        "symbol": "XAUUSD",
        "simulate": True,
        "telegram_connected": True,
        "mt5_connected": True,
        "balance": 10250.40,
        "equity": 10418.75,
        "halted": False,
        "daily_loss": 120.0,
        "daily_start_balance": 10250.40,
        "max_daily_loss": 0.05,
        "open_signals": 1,
        "open_positions": 5,
        "emergency_stop": os.path.exists(cfg.emergency_stop_file),
        "intel_enabled": True,
        "review_queue": 2,
    }

    # Trade journal → drives the Performance panel.
    trades = [
        {"signal_id": 3, "ticket": 500031, "direction": "BUY", "symbol": "XAUUSD", "label": "TP1", "volume": 0.10, "open_price": 4467.0, "close_price": 4472.0, "profit": 50.0, "risk_amount": 30.0, "reason": "tp", "opened_at": now - 9000, "closed_at": now - 8800, "r_multiple": 1.67},
        {"signal_id": 3, "ticket": 500032, "direction": "BUY", "symbol": "XAUUSD", "label": "TP2", "volume": 0.10, "open_price": 4467.0, "close_price": 4474.0, "profit": 70.0, "risk_amount": 30.0, "reason": "tp", "opened_at": now - 9000, "closed_at": now - 8600, "r_multiple": 2.33},
        {"signal_id": 4, "ticket": 500041, "direction": "SELL", "symbol": "XAUUSD", "label": "TP1", "volume": 0.10, "open_price": 4480.0, "close_price": 4484.0, "profit": -40.0, "risk_amount": 30.0, "reason": "sl", "opened_at": now - 7000, "closed_at": now - 6900, "r_multiple": -1.33},
        {"signal_id": 5, "ticket": 500051, "direction": "BUY", "symbol": "XAUUSD", "label": "TP1", "volume": 0.10, "open_price": 4460.0, "close_price": 4465.0, "profit": 50.0, "risk_amount": 30.0, "reason": "tp", "opened_at": now - 5000, "closed_at": now - 4800, "r_multiple": 1.67},
        {"signal_id": 7, "ticket": 500101, "direction": "SELL", "symbol": "XAUUSD", "label": "TP1", "volume": 0.10, "open_price": 4456.2, "close_price": 4452.0, "profit": 42.0, "risk_amount": 30.0, "reason": "tp", "opened_at": now - 1800, "closed_at": now - 900, "r_multiple": 1.40},
        {"signal_id": 7, "ticket": 500102, "direction": "SELL", "symbol": "XAUUSD", "label": "TP2", "volume": 0.10, "open_price": 4456.2, "close_price": 4450.0, "profit": 62.0, "risk_amount": 30.0, "reason": "tp", "opened_at": now - 1800, "closed_at": now - 600, "r_multiple": 2.07},
    ]
    with open(cfg.trades_file, "w", encoding="utf-8") as fh:
        for t in trades:
            fh.write(json.dumps(t) + "\n")

    from bot.analytics.performance import summarize
    status["performance"] = summarize(trades)
    with open(cfg.status_file, "w", encoding="utf-8") as fh:
        json.dump(status, fh, indent=2)

    # A couple of items in the parser review queue.
    with open(cfg.review_file, "w", encoding="utf-8") as fh:
        for r in [
            {"ts": now - 800, "message_id": 4840, "text": "scale out a touch here, momentum fading", "reason": "trade_hint_but_noise", "resolved": False},
            {"ts": now - 400, "message_id": 4842, "text": "let the runner breathe, trail under structure", "reason": "trade_hint_but_noise", "resolved": False},
        ]:
            fh.write(json.dumps(r) + "\n")

    signals = {
        "next_id": 8,
        "signals": {
            "7": {
                "signal_id": 7, "magic": 990007, "direction": "SELL",
                "symbol": "XAUUSD", "entry_low": 4454.3, "entry_high": 4458.3,
                "sl": 4462.0, "order_kind": "MARKET", "message_id": 4821,
                "created_at": now - 1800, "closed": False, "tps_hit": [1, 2],
                "positions": [
                    {"ticket": 500101, "tp_label": "TP1", "tp_price": 4452.0, "volume": 0.10, "sl": 4456.3, "is_open_runner": False, "closed": True, "breakeven": True},
                    {"ticket": 500102, "tp_label": "TP2", "tp_price": 4450.0, "volume": 0.10, "sl": 4456.3, "is_open_runner": False, "closed": True, "breakeven": True},
                    {"ticket": 500103, "tp_label": "TP3", "tp_price": 4448.0, "volume": 0.08, "sl": 4456.3, "is_open_runner": False, "closed": False, "breakeven": True},
                    {"ticket": 500104, "tp_label": "TP4", "tp_price": 4446.0, "volume": 0.08, "sl": 4456.3, "is_open_runner": False, "closed": False, "breakeven": True},
                    {"ticket": 500105, "tp_label": "TP_OPEN", "tp_price": None, "volume": 0.08, "sl": 4456.3, "is_open_runner": True, "closed": False, "breakeven": True},
                ],
            }
        },
    }
    with open(cfg.state_file, "w", encoding="utf-8") as fh:
        json.dump(signals, fh, indent=2)

    events = [
        {"event": "bot_start", "dry_run": True, "parser_mode": "hybrid"},
        {"event": "telegram_message", "message_id": 4821,
         "text": "Gold sell now 4454.3 - 4458.3\nSL: 4462\nTP: 4452\nTP: 4450\nTP: 4448\nTP: 4446\nTP: open"},
        {"event": "intent", "message_id": 4821, "type": "ENTRY", "rule": "entry",
         "detail": "SELL MARKET zone=4454.3-4458.3 sl=4462 tps=[4452,4450,4448,4446,open]"},
        {"event": "mt5_open", "sim": True, "direction": "SELL", "ticket": 500101, "volume": 0.10, "price": 4456.2, "sl": 4462.0, "tp": 4452.0},
        {"event": "mt5_open", "sim": True, "direction": "SELL", "ticket": 500105, "volume": 0.08, "price": 4456.2, "sl": 4462.0, "tp": None},
        {"event": "telegram_message", "message_id": 4830, "text": "Already touched our top of the zone!"},
        {"event": "intent", "message_id": 4830, "type": "NOISE", "rule": "noise", "detail": ""},
        {"event": "telegram_message", "message_id": 4835, "text": "TP1 checkkk ✅"},
        {"event": "intent", "message_id": 4835, "type": "TP_HIT", "rule": "tp_hit", "detail": "tp_index=1"},
        {"event": "telegram_message", "message_id": 4839, "text": "TP2 smasssheddd take some more profits and set breakeven now for zero risk!!"},
        {"event": "intent", "message_id": 4839, "type": "TP_HIT", "rule": "tp_hit", "detail": "tp_index=2"},
        {"event": "intent", "message_id": 4839, "type": "PARTIAL_CLOSE", "rule": "partial", "detail": "close_fraction=0.5"},
        {"event": "intent", "message_id": 4839, "type": "BREAKEVEN", "rule": "breakeven", "detail": ""},
        {"event": "mt5_modify", "sim": True, "ticket": 500103, "sl": 4456.3},
        {"event": "telegram_message", "message_id": 4844, "text": "EVERYTHING IN PROFITSSSS!!! 😂😂😂"},
        {"event": "intent", "message_id": 4844, "type": "NOISE", "rule": "noise", "detail": ""},
    ]
    base = now - len(events) * 40
    with open(os.path.join(cfg.log_dir, "audit.jsonl"), "w", encoding="utf-8") as fh:
        for i, ev in enumerate(events):
            ev = {"ts": round(base + i * 40, 3), **ev}
            fh.write(json.dumps(ev) + "\n")

    print("Demo data seeded. Run:  python main.py --web")
