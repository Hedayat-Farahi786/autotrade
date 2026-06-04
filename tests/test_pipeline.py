"""End-to-end pipeline test using the MT5 simulator (no network, no real MT5).

Drives the same sequence of messages seen across the screenshots and asserts the
resulting position/state changes, exercising parser → trader → executor.
"""
from __future__ import annotations

import pytest

from bot.analytics.performance import PerformanceTracker
from bot.config import (BotConfig, ControlConfig, DashboardConfig,
                        ExecutionConfig, FiltersConfig, IntelligenceConfig,
                        MT5Config, ParserConfig, RiskConfig, TelegramConfig)
from bot.intelligence.scorer import SignalScorer
from bot.mt5.executor import MT5Executor
from bot.parser.signal_parser import SignalParser
from bot.risk.manager import RiskManager
from bot.state.manager import StateManager
from bot.trader import Trader



def _make(tmp_path, intel=None):
    intel = intel or IntelligenceConfig()
    cfg = BotConfig(
        telegram=TelegramConfig(api_id=0, api_hash="x", channel="t"),
        mt5=MT5Config(symbol="XAUUSD"),
        risk=RiskConfig(risk_per_signal=0.01, one_position_per_tp=True),
        parser=ParserConfig(mode="regex"),
        intelligence=intel,
        execution=ExecutionConfig(trailing_enabled=False),
        filters=FiltersConfig(enabled=False),
        control=ControlConfig(
            command_file=str(tmp_path / "cmd.jsonl"),
            control_file=str(tmp_path / "control.json"),
            pause_file=str(tmp_path / "paused.flag"),
        ),
        dashboard=DashboardConfig(),
        dry_run=True,
        state_file=str(tmp_path / "state.json"),
        trades_file=str(tmp_path / "trades.jsonl"),
        emergency_stop_file=str(tmp_path / "nope.STOP"),
    )
    ex = MT5Executor(cfg.mt5, dry_run=True)
    state = StateManager(cfg.state_file, magic_base=cfg.mt5.magic_base)
    risk = RiskManager(cfg.risk)
    tracker = PerformanceTracker(cfg.trades_file)
    scorer = SignalScorer(cfg.intelligence, pip_size=cfg.risk.pip_size)
    trader = Trader(cfg, ex, state, risk, tracker=tracker, scorer=scorer)
    return cfg, ex, state, risk, trader


async def _feed(parser, trader, text):
    await trader.handle(parser.parse(text, message_id=1))


async def test_full_signal_lifecycle(tmp_path):
    cfg, ex, state, risk, trader = _make(tmp_path)
    assert await ex.connect()
    risk.start_day(await ex.account_balance())
    parser = SignalParser()

    # 1) Entry → opens one position per TP (4 concrete + 1 open runner = 5).
    await _feed(parser, trader,
                "Gold buy now 4470 - 4467\nSL: 4464\n"
                "TP: 4472\nTP: 4474\nTP: 4476\nTP: 4478\nTP: open (100+ pips)")
    sig = state.latest_active()
    assert sig is not None
    assert len(sig.open_positions()) == 5
    assert sig.sl == 4464

    # 2) Adjust SL → all legs updated.
    await _feed(parser, trader, "Adjust SL to 4462, at the bottom of ourr zone")
    assert state.latest_active().sl == 4462
    for p in state.latest_active().open_positions():
        assert p.sl == 4462

    # 3) Partial close → each leg's volume reduced.
    before = [p.volume for p in state.latest_active().open_positions()]
    await _feed(parser, trader, "Take some profits as well ok")
    after = [p.volume for p in state.latest_active().open_positions()]
    assert all(a < b or b <= 0.01 for a, b in zip(after, before))

    # 4) Breakeven on all entries → SL moved to entry midpoint.
    await _feed(parser, trader, "Breakeven set for zero risk on all entries!!")
    be = state.latest_active().avg_entry
    for p in state.latest_active().open_positions():
        assert p.breakeven and p.sl == be


async def test_daily_loss_halt_blocks_entries(tmp_path):
    cfg, ex, state, risk, trader = _make(tmp_path)
    await ex.connect()
    risk.start_day(1000.0)
    risk.register_realized_pl(-60.0)  # > 5% of 1000 → halt
    assert risk.halted

    parser = SignalParser()
    await _feed(parser, trader, "Gold buy now 4470 - 4467\nSL: 4464\nTP: 4472")
    assert state.latest_active() is None  # entry rejected
