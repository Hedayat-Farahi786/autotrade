"""Application orchestrator: wires every component together and runs the bot.

Flow:  Telegram message → parser (regex/AI/hybrid) → intents → Trader → MT5.

The whole pipeline is asyncio-native. The only blocking dependency (the MT5
terminal) is isolated behind a dedicated worker thread in :mod:`bot.mt5`.
"""
from __future__ import annotations

import asyncio
import signal
from typing import List, Optional

from .config import BotConfig, get_config
from .logger import audit, get_logger, setup_logging
from .models import Intent
from .mt5.executor import MT5Executor
from .parser import build_parser
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
        self.trader = Trader(cfg, self.executor, self.state, self.risk)
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
        self._stopping = False

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
        await self.risk.start_day(await self.executor.account_balance())

        await self.listener.start()
        log.info("Bot is live. Listening for signals…")

    async def run(self) -> None:
        await self.start()
        await self.listener.run_forever()

    async def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        log.info("Shutting down…")
        audit("bot_stop")
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
