"""The intelligent, extensible signal parser.

Design goals
------------
* **Robust to noise** — motivational text, disclaimers and chart captions must
  never trigger a trade.
* **Compound aware** — one Telegram message can carry several instructions
  (e.g. *"TP2 smasssheddd take some more profits and set breakeven now"*), so
  :meth:`parse` returns a *list* of :class:`Intent` objects in execution order.
* **Easily updatable** — phrasing lives in :mod:`bot.parser.patterns`; adding a
  new wording is a one-line change there.

The parser is pure/synchronous and side-effect free, which makes it trivially
unit-testable (see ``tests/test_parser.py``).
"""
from __future__ import annotations

from typing import List, Optional

from ..logger import get_logger
from ..models import (
    Direction,
    EntrySignal,
    Intent,
    IntentType,
    OrderKind,
    TakeProfit,
)
from . import patterns as P

log = get_logger("parser")


def _to_float(token: Optional[str]) -> Optional[float]:
    if token is None:
        return None
    token = token.replace(",", "")
    try:
        return float(token)
    except ValueError:
        return None


class SignalParser:
    """Turns raw channel text into zero or more executable :class:`Intent`s."""

    def __init__(self, default_symbol: str = "XAUUSD") -> None:
        self.default_symbol = default_symbol

    # ------------------------------------------------------------------ API
    def parse(self, text: Optional[str], message_id: Optional[int] = None) -> List[Intent]:
        if not text or not text.strip():
            return []
        text = text.strip()

        # 1) An entry block is self-contained; if we detect one, return it alone.
        entry = self._try_entry(text)
        if entry is not None:
            intent = Intent(
                type=IntentType.ENTRY,
                raw_text=text,
                message_id=message_id,
                entry=entry,
                matched_rule="entry",
                confidence=self._entry_confidence(entry),
            )
            return [intent]

        # 2) Otherwise collect every management instruction present.
        intents: List[Intent] = []
        applies_all = bool(P.RE_ALL_ENTRIES.search(text))

        # TP-hit is informational housekeeping → emit first.
        tp_idx = self._tp_hit_index(text)
        if tp_idx is not None:
            intents.append(
                Intent(
                    type=IntentType.TP_HIT,
                    raw_text=text,
                    message_id=message_id,
                    tp_index=tp_idx,
                    matched_rule="tp_hit",
                )
            )

        # Partial profit taking.
        if P.RE_PARTIAL.search(text):
            intents.append(
                Intent(
                    type=IntentType.PARTIAL_CLOSE,
                    raw_text=text,
                    message_id=message_id,
                    close_fraction=0.5,
                    matched_rule="partial",
                )
            )

        # Explicit SL move with a price beats a generic breakeven phrase.
        new_sl = self._sl_modify(text)
        if new_sl is not None:
            intents.append(
                Intent(
                    type=IntentType.MODIFY_SL,
                    raw_text=text,
                    message_id=message_id,
                    new_sl=new_sl,
                    matched_rule="sl_modify",
                )
            )
        elif P.RE_BREAKEVEN.search(text):
            intents.append(
                Intent(
                    type=IntentType.BREAKEVEN,
                    raw_text=text,
                    message_id=message_id,
                    matched_rule="breakeven",
                )
            )

        if P.RE_CLOSE_ALL.search(text):
            intents.append(
                Intent(
                    type=IntentType.CLOSE_ALL,
                    raw_text=text,
                    message_id=message_id,
                    matched_rule="close_all",
                )
            )

        if not intents:
            return [
                Intent(
                    type=IntentType.NOISE,
                    raw_text=text,
                    message_id=message_id,
                    matched_rule="noise",
                    confidence=0.0,
                )
            ]

        # Annotate "all entries" scope on every management intent.
        for it in intents:
            if applies_all:
                it.confidence = min(1.0, it.confidence + 0.0)  # placeholder hook
        return intents

    # -------------------------------------------------------------- entries
    def _try_entry(self, text: str) -> Optional[EntrySignal]:
        m = P.RE_ENTRY.search(text)
        if not m:
            return None

        direction = (
            Direction.BUY
            if m.group("dir").lower() in {"buy", "long"}
            else Direction.SELL
        )

        p1 = _to_float(m.group("p1"))
        p2 = _to_float(m.group("p2"))
        if p1 is None:
            return None
        # Reject implausible XAUUSD prices to avoid parsing random numbers.
        if not self._plausible_price(p1) or (p2 is not None and not self._plausible_price(p2)):
            return None

        if p2 is not None:
            entry_low, entry_high = sorted((p1, p2))
        else:
            entry_low = entry_high = p1

        order_kind = self._order_kind(text, m)
        sl = self._extract_sl(text)
        tps = self._extract_tps(text)

        return EntrySignal(
            direction=direction,
            symbol=self.default_symbol,
            order_kind=order_kind,
            entry_low=entry_low,
            entry_high=entry_high,
            sl=sl,
            take_profits=tps,
        )

    @staticmethod
    def _plausible_price(price: float) -> bool:
        # XAUUSD trades in the high hundreds to several thousand.
        return 100.0 <= price <= 100000.0

    def _order_kind(self, text: str, entry_match) -> OrderKind:
        # Only inspect the entry's own line so distant words like "stop loss"
        # don't misclassify a market order as a stop order.
        start = text.rfind("\n", 0, entry_match.start()) + 1
        end = text.find("\n", entry_match.start())
        line = text[start:] if end == -1 else text[start:end]
        if P.RE_LIMIT.search(line):
            return OrderKind.LIMIT
        if P.RE_STOP.search(line):
            return OrderKind.STOP
        return OrderKind.MARKET

    def _extract_sl(self, text: str) -> Optional[float]:
        m = P.RE_SL.search(text)
        if m:
            sl = _to_float(m.group("sl"))
            if sl is not None and self._plausible_price(sl):
                return sl
        return None

    def _extract_tps(self, text: str) -> List[TakeProfit]:
        tps: List[TakeProfit] = []
        seen: set = set()

        for m in P.RE_TP.finditer(text):
            price = _to_float(m.group("price"))
            if price is None or not self._plausible_price(price):
                continue
            if price in seen:
                continue
            seen.add(price)
            idx = m.group("idx")
            label = f"TP{idx}" if idx else None
            tps.append(TakeProfit(price=price, label=label))

        # Open-ended runner ("TP: open (100+ pips)").
        om = P.RE_TP_OPEN.search(text)
        if om:
            pips = _to_float(om.group("pips"))
            tps.append(TakeProfit(price=None, min_pips=pips, label="TP_OPEN"))

        # Assign sequential labels where the channel omitted them.
        counter = 1
        for tp in tps:
            if tp.label is None:
                tp.label = f"TP{counter}"
            if not tp.is_open:
                counter += 1
        return tps

    @staticmethod
    def _entry_confidence(entry: EntrySignal) -> float:
        score = 0.5
        if entry.sl is not None:
            score += 0.25
        if entry.concrete_tps:
            score += 0.25
        return round(min(score, 1.0), 2)

    # ---------------------------------------------------------- management
    def _sl_modify(self, text: str) -> Optional[float]:
        for rx in (P.RE_SL_MODIFY, P.RE_SL_TO):
            m = rx.search(text)
            if m:
                sl = _to_float(m.group("sl"))
                if sl is not None and self._plausible_price(sl):
                    return sl
        return None

    def _tp_hit_index(self, text: str) -> Optional[int]:
        for rx in (P.RE_TP_HIT, P.RE_TP_HIT_REV):
            m = rx.search(text)
            if m:
                idx = m.groupdict().get("idx")
                try:
                    return int(idx) if idx else 0  # 0 → unspecified TP
                except (TypeError, ValueError):
                    return 0
        return None
