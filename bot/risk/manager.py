"""Risk management: position sizing, daily-loss protection and validation.

Sizing is done from the stop-loss distance so the configured ``risk_per_signal``
fraction of equity is what's actually at risk if every position hits SL. Risk is
split evenly across the positions opened for a signal (one per take-profit).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..config import IntelligenceConfig, RiskConfig
from ..logger import get_logger
from ..models import EntrySignal
from ..mt5.executor import SymbolSpec

log = get_logger("risk")


@dataclass
class DailyGuard:
    day: str
    start_balance: float
    realized_loss: float = 0.0


class RiskManager:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg
        self._guard: Optional[DailyGuard] = None
        self.halted = False

    # ----- daily loss protection ------------------------------------------
    def start_day(self, balance: float) -> None:
        today = time.strftime("%Y-%m-%d")
        if self._guard is None or self._guard.day != today:
            self._guard = DailyGuard(day=today, start_balance=balance)
            self.halted = False
            log.info("Daily guard initialised: start_balance=%.2f", balance)

    def register_realized_pl(self, pl: float) -> None:
        if self._guard is None:
            return
        if pl < 0:
            self._guard.realized_loss += abs(pl)
        self._check_daily_limit()

    # ----- adaptive throttle ----------------------------------------------
    def adaptive_multiplier(self, perf: dict, intel: IntelligenceConfig) -> tuple:
        """Bounded risk multiplier from recent performance.

        Reduces size after a losing streak or while in journal drawdown, and
        restores toward full size as results recover. Never scales above base.
        Returns ``(multiplier, reasons)``.
        """

        if not intel.adaptive_risk or not perf or perf.get("trades", 0) == 0:
            return 1.0, []
        mult = 1.0
        reasons = []

        streak = perf.get("streak", 0)
        if streak <= -intel.loss_streak_throttle:
            mult *= intel.throttle_factor
            reasons.append(f"{-streak} loss streak → ×{intel.throttle_factor}")

        dd = perf.get("max_drawdown", 0.0)
        start = self._guard.start_balance if self._guard else 0.0
        if start and dd >= start * intel.drawdown_throttle:
            mult *= intel.throttle_factor
            reasons.append(f"drawdown {dd:.0f} → ×{intel.throttle_factor}")

        mult = max(intel.min_size_multiplier, min(intel.max_size_multiplier, mult))
        return round(mult, 3), reasons

    def _check_daily_limit(self) -> None:
        if not self._guard or self._guard.start_balance <= 0:
            return
        limit = self._guard.start_balance * self.cfg.max_daily_loss
        if self._guard.realized_loss >= limit and not self.halted:
            self.halted = True
            log.critical(
                "DAILY LOSS LIMIT HIT: lost %.2f >= limit %.2f — trading halted.",
                self._guard.realized_loss, limit,
            )

    # ----- validation ------------------------------------------------------
    def can_open_new_signal(self, open_signal_count: int) -> Tuple[bool, str]:
        if self.halted:
            return False, "daily loss limit reached"
        if open_signal_count >= self.cfg.max_open_signals:
            return False, f"max open signals ({self.cfg.max_open_signals}) reached"
        return True, "ok"

    def validate_entry(self, entry: EntrySignal, market_price: float) -> Tuple[bool, str]:
        if entry.entry_low is None and entry.entry_high is None:
            return False, "no entry price"
        if entry.sl is None:
            log.warning("Entry has no SL — will use fallback lot and no risk sizing.")
        # Sanity: SL must be on the correct side of entry.
        if entry.sl is not None and entry.entry_mid is not None:
            if entry.direction.value == "BUY" and entry.sl >= entry.entry_mid:
                return False, "BUY SL is not below entry"
            if entry.direction.value == "SELL" and entry.sl <= entry.entry_mid:
                return False, "SELL SL is not above entry"
        return True, "ok"

    # ----- position sizing -------------------------------------------------
    def size_positions(
        self,
        entry: EntrySignal,
        equity: float,
        spec: SymbolSpec,
        num_positions: int,
    ) -> List[float]:
        """Return a list of lot sizes, one per position to open."""

        num_positions = max(1, num_positions)
        if entry.sl is None or entry.entry_mid is None:
            return [self._clamp(spec, self.cfg.fallback_lot)] * num_positions

        sl_distance = abs(entry.entry_mid - entry.sl)
        if sl_distance <= 0:
            return [self._clamp(spec, self.cfg.fallback_lot)] * num_positions

        risk_money = max(0.0, equity * self.cfg.risk_per_signal)
        # Loss per 1.0 lot if SL is hit = (distance / tick_size) * tick_value.
        ticks = sl_distance / (spec.tick_size or spec.point or 0.01)
        loss_per_lot = ticks * (spec.tick_value or 1.0)
        if loss_per_lot <= 0:
            return [self._clamp(spec, self.cfg.fallback_lot)] * num_positions

        total_lots = risk_money / loss_per_lot
        per_position = total_lots / num_positions
        lot = self._clamp(spec, per_position)
        log.info(
            "Sizing: equity=%.2f risk=%.2f sl_dist=%.3f loss/lot=%.2f "
            "total_lots=%.3f per_pos=%.3f → %.2f",
            equity, risk_money, sl_distance, loss_per_lot, total_lots,
            per_position, lot,
        )
        return [lot] * num_positions

    def _clamp(self, spec: SymbolSpec, lot: float) -> float:
        lot = max(self.cfg.min_lot, min(lot, self.cfg.max_lot))
        lot = max(spec.volume_min, min(lot, spec.volume_max))
        step = spec.volume_step or 0.01
        steps = max(1, int(lot / step + 1e-9))
        return round(steps * step, 2)
