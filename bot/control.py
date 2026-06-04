"""Remote control & alerts.

Three small pieces:

* :class:`Notifier` — pushes alert messages to a Telegram chat (your Saved
  Messages by default) for trades, errors and the daily summary.
* :class:`Controller` — the actions a human can trigger: pause/resume new
  entries, flatten everything, and produce a status summary. State is mirrored
  to ``control.json`` and a ``pause`` flag file so the dashboard and trader see
  it too.
* :class:`CommandBus` — tails a JSONL command file so the (decoupled) web
  dashboard or CLI can drive the :class:`Controller`.

Telegram commands are wired directly in :mod:`bot.app` (same process), so both
the dashboard and your phone funnel into the same :class:`Controller`.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from .config import BotConfig
from .logger import audit, get_logger

log = get_logger("control")

HELP_TEXT = (
    "GTMO bot commands:\n"
    "/status — account, positions & performance\n"
    "/pause — stop opening new trades\n"
    "/resume — allow new trades\n"
    "/closeall — close every open position now\n"
    "/flat — alias for /closeall\n"
    "/perf — performance summary\n"
    "/stop — engage emergency stop\n"
    "/go — clear emergency stop\n"
    "/help — this message"
)


class Notifier:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._client = None
        self._target = None

    def bind(self, client, target) -> None:
        self._client = client
        self._target = target

    async def notify(self, text: str) -> None:
        if not self.enabled or self._client is None or self._target is None:
            return
        try:
            await self._client.send_message(self._target, text)
        except Exception as exc:  # noqa: BLE001
            log.debug("notify failed: %s", exc)


class Controller:
    def __init__(self, cfg: BotConfig, executor, state, trader, tracker,
                 notifier: Notifier | None = None) -> None:
        self.cfg = cfg
        self.ex = executor
        self.state = state
        self.trader = trader
        self.tracker = tracker
        self.notifier = notifier
        self._write_control()

    # ----- pause / resume --------------------------------------------------
    @property
    def paused(self) -> bool:
        return os.path.exists(self.cfg.control.pause_file)

    async def pause(self, source: str = "?") -> str:
        self._touch(self.cfg.control.pause_file, "paused")
        self._write_control()
        audit("control_pause", source=source)
        msg = "⏸ Paused — no new trades will be opened."
        await self._announce(msg)
        return msg

    async def resume(self, source: str = "?") -> str:
        self._remove(self.cfg.control.pause_file)
        self._write_control()
        audit("control_resume", source=source)
        msg = "▶️ Resumed — new trades enabled."
        await self._announce(msg)
        return msg

    async def emergency_stop(self, on: bool, source: str = "?") -> str:
        if on:
            self._touch(self.cfg.emergency_stop_file, "emergency stop")
            msg = "🛑 EMERGENCY STOP engaged."
        else:
            self._remove(self.cfg.emergency_stop_file)
            msg = "✅ Emergency stop cleared."
        audit("control_estop", on=on, source=source)
        await self._announce(msg)
        return msg

    async def close_all(self, source: str = "?") -> str:
        n = await self.trader.close_everything(reason="manual")
        audit("control_close_all", source=source, closed=n)
        msg = f"🔻 Closed {n} open position(s)."
        await self._announce(msg)
        return msg

    # ----- status ----------------------------------------------------------
    async def status_text(self) -> str:
        bal = await self.ex.account_balance()
        eq = await self.ex.account_equity()
        actives = self.state.active_signals()
        positions = sum(len(s.open_positions()) for s in actives)
        perf = self.tracker.summary() if self.tracker else {}
        mode = "DRY-RUN" if self.cfg.dry_run else "LIVE"
        flags = []
        if self.paused:
            flags.append("PAUSED")
        if os.path.exists(self.cfg.emergency_stop_file):
            flags.append("E-STOP")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        return (
            f"📊 GTMO {mode}{flag_str}\n"
            f"Balance: {bal:.2f}  Equity: {eq:.2f}\n"
            f"Open: {len(actives)} signal(s), {positions} position(s)\n"
            + self.perf_text(perf)
        )

    def perf_text(self, perf: dict | None = None) -> str:
        perf = perf if perf is not None else (
            self.tracker.summary() if self.tracker else {})
        if not perf or perf.get("trades", 0) == 0:
            return "No closed trades yet."
        return (
            f"Trades: {perf['trades']}  Win: {perf['win_rate']*100:.0f}%  "
            f"PF: {perf.get('profit_factor')}\n"
            f"Net: {perf['net_profit']:.2f}  Exp: {perf['expectancy']:.2f}  "
            f"Avg R: {perf.get('avg_r')}  DD: {perf['max_drawdown']:.2f}"
        )

    async def handle_command(self, cmd: str, source: str = "?") -> str | None:
        cmd = (cmd or "").strip().lstrip("/").lower()
        if cmd in {"status", "s"}:
            return await self.status_text()
        if cmd in {"perf", "performance"}:
            return self.perf_text()
        if cmd == "pause":
            return await self.pause(source)
        if cmd == "resume":
            return await self.resume(source)
        if cmd in {"closeall", "close_all", "flat"}:
            return await self.close_all(source)
        if cmd == "stop":
            return await self.emergency_stop(True, source)
        if cmd == "go":
            return await self.emergency_stop(False, source)
        if cmd in {"help", "start", "?"}:
            return HELP_TEXT
        return None

    # ----- helpers ---------------------------------------------------------
    async def _announce(self, text: str) -> None:
        if self.notifier:
            await self.notifier.notify(text)

    def _write_control(self) -> None:
        try:
            path = self.cfg.control.control_file
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"paused": self.paused, "ts": round(time.time(), 3)}, fh)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _touch(path: str, note: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(note + "\n")

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


class CommandBus:
    """Tails the JSONL command file and dispatches to the controller."""

    def __init__(self, cfg: BotConfig, controller: Controller) -> None:
        self.cfg = cfg
        self.controller = controller
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._offset = 0
        f = cfg.control.command_file
        if os.path.exists(f):
            self._offset = os.path.getsize(f)  # ignore pre-existing commands

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        f = self.cfg.control.command_file
        while not self._stop.is_set():
            try:
                if os.path.exists(f):
                    size = os.path.getsize(f)
                    if size < self._offset:
                        self._offset = 0
                    if size > self._offset:
                        with open(f, encoding="utf-8") as fh:
                            fh.seek(self._offset)
                            lines = fh.readlines()
                            self._offset = fh.tell()
                        for line in lines:
                            await self._dispatch(line)
            except Exception as exc:  # noqa: BLE001
                log.debug("command bus error: %s", exc)
            await asyncio.sleep(self.cfg.control.poll_interval)

    async def _dispatch(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
        cmd = data.get("cmd")
        source = data.get("source", "bus")
        log.info("Command from %s: %s", source, cmd)
        await self.controller.handle_command(cmd, source=source)


def send_command(command_file: str, cmd: str, source: str = "cli",
                 **args: Any) -> None:
    """Append a command for the running bot to pick up (used by dashboard/CLI)."""

    os.makedirs(os.path.dirname(command_file) or ".", exist_ok=True)
    rec = {"ts": round(time.time(), 3), "cmd": cmd, "source": source, "args": args}
    with open(command_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
