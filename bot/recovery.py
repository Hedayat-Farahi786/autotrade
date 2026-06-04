"""Startup reconciliation / crash recovery.

After a restart the persisted state may disagree with the broker. This module
brings them back into sync:

* tracked positions the broker no longer has are marked closed (and their P/L
  journaled), and
* live broker positions carrying one of our magic numbers that we *aren't*
  tracking are **adopted** back into state, so trailing/management resumes.

Run automatically on startup; safe to call repeatedly.
"""
from __future__ import annotations

from .logger import audit, get_logger
from .mt5.executor import MT5Executor
from .state.manager import StateManager, TrackedPosition, TrackedSignal

log = get_logger("recovery")


async def reconcile_on_startup(
    executor: MT5Executor,
    state: StateManager,
    magic_base: int,
    tracker=None,
    magic_span: int = 100000,
) -> dict:
    live = await executor.all_positions()
    by_ticket = {p["ticket"]: p for p in live}
    live_tickets = set(by_ticket)

    closed = 0
    adopted = 0

    # 1) Tracked positions that have vanished at the broker → mark closed.
    for sig in state.active_signals():
        for pos in list(sig.open_positions()):
            if pos.ticket not in live_tickets:
                profit = await executor.position_profit(pos.ticket)
                if tracker and profit is not None:
                    _journal(tracker, sig, pos, profit)
                state.mark_position_closed(sig.signal_id, pos.ticket)
                closed += 1
                log.info("Recovery: tracked ticket %s gone at broker → closed.",
                         pos.ticket)

    # 2) Broker positions with our magic that we don't track → adopt.
    tracked_tickets = {p.ticket for s in state.active_signals()
                       for p in s.positions}
    for ticket, p in by_ticket.items():
        magic = p.get("magic", 0)
        if ticket in tracked_tickets:
            continue
        if not (magic_base < magic <= magic_base + magic_span):
            continue  # not ours
        _adopt(state, p, magic_base)
        adopted += 1
        log.warning("Recovery: adopted untracked broker position %s (magic %s).",
                    ticket, magic)

    if closed or adopted:
        audit("recovery", closed=closed, adopted=adopted)
        log.info("Recovery complete: %d closed, %d adopted.", closed, adopted)
    return {"closed": closed, "adopted": adopted}


def _adopt(state: StateManager, p: dict, magic_base: int) -> None:
    """Recreate a minimal tracked signal around an orphan broker position."""

    from .models import Direction, EntrySignal, OrderKind

    direction = Direction.BUY if p.get("type", 0) == 0 else Direction.SELL
    entry = EntrySignal(
        direction=direction,
        symbol=p.get("symbol", "XAUUSD"),
        order_kind=OrderKind.MARKET,
        entry_low=p.get("price_open"),
        entry_high=p.get("price_open"),
        sl=p.get("sl") or None,
        take_profits=[],
    )
    sig = state.create_signal(entry, message_id=None)
    state.add_position(sig.signal_id, TrackedPosition(
        ticket=p["ticket"],
        tp_label="ADOPTED",
        tp_price=p.get("tp") or None,
        volume=p.get("volume", 0.0),
        sl=p.get("sl") or None,
        open_price=p.get("price_open"),
    ))


def _journal(tracker, sig: TrackedSignal, pos: TrackedPosition,
             profit: float) -> None:
    from .analytics.performance import TradeRecord

    tracker.record(TradeRecord(
        signal_id=sig.signal_id, ticket=pos.ticket, direction=sig.direction,
        symbol=sig.symbol, label=pos.tp_label, volume=pos.volume,
        open_price=pos.open_price or 0.0, close_price=pos.tp_price or 0.0,
        profit=round(profit, 2), risk_amount=pos.risk_amount, reason="recovery",
        opened_at=pos.opened_at,
    ))
