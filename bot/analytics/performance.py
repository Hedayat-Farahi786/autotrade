"""Performance analytics — the foundation of an evolving system.

Every closed trade is recorded as a :class:`TradeRecord` and appended to a JSONL
journal. :func:`summarize` turns a list of records into the metrics that
actually matter for judging and improving an edge:

* win rate, profit factor, expectancy
* average R-multiple (profit measured in units of risk)
* maximum drawdown and current win/loss streak

These metrics feed the signal scorer and the adaptive risk throttle, closing the
loop: outcomes → measurement → adapted future behaviour.

Honest note: measurement makes the system *disciplined and self-correcting*, not
clairvoyant. It cannot guarantee profit — nothing can.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field

from ..logger import audit, get_logger

log = get_logger("analytics")


@dataclass
class TradeRecord:
    signal_id: int
    ticket: int
    direction: str
    symbol: str
    label: str                # which leg, e.g. "TP1" / "TP_OPEN"
    volume: float
    open_price: float
    close_price: float
    profit: float             # account currency
    risk_amount: float        # money that was at risk (open→SL)
    reason: str               # tp | sl | partial | manual | breakeven
    opened_at: float
    closed_at: float = field(default_factory=time.time)

    @property
    def r_multiple(self) -> float | None:
        if self.risk_amount and self.risk_amount > 0:
            return round(self.profit / self.risk_amount, 3)
        return None

    @property
    def won(self) -> bool:
        return self.profit > 0


def summarize(records: list[dict]) -> dict:
    """Pure function: aggregate raw record dicts into performance metrics."""

    closed = [r for r in records if r.get("close_price") is not None]
    n = len(closed)
    if n == 0:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "net_profit": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
            "profit_factor": None, "expectancy": 0.0, "avg_win": 0.0,
            "avg_loss": 0.0, "avg_r": None, "total_r": 0.0,
            "max_drawdown": 0.0, "streak": 0, "best": 0.0, "worst": 0.0,
        }

    profits = [float(r.get("profit", 0.0)) for r in closed]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net = sum(profits)

    rs = [r["profit"] / r["risk_amount"] for r in closed
          if r.get("risk_amount")]
    avg_r = round(sum(rs) / len(rs), 3) if rs else None

    # Max drawdown over the cumulative-profit equity curve.
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for p in profits:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    # Current streak (sign of the tail run).
    streak = 0
    for p in reversed(profits):
        if p > 0 and streak >= 0:
            streak += 1
        elif p <= 0 and streak <= 0:
            streak -= 1
        else:
            break

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 4),
        "net_profit": round(net, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": (round(gross_profit / gross_loss, 3)
                          if gross_loss > 0 else None),
        "expectancy": round(net / n, 2),
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "avg_r": avg_r,
        "total_r": round(sum(rs), 3) if rs else 0.0,
        "max_drawdown": round(max_dd, 2),
        "streak": streak,
        "best": round(max(profits), 2),
        "worst": round(min(profits), 2),
    }


class PerformanceTracker:
    """Append-only trade journal with cached, queryable metrics."""

    def __init__(self, trades_file: str) -> None:
        self.trades_file = trades_file
        self._records: list[dict] = []
        self._lock = threading.RLock()
        self._load()

    def record(self, trade: TradeRecord) -> None:
        with self._lock:
            data = asdict(trade)
            data["r_multiple"] = trade.r_multiple
            self._records.append(data)
            self._append(data)
        audit("trade_closed", **{k: data[k] for k in (
            "signal_id", "ticket", "label", "profit", "risk_amount", "reason")},
            r_multiple=trade.r_multiple)
        log.info("Trade closed: signal #%s %s %s profit=%.2f R=%s (%s)",
                 trade.signal_id, trade.label, trade.direction, trade.profit,
                 trade.r_multiple, trade.reason)

    def summary(self) -> dict:
        with self._lock:
            return summarize(list(self._records))

    def recent(self, n: int = 20) -> list[dict]:
        with self._lock:
            return self._records[-n:]

    # ----- persistence -----------------------------------------------------
    def _append(self, data: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.trades_file) or ".", exist_ok=True)
            with open(self.trades_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(data, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to persist trade: %s", exc)

    def _load(self) -> None:
        if not os.path.exists(self.trades_file):
            return
        try:
            with open(self.trades_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self._records.append(json.loads(line))
            log.info("Loaded %d historical trade(s).", len(self._records))
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to load trade journal: %s", exc)


def load_records(trades_file: str) -> list[dict]:
    """Read raw trade records from a journal file (used by the dashboard)."""

    out: list[dict] = []
    if not os.path.exists(trades_file):
        return out
    try:
        with open(trades_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except Exception:  # noqa: BLE001
        pass
    return out
