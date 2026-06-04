"""Tests for the event-driven backtester."""
from __future__ import annotations

from bot.backtest import run_backtest


def test_backtest_winning_trade():
    # BUY 4467-4470 SL 4464; price rises and tags both TPs.
    messages = [(0, "Gold buy now 4470 - 4467\nSL: 4464\nTP: 4472\nTP: 4474")]
    bars = [
        (0, 4470, 4467, 4469),    # entry context
        (1, 4473, 4469, 4472),    # hits TP1 (4472)
        (2, 4475, 4472, 4474),    # hits TP2 (4474)
    ]
    res = run_backtest(messages, bars)
    p = res.performance
    assert p["trades"] == 2 and p["wins"] == 2
    assert p["net_profit"] > 0 and p["avg_r"] > 0
    assert len(res.equity_curve) == 2


def test_backtest_losing_trade():
    # BUY then price falls through SL → both legs stopped out at -1R.
    messages = [(0, "Gold buy now 4470 - 4470\nSL: 4464\nTP: 4480\nTP: 4490")]
    bars = [
        (0, 4471, 4469, 4470),
        (1, 4470, 4463, 4464),   # low 4463 ≤ SL 4464 → stop
    ]
    res = run_backtest(messages, bars)
    p = res.performance
    assert p["trades"] == 2 and p["losses"] == 2
    assert p["net_profit"] < 0
    # Each stopped leg is ~ -1R.
    assert all(t["r_multiple"] < 0 for t in res.trades)


def test_backtest_breakeven_protects():
    # Price hits TP1, message moves SL to breakeven, then price falls back.
    messages = [
        (0, "Gold buy now 4470 - 4470\nSL: 4464\nTP: 4476\nTP: open (100+ pips)"),
        (2, "TP1 smashed, set breakeven now"),
    ]
    bars = [
        (0, 4470, 4470, 4470),
        (1, 4477, 4470, 4476),   # TP1 (4476) hit → first leg closes in profit
        (3, 4471, 4469, 4470),   # falls back to ~entry; runner exits at BE (4470)
    ]
    res = run_backtest(messages, bars)
    # The runner should exit at/near breakeven (≈0R), not at the original SL (−1R).
    runner = [t for t in res.trades if t["label"] == "TP_OPEN"]
    assert runner and runner[0]["r_multiple"] >= -0.1


def test_backtest_empty():
    res = run_backtest([], [])
    assert res.performance["trades"] == 0
    assert "No trades" in res.report()
