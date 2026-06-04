"""MetaTrader 5 execution layer.

The official ``MetaTrader5`` package is synchronous and Windows-only, and the
terminal is **not** thread-safe. To keep the asyncio event loop responsive we
run every blocking MT5 call inside a single dedicated worker thread (a
1-worker ``ThreadPoolExecutor``) and ``await`` it from the loop. This gives us
serialised, low-latency access without ever stalling the Telegram listener.

When the package is unavailable (e.g. running on Linux for development) or when
``DRY_RUN`` is enabled, a fully-featured in-memory simulator stands in so the
whole pipeline can be exercised end-to-end.
"""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import MT5Config
from ..logger import audit, get_logger

log = get_logger("mt5")

try:  # pragma: no cover - import guarded for non-Windows dev machines
    import MetaTrader5 as mt5  # type: ignore

    _MT5_AVAILABLE = True
except Exception:  # noqa: BLE001
    mt5 = None  # type: ignore
    _MT5_AVAILABLE = False


@dataclass
class OrderResult:
    ok: bool
    ticket: Optional[int] = None
    price: Optional[float] = None
    volume: Optional[float] = None
    comment: str = ""
    raw: Any = None
    profit: Optional[float] = None      # realized P/L for closes


@dataclass
class SymbolSpec:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    contract_size: float
    stops_level: int  # min distance in points for SL/TP


# --------------------------------------------------------------------------- #
#  Simulator
# --------------------------------------------------------------------------- #
@dataclass
class _SimPosition:
    ticket: int
    symbol: str
    type: int  # 0 buy, 1 sell
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int
    comment: str
    profit: float = 0.0


class _Simulator:
    """A small but faithful MT5 stand-in for dry-run / non-Windows use."""

    def __init__(self, symbol: str, start_balance: float = 10000.0) -> None:
        self._symbol = symbol
        self.balance = start_balance
        self.equity = start_balance
        self._next_ticket = 500000
        self.positions: Dict[int, _SimPosition] = {}
        # Seed a plausible XAUUSD price; updated on each entry.
        self._price = 4470.0

    def _new_ticket(self) -> int:
        self._next_ticket += 1
        return self._next_ticket

    def symbol_spec(self) -> SymbolSpec:
        return SymbolSpec(
            name=self._symbol,
            digits=2,
            point=0.01,
            tick_size=0.01,
            tick_value=1.0,  # ~$1 per 0.01 move per 1.0 lot (100oz) → approx
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            contract_size=100.0,
            stops_level=0,
        )

    def tick(self) -> Dict[str, float]:
        return {"bid": self._price - 0.05, "ask": self._price + 0.05}

    def set_reference_price(self, price: float) -> None:
        self._price = price

    def open(self, *, type_: int, volume: float, price: float, sl: float,
             tp: float, magic: int, comment: str) -> OrderResult:
        t = self._new_ticket()
        self.positions[t] = _SimPosition(
            ticket=t, symbol=self._symbol, type=type_, volume=round(volume, 2),
            price_open=price, sl=sl, tp=tp, magic=magic, comment=comment,
        )
        return OrderResult(ok=True, ticket=t, price=price, volume=volume,
                           comment="sim-open")

    def modify(self, ticket: int, sl: Optional[float], tp: Optional[float]) -> OrderResult:
        pos = self.positions.get(ticket)
        if not pos:
            return OrderResult(ok=False, comment="sim: position not found")
        if sl is not None:
            pos.sl = sl
        if tp is not None:
            pos.tp = tp
        return OrderResult(ok=True, ticket=ticket, comment="sim-modify")

    def close(self, ticket: int, volume: Optional[float],
              price: Optional[float] = None) -> OrderResult:
        pos = self.positions.get(ticket)
        if not pos:
            return OrderResult(ok=False, comment="sim: position not found")
        vol = pos.volume if volume is None else min(volume, pos.volume)
        tick = self.tick()
        is_buy = pos.type == 0
        fill = price if price is not None else (tick["bid"] if is_buy else tick["ask"])
        sign = 1.0 if is_buy else -1.0
        contract = self.symbol_spec().contract_size
        profit = round((fill - pos.price_open) * sign * vol * contract, 2)
        self.balance = round(self.balance + profit, 2)
        self.equity = self.balance
        pos.volume = round(pos.volume - vol, 2)
        if pos.volume <= 0:
            del self.positions[ticket]
        return OrderResult(ok=True, ticket=ticket, volume=vol, profit=profit,
                           comment="sim-close")


