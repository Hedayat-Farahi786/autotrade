"""Application orchestrator: wires every component together and runs the bot.

Flow:  Telegram message → parser (regex/AI/hybrid) → intents → Trader → MT5.

The whole pipeline is asyncio-native. The only blocking dependency (the MT5
terminal) is isolated behind a dedicated worker thread in :mod:`bot.mt5`.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from typing import List, Optional

from .analytics.performance import PerformanceTracker
from .config import BotConfig, get_config
from .control import CommandBus, Controller, Notifier
from .execution.monitor import PositionMonitor
from .filters import TradingFilters
from .intelligence.review import ReviewLogger
from .intelligence.scorer import SignalScorer
from .logger import audit, get_logger, setup_logging
from .models import Intent
from .mt5.executor import MT5Executor
from .parser import build_parser
from .recovery import reconcile_on_startup
from .risk.manager import RiskManager
from .state.manager import StateManager
from .telegram.listener import TelegramListener
from .trader import Trader

log = get_logger("app")


class TradingBot:
    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        self.executor = MT5Executor(cfg.mt5, dry_run=cfg.dry_run)
        self.state = StateManager(cfg.state_file, magic_base=cfg.mt5.magic_base)
        self.risk = RiskManager(cfg.risk)
        self.tracker = PerformanceTracker(cfg.trades_file)
        self.scorer = SignalScorer(cfg.intelligence, pip_size=cfg.risk.pip_size)
        self.review = ReviewLogger(cfg.review_file)
        self.filters = TradingFilters(cfg.filters)
        self.trader = Trader(cfg, self.executor, self.state, self.risk,
                             tracker=self.tracker, scorer=self.scorer,
                             filters=self.filters)
        self.trader.on_trade = self._on_trade_event
        self.parser = build_parser(
            cfg.parser.mode,
            provider=cfg.parser.provider,
            default_symbol=cfg.mt5.symbol,
            anthropic_api_key=cfg.parser.anthropic_api_key,
            gemini_api_key=cfg.parser.gemini_api_key,
            anthropic_model=cfg.parser.anthropic_model,
            gemini_model=cfg.parser.gemini_model,
        )
        self.listener = TelegramListener(cfg.telegram, self._on_message)
        self.notifier = Notifier(enabled=cfg.control.alerts_enabled)
        self.controller = Controller(cfg, self.executor, self.state, self.trader,
                                     self.tracker, notifier=self.notifier)
        self.commands = CommandBus(cfg, self.controller)
        self.monitor = PositionMonitor(cfg.execution, self.executor, self.state,
                                       pip_size=cfg.risk.pip_size)
        self._stopping = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._summary_task: Optional[asyncio.Task] = None
        self._summary_day: Optional[str] = None

    # ----- message pipeline ------------------------------------------------
    async def _on_message(self, text: str, message_id: int) -> None:
        intents: List[Intent] = await self.parser.parse(text, message_id)
        if not intents:
            return
        actionable = [i for i in intents if i.type.value != "NOISE"]
        if actionable:
            log.info("Parsed %d actionable intent(s) from #%s: %s",
                     len(actionable), message_id,
                     ", ".join(i.type.value for i in actionable))
        # Self-learning: queue uncertain parses for human review.
        if self.cfg.intelligence.review_enabled:
            self.review.consider(text, intents, message_id)
        await self.trader.handle(intents)

    # ----- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        setup_logging(self.cfg.log_level, self.cfg.log_dir)
        log.info("Starting GTMO XAUUSD bot v%s (dry_run=%s, parser=%s/%s)",
                 _version(), self.cfg.dry_run, self.cfg.parser.mode,
                 self.cfg.parser.provider)
        audit("bot_start", dry_run=self.cfg.dry_run,
              parser_mode=self.cfg.parser.mode, provider=self.cfg.parser.provider)

        if not await self.executor.connect():
            raise RuntimeError("Failed to connect to MT5.")
        self.risk.start_day(await self.executor.account_balance())

        # Crash recovery: reconcile persisted state with the broker.
        try:
            await reconcile_on_startup(self.executor, self.state,
                                       self.cfg.mt5.magic_base, self.tracker)
        except Exception as exc:  # noqa: BLE001
            log.warning("Startup reconciliation failed: %s", exc)

        await self.listener.start()

        # Telegram alerts + remote control.
        if self.cfg.control.alerts_enabled or self.cfg.control.telegram_control_enabled:
            try:
                target = await self.listener.resolve(self.cfg.control.control_chat)
                self.notifier.bind(self.listener.client, target)
                if self.cfg.control.telegram_control_enabled:
                    self.listener.add_command_handler(
                        target, self.controller.handle_command)
                    log.info("Telegram control enabled on chat: %s",
                             getattr(target, "title", None)
                             or getattr(target, "username", None) or "Saved Messages")
                await self.notifier.notify(
                    f"🤖 GTMO bot online ({'DRY-RUN' if self.cfg.dry_run else 'LIVE'}, "
                    f"parser={self.cfg.parser.mode}/{self.cfg.parser.provider}).")
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not set up Telegram control/alerts: %s", exc)

        self.commands.start()
        self.monitor.start()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._summary_task = asyncio.create_task(self._summary_loop())
        log.info("Bot is live. Listening for signals…")

    # ----- alerts ----------------------------------------------------------
    def _on_trade_event(self, ev: dict) -> None:
        """Sync hook from the trader → schedule a Telegram alert."""

        if not self.cfg.control.alerts_enabled:
            return
        try:
            asyncio.get_running_loop().create_task(self._send_trade_alert(ev))
        except RuntimeError:
            pass  # no running loop (e.g. tests) → skip

    async def _send_trade_alert(self, ev: dict) -> None:
        kind = ev.get("event")
        if kind == "entry":
            lo, hi = ev.get("zone", (None, None))
            tag = " [DRY-RUN]" if ev.get("dry_run") else ""
            text = (f"🟢 ENTRY #{ev['signal_id']} {ev['direction']} "
                    f"{ev.get('symbol','XAUUSD')} {lo}-{hi} "
                    f"SL {ev.get('sl')} · {ev.get('legs')} legs{tag}")
        elif kind == "close":
            pl = ev.get("profit", 0.0)
            emoji = "✅" if pl >= 0 else "🔻"
            text = (f"{emoji} CLOSE #{ev['signal_id']} {ev.get('label')} "
                    f"{ev.get('reason')} · P/L {pl:+.2f}")
        else:
            return
        await self.notifier.notify(text)

    async def _summary_loop(self, interval: float = 300.0) -> None:
        """Send a once-a-day performance summary (UTC date rollover)."""

        if not self.cfg.control.daily_summary:
            return
        while not self._stopping:
            try:
                day = time.strftime("%Y-%m-%d", time.gmtime())
                if self._summary_day is None:
                    self._summary_day = day  # don't fire immediately on boot
                elif day != self._summary_day:
                    self._summary_day = day
                    await self.notifier.notify(
                        "📅 Daily summary\n" + self.controller.perf_text())
            except Exception as exc:  # noqa: BLE001
                log.debug("summary loop error: %s", exc)
            await asyncio.sleep(interval)

    async def _heartbeat_loop(self, interval: float = 3.0) -> None:
        """Periodically persist a status snapshot for the web dashboard."""

        while not self._stopping:
            try:
                await self._write_status()
            except Exception as exc:  # noqa: BLE001
                log.debug("Heartbeat write failed: %s", exc)
            await asyncio.sleep(interval)

    async def _write_status(self) -> None:
        actives = self.state.active_signals()
        snapshot = {
            "ts": round(time.time(), 3),
            "dry_run": self.cfg.dry_run,
            "parser_mode": self.cfg.parser.mode,
            "provider": self.cfg.parser.provider,
            "symbol": self.executor.symbol,
            "simulate": self.executor.simulate,
            "telegram_connected": bool(
                getattr(self.listener, "_client", None)
                and self.listener._client.is_connected()
            ),
            "mt5_connected": self.executor._connected,
            "balance": await self.executor.account_balance(),
            "equity": await self.executor.account_equity(),
            "halted": self.risk.halted,
            "daily_loss": (self.risk._guard.realized_loss if self.risk._guard else 0.0),
            "daily_start_balance": (
                self.risk._guard.start_balance if self.risk._guard else 0.0
            ),
            "max_daily_loss": self.cfg.risk.max_daily_loss,
            "open_signals": len(actives),
            "open_positions": sum(len(s.open_positions()) for s in actives),
            "emergency_stop": os.path.exists(self.cfg.emergency_stop_file),
            "paused": self.controller.paused,
            "performance": self.tracker.summary(),
            "review_queue": self.review.count,
            "intel_enabled": self.cfg.intelligence.enabled,
            "trailing_enabled": self.cfg.execution.trailing_enabled,
        }
        path = self.cfg.status_file
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2)
        os.replace(tmp, path)

    async def run(self) -> None:
        await self.start()
        await self.listener.run_forever()

    async def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        log.info("Shutting down…")
        audit("bot_stop")
        try:
            await self.notifier.notify("🛑 GTMO bot shutting down.")
        except Exception:  # noqa: BLE001
            pass
        for task in (self._heartbeat_task, self._summary_task):
            if task:
                task.cancel()
        await self.monitor.stop()
        await self.commands.stop()
        await self.listener.stop()
        await self.executor.shutdown()


def _version() -> str:
    from . import __version__

    return __version__


async def _amain() -> None:
    cfg = get_config(require_secrets=True)
    bot = TradingBot(cfg)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        log.info("Signal received; stopping.")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    runner = asyncio.create_task(bot.run())
    stopper = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {runner, stopper}, return_when=asyncio.FIRST_COMPLETED
    )
    await bot.shutdown()
    for task in pending:
        task.cancel()
    # Surface any exception from the runner.
    if runner in done:
        runner.result()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
