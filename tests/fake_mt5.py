"""A faithful in-test fake of the ``MetaTrader5`` package.

Lets the executor's **live** (non-simulate) code path run on Linux so we can
assert it constructs correct broker requests and handles results, exactly as it
would against a real terminal. Order requests are captured for inspection.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class FakeMT5:
    # --- constants (values mirror the real package's semantics) ----------
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    TRADE_RETCODE_DONE = 10009
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(self, symbols=("XAUUSD",), bid=4469.95, ask=4470.05,
                 balance=10000.0, equity=10050.0):
        self._symbols = list(symbols)
        self._bid, self._ask = bid, ask
        self._balance, self._equity = balance, equity
        self.requests: list[dict] = []        # every order_send request
        self._positions: list[SimpleNamespace] = []
        self._deal_profit = 73.5
        self._ticket_seq = 700000
        self.initialized = False
        self.shutdown_called = False

    # --- lifecycle -------------------------------------------------------
    def initialize(self, **kwargs) -> bool:
        self.init_kwargs = kwargs
        self.initialized = True
        return True

    def last_error(self):
        return (0, "ok")

    def shutdown(self):
        self.shutdown_called = True

    # --- symbols / market data ------------------------------------------
    def symbols_get(self):
        return [SimpleNamespace(name=n) for n in self._symbols]

    def symbol_select(self, name, on):
        return True

    def symbol_info(self, name):
        if name not in self._symbols:
            return None
        return SimpleNamespace(
            digits=2, point=0.01, trade_tick_size=0.01, trade_tick_value=1.0,
            volume_min=0.01, volume_max=100.0, volume_step=0.01,
            trade_contract_size=100.0, trade_stops_level=0,
            filling_mode=2,  # IOC supported
        )

    def symbol_info_tick(self, name):
        return SimpleNamespace(bid=self._bid, ask=self._ask)

    def account_info(self):
        return SimpleNamespace(login=12345, server="FakeBroker", currency="USD",
                               balance=self._balance, equity=self._equity)

    # --- positions -------------------------------------------------------
    def add_position(self, *, ticket, type, volume, price_open, sl, tp, magic,
                     symbol="XAUUSD", comment="", profit=0.0):
        self._positions.append(SimpleNamespace(
            ticket=ticket, type=type, volume=volume, price_open=price_open,
            sl=sl, tp=tp, magic=magic, symbol=symbol, comment=comment,
            profit=profit))

    def positions_get(self, ticket=None, symbol=None):
        out = self._positions
        if ticket is not None:
            out = [p for p in out if p.ticket == ticket]
        if symbol is not None:
            out = [p for p in out if p.symbol == symbol]
        return list(out)

    def history_deals_get(self, dt_from, dt_to, position=None):
        return [SimpleNamespace(profit=self._deal_profit, swap=0.0, commission=0.0)]

    # --- order entry -----------------------------------------------------
    def order_send(self, request: dict[str, Any]):
        self.requests.append(dict(request))
        self._ticket_seq += 1
        # Emulate a successful fill.
        return SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            order=self._ticket_seq,
            deal=self._ticket_seq,
            price=request.get("price", self._ask),
            volume=request.get("volume", 0.0),
            comment="done",
        )

    @property
    def last_request(self) -> dict:
        return self.requests[-1] if self.requests else {}


class FailingMT5(FakeMT5):
    """order_send always rejects — to test error handling."""

    def order_send(self, request):
        self.requests.append(dict(request))
        return SimpleNamespace(retcode=10013, order=0, price=0.0, volume=0.0,
                               comment="rejected")
