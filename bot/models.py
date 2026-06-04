"""Core data models shared across the bot.

Everything the parser produces and the executor consumes is described here so
the contract between modules is explicit and easy to extend.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderKind(str, Enum):
    """How the entry should hit the market."""

    MARKET = "MARKET"   # "buy now"
    LIMIT = "LIMIT"     # "buy limit" / waiting for a pullback into the zone
    STOP = "STOP"       # "buy stop" / breakout


class IntentType(str, Enum):
    """The actionable command extracted from a Telegram message."""

    ENTRY = "ENTRY"               # open a new signal/position set
    MODIFY_SL = "MODIFY_SL"       # "Adjust SL to 4462"
    BREAKEVEN = "BREAKEVEN"       # "set breakeven for zero risk"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"  # "take some profits"
    TP_HIT = "TP_HIT"             # "TP1 has been touched" (informational / housekeeping)
    CLOSE_ALL = "CLOSE_ALL"       # "close everything"
    NOISE = "NOISE"               # motivational text / charts / disclaimers


@dataclass
class TakeProfit:
    """A single take-profit level.

    ``price`` is ``None`` for open-ended runners ("TP: open (100+ pips)"); in
    that case ``min_pips`` carries the floor distance the channel hinted at.
    """

    price: float | None = None
    min_pips: float | None = None
    label: str | None = None  # e.g. "TP1"

    @property
    def is_open(self) -> bool:
        return self.price is None


@dataclass
class EntrySignal:
    """A fully parsed entry instruction."""

    direction: Direction
    symbol: str = "XAUUSD"
    order_kind: OrderKind = OrderKind.MARKET
    entry_low: float | None = None
    entry_high: float | None = None
    sl: float | None = None
    take_profits: list[TakeProfit] = field(default_factory=list)

    @property
    def entry_mid(self) -> float | None:
        lo, hi = self.entry_low, self.entry_high
        if lo is None and hi is None:
            return None
        if lo is None:
            return hi
        if hi is None:
            return lo
        return round((lo + hi) / 2.0, 3)

    @property
    def concrete_tps(self) -> list[TakeProfit]:
        return [tp for tp in self.take_profits if not tp.is_open]


@dataclass
class Intent:
    """Normalised, executable command produced by the parser.

    Only the fields relevant to ``type`` are populated. ``raw_text`` and
    ``message_id`` are always carried for auditability.
    """

    type: IntentType
    raw_text: str = ""
    message_id: int | None = None
    confidence: float = 1.0

    # ENTRY
    entry: EntrySignal | None = None

    # MODIFY_SL
    new_sl: float | None = None

    # PARTIAL_CLOSE
    close_fraction: float = 0.5  # default "take some profits" → halve exposure

    # TP_HIT
    tp_index: int | None = None  # 1-based TP number referenced in the text

    # debugging / explainability
    matched_rule: str | None = None
    created_at: float = field(default_factory=time.time)

    def __repr__(self) -> str:  # concise, log-friendly
        bits = [f"type={self.type.value}", f"rule={self.matched_rule}"]
        if self.entry:
            e = self.entry
            bits.append(
                f"{e.direction.value} {e.order_kind.value} "
                f"zone={e.entry_low}-{e.entry_high} sl={e.sl} "
                f"tps={[tp.price or 'open' for tp in e.take_profits]}"
            )
        if self.new_sl is not None:
            bits.append(f"new_sl={self.new_sl}")
        if self.type is IntentType.PARTIAL_CLOSE:
            bits.append(f"close_fraction={self.close_fraction}")
        if self.tp_index is not None:
            bits.append(f"tp_index={self.tp_index}")
        return f"<Intent {' '.join(bits)}>"
