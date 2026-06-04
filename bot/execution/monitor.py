"""Active position management: trailing stop + profit lock.

Runs as a background asyncio task. On each pass it walks the open positions and,
once a position is far enough in profit, drags its stop-loss along behind price
(never loosening it). This protects open profit on the runner legs beyond the
channel's manual "breakeven" calls.

The maths are pip-based so they read naturally for XAUUSD (1 pip = 0.1 by
default). All SL modifications are idempotent and only sent when they actually
improve the stop, to avoid hammering the broker.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from ..config import ExecutionConfig
from ..logger import audit, get_logger
from ..mt5.executor import MT5Executor
from ..state.manager import StateManager

log = get_logger("monitor")


class PositionMonitor:
    def __init__(
        self,
        cfg: ExecutionConfig,
        executor: MT5Executor,
        state: StateManager,
        pip_size: float = 0.1,
    ) -> None:
        self.cfg = cfg
        self.ex = executor
        self.state = state
        self.pip = pip_size
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self.cfg.trailing_enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())
            log.info("Trailing monitor started (start=%.0f pips, distance=%.0f pips).",
                     self.cfg.trail_start_pips, self.cfg.trail_distance_pips)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._pass()
            except Exception as exc:  # noqa: BLE001
                log.debug("Monitor pass failed: %s", exc)
            await asyncio.sleep(self.cfg.monitor_interval)

    async def _pass(self) -> None:
        signals = self.state.active_signals()
        if not signals:
            return
        price = await self.ex.current_price()
        bid, ask = price.get("bid", 0.0), price.get("ask", 0.0)
        if not bid or not ask:
            return

        for sig in signals:
            is_buy = sig.direction == "BUY"
            for pos in sig.open_positions():
                if pos.open_price is None:
                    continue
                new_sl = self._trail_target(is_buy, pos.open_price, bid, ask)
                if new_sl is None:
                    continue
                if self._improves(is_buy, pos.sl, new_sl):
                    res = await self.ex.modify_position(pos.ticket, sl=round(new_sl, 3))
                    if res.ok:
                        self.state.update_position_sl(sig.signal_id, pos.ticket,
                                                      round(new_sl, 3))
                        audit("trail_sl", signal_id=sig.signal_id, ticket=pos.ticket,
                              new_sl=round(new_sl, 3))
                        log.info("Trailed signal #%d ticket %s SL → %.3f",
                                 sig.signal_id, pos.ticket, new_sl)

    def _trail_target(self, is_buy: bool, open_price: float, bid: float,
                      ask: float) -> Optional[float]:
        start = self.cfg.trail_start_pips * self.pip
        dist = self.cfg.trail_distance_pips * self.pip
        if is_buy:
            profit = bid - open_price
            if profit < start:
                return None
            return bid - dist
        else:
            profit = open_price - ask
            if profit < start:
                return None
            return ask + dist

    @staticmethod
    def _improves(is_buy: bool, current_sl: Optional[float], new_sl: float) -> bool:
        if current_sl is None:
            return True
        # Only tighten: raise SL for buys, lower SL for sells.
        return new_sl > current_sl if is_buy else new_sl < current_sl
