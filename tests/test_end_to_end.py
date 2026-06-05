"""Comprehensive end-to-end session — no API keys, no real MT5, no network.

Simulates a realistic GTMO trading session against the in-memory simulator and
asserts that every subsystem cooperates: parsing → scoring → sizing → execution
→ trailing → TP fills → partials → breakeven → performance journaling →
remote control. Also verifies graceful degradation when no AI key is present.
"""
from __future__ import annotations

from bot.config import ExecutionConfig
from bot.control import Controller
from bot.execution.monitor import PositionMonitor
from bot.parser import build_parser
from bot.parser.hybrid import RegexParserAsync
from bot.parser.signal_parser import SignalParser
from tests.test_pipeline import _make

PARSER = SignalParser()


async def _feed(trader, text):
    await trader.handle(PARSER.parse(text, message_id=1))


async def test_full_realistic_session(tmp_path):
    cfg, ex, state, risk, trader = _make(tmp_path)
    assert await ex.connect()
    risk.start_day(await ex.account_balance())

    # 1) ENTRY — full GTMO signal → 5 legs (4 TPs + open runner), risk-sized.
    await _feed(trader,
                "Gold buy now 4470 - 4467\nSL: 4464\n"
                "TP: 4472\nTP: 4474\nTP: 4476\nTP: 4478\nTP: open (100+ pips)")
    sig = state.latest_active()
    assert sig is not None and len(sig.open_positions()) == 5
    assert all(p.risk_amount > 0 for p in sig.open_positions())

    # 2) Trailing stop — price runs up, monitor drags the SL behind it.
    ex._sim.set_reference_price(4490.0)
    mon = PositionMonitor(ExecutionConfig(trailing_enabled=True,
                                          trail_start_pips=100,
                                          trail_distance_pips=80),
                          ex, state, pip_size=cfg.risk.pip_size)
    await mon._pass()
    assert state.latest_active().open_positions()[0].sl > 4464.0

    # 3) TP hits + partial + breakeven (compound message), journaled as wins.
    await _feed(trader, "TP1 has been touched")
    await _feed(trader, "TP2 smasssheddd take some more profits and set breakeven now")
    perf = trader.tracker.summary()
    assert perf["trades"] >= 2 and perf["wins"] >= 2
    assert perf["net_profit"] > 0

    # 4) Remote control — pause blocks a new entry, resume re-enables.
    ctrl = Controller(cfg, ex, state, trader, trader.tracker, notifier=None)
    before = len(state.active_signals())
    await ctrl.pause("e2e")
    await _feed(trader, "Gold buy now 4500 - 4498\nSL: 4495\nTP: 4505")
    assert len(state.active_signals()) == before  # paused → no new signal
    await ctrl.resume("e2e")

    # 5) Close everything → flat.
    await ctrl.close_all("e2e")
    assert state.latest_active() is None

    # 6) Status text reflects the journal.
    status = await ctrl.status_text()
    assert "GTMO" in status and "Trades" in status


async def test_sell_signal_session(tmp_path):
    cfg, ex, state, risk, trader = _make(tmp_path)
    await ex.connect()
    risk.start_day(await ex.account_balance())
    ex._sim.set_reference_price(4456.0)
    await _feed(trader,
                "Gold sell now 4454.3 - 4458.3\nSL: 4462\n"
                "TP: 4452\nTP: 4450\nTP: 4448\nTP: 4446\nTP: open")
    sig = state.latest_active()
    assert sig is not None and sig.direction == "SELL"
    await _feed(trader, "TP1 checkkk")
    assert any(t["direction"] == "SELL" for t in trader.tracker.recent())


# --------------------------------------------------------------------------- #
#  No API key → graceful regex fallback (dummy-data friendly)
# --------------------------------------------------------------------------- #
def test_parser_falls_back_to_regex_without_keys():
    # AI requested but no key/SDK → must fall back to the regex parser, not crash.
    for mode in ("ai", "hybrid"):
        p = build_parser(mode, provider="gemini", gemini_api_key=None,
                         anthropic_api_key=None)
        assert isinstance(p, RegexParserAsync)


async def test_regex_parser_handles_all_message_types():
    p = build_parser("regex")
    cases = {
        "Gold buy now 4470 - 4467\nSL: 4464\nTP: 4472": "ENTRY",
        "Adjust SL to 4462": "MODIFY_SL",
        "Breakeven set for zero risk on all entries!!": "BREAKEVEN",
        "Take some profits": "PARTIAL_CLOSE",
        "TP2 smashed": "TP_HIT",
        "close all now": "CLOSE_ALL",
        "We in blueeee 😎": "NOISE",
    }
    for text, expected in cases.items():
        intents = await p.parse(text, 1)
        assert any(i.type.value == expected for i in intents), (text, expected)
