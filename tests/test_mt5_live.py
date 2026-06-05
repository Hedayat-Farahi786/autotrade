"""Live MT5 path tests — drive the real (non-simulate) executor with a fake mt5.

Verifies the executor connects, resolves the symbol, loads the spec, and builds
correct ``order_send`` requests for market/limit entries, SL/TP modifications,
and closes (incl. realized-profit lookup) — i.e. exactly what runs against a
real MetaTrader 5 terminal.
"""
from __future__ import annotations

import bot.mt5.executor as E
from bot.config import MT5Config
from tests.fake_mt5 import FailingMT5, FakeMT5


def _live_executor(monkeypatch, fake, cfg=None):
    cfg = cfg or MT5Config(symbol="XAUUSD", login=123, password="x",
                           server="FakeBroker", deviation_points=30,
                           magic_base=990000)
    monkeypatch.setattr(E, "mt5", fake)
    monkeypatch.setattr(E, "_MT5_AVAILABLE", True)
    ex = E.MT5Executor(cfg, dry_run=False)
    assert ex.simulate is False  # confirm we're on the LIVE path
    return ex


# --------------------------------------------------------------------------- #
#  Connection
# --------------------------------------------------------------------------- #
async def test_connect_initializes_and_loads_spec(monkeypatch):
    fake = FakeMT5()
    ex = _live_executor(monkeypatch, fake)
    assert await ex.connect() is True
    assert fake.initialized is True
    # Account login happens via mt5.login() after initialize().
    assert fake.logged_in is True
    assert fake.login_args == (123, "x", "FakeBroker")
    assert ex.symbol == "XAUUSD"
    spec = ex.spec
    assert spec and spec.contract_size == 100.0 and spec.volume_min == 0.01
    assert await ex.account_balance() == 10000.0
    assert await ex.account_equity() == 10050.0
    price = await ex.current_price()
    assert price["bid"] == 4469.95 and price["ask"] == 4470.05
    await ex.shutdown()
    assert fake.shutdown_called is True


async def test_symbol_resolution_with_broker_suffix(monkeypatch):
    # Broker exposes only "XAUUSD.r"; override list must resolve it.
    fake = FakeMT5(symbols=["XAUUSD.r", "EURUSD"])
    cfg = MT5Config(symbol="XAUUSD", symbol_overrides=["XAUUSD.r"],
                    login=1, password="x", server="B")
    ex = _live_executor(monkeypatch, fake, cfg)
    assert await ex.connect() is True
    assert ex.symbol == "XAUUSD.r"


async def test_symbol_resolution_fuzzy(monkeypatch):
    # No exact/override match → fuzzy startswith("XAUUSD").
    fake = FakeMT5(symbols=["XAUUSDm"])
    cfg = MT5Config(symbol="XAUUSD", login=1, password="x", server="B")
    ex = _live_executor(monkeypatch, fake, cfg)
    assert await ex.connect() is True
    assert ex.symbol == "XAUUSDm"


# --------------------------------------------------------------------------- #
#  Order entry
# --------------------------------------------------------------------------- #
async def test_market_buy_builds_correct_request(monkeypatch):
    fake = FakeMT5()
    ex = _live_executor(monkeypatch, fake)
    await ex.connect()
    res = await ex.open_position(direction="BUY", volume=0.10, price=None,
                                 sl=4464.0, tp=4472.0, order_kind="MARKET",
                                 magic=990007, comment="GTMO#7-TP1")
    assert res.ok and res.ticket
    r = fake.last_request
    assert r["action"] == FakeMT5.TRADE_ACTION_DEAL
    assert r["type"] == FakeMT5.ORDER_TYPE_BUY
    assert r["symbol"] == "XAUUSD"
    assert r["volume"] == 0.10
    assert r["price"] == 4470.05          # filled at ask
    assert r["sl"] == 4464.0 and r["tp"] == 4472.0
    assert r["magic"] == 990007
    assert r["deviation"] == 30
    assert r["type_filling"] == FakeMT5.ORDER_FILLING_IOC


async def test_sell_limit_builds_pending_request(monkeypatch):
    fake = FakeMT5()
    ex = _live_executor(monkeypatch, fake)
    await ex.connect()
    res = await ex.open_position(direction="SELL", volume=0.05, price=4480.0,
                                 sl=4486.0, tp=4470.0, order_kind="LIMIT",
                                 magic=990008, comment="x")
    assert res.ok
    r = fake.last_request
    assert r["action"] == FakeMT5.TRADE_ACTION_PENDING
    assert r["type"] == FakeMT5.ORDER_TYPE_SELL_LIMIT
    assert r["price"] == 4480.0


async def test_rejected_order_returns_not_ok(monkeypatch):
    fake = FailingMT5()
    ex = _live_executor(monkeypatch, fake)
    await ex.connect()
    res = await ex.open_position(direction="BUY", volume=0.1, price=None,
                                 sl=4464.0, tp=4472.0, order_kind="MARKET",
                                 magic=1, comment="x")
    assert res.ok is False


# --------------------------------------------------------------------------- #
#  Modify / close / profit
# --------------------------------------------------------------------------- #
async def test_modify_sl_builds_sltp_request(monkeypatch):
    fake = FakeMT5()
    fake.add_position(ticket=700001, type=FakeMT5.POSITION_TYPE_BUY, volume=0.1,
                      price_open=4470.0, sl=4464.0, tp=4472.0, magic=990007)
    ex = _live_executor(monkeypatch, fake)
    await ex.connect()
    res = await ex.modify_position(700001, sl=4468.0)
    assert res.ok
    r = fake.last_request
    assert r["action"] == FakeMT5.TRADE_ACTION_SLTP
    assert r["position"] == 700001 and r["sl"] == 4468.0


async def test_close_buy_and_realized_profit(monkeypatch):
    fake = FakeMT5()
    fake.add_position(ticket=700002, type=FakeMT5.POSITION_TYPE_BUY, volume=0.1,
                      price_open=4470.0, sl=4464.0, tp=4472.0, magic=990007)
    ex = _live_executor(monkeypatch, fake)
    await ex.connect()
    res = await ex.close_position(700002)
    assert res.ok
    r = fake.last_request
    assert r["action"] == FakeMT5.TRADE_ACTION_DEAL
    assert r["type"] == FakeMT5.ORDER_TYPE_SELL     # closing a BUY → SELL
    assert r["position"] == 700002
    assert res.profit == 73.5                       # from history_deals_get


async def test_order_check_validates_without_placing(monkeypatch):
    fake = FakeMT5()
    ex = _live_executor(monkeypatch, fake)
    await ex.connect()
    result = await ex.check_order(direction="BUY", volume=0.01)
    assert result["ok"] is True and result["retcode"] == 0
    # It used order_check (not order_send) → no real order was sent.
    assert any(r.get("_check") for r in fake.requests)
    assert all("order" not in r or r.get("_check") for r in fake.requests)


async def test_positions_by_magic_and_all(monkeypatch):
    fake = FakeMT5()
    fake.add_position(ticket=1, type=0, volume=0.1, price_open=4470, sl=4464,
                      tp=4472, magic=990007)
    fake.add_position(ticket=2, type=1, volume=0.1, price_open=4480, sl=4486,
                      tp=4470, magic=111111)
    ex = _live_executor(monkeypatch, fake)
    await ex.connect()
    mine = await ex.positions_by_magic(990007)
    assert len(mine) == 1 and mine[0]["ticket"] == 1
    every = await ex.all_positions()
    assert len(every) == 2
