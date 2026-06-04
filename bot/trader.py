"""Trade dispatcher: turns parsed :class:`Intent`s into MT5 actions.

This is the bridge between the parser and the executor. It owns no I/O of its
own beyond delegating to :class:`MT5Executor`, :class:`StateManager` and
:class:`RiskManager`, which keeps the trading logic easy to read and test.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import List, Optional

from .analytics.performance import PerformanceTracker, TradeRecord
from .config import BotConfig
from .intelligence.scorer import SignalScorer
from .logger import audit, get_logger
from .models import EntrySignal, Intent, IntentType, TakeProfit
from .mt5.executor import MT5Executor
from .risk.manager import RiskManager
from .state.manager import StateManager, TrackedPosition, TrackedSignal

log = get_logger("trader")


class Trader:
    def __init__(
        self,
        cfg: BotConfig,
        executor: MT5Executor,
        state: StateManager,
        risk: RiskManager,
        tracker: Optional[PerformanceTracker] = None,
        scorer: Optional[SignalScorer] = None,
        filters=None,
    ) -> None:
        self.cfg = cfg
        self.ex = executor
        self.state = state
        self.risk = risk
        self.tracker = tracker
        self.scorer = scorer
        self.filters = filters
        # Optional async callback fired after a position is opened/closed so the
        # app can push Telegram alerts without the trader knowing about it.
        self.on_trade = None
        self._lock = asyncio.Lock()

    # ----- top-level dispatch ---------------------------------------------
    async def handle(self, intents: List[Intent]) -> None:
        for intent in intents:
            try:
                await self._dispatch(intent)
            except Exception as exc:  # noqa: BLE001
                log.exception("Failed handling intent %s: %s", intent, exc)
                audit("intent_error", intent=repr(intent), error=str(exc))

    async def _dispatch(self, intent: Intent) -> None:
        audit("intent", type=intent.type.value, rule=intent.matched_rule,
              confidence=intent.confidence, message_id=intent.message_id,
              detail=repr(intent))
        if intent.type is IntentType.NOISE:
            log.debug("Noise ignored: %r", intent.raw_text[:80])
            return

        if self._emergency_stop():
            log.critical("EMERGENCY STOP active — ignoring %s.", intent.type.value)
            return

        async with self._lock:  # serialise mutations to state/positions
            if intent.type is IntentType.ENTRY:
                await self._do_entry(intent)
            elif intent.type is IntentType.MODIFY_SL:
                await self._do_modify_sl(intent)
            elif intent.type is IntentType.BREAKEVEN:
                await self._do_breakeven(intent)
            elif intent.type is IntentType.PARTIAL_CLOSE:
                await self._do_partial(intent)
            elif intent.type is IntentType.CLOSE_ALL:
                await self._do_close_all(intent)
            elif intent.type is IntentType.TP_HIT:
                await self._do_tp_hit(intent)

    # ----- ENTRY -----------------------------------------------------------
    async def _do_entry(self, intent: Intent) -> None:
        entry = intent.entry
        if entry is None:
            return

        if self._is_paused():
            log.warning("Paused — entry ignored.")
            audit("entry_rejected", reason="paused", detail=repr(intent))
            return

        ok, reason = self.risk.can_open_new_signal(len(self.state.active_signals()))
        if not ok:
            log.warning("Entry rejected: %s", reason)
            audit("entry_rejected", reason=reason, detail=repr(intent))
            return

        # Condition filters: sessions / news blackout.
        if self.filters is not None:
            allowed, why = self.filters.allowed()
            if not allowed:
                log.warning("Entry blocked by filter: %s", why)
                audit("entry_rejected", reason=f"filter:{why}", detail=repr(intent))
                return

        price_info = await self.ex.current_price()
        market = price_info["ask"] if entry.direction.value == "BUY" else price_info["bid"]

        # Spread guard.
        max_spread = self.cfg.execution.max_spread_pips
        if max_spread > 0:
            spread_pips = (price_info["ask"] - price_info["bid"]) / self.cfg.risk.pip_size
            if spread_pips > max_spread:
                log.warning("Entry blocked: spread %.1f > max %.1f pips",
                            spread_pips, max_spread)
                audit("entry_rejected", reason="spread", spread_pips=round(spread_pips, 1))
                return
        valid, reason = self.risk.validate_entry(entry, market)
        if not valid:
            log.warning("Entry failed validation: %s", reason)
            audit("entry_invalid", reason=reason, detail=repr(intent))
            return

        # --- Intelligence: score the signal and derive a size multiplier ----
        perf = self.tracker.summary() if self.tracker else {}
        size_mult = 1.0
        if self.scorer and self.cfg.intelligence.enabled:
            score = self.scorer.score(entry, market, perf)
            audit("signal_score", message_id=intent.message_id,
                  **score.as_dict())
            if not score.take:
                log.warning("Entry skipped by scorer (%.2f): %s",
                            score.value, "; ".join(score.reasons))
                return
            size_mult *= score.size_factor
            log.info("Signal score %.2f → size_factor %.2f (%s)",
                     score.value, score.size_factor, "; ".join(score.reasons))

        # --- Adaptive risk: throttle off recent performance -----------------
        adj, why = self.risk.adaptive_multiplier(perf, self.cfg.intelligence)
        if adj != 1.0:
            size_mult *= adj
            log.info("Adaptive risk ×%.2f (%s)", adj, "; ".join(why))
        size_mult = max(self.cfg.intelligence.min_size_multiplier, size_mult)

        # Decide how many positions to open: one per concrete TP (+ runner),
        # unless configured for a single position.
        legs = self._plan_legs(entry)
        spec = self.ex.spec
        equity = await self.ex.account_equity()
        base_lots = self.risk.size_positions(entry, equity, spec, len(legs))
        lots = [self.ex.normalize_volume(l * size_mult) for l in base_lots]

        sig = self.state.create_signal(entry, intent.message_id)
        log.info(
            "ENTRY signal #%d %s %s zone=%s-%s sl=%s legs=%d lots=%s size×%.2f%s",
            sig.signal_id, entry.direction.value, entry.order_kind.value,
            entry.entry_low, entry.entry_high, entry.sl, len(legs), lots,
            size_mult, " [DRY-RUN]" if self.cfg.dry_run else "",
        )
        self._alert("entry", signal_id=sig.signal_id, direction=entry.direction.value,
                    symbol=entry.symbol, zone=(entry.entry_low, entry.entry_high),
                    sl=entry.sl, legs=len(legs), dry_run=self.cfg.dry_run)

        for leg, lot in zip(legs, lots):
            tp_price = leg.price
            # For an open runner with a hinted min distance, project a TP.
            if leg.is_open and leg.min_pips and entry.entry_mid:
                pip = self.cfg.risk.pip_size
                if entry.direction.value == "BUY":
                    tp_price = round(entry.entry_mid + leg.min_pips * pip, 3)
                else:
                    tp_price = round(entry.entry_mid - leg.min_pips * pip, 3)

            limit_price = self._leg_entry_price(entry, leg, legs)
            comment = f"GTMO#{sig.signal_id}-{leg.label}"
            res = await self.ex.open_position(
                direction=entry.direction.value,
                volume=lot,
                price=limit_price,
                sl=entry.sl,
                tp=tp_price if not leg.is_open or tp_price else None,
                order_kind=entry.order_kind.value,
                magic=sig.magic,
                comment=comment,
            )
            if res.ok and res.ticket:
                open_price = res.price if res.price else (limit_price or market)
                vol = res.volume or lot
                contract = spec.contract_size if spec else 100.0
                risk_amount = (abs(open_price - entry.sl) * vol * contract
                               if entry.sl is not None else 0.0)
                self.state.add_position(
                    sig.signal_id,
                    TrackedPosition(
                        ticket=res.ticket,
                        tp_label=leg.label or "TP",
                        tp_price=tp_price,
                        volume=vol,
                        sl=entry.sl,
                        is_open_runner=leg.is_open and not tp_price,
                        open_price=open_price,
                        risk_amount=round(risk_amount, 2),
                    ),
                )
            else:
                log.error("Leg %s open failed: %s", leg.label, res.comment)

    def _plan_legs(self, entry: EntrySignal) -> List[TakeProfit]:
        if not self.cfg.risk.one_position_per_tp:
            concrete = entry.concrete_tps
            return [concrete[0]] if concrete else [TakeProfit(price=None, label="TP1")]
        legs = list(entry.take_profits)
        if not legs:
            legs = [TakeProfit(price=None, label="TP1")]
        return legs

    def _leg_entry_price(self, entry: EntrySignal, leg: TakeProfit,
                         legs: List[TakeProfit]) -> Optional[float]:
        """Price for pending orders; ``None`` => market fill.

        For a LIMIT/STOP entry across a zone we spread the legs evenly between
        the zone bounds so the basket is filled across the range.
        """

        if entry.order_kind.value == "MARKET":
            return None
        lo, hi = entry.entry_low, entry.entry_high
        if lo is None or hi is None or lo == hi:
            return lo or hi
        n = len(legs)
        try:
            i = legs.index(leg)
        except ValueError:
            i = 0
        if n == 1:
            return round((lo + hi) / 2, 3)
        frac = i / (n - 1)
        return round(lo + (hi - lo) * frac, 3)

    # ----- MODIFY SL -------------------------------------------------------
    async def _do_modify_sl(self, intent: Intent) -> None:
        if intent.new_sl is None:
            return
        targets = self.state.targets(self._applies_all(intent))
        if not targets:
            log.warning("MODIFY_SL but no active signal to apply to.")
            return
        for sig in targets:
            for pos in sig.open_positions():
                res = await self.ex.modify_position(pos.ticket, sl=intent.new_sl)
                if res.ok:
                    log.info("Signal #%d ticket %s SL → %s", sig.signal_id,
                             pos.ticket, intent.new_sl)
            self.state.update_sl(sig.signal_id, intent.new_sl)

    # ----- BREAKEVEN -------------------------------------------------------
    async def _do_breakeven(self, intent: Intent) -> None:
        targets = self.state.targets(self._applies_all(intent))
        if not targets:
            log.warning("BREAKEVEN but no active signal.")
            return
        for sig in targets:
            be = sig.avg_entry
            if be is None:
                continue
            for pos in sig.open_positions():
                res = await self.ex.modify_position(pos.ticket, sl=be)
                if res.ok:
                    log.info("Signal #%d ticket %s → breakeven @ %s",
                             sig.signal_id, pos.ticket, be)
            self.state.update_sl(sig.signal_id, be)
            self.state.mark_breakeven(sig.signal_id)

    # ----- PARTIAL CLOSE ---------------------------------------------------
    async def _do_partial(self, intent: Intent) -> None:
        targets = self.state.targets(self._applies_all(intent))
        if not targets:
            log.warning("PARTIAL_CLOSE but no active signal.")
            return
        frac = max(0.05, min(intent.close_fraction, 0.95))
        for sig in targets:
            for pos in sig.open_positions():
                close_vol = self.ex.normalize_volume(pos.volume * frac)
                if close_vol <= 0:
                    continue
                res = await self.ex.close_position(pos.ticket, volume=close_vol)
                if res.ok:
                    remaining = round(pos.volume - (res.volume or close_vol), 2)
                    self._record_trade(sig, pos, res, "partial",
                                       closed_volume=res.volume or close_vol)
                    self.state.mark_position_closed(sig.signal_id, pos.ticket,
                                                    remaining_volume=remaining)
                    log.info("Signal #%d ticket %s partial close %.2f (%.0f%%) pl=%s",
                             sig.signal_id, pos.ticket, close_vol, frac * 100,
                             res.profit)

    # ----- CLOSE ALL -------------------------------------------------------
    async def _do_close_all(self, intent: Intent) -> None:
        targets = self.state.targets(self._applies_all(intent))
        if not targets:
            # "close all" with no tracked signal → flatten everything by magic.
            log.warning("CLOSE_ALL but no tracked signal; nothing to do.")
            return
        for sig in targets:
            for pos in sig.open_positions():
                res = await self.ex.close_position(pos.ticket)
                if res.ok:
                    self._record_trade(sig, pos, res, "manual")
                    self.state.mark_position_closed(sig.signal_id, pos.ticket)
            self.state.close_signal(sig.signal_id)
            log.info("Signal #%d closed (CLOSE_ALL).", sig.signal_id)

    # ----- TP HIT ----------------------------------------------------------
    async def _do_tp_hit(self, intent: Intent) -> None:
        # The broker's TP order closes the leg automatically. We record the
        # outcome and reconcile state. In simulation (no broker), close the
        # referenced leg at its TP so the journal reflects the win.
        targets = self.state.targets(self._applies_all(intent))
        for sig in targets:
            if intent.tp_index:
                self.state.record_tp_hit(sig.signal_id, intent.tp_index)
            if self.ex.simulate and intent.tp_index:
                await self._sim_fill_tp(sig, intent.tp_index)
            await self._reconcile(sig)
        log.info("TP_HIT recorded (tp_index=%s).", intent.tp_index)

    async def _sim_fill_tp(self, sig: TrackedSignal, tp_index: int) -> None:
        """Simulate the broker filling the leg whose label is TP<index>."""

        label = f"TP{tp_index}"
        for pos in sig.open_positions():
            if pos.tp_label == label and pos.tp_price:
                res = await self.ex.close_position(pos.ticket, price=pos.tp_price)
                if res.ok:
                    self._record_trade(sig, pos, res, "tp")
                    self.state.mark_position_closed(sig.signal_id, pos.ticket)
                    log.info("Sim TP fill: signal #%d %s @ %s pl=%s",
                             sig.signal_id, label, pos.tp_price, res.profit)

    async def _reconcile(self, sig: TrackedSignal) -> None:
        """Sync tracked positions with the broker (legs closed by TP orders)."""

        live = await self.ex.positions_by_magic(sig.magic)
        live_tickets = {p["ticket"] for p in live}
        for pos in sig.open_positions():
            if pos.ticket not in live_tickets:
                # Closed at broker (TP/SL) — pull its realized P/L for the journal.
                profit = await self.ex.position_profit(pos.ticket)
                self._record_trade(sig, pos, None, "tp", profit=profit)
                self.state.mark_position_closed(sig.signal_id, pos.ticket)
                log.info("Reconciled: signal #%d ticket %s closed at broker pl=%s.",
                         sig.signal_id, pos.ticket, profit)

    # ----- trade journaling ------------------------------------------------
    def _record_trade(self, sig: TrackedSignal, pos: TrackedPosition, res,
                      reason: str, closed_volume: Optional[float] = None,
                      profit: Optional[float] = None) -> None:
        if not self.tracker:
            return
        pl = profit if profit is not None else (res.profit if res else None)
        if pl is None:
            return
        vol = closed_volume if closed_volume is not None else (
            (res.volume if res and res.volume else pos.volume))
        # Risk for this slice scales with the fraction of the leg closed.
        frac = (vol / pos.volume) if pos.volume else 1.0
        risk_slice = round(pos.risk_amount * frac, 2)
        close_price = (res.price if res and res.price else pos.tp_price) or 0.0
        self.tracker.record(TradeRecord(
            signal_id=sig.signal_id,
            ticket=pos.ticket,
            direction=sig.direction,
            symbol=sig.symbol,
            label=pos.tp_label,
            volume=round(vol, 2),
            open_price=pos.open_price or 0.0,
            close_price=close_price,
            profit=round(pl, 2),
            risk_amount=risk_slice,
            reason=reason,
            opened_at=pos.opened_at,
        ))
        # Feed realized P/L into the daily-loss guard too.
        self.risk.register_realized_pl(pl)
        self._alert("close", signal_id=sig.signal_id, label=pos.tp_label,
                    direction=sig.direction, profit=round(pl, 2), reason=reason)

    def _alert(self, event: str, **data) -> None:
        """Fire the optional trade-alert callback (set by the app)."""

        if self.on_trade:
            try:
                self.on_trade({"event": event, **data})
            except Exception as exc:  # noqa: BLE001
                log.debug("alert callback failed: %s", exc)

    # ----- helpers ---------------------------------------------------------
    @staticmethod
    def _applies_all(intent: Intent) -> bool:
        from .parser import patterns as P  # local import to avoid cycle

        return bool(P.RE_ALL_ENTRIES.search(intent.raw_text or ""))

    def _emergency_stop(self) -> bool:
        return os.path.exists(self.cfg.emergency_stop_file)

    def _is_paused(self) -> bool:
        return os.path.exists(self.cfg.control.pause_file)

    # ----- public control API ---------------------------------------------
    async def close_everything(self, reason: str = "manual") -> int:
        """Close every open position across all active signals. Returns count."""

        closed = 0
        async with self._lock:
            for sig in self.state.active_signals():
                for pos in sig.open_positions():
                    res = await self.ex.close_position(pos.ticket)
                    if res.ok:
                        self._record_trade(sig, pos, res, reason)
                        self.state.mark_position_closed(sig.signal_id, pos.ticket)
                        closed += 1
                self.state.close_signal(sig.signal_id)
        log.info("close_everything(%s): closed %d position(s).", reason, closed)
        audit("close_everything", reason=reason, closed=closed)
        return closed
