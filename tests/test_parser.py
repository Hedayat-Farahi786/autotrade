"""Regex parser tests built directly from the GTMO VIP screenshots.

These pin the behaviour for the exact message styles the channel posts, and act
as living documentation/regression guard for the parser rules.
"""
from __future__ import annotations

import pytest

from bot.models import Direction, IntentType, OrderKind
from bot.parser.signal_parser import SignalParser

P = SignalParser()


def only(intents, itype):
    matches = [i for i in intents if i.type is itype]
    assert matches, f"expected a {itype} intent, got {[i.type for i in intents]}"
    return matches[0]


# --------------------------------------------------------------------------- #
#  Entry signals
# --------------------------------------------------------------------------- #
def test_full_entry_with_zone_sl_and_multiple_tps():
    text = (
        "Gold buy now 4470 - 4467\n"
        "SL: 4464\n"
        "TP: 4472\n"
        "TP: 4474\n"
        "TP: 4476\n"
        "TP: 4478\n"
        "TP: open (100+ pips)"
    )
    intents = P.parse(text, 1)
    assert len(intents) == 1
    e = only(intents, IntentType.ENTRY).entry
    assert e.direction is Direction.BUY
    assert e.order_kind is OrderKind.MARKET
    assert e.entry_low == 4467 and e.entry_high == 4470
    assert e.sl == 4464
    concrete = [tp.price for tp in e.concrete_tps]
    assert concrete == [4472, 4474, 4476, 4478]
    # The open runner is captured with its hinted minimum distance.
    open_tps = [tp for tp in e.take_profits if tp.is_open]
    assert len(open_tps) == 1 and open_tps[0].min_pips == 100


def test_entry_single_price():
    intents = P.parse("Gold sell now 4475", 2)
    e = only(intents, IntentType.ENTRY).entry
    assert e.direction is Direction.SELL
    assert e.entry_low == 4475 and e.entry_high == 4475


def test_entry_limit_keyword_sets_limit_order():
    intents = P.parse("Gold buy limit 4460 - 4458\nSL 4455\nTP 4465", 3)
    e = only(intents, IntentType.ENTRY).entry
    assert e.order_kind is OrderKind.LIMIT


# --------------------------------------------------------------------------- #
#  Management updates
# --------------------------------------------------------------------------- #
def test_adjust_sl():
    intents = P.parse("Adjust SL to 4462, at the bottom of ourr zone", 4)
    i = only(intents, IntentType.MODIFY_SL)
    assert i.new_sl == 4462


def test_breakeven_on_all_entries():
    intents = P.parse("Breakeven set for zero risk on all entries!!", 5)
    only(intents, IntentType.BREAKEVEN)


def test_tp_touched():
    intents = P.parse("TP1 has been touched once but back in the zone Straight ✅", 6)
    i = only(intents, IntentType.TP_HIT)
    assert i.tp_index == 1


def test_tp_checked_with_pips():
    intents = P.parse("TP2 checkkk!✅✅ 70+ pips!", 7)
    i = only(intents, IntentType.TP_HIT)
    assert i.tp_index == 2


def test_take_some_profits():
    intents = P.parse("Take some profits as well ok", 8)
    only(intents, IntentType.PARTIAL_CLOSE)


def test_compound_tp_partial_and_breakeven():
    text = "TP2 smasssheddd take some more profits and set breakeven now for zero risk!!"
    intents = P.parse(text, 9)
    types = {i.type for i in intents}
    assert IntentType.TP_HIT in types
    assert IntentType.PARTIAL_CLOSE in types
    assert IntentType.BREAKEVEN in types
    assert only(intents, IntentType.TP_HIT).tp_index == 2


# --------------------------------------------------------------------------- #
#  Noise — must NOT trigger trades
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Ready Signal this is no financial advice. Trade at your own risk",
        "Slowly no over risk or revenge trading, we follow the momentum and trend",
        "Come on bulls, we made the bears struggle hard, now its time to kill them "
        "with a rocket, fly up!",
        "Above all entries again",
        "Floating in profits, next to TP1 again",
        "Its gotta be a jackpot trade come on flyyyyy",
        "god we been plenty of wins in profit, so this was more a management trade",
    ],
)
def test_noise_is_ignored(text):
    intents = P.parse(text, 10)
    assert all(i.type is IntentType.NOISE for i in intents), [i.type for i in intents]


def test_empty_message():
    assert P.parse("", 11) == []
    assert P.parse(None, 12) == []
