"""In-process demo bot for the dashboard's live experience (no API keys).

Drives the same artifacts a real bot writes — ``status.json`` heartbeat,
``audit.jsonl`` feed, ``active_signals.json`` and ``trades.jsonl`` — so the UI
shows a *live*, animated session: a connection boot sequence, drifting
balance/equity, a streaming signal feed, and a growing equity curve.

This is purely for demonstration; it places no real orders.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from typing import Any

from bot.config import get_config

_cfg = get_config(require_secrets=False)


def _audit(event: str, **fields: Any) -> None:
    rec = {"ts": round(time.time(), 3), "event": event, **fields}
    os.makedirs(_cfg.log_dir, exist_ok=True)
    with open(os.path.join(_cfg.log_dir, "audit.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


# A short, realistic script the demo feed cycles through.
_SCRIPT = [
    ("Gold buy now 4470 - 4467\nSL: 4464\nTP: 4472\nTP: 4474\nTP: open (100+ pips)",
     [("intent", {"type": "ENTRY", "rule": "entry",
                  "detail": "BUY MARKET 4467-4470 sl=4464 tps=[4472,4474,open]"})],
     [("mt5_open", {"direction": "BUY", "ticket": None, "volume": 0.06,
                    "price": 4470.05, "sl": 4464.0, "tp": 4472.0})]),
    ("Floating in profits, next to TP1 again", [("intent", {"type": "NOISE", "rule": "noise"})], []),
    ("TP1 has been touched ✅", [("intent", {"type": "TP_HIT", "rule": "tp_hit", "detail": "tp_index=1"})],
     [("mt5_close", {"ticket": None, "volume": 0.06, "profit": 35.4})]),
    ("Adjust SL to 4468, lock it in", [("intent", {"type": "MODIFY_SL", "rule": "sl_modify", "detail": "new_sl=4468"})],
     [("mt5_modify", {"ticket": None, "sl": 4468.0})]),
    ("TP2 smasssheddd take some more profits and set breakeven now!!",
     [("intent", {"type": "TP_HIT", "rule": "tp_hit", "detail": "tp_index=2"}),
      ("intent", {"type": "PARTIAL_CLOSE", "rule": "partial", "detail": "close_fraction=0.5"}),
      ("intent", {"type": "BREAKEVEN", "rule": "breakeven"})],
     [("mt5_close", {"ticket": None, "volume": 0.03, "profit": 58.0})]),
    ("EVERYTHING IN PROFITSSSS!!! 😂", [("intent", {"type": "NOISE", "rule": "noise"})], []),
]


class DemoBot:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.start_balance = 10000.0
        self.balance = 10000.0
        self.equity = 10000.0
        self._step = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._write_status(online=False, tg=False, mt5=False, connecting=False)

    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        from web.demo import seed
        seed()  # baseline populated data
        _audit("bot_start", dry_run=True, parser_mode="hybrid", demo=True)

        # --- connection boot sequence (animated in the UI) --------------
        self._write_status(online=True, tg=False, mt5=False, connecting=True)
        if self._wait(1.3):
            return
        self._write_status(online=True, tg=True, mt5=False, connecting=True)
        _audit("telegram_message", message_id=9000, text="🤖 connected to GTMO VIP")
        if self._wait(1.3):
            return
        self._write_status(online=True, tg=True, mt5=True, connecting=False)

        # --- live loop --------------------------------------------------
        last_feed = 0.0
        last_trade = 0.0
        while not self._stop.is_set():
            # Drift equity around balance (random walk).
            self.equity = round(self.equity + random.uniform(-6, 9), 2)
            self.equity = max(self.balance - 120, min(self.balance + 260, self.equity))
            self._write_status(online=True, tg=True, mt5=True, connecting=False)

            now = time.time()
            if now - last_feed > 4.5:
                last_feed = now
                self._emit_feed_step()
            if now - last_trade > 9:
                last_trade = now
                self._book_trade()

            if self._wait(2.0):
                return

    def _emit_feed_step(self) -> None:
        text, intents, actions = _SCRIPT[self._step % len(_SCRIPT)]
        self._step += 1
        mid = 9100 + self._step
        _audit("telegram_message", message_id=mid, text=text)
        for _, payload in intents:
            _audit("intent", message_id=mid, **payload)
        for ev, payload in actions:
            tk = random.randint(500100, 500999)
            payload = {**payload, "sim": True, "ticket": payload.get("ticket") or tk}
            _audit(ev, **payload)

    def _book_trade(self) -> None:
        win = random.random() < 0.7
        profit = round(random.uniform(25, 80) if win else -random.uniform(15, 45), 2)
        self.balance = round(self.balance + profit, 2)
        self.equity = self.balance
        rec = {
            "signal_id": random.randint(10, 99), "ticket": random.randint(500100, 500999),
            "direction": random.choice(["BUY", "SELL"]), "symbol": "XAUUSD",
            "label": random.choice(["TP1", "TP2", "TP3"]),
            "volume": 0.06, "open_price": 4468.0, "close_price": 4472.0,
            "profit": profit, "risk_amount": 30.0,
            "reason": "tp" if win else "sl",
            "opened_at": time.time() - 600, "closed_at": time.time(),
            "r_multiple": round(profit / 30.0, 2),
        }
        with open(_cfg.trades_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def _wait(self, seconds: float) -> bool:
        return self._stop.wait(seconds)

    def _write_status(self, *, online: bool, tg: bool, mt5: bool,
                      connecting: bool) -> None:
        from bot.analytics.performance import load_records, summarize
        perf = summarize(load_records(_cfg.trades_file))
        daily = round(self.equity - self.start_balance, 2)
        snapshot = {
            "ts": round(time.time(), 3), "dry_run": True, "parser_mode": "hybrid",
            "provider": "gemini", "symbol": "XAUUSD", "simulate": True,
            "telegram_connected": tg, "mt5_connected": mt5,
            "connecting": connecting, "bot_running": online,
            "balance": self.balance, "equity": self.equity,
            "halted": False, "daily_loss": max(0.0, -daily),
            "daily_start_balance": self.start_balance, "max_daily_loss": 0.05,
            "open_signals": 1, "open_positions": 3,
            "emergency_stop": os.path.exists(_cfg.emergency_stop_file),
            "paused": os.path.exists(_cfg.control.pause_file),
            "performance": perf, "review_queue": 2,
            "intel_enabled": True, "trailing_enabled": True,
            "demo": True,
        }
        os.makedirs(os.path.dirname(_cfg.status_file) or ".", exist_ok=True)
        tmp = _cfg.status_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh)
        os.replace(tmp, _cfg.status_file)


_demo = DemoBot()


def get_demo_bot() -> DemoBot:
    return _demo