# --------------------------------------------------------------------------- #
#  Executor
# --------------------------------------------------------------------------- #
class MT5Executor:
    def __init__(self, cfg: MT5Config, dry_run: bool = True) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.simulate = dry_run or not _MT5_AVAILABLE
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")
        self._lock = threading.Lock()
        self._sim: Optional[_Simulator] = None
        self._symbol: str = cfg.symbol
        self._spec: Optional[SymbolSpec] = None
        self._connected = False
        if self.simulate:
            self._sim = _Simulator(cfg.symbol)

    # ----- lifecycle -------------------------------------------------------
    async def connect(self) -> bool:
        return await self._run(self._connect_sync)

    def _connect_sync(self) -> bool:
        if self.simulate:
            self._spec = self._sim.symbol_spec()  # type: ignore[union-attr]
            self._connected = True
            mode = "DRY-RUN" if self.dry_run else "SIM (MT5 package missing)"
            log.warning("MT5 executor running in %s mode — no real orders.", mode)
            return True

        kwargs: Dict[str, Any] = {}
        if self.cfg.terminal_path:
            kwargs["path"] = self.cfg.terminal_path
        if self.cfg.login and self.cfg.password and self.cfg.server:
            kwargs.update(
                login=int(self.cfg.login),
                password=self.cfg.password,
                server=self.cfg.server,
            )
        if not mt5.initialize(**kwargs):  # type: ignore[union-attr]
            log.error("mt5.initialize failed: %s", mt5.last_error())  # type: ignore
            return False

        self._symbol = self._resolve_symbol()
        if not self._symbol:
            log.error("Could not resolve a tradable symbol for %s", self.cfg.symbol)
            return False
        mt5.symbol_select(self._symbol, True)  # type: ignore[union-attr]
        self._spec = self._load_symbol_spec(self._symbol)
        self._connected = self._spec is not None
        if self._connected:
            info = mt5.account_info()  # type: ignore[union-attr]
            log.info(
                "Connected to MT5: account=%s server=%s balance=%s symbol=%s",
                getattr(info, "login", "?"), getattr(info, "server", "?"),
                getattr(info, "balance", "?"), self._symbol,
            )
        return self._connected

    def _resolve_symbol(self) -> str:
        candidates = [self.cfg.symbol] + list(self.cfg.symbol_overrides)
        all_syms = mt5.symbols_get()  # type: ignore[union-attr]
        names = {s.name for s in all_syms} if all_syms else set()
        for c in candidates:
            if c in names:
                return c
        # Fuzzy: first symbol that starts with XAUUSD.
        for n in names:
            if n.upper().startswith("XAUUSD"):
                return n
        return self.cfg.symbol

    def _load_symbol_spec(self, name: str) -> Optional[SymbolSpec]:
        si = mt5.symbol_info(name)  # type: ignore[union-attr]
        if si is None:
            return None
        return SymbolSpec(
            name=name,
            digits=si.digits,
            point=si.point,
            tick_size=si.trade_tick_size or si.point,
            tick_value=si.trade_tick_value or 1.0,
            volume_min=si.volume_min,
            volume_max=si.volume_max,
            volume_step=si.volume_step or 0.01,
            contract_size=si.trade_contract_size or 100.0,
            stops_level=getattr(si, "trade_stops_level", 0),
        )

    async def shutdown(self) -> None:
        def _sd() -> None:
            if not self.simulate and _MT5_AVAILABLE:
                mt5.shutdown()  # type: ignore[union-attr]
        await self._run(_sd)
        self._pool.shutdown(wait=False)

    # ----- account / market data ------------------------------------------
    @property
    def spec(self) -> Optional[SymbolSpec]:
        return self._spec

    @property
    def symbol(self) -> str:
        return self._symbol

    async def account_balance(self) -> float:
        def _bal() -> float:
            if self.simulate:
                return self._sim.balance  # type: ignore[union-attr]
            info = mt5.account_info()  # type: ignore[union-attr]
            return float(info.balance) if info else 0.0
        return await self._run(_bal)

    async def account_equity(self) -> float:
        def _eq() -> float:
            if self.simulate:
                return self._sim.equity  # type: ignore[union-attr]
            info = mt5.account_info()  # type: ignore[union-attr]
            return float(info.equity) if info else 0.0
        return await self._run(_eq)

    async def current_price(self) -> Dict[str, float]:
        def _tick() -> Dict[str, float]:
            if self.simulate:
                return self._sim.tick()  # type: ignore[union-attr]
            t = mt5.symbol_info_tick(self._symbol)  # type: ignore[union-attr]
            return {"bid": t.bid, "ask": t.ask} if t else {"bid": 0.0, "ask": 0.0}
        return await self._run(_tick)

    # ----- trading ---------------------------------------------------------
    async def open_position(
        self,
        *,
        direction: str,
        volume: float,
        price: Optional[float],
        sl: Optional[float],
        tp: Optional[float],
        order_kind: str,
        magic: int,
        comment: str,
    ) -> OrderResult:
        return await self._run(
            self._open_sync, direction, volume, price, sl, tp, order_kind,
            magic, comment,
        )

    def _open_sync(self, direction, volume, price, sl, tp, order_kind, magic,
                   comment) -> OrderResult:
        is_buy = direction.upper() == "BUY"
        if self.simulate:
            if price:
                self._sim.set_reference_price(price)  # type: ignore[union-attr]
            tick = self._sim.tick()  # type: ignore[union-attr]
            fill = price or (tick["ask"] if is_buy else tick["bid"])
            res = self._sim.open(  # type: ignore[union-attr]
                type_=0 if is_buy else 1, volume=volume, price=fill,
                sl=sl or 0.0, tp=tp or 0.0, magic=magic, comment=comment,
            )
            audit("mt5_open", sim=True, direction=direction, volume=volume,
                  price=fill, sl=sl, tp=tp, magic=magic, comment=comment,
                  ticket=res.ticket)
            return res

        tick = mt5.symbol_info_tick(self._symbol)  # type: ignore[union-attr]
        if order_kind.upper() == "MARKET" or price is None:
            order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL  # type: ignore
            fill = tick.ask if is_buy else tick.bid
            action = mt5.TRADE_ACTION_DEAL  # type: ignore
        else:
            kind = order_kind.upper()
            if is_buy:
                order_type = (mt5.ORDER_TYPE_BUY_LIMIT if kind == "LIMIT"  # type: ignore
                              else mt5.ORDER_TYPE_BUY_STOP)
            else:
                order_type = (mt5.ORDER_TYPE_SELL_LIMIT if kind == "LIMIT"  # type: ignore
                              else mt5.ORDER_TYPE_SELL_STOP)
            fill = price
            action = mt5.TRADE_ACTION_PENDING  # type: ignore

        request: Dict[str, Any] = {
            "action": action,
            "symbol": self._symbol,
            "volume": float(volume),
            "type": order_type,
            "price": float(fill),
            "deviation": self.cfg.deviation_points,
            "magic": int(magic),
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,  # type: ignore
            "type_filling": self._filling_mode(),
        }
        if sl:
            request["sl"] = float(sl)
        if tp:
            request["tp"] = float(tp)

        result = mt5.order_send(request)  # type: ignore[union-attr]
        ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE  # type: ignore
        audit("mt5_open", sim=False, ok=ok, direction=direction, volume=volume,
              price=fill, sl=sl, tp=tp, magic=magic, comment=comment,
              retcode=getattr(result, "retcode", None),
              ticket=getattr(result, "order", None))
        if not ok:
            log.error("order_send failed: %s | request=%s", result, request)
            return OrderResult(ok=False, comment=str(getattr(result, "comment", "")),
                               raw=result)
        return OrderResult(ok=True, ticket=result.order, price=result.price,
                           volume=result.volume, comment="ok", raw=result)

    def _filling_mode(self):
        # Pick a filling mode the symbol supports; default to IOC.
        try:
            si = mt5.symbol_info(self._symbol)  # type: ignore[union-attr]
            mode = si.filling_mode if si else 0
            if mode & 1:
                return mt5.ORDER_FILLING_FOK  # type: ignore
            if mode & 2:
                return mt5.ORDER_FILLING_IOC  # type: ignore
        except Exception:  # noqa: BLE001
            pass
        return mt5.ORDER_FILLING_IOC  # type: ignore

    async def modify_position(self, ticket: int, sl: Optional[float] = None,
                              tp: Optional[float] = None) -> OrderResult:
        return await self._run(self._modify_sync, ticket, sl, tp)

    def _modify_sync(self, ticket: int, sl, tp) -> OrderResult:
        if self.simulate:
            res = self._sim.modify(ticket, sl, tp)  # type: ignore[union-attr]
            audit("mt5_modify", sim=True, ticket=ticket, sl=sl, tp=tp, ok=res.ok)
            return res
        pos = self._find_position(ticket)
        if not pos:
            return OrderResult(ok=False, comment="position not found")
        request = {
            "action": mt5.TRADE_ACTION_SLTP,  # type: ignore
            "symbol": pos.symbol,
            "position": ticket,
            "sl": float(sl) if sl is not None else pos.sl,
            "tp": float(tp) if tp is not None else pos.tp,
            "magic": pos.magic,
        }
        result = mt5.order_send(request)  # type: ignore[union-attr]
        ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE  # type: ignore
        audit("mt5_modify", sim=False, ticket=ticket, sl=sl, tp=tp, ok=ok,
              retcode=getattr(result, "retcode", None))
        if not ok:
            log.error("modify failed for %s: %s", ticket, result)
        return OrderResult(ok=ok, ticket=ticket, comment=str(getattr(result, "comment", "")),
                           raw=result)

    async def close_position(self, ticket: int, volume: Optional[float] = None,
                             price: Optional[float] = None) -> OrderResult:
        return await self._run(self._close_sync, ticket, volume, price)

    def _close_sync(self, ticket: int, volume, price=None) -> OrderResult:
        if self.simulate:
            res = self._sim.close(ticket, volume, price)  # type: ignore[union-attr]
            audit("mt5_close", sim=True, ticket=ticket, volume=res.volume,
                  ok=res.ok, profit=res.profit)
            return res
        pos = self._find_position(ticket)
        if not pos:
            return OrderResult(ok=False, comment="position not found")
        is_buy = pos.type == mt5.POSITION_TYPE_BUY  # type: ignore
        tick = mt5.symbol_info_tick(pos.symbol)  # type: ignore[union-attr]
        close_vol = pos.volume if volume is None else min(volume, pos.volume)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,  # type: ignore
            "symbol": pos.symbol,
            "position": ticket,
            "volume": float(close_vol),
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,  # type: ignore
            "price": tick.bid if is_buy else tick.ask,
            "deviation": self.cfg.deviation_points,
            "magic": pos.magic,
            "comment": "close",
            "type_time": mt5.ORDER_TIME_GTC,  # type: ignore
            "type_filling": self._filling_mode(),
        }
        result = mt5.order_send(request)  # type: ignore[union-attr]
        ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE  # type: ignore
        profit = self._deal_profit(ticket) if ok else None
        audit("mt5_close", sim=False, ticket=ticket, volume=close_vol, ok=ok,
              profit=profit, retcode=getattr(result, "retcode", None))
        return OrderResult(ok=ok, ticket=ticket, volume=close_vol, profit=profit,
                           comment=str(getattr(result, "comment", "")), raw=result)

    def _deal_profit(self, position_ticket: int) -> Optional[float]:
        """Sum realized profit of deals belonging to a position (live MT5)."""

        try:
            import time as _t

            deals = mt5.history_deals_get(  # type: ignore[union-attr]
                _t.time() - 7 * 86400, _t.time() + 60, position=position_ticket)
            if not deals:
                return None
            return round(sum(d.profit + d.swap + d.commission for d in deals), 2)
        except Exception:  # noqa: BLE001
            return None

    async def position_profit(self, ticket: int) -> Optional[float]:
        """Realized profit for a position already closed at the broker."""

        return await self._run(self._deal_profit, ticket)

    def _find_position(self, ticket: int):
        positions = mt5.positions_get(ticket=ticket)  # type: ignore[union-attr]
        return positions[0] if positions else None

    async def positions_by_magic(self, magic: int) -> List[Dict[str, Any]]:
        return await self._run(self._positions_by_magic_sync, magic)

    def _positions_by_magic_sync(self, magic: int) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if self.simulate:
            for p in self._sim.positions.values():  # type: ignore[union-attr]
                if p.magic == magic:
                    out.append(self._sim_pos_dict(p))
            return out
        positions = mt5.positions_get(symbol=self._symbol)  # type: ignore[union-attr]
        for p in positions or []:
            if p.magic == magic:
                out.append({
                    "ticket": p.ticket, "volume": p.volume, "type": p.type,
                    "price_open": p.price_open, "sl": p.sl, "tp": p.tp,
                    "magic": p.magic, "comment": p.comment, "profit": p.profit,
                })
        return out

    @staticmethod
    def _sim_pos_dict(p: _SimPosition) -> Dict[str, Any]:
        return {"ticket": p.ticket, "volume": p.volume, "type": p.type,
                "price_open": p.price_open, "sl": p.sl, "tp": p.tp,
                "magic": p.magic, "comment": p.comment, "profit": p.profit}

    # ----- helpers ---------------------------------------------------------
    def normalize_volume(self, volume: float) -> float:
        spec = self._spec
        if not spec:
            return round(volume, 2)
        step = spec.volume_step or 0.01
        v = max(spec.volume_min, min(volume, spec.volume_max))
        # Round down to the nearest step to never exceed intended risk.
        steps = int(v / step + 1e-9)
        v = round(steps * step, 8)
        return max(spec.volume_min, round(v, 2))

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, lambda: self._guard(fn, *args))

    def _guard(self, fn, *args):
        with self._lock:
            try:
                return fn(*args)
            except Exception as exc:  # noqa: BLE001
                log.exception("MT5 call failed: %s", exc)
                return OrderResult(ok=False, comment=f"exception: {exc}")
