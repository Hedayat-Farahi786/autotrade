#!/usr/bin/env python3
"""End-to-end smoke test — exercises every feature with dummy data, no API keys.

A single runnable demonstration that the whole system works:
parser → scoring → sizing → execution (simulator) → trailing → TP fills →
partials → breakeven → performance → remote control → backtest → dashboard API.

Run:  python scripts/smoke.py
Exits non-zero if any check fails.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, BAD = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
_fails = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _fails
    mark = OK if cond else BAD
    if not cond:
        _fails += 1
    print(f"  {mark} {label}" + (f"  \033[2m{detail}\033[0m" if detail else ""))


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


async def main() -> int:
    from bot.analytics.performance import PerformanceTracker
    from bot.config import (
        BotConfig,
        ControlConfig,
        DashboardConfig,
        ExecutionConfig,
        FiltersConfig,
        IntelligenceConfig,
        MT5Config,
        ParserConfig,
        RiskConfig,
        TelegramConfig,
    )
    from bot.control import Controller
    from bot.execution.monitor import PositionMonitor
    from bot.filters import TradingFilters
    from bot.intelligence.scorer import SignalScorer
    from bot.mt5.executor import MT5Executor
    from bot.parser import build_parser
    from bot.parser.signal_parser import SignalParser
    from bot.risk.manager import RiskManager
    from bot.state.manager import StateManager
    from bot.trader import Trader

    tmp = Path(tempfile.mkdtemp())
    cfg = BotConfig(
        telegram=TelegramConfig(api_id=0, api_hash="x", channel="t"),
        mt5=MT5Config(symbol="XAUUSD"), risk=RiskConfig(),
        parser=ParserConfig(mode="regex"), intelligence=IntelligenceConfig(),
        execution=ExecutionConfig(trailing_enabled=True, trail_start_pips=100,
                                  trail_distance_pips=80),
        filters=FiltersConfig(), control=ControlConfig(
            command_file=str(tmp / "cmd.jsonl"), control_file=str(tmp / "ctl.json"),
            pause_file=str(tmp / "pause.flag")),
        dashboard=DashboardConfig(), dry_run=True,
        state_file=str(tmp / "state.json"), trades_file=str(tmp / "trades.jsonl"),
        emergency_stop_file=str(tmp / "stop.flag"))

    ex = MT5Executor(cfg.mt5, dry_run=True)
    state = StateManager(cfg.state_file, magic_base=cfg.mt5.magic_base)
    risk = RiskManager(cfg.risk)
    tracker = PerformanceTracker(cfg.trades_file)
    scorer = SignalScorer(cfg.intelligence)
    trader = Trader(cfg, ex, state, risk, tracker=tracker, scorer=scorer,
                    filters=TradingFilters(cfg.filters))
    await ex.connect()
    risk.start_day(await ex.account_balance())
    parser = SignalParser()

    async def feed(t):
        await trader.handle(parser.parse(t, 1))

    # 1) Parsing every message type ------------------------------------------
    section("1. Parser — message types")
    samples = {
        "Gold buy now 4470 - 4467\nSL: 4464\nTP: 4472\nTP: open (100+ pips)": "ENTRY",
        "Gold sell now 4454.3 - 4458.3\nSL: 4462\nTP: 4452": "ENTRY",
        "EURUSD buy now 1.0850 - 1.0840\nSL: 1.0820\nTP: 1.0880": "ENTRY",
        "Adjust SL to 4462": "MODIFY_SL",
        "Breakeven set for zero risk on all entries!!": "BREAKEVEN",
        "Take some profits": "PARTIAL_CLOSE",
        "TP2 smasssheddd": "TP_HIT",
        "close everything now": "CLOSE_ALL",
        "We in blueeee 😎😎😎": "NOISE",
    }
    for text, want in samples.items():
        got = [i.type.value for i in parser.parse(text, 1)]
        check(f"{want:<13} ← {text.splitlines()[0][:42]}", want in got, str(got))

    # 2) Full trade lifecycle -------------------------------------------------
    section("2. Execution — full lifecycle (simulated)")
    await feed("Gold buy now 4470 - 4467\nSL: 4464\n"
               "TP: 4472\nTP: 4474\nTP: 4476\nTP: 4478\nTP: open (100+ pips)")
    sig = state.latest_active()
    check("entry opens 5 legs", sig and len(sig.open_positions()) == 5,
          f"{len(sig.open_positions()) if sig else 0} legs")
    check("legs are risk-sized", all(p.risk_amount > 0 for p in sig.open_positions()))

    ex._sim.set_reference_price(4490.0)
    mon = PositionMonitor(cfg.execution, ex, state, pip_size=cfg.risk.pip_size)
    await mon._pass()
    check("trailing stop tightens SL", state.latest_active().open_positions()[0].sl > 4464)

    await feed("TP1 has been touched")
    await feed("TP2 smasssheddd take some more profits and set breakeven now")
    perf = tracker.summary()
    check("TP fills journaled as wins", perf["wins"] >= 2, f"{perf['wins']} wins")
    check("performance computed", perf["profit_factor"] is not None or perf["trades"] > 0,
          f"trades={perf['trades']} net={perf['net_profit']:.2f} winrate={perf['win_rate']*100:.0f}%")

    # 3) Intelligence ---------------------------------------------------------
    section("3. Intelligence — scoring, adaptive risk, filters")
    s = scorer.score(parser.parse("Gold buy now 4470 - 4467\nSL: 4464\nTP: 4472", 1)[0].entry, 4468.0)
    check("scorer accepts a good signal", s.take, f"score={s.value}")
    s2 = scorer.score(parser.parse("Gold buy now 4470 - 4467\nTP: 4472", 1)[0].entry, 4468.0)
    check("scorer rejects no-SL signal", not s2.take)
    mult, _ = risk.adaptive_multiplier({"trades": 5, "streak": -3, "max_drawdown": 0}, cfg.intelligence)
    check("adaptive risk throttles on loss streak", mult < 1.0, f"×{mult}")
    import datetime as dt
    ft = TradingFilters(FiltersConfig(enabled=True, sessions=["08:00-16:00"]))
    check("session filter blocks off-hours",
          not ft.allowed(dt.datetime(2026, 6, 4, 22, 0, tzinfo=dt.timezone.utc))[0])

    # 4) Remote control -------------------------------------------------------
    section("4. Remote control")
    ctrl = Controller(cfg, ex, state, trader, tracker, notifier=None)
    await ctrl.pause("smoke")
    check("pause engages", ctrl.paused and trader._is_paused())
    await ctrl.resume("smoke")
    check("resume clears", not ctrl.paused)
    closed = await ctrl.close_all("smoke")
    check("close-all flattens", state.latest_active() is None, closed)
    check("/status command works", "GTMO" in (await ctrl.handle_command("/status")))

    # 5) AI fallback ----------------------------------------------------------
    section("5. Parser fallback without API keys")
    from bot.parser.hybrid import RegexParserAsync
    check("ai mode → regex (no key)",
          isinstance(build_parser("ai", provider="gemini", gemini_api_key=None), RegexParserAsync))
    check("hybrid mode → regex (no key)",
          isinstance(build_parser("hybrid", provider="gemini", gemini_api_key=None), RegexParserAsync))

    # 6) Backtest -------------------------------------------------------------
    section("6. Backtest engine")
    from bot.backtest import run_backtest
    res = run_backtest(
        [(0, "Gold buy now 4470 - 4467\nSL: 4464\nTP: 4472\nTP: 4474")],
        [(0, 4470, 4467, 4469), (1, 4473, 4469, 4472), (2, 4475, 4472, 4474)])
    check("backtest produces trades", res.performance["trades"] == 2, res.report())

    # 7) Dashboard API --------------------------------------------------------
    section("7. Dashboard API")
    try:
        from fastapi.testclient import TestClient

        import web.server as srv
        srv.TRADES_FILE = cfg.trades_file
        srv.STATUS_FILE = str(tmp / "nope.json")
        c = TestClient(srv.app)
        check("/api/status", c.get("/api/status").status_code == 200)
        check("/api/performance", c.get("/api/performance").json()["performance"]["trades"] >= 0)
        check("/ dashboard HTML", "GTMO" in c.get("/").text)
        check("/api/auth (open)", c.get("/api/auth").json()["ok"] is True)
    except Exception as e:  # noqa: BLE001
        check("dashboard import", False, str(e)[:80])

    print()
    if _fails:
        print(f"\033[31m{_fails} check(s) FAILED\033[0m\n")
        return 1
    print("\033[32mAll checks passed — every feature works with dummy data.\033[0m\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
