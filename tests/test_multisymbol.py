"""Multi-symbol support: parser detection + symbol-aware executor/trader."""
from __future__ import annotations

from bot.models import Direction
from bot.parser.patterns import detect_symbol
from bot.parser.signal_parser import SignalParser
from tests.test_pipeline import _make

P = SignalParser()


def test_detect_symbol_aliases():
    assert detect_symbol("Gold buy now 4470") == "XAUUSD"
    assert detect_symbol("EURUSD sell now 1.0850") == "EURUSD"
    assert detect_symbol("buy bitcoin now") == "BTCUSD"
    assert detect_symbol("US30 long now 39000") == "US30"
    assert detect_symbol("no instrument here", default="XAUUSD") == "XAUUSD"


def test_parser_tags_symbol_on_entry():
    e = P.parse("EURUSD buy now 1.0850 - 1.0840\nSL: 1.0820\nTP: 1.0880", 1)[0].entry
    assert e is not None and e.symbol == "EURUSD"
    assert e.direction is Direction.BUY


async def test_executor_prices_non_primary_symbol(tmp_path):
    cfg, ex, state, risk, trader = _make(tmp_path)
    await ex.connect()
    # The simulator seeds a sensible default price per instrument family.
    p = await ex.current_price("BTCUSD")
    assert p["ask"] > 1000  # BTC ~ tens of thousands, not gold's ~4470
    spec = await ex.ensure_symbol("EURUSD")
    assert spec is not None and spec.name == "EURUSD"


async def test_unconfigured_symbol_is_skipped(tmp_path):
    # Default SYMBOLS is just XAUUSD, so a EURUSD entry must be skipped.
    cfg, ex, state, risk, trader = _make(tmp_path)
    await ex.connect()
    risk.start_day(await ex.account_balance())
    await trader.handle(P.parse(
        "EURUSD buy now 1.0850 - 1.0840\nSL: 1.0820\nTP: 1.0880", 1))
    assert state.latest_active() is None


async def test_configured_symbol_trades(tmp_path):
    cfg, ex, state, risk, trader = _make(tmp_path)
    cfg.symbols = ["XAUUSD", "EURUSD"]
    await ex.connect()
    risk.start_day(await ex.account_balance())
    # Market sits inside the entry zone (so the anti-chase scorer is satisfied).
    ex._sim.set_reference_price(1.0845, "EURUSD")
    await trader.handle(P.parse(
        "EURUSD buy now 1.0850 - 1.0840\nSL: 1.0820\nTP: 1.0880\nTP: 1.0900", 1))
    sig = state.latest_active()
    assert sig is not None and sig.symbol == "EURUSD"
    assert len(sig.open_positions()) >= 1
