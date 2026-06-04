"""Event-driven backtester: replay messages against price history.

Feed a list of channel messages (with timestamps) and a price series, and this
simulates the *exact same* parsing and management logic the live bot uses,
filling each leg at its TP/SL as price moves. It produces the same performance
metrics as live trading (win rate, profit factor, expectancy, avg R, drawdown)
plus an equity curve — so you can validate the edge on history before risking
money.

Sizing is expressed in **risk units (R)**: each leg risks a fixed notional
(``risk_money``), so profit is ``R × risk_money`` and results are comparable
regardless of account size. This keeps the backtest honest and size-independent.

Pure and synchronous → fully unit-testable. Not a guarantee of future results.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .analytics.performance import summarize
from .logger import get_logger
from .models import IntentType
from .parser.signal_parser import SignalParser

log = get_logger("backtest")


@dataclass
class _Leg:
    signal_id: int
    direction: str
    symbol: str
    entry: float
    sl: float | None
    tp: float | None
    label: str
    is_runner: bool
    volume: float
    opened_ts: float
    risk: float


@dataclass
class BacktestResult:
    performance: dict
    equity_curve: list[dict]
    trades: list[dict]

    def report(self) -> str:
        p = self.performance
        if not p["trades"]:
            return "No trades generated from the supplied messages/prices."
        return (
            f"Backtest: {p['trades']} trades · win {p['win_rate']*100:.0f}% · "
            f"PF {p.get('profit_factor')} · expectancy {p['expectancy']:.2f} · "
            f"avg R {p.get('avg_r')} · net {p['net_profit']:.2f} · "
            f"max DD {p['max_drawdown']:.2f}"
        )


def _norm_bar(bar) -> tuple[float, float, float, float]:
    """Return (ts, high, low, close) from a dict or tuple bar."""

    if isinstance(bar, dict):
        ts = bar.get("ts") or bar.get("time") or 0
        close = bar.get("close", bar.get("price"))
        high = bar.get("high", close)
        low = bar.get("low", close)
        return float(ts), float(high), float(low), float(close)
    if len(bar) >= 4:
        return float(bar[0]), float(bar[1]), float(bar[2]), float(bar[3])
    # (ts, price)
    return float(bar[0]), float(bar[1]), float(bar[1]), float(bar[1])


def _norm_msg(msg) -> tuple[float, str]:
    if isinstance(msg, dict):
        return float(msg.get("ts", 0)), str(msg.get("text", ""))
    return float(msg[0]), str(msg[1])


class BacktestEngine:
    def __init__(self, risk_money: float = 100.0, start_balance: float = 10000.0,
                 default_symbol: str = "XAUUSD") -> None:
        self.parser = SignalParser(default_symbol=default_symbol)
        self.risk_money = risk_money
        self.start_balance = start_balance
        self._open: list[_Leg] = []
        self._trades: list[dict] = []
        self._equity: list[dict] = []
        self._cum = 0.0
        self._next_sid = 1
        self._last_price = 0.0

    def run(self, messages: Sequence, bars: Sequence) -> BacktestResult:
        events: list[tuple[float, str, object]] = []
        for m in messages:
            ts, text = _norm_msg(m)
            events.append((ts, "msg", text))
        for b in bars:
            ts, hi, lo, close = _norm_bar(b)
            events.append((ts, "bar", (hi, lo, close)))
        # Bars before messages at the same timestamp so fills use fresh price.
        events.sort(key=lambda e: (e[0], 0 if e[1] == "bar" else 1))

        for ts, kind, payload in events:
            if kind == "bar":
                hi, lo, close = payload
                self._last_price = close
                self._check_fills(ts, hi, lo)
            else:
                self._on_message(ts, payload)

        # Close anything still open at the final price.
        for leg in list(self._open):
            self._close(leg, self._last_price, ts, "end")
        self._open.clear()

        return BacktestResult(
            performance=summarize(self._trades),
            equity_curve=self._equity,
            trades=self._trades,
        )

    # ------------------------------------------------------------------ #
    def _on_message(self, ts: float, text: str) -> None:
        for intent in self.parser.parse(text, message_id=None):
            t = intent.type
            if t is IntentType.ENTRY and intent.entry:
                self._open_entry(ts, intent.entry)
            elif t is IntentType.MODIFY_SL and intent.new_sl is not None:
                for leg in self._open:
                    leg.sl = intent.new_sl
            elif t is IntentType.BREAKEVEN:
                for leg in self._open:
                    leg.sl = leg.entry
            elif t is IntentType.PARTIAL_CLOSE:
                for leg in list(self._open):
                    self._close(leg, self._last_price, ts, "partial",
                                fraction=intent.close_fraction)
            elif t is IntentType.CLOSE_ALL:
                for leg in list(self._open):
                    self._close(leg, self._last_price, ts, "manual")

    def _open_entry(self, ts: float, entry) -> None:
        sid = self._next_sid
        self._next_sid += 1
        mid = entry.entry_mid if entry.entry_mid is not None else self._last_price
        if entry.sl is None or mid is None:
            return
        risk_per_unit = abs(mid - entry.sl)
        if risk_per_unit <= 0:
            return
        legs = entry.take_profits or []
        for tp in legs:
            tp_price = tp.price
            if tp.is_open:
                tp_price = None  # runner: exits on SL / management / end
            self._open.append(_Leg(
                signal_id=sid, direction=entry.direction.value,
                symbol=entry.symbol, entry=mid, sl=entry.sl, tp=tp_price,
                label=tp.label or "TP", is_runner=tp.is_open, volume=1.0,
                opened_ts=ts, risk=risk_per_unit,
            ))

    def _check_fills(self, ts: float, hi: float, lo: float) -> None:
        for leg in list(self._open):
            is_buy = leg.direction == "BUY"
            # Stop-loss first (conservative): if both SL and TP in a bar, assume SL.
            if leg.sl is not None and ((is_buy and lo <= leg.sl) or
                                       (not is_buy and hi >= leg.sl)):
                self._close(leg, leg.sl, ts, "sl")
                continue
            if leg.tp is not None and ((is_buy and hi >= leg.tp) or
                                       (not is_buy and lo <= leg.tp)):
                self._close(leg, leg.tp, ts, "tp")

    def _close(self, leg: _Leg, price: float, ts: float, reason: str,
               fraction: float = 1.0) -> None:
        if leg not in self._open:
            return
        frac = max(0.05, min(fraction, 1.0))
        sign = 1.0 if leg.direction == "BUY" else -1.0
        r = ((price - leg.entry) * sign) / leg.risk if leg.risk else 0.0
        risk_slice = self.risk_money * (leg.volume * frac)
        profit = round(r * risk_slice, 2)
        self._cum = round(self._cum + profit, 2)
        self._equity.append({"t": ts, "equity": self._cum})
        self._trades.append({
            "signal_id": leg.signal_id, "ticket": 0, "direction": leg.direction,
            "symbol": leg.symbol, "label": leg.label, "volume": leg.volume * frac,
            "open_price": leg.entry, "close_price": price, "profit": profit,
            "risk_amount": round(risk_slice, 2), "reason": reason,
            "opened_at": leg.opened_ts, "closed_at": ts, "r_multiple": round(r, 3),
        })
        if frac >= 1.0:
            self._open.remove(leg)
        else:
            leg.volume = round(leg.volume * (1 - frac), 4)


def run_backtest(messages: Sequence, bars: Sequence, **kw) -> BacktestResult:
    return BacktestEngine(**kw).run(messages, bars)


# --------------------------------------------------------------------------- #
#  File loaders + CLI
# --------------------------------------------------------------------------- #
def load_messages(path: str) -> list[tuple[float, str]]:
    """Load messages from JSONL ({ts,text}) or a Telegram export JSON."""

    import datetime as dt
    import json

    out: list[tuple[float, str]] = []
    def _ts(date) -> float:
        try:
            return dt.datetime.fromisoformat(date).timestamp() if date else 0
        except Exception:  # noqa: BLE001
            return 0

    def _flatten(text):
        if isinstance(text, list):  # Telegram rich entities
            return "".join(t if isinstance(t, str) else t.get("text", "")
                           for t in text)
        return text

    with open(path, encoding="utf-8") as fh:
        content = fh.read()

    # Prefer a whole-file parse (Telegram Desktop export object, or a JSON array).
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict) and "messages" in data:  # Telegram export
        for m in data["messages"]:
            text = _flatten(m.get("text"))
            if text:
                out.append((_ts(m.get("date")), text))
        return out
    if isinstance(data, list):  # JSON array of {ts,text}
        for rec in data:
            out.append((float(rec.get("ts", 0)), str(rec.get("text", ""))))
        return out

    # Otherwise JSONL: one {ts,text} object per line.
    for line in content.splitlines():
        line = line.strip()
        if line:
            rec = json.loads(line)
            out.append((float(rec.get("ts", 0)), str(rec.get("text", ""))))
    return out


def load_prices(path: str) -> list[tuple[float, float, float, float]]:
    """Load OHLC/price bars from CSV (header: ts/time, high, low, close|price)."""

    import csv

    out: list[tuple[float, float, float, float]] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            r = {k.lower().strip(): v for k, v in row.items()}
            ts = float(r.get("ts") or r.get("time") or r.get("timestamp") or 0)
            close = float(r.get("close") or r.get("price") or r.get("c"))
            high = float(r.get("high") or r.get("h") or close)
            low = float(r.get("low") or r.get("l") or close)
            out.append((ts, high, low, close))
    return out


def run_cli(messages_path: str, prices_path: str) -> None:
    from .logger import setup_logging

    setup_logging("INFO", "logs")
    messages = load_messages(messages_path)
    bars = load_prices(prices_path)
    print(f"\nLoaded {len(messages)} message(s) and {len(bars)} price bar(s).")
    res = run_backtest(messages, bars)
    print("\n" + res.report())
    p = res.performance
    if p["trades"]:
        print(f"  wins {p['wins']} / losses {p['losses']} · "
              f"best {p['best']:+.2f} · worst {p['worst']:+.2f} · "
              f"gross +{p['gross_profit']:.0f} / -{p['gross_loss']:.0f}")
    print()
