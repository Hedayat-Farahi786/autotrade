"""Trade dispatcher: turns parsed :class:`Intent`s into MT5 actions.

This is the bridge between the parser and the executor. It owns no I/O of its
own beyond delegating to :class:`MT5Executor`, :class:`StateManager` and
:class:`RiskManager`, which keeps the trading logic easy to read and test.
"""
from __future__ import annotations

import asyncio
import os
from typing import List, Optional

from .config import BotConfig
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
    ) -> None:
        self.cfg = cfg
        self.ex = executor
        self.state = state
        self.risk = risk
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

        ok, reason = self.risk.can_open_new_signal(len(self.state.active_signals()))
        if not ok:
            log.warning("Entry rejected: %s", reason)
            audit("entry_rejected", reason=reason, detail=repr(intent))
            return

        price_info = await self.ex.current_price()
        market = price_info["ask"] if entry.direction.value == "BUY" else price_info["bid"]
        valid, reason = self.risk.validate_entry(entry, market)
        if not valid:
            log.warning("Entry failed validation: %s", reason)
            audit("entry_invalid", reason=reason, detail=repr(intent))
            return

        # Decide how many positions to open: one per concrete TP (+ runner),
        # unless configured for a single position.
        legs = self._plan_legs(entry)
        spec = self.ex.spec
        equity = await self.ex.account_equity()
        lots = self.risk.size_positions(entry, equity, spec, len(legs))

        sig = self.state.create_signal(entry, intent.message_id)
        log.info(
            "ENTRY signal #%d %s %s zone=%s-%s sl=%s legs=%d lots=%s%s",
            sig.signal_id, entry.direction.value, entry.order_kind.value,
            entry.entry_low, entry.entry_high, entry.sl, len(legs), lots,
            " [DRY-RUN]" if self.cfg.dry_run else "",
        )

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
                self.state.add_position(
                    sig.signal_id,
                    TrackedPosition(
                        ticket=res.ticket,
                        tp_label=leg.label or "TP",
                        tp_price=tp_price,
                        volume=res.volume or lot,
                        sl=entry.sl,
                        is_open_runner=leg.is_open and not tp_price,
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
                    self.state.mark_position_closed(sig.signal_id, pos.ticket,
                                                    remaining_volume=remaining)
                    log.info("Signal #%d ticket %s partial close %.2f (%.0f%%)",
                             sig.signal_id, pos.ticket, close_vol, frac * 100)

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
                    self.state.mark_position_closed(sig.signal_id, pos.ticket)
            self.state.close_signal(sig.signal_id)
            log.info("Signal #%d closed (CLOSE_ALL).", sig.signal_id)

    # ----- TP HIT (housekeeping) ------------------------------------------
    async def _do_tp_hit(self, intent: Intent) -> None:
        # Informational: the broker's TP order closes the leg automatically.
        # We record it and reconcile our state with the broker.
        targets = self.state.targets(self._applies_all(intent))
        for sig in targets:
            if intent.tp_index:
                self.state.record_tp_hit(sig.signal_id, intent.tp_index)
            await self._reconcile(sig)
        log.info("TP_HIT recorded (tp_index=%s).", intent.tp_index)

    async def _reconcile(self, sig: TrackedSignal) -> None:
        """Sync tracked positions with the broker (legs closed by TP orders)."""

        live = await self.ex.positions_by_magic(sig.magic)
        live_tickets = {p["ticket"] for p in live}
        for pos in sig.open_positions():
            if pos.ticket not in live_tickets:
                self.state.mark_position_closed(sig.signal_id, pos.ticket)
                log.info("Reconciled: signal #%d ticket %s closed at broker.",
                         sig.signal_id, pos.ticket)

    # ----- helpers ---------------------------------------------------------
    @staticmethod
    def _applies_all(intent: Intent) -> bool:
        from .parser import patterns as P  # local import to avoid cycle

        return bool(P.RE_ALL_ENTRIES.search(intent.raw_text or ""))

    def _emergency_stop(self) -> bool:
        return os.path.exists(self.cfg.emergency_stop_file)
