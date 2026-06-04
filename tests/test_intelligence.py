"""Tests for the intelligence stack: analytics, scoring, adaptive risk."""
from __future__ import annotations

import pytest

from bot.analytics.performance import PerformanceTracker, TradeRecord, summarize
from bot.config import IntelligenceConfig, RiskConfig
from bot.intelligence.review import ReviewLogger
from bot.intelligence.scorer import SignalScorer
from bot.models import Direction, EntrySignal, IntentType, OrderKind, TakeProfit
from bot.parser.signal_parser import SignalParser
from bot.risk.manager import RiskManager


# --------------------------------------------------------------------------- #
#  Performance analytics
# --------------------------------------------------------------------------- #
def test_summarize_empty():
    s = summarize([])
    assert s["trades"] == 0 and s["profit_factor"] is None


def test_summarize_metrics():
    recs = [
        {"profit": 100, "risk_amount": 50, "close_price": 1},
        {"profit": -50, "risk_amount": 50, "close_price": 1},
        {"profit": 200, "risk_amount": 50, "close_price": 1},
    ]
    s = summarize(recs)
    assert s["trades"] == 3 and s["wins"] == 2 and s["losses"] == 1
    assert s["win_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert s["gross_profit"] == 300 and s["gross_loss"] == 50
    assert s["profit_factor"] == 6.0
    assert s["expectancy"] == pytest.approx(83.33, abs=0.1)
    assert s["max_drawdown"] == 50  # peak 100 → trough 50
    assert s["avg_r"] == pytest.approx((2 - 1 + 4) / 3, abs=1e-3)
    assert s["streak"] == 1  # last trade was a win


def test_tracker_persists_and_reloads(tmp_path):
    f = str(tmp_path / "trades.jsonl")
    t = PerformanceTracker(f)
    t.record(TradeRecord(1, 111, "BUY", "XAUUSD", "TP1", 0.1, 4470, 4472,
                         profit=20.0, risk_amount=10.0, reason="tp", opened_at=0))
    assert t.summary()["trades"] == 1
    # Reload from disk → record survives.
    t2 = PerformanceTracker(f)
    assert t2.summary()["trades"] == 1 and t2.summary()["net_profit"] == 20.0


# --------------------------------------------------------------------------- #
#  Signal scorer
# --------------------------------------------------------------------------- #
def _entry(sl=4464.0, tps=(4472.0,), direction=Direction.BUY, lo=4467.0, hi=4470.0):
    return EntrySignal(
        direction=direction, symbol="XAUUSD", order_kind=OrderKind.MARKET,
        entry_low=lo, entry_high=hi, sl=sl,
        take_profits=[TakeProfit(price=p) for p in tps],
    )


def test_scorer_accepts_good_signal():
    sc = SignalScorer(IntelligenceConfig())
    s = sc.score(_entry(), market_price=4470.0)
    assert s.take and s.value > 0.5 and 0 < s.size_factor <= 1.0


def test_scorer_rejects_missing_sl():
    sc = SignalScorer(IntelligenceConfig(require_sl=True))
    s = sc.score(_entry(sl=None), market_price=4470.0)
    assert not s.take and s.size_factor == 0.0


def test_scorer_rejects_chasing_price():
    # BUY zone 4467-4470, SL 4464 (dist ~6). Price already 20 above zone → >2×SL.
    sc = SignalScorer(IntelligenceConfig(max_chase_ratio=2.0))
    s = sc.score(_entry(), market_price=4490.0)
    assert not s.take


def test_scorer_low_rr_can_block():
    # TP just 1.0 away but SL 6 away → RR ~0.16; require_min_rr blocks it.
    sc = SignalScorer(IntelligenceConfig(require_min_rr=True, min_rr=0.8))
    s = sc.score(_entry(tps=(4471.0,)), market_price=4470.0)
    assert not s.take


# --------------------------------------------------------------------------- #
#  Adaptive risk
# --------------------------------------------------------------------------- #
def test_adaptive_throttles_on_loss_streak():
    rm = RiskManager(RiskConfig())
    rm.start_day(10000.0)
    intel = IntelligenceConfig(adaptive_risk=True, loss_streak_throttle=3,
                               throttle_factor=0.5)
    mult, reasons = rm.adaptive_multiplier(
        {"trades": 5, "streak": -3, "max_drawdown": 0.0}, intel)
    assert mult == 0.5 and reasons


def test_adaptive_no_throttle_when_healthy():
    rm = RiskManager(RiskConfig())
    rm.start_day(10000.0)
    intel = IntelligenceConfig(adaptive_risk=True)
    mult, _ = rm.adaptive_multiplier(
        {"trades": 5, "streak": 2, "max_drawdown": 0.0}, intel)
    assert mult == 1.0


# --------------------------------------------------------------------------- #
#  Parser self-learning review queue
# --------------------------------------------------------------------------- #
def test_review_queues_trade_hint_noise(tmp_path):
    f = str(tmp_path / "review.jsonl")
    rl = ReviewLogger(f)
    parser = SignalParser()
    # A trade-flavoured message the regex parser doesn't act on.
    text = "partial secured on the gold runner, trail your stop carefully"
    intents = parser.parse(text, 1)
    queued = rl.consider(text, intents, 1)
    # Either it parsed as actionable, or it was queued for review — never silently lost.
    if all(i.type is IntentType.NOISE for i in intents):
        assert queued and rl.count == 1


def test_review_ignores_clear_noise(tmp_path):
    rl = ReviewLogger(str(tmp_path / "review.jsonl"))
    parser = SignalParser()
    text = "We in blueeee 😎😎😎"
    assert rl.consider(text, parser.parse(text, 1), 1) is False
