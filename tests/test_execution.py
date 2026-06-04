"""Tests for execution quality, filters, remote control, and recovery."""
from __future__ import annotations

import datetime as dt

import pytest

from bot.config import ExecutionConfig, FiltersConfig
from bot.execution.monitor import PositionMonitor
from bot.filters import TradingFilters
from bot.state.manager import TrackedPosition
from tests.test_pipeline import _make



# --------------------------------------------------------------------------- #
#  Condition filters (pure)
# --------------------------------------------------------------------------- #
def test_filters_disabled_allows():
    f = TradingFilters(FiltersConfig(enabled=False))
    ok, _ = f.allowed()
    assert ok


def test_filters_session_window():
    f = TradingFilters(FiltersConfig(enabled=True, sessions=["08:00-16:00"]))
    inside = dt.datetime(2026, 6, 4, 10, 0, tzinfo=dt.timezone.utc)
    outside = dt.datetime(2026, 6, 4, 20, 0, tzinfo=dt.timezone.utc)
    assert f.allowed(inside)[0] is True
    assert f.allowed(outside)[0] is False


def test_filters_news_blackout():
    f = TradingFilters(FiltersConfig(
        enabled=True,
        news_blackout=["2026-06-04T12:00/2026-06-04T13:00"]))
    during = dt.datetime(2026, 6, 4, 12, 30, tzinfo=dt.timezone.utc)
    after = dt.datetime(2026, 6, 4, 13, 30, tzinfo=dt.timezone.utc)
    assert f.allowed(during)[0] is False
    assert f.allowed(after)[0] is True


def test_filters_session_wraps_midnight():
    f = TradingFilters(FiltersConfig(enabled=True, sessions=["22:00-02:00"]))
    assert f.allowed(dt.datetime(2026, 6, 4, 23, 0, tzinfo=dt.timezone.utc))[0]
    assert f.allowed(dt.datetime(2026, 6, 4, 1, 0, tzinfo=dt.timezone.utc))[0]
    assert not f.allowed(dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc))[0]


# --------------------------------------------------------------------------- #
#  Trailing stop
# --------------------------------------------------------------------------- #
def test_trail_target_and_improves():
    m = PositionMonitor(ExecutionConfig(trail_start_pips=100, trail_distance_pips=80),
                        executor=None, state=None, pip_size=0.1)
    # BUY opened at 4470; price now 4485 (+150 pips ≥ 100 start) → SL = 4485-8 = 4477
    target = m._trail_target(True, open_price=4470.0, bid=4485.0, ask=4485.1)
    assert target == pytest.approx(4477.0, abs=1e-6)
    assert m._improves(True, current_sl=4464.0, new_sl=4477.0) is True
    assert m._improves(True, current_sl=4480.0, new_sl=4477.0) is False
    # Not enough profit yet → no trail.
    assert m._trail_target(True, 4470.0, 4475.0, 4475.1) is None


async def test_trailing_monitor_pass(tmp_path):
    cfg, ex, state, risk, trader = _make(tmp_path)
    await ex.connect()
    risk.start_day(await ex.account_balance())
    from bot.parser.signal_parser import SignalParser
    parser = SignalParser()
    await trader.handle(parser.parse(
        "Gold buy now 4470 - 4470\nSL: 4464\nTP: 4472", 1))
    sig = state.latest_active()
    pos = sig.open_positions()[0]
    # Drive the simulated price well into profit, then run a monitor pass.
    ex._sim.set_reference_price(4490.0)
    mon = PositionMonitor(ExecutionConfig(trailing_enabled=True, trail_start_pips=100,
                                          trail_distance_pips=80),
                          ex, state, pip_size=0.1)
    await mon._pass()
    assert state.latest_active().open_positions()[0].sl > 4464.0


# --------------------------------------------------------------------------- #
#  Remote control
# --------------------------------------------------------------------------- #
async def test_controller_pause_blocks_entries(tmp_path):
    from bot.control import Controller
    cfg, ex, state, risk, trader = _make(tmp_path)
    await ex.connect()
    risk.start_day(await ex.account_balance())
    ctrl = Controller(cfg, ex, state, trader, trader.tracker, notifier=None)

    await ctrl.pause("test")
    assert ctrl.paused and trader._is_paused()
    from bot.parser.signal_parser import SignalParser
    parser = SignalParser()
    await trader.handle(parser.parse("Gold buy now 4470 - 4467\nSL: 4464\nTP: 4472", 1))
    assert state.latest_active() is None  # blocked

    await ctrl.resume("test")
    assert not ctrl.paused
    await trader.handle(parser.parse("Gold buy now 4470 - 4467\nSL: 4464\nTP: 4472", 2))
    assert state.latest_active() is not None


async def test_controller_close_all(tmp_path):
    from bot.control import Controller
    cfg, ex, state, risk, trader = _make(tmp_path)
    await ex.connect()
    risk.start_day(await ex.account_balance())
    from bot.parser.signal_parser import SignalParser
    parser = SignalParser()
    await trader.handle(parser.parse(
        "Gold buy now 4470 - 4467\nSL: 4464\nTP: 4472\nTP: 4474", 1))
    assert state.latest_active() is not None
    ctrl = Controller(cfg, ex, state, trader, trader.tracker, notifier=None)
    msg = await ctrl.close_all("test")
    assert "Closed" in msg
    assert state.latest_active() is None


async def test_controller_status_and_commands(tmp_path):
    from bot.control import Controller
    cfg, ex, state, risk, trader = _make(tmp_path)
    await ex.connect()
    risk.start_day(await ex.account_balance())
    ctrl = Controller(cfg, ex, state, trader, trader.tracker, notifier=None)
    assert "GTMO" in await ctrl.status_text()
    assert await ctrl.handle_command("/help")
    assert await ctrl.handle_command("/status")
    assert await ctrl.handle_command("/unknown") is None


# --------------------------------------------------------------------------- #
#  Crash recovery
# --------------------------------------------------------------------------- #
async def test_recovery_adopts_orphan_and_closes_vanished(tmp_path):
    from bot.recovery import reconcile_on_startup
    cfg, ex, state, risk, trader = _make(tmp_path)
    await ex.connect()
    base = cfg.mt5.magic_base

    # Orphan broker position with one of our magics, not tracked.
    ex._sim.open(type_=0, volume=0.1, price=4470.0, sl=4464.0, tp=4472.0,
                 magic=base + 5, comment="orphan")
    # A tracked position the broker no longer has.
    entry_sig = state.create_signal(
        __import__("bot.models", fromlist=["EntrySignal", "Direction", "OrderKind"]).EntrySignal(
            direction=__import__("bot.models", fromlist=["Direction"]).Direction.BUY,
            symbol="XAUUSD", entry_low=4400, entry_high=4400, sl=4395,
        ), message_id=None)
    state.add_position(entry_sig.signal_id, TrackedPosition(
        ticket=999999, tp_label="TP1", tp_price=4410, volume=0.1, sl=4395,
        open_price=4400))

    res = await reconcile_on_startup(ex, state, base, trader.tracker)
    assert res["adopted"] == 1
    assert res["closed"] == 1
