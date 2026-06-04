"""Signal intelligence — score each entry *before* risking money on it.

A signal is not automatically good just because it was posted. The scorer turns
an entry plus live context into a 0–1 quality score and a size factor, with
human-readable reasons. The trader uses it to **skip** weak setups and
**downsize** mediocre ones. Combined with the adaptive-risk throttle, this is
how the system gets more selective as it learns what works.

Scoring factors (all bounded, explainable — no black box):

* **Stop-loss present** — a signal without a defined risk is penalised hard.
* **Risk:reward** — distance to the nearest TP vs. the SL distance.
* **Not chasing** — if price has already run past the entry toward target, the
  remaining edge is smaller; far beyond the entry we skip.
* **Recent edge** — recent win rate / profit factor nudges the score up or down.

Nothing here predicts the market. It enforces discipline and consistency.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import IntelligenceConfig
from ..logger import get_logger
from ..models import EntrySignal

log = get_logger("scorer")


@dataclass
class Score:
    value: float                       # 0..1 overall quality
    take: bool                         # gate: should we act on it?
    size_factor: float                 # 0..1 multiplier on risk
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "value": round(self.value, 3),
            "take": self.take,
            "size_factor": round(self.size_factor, 3),
            "reasons": self.reasons,
        }


class SignalScorer:
    def __init__(self, cfg: IntelligenceConfig, pip_size: float = 0.1) -> None:
        self.cfg = cfg
        self.pip_size = pip_size

    def score(
        self,
        entry: EntrySignal,
        market_price: float,
        perf: dict | None = None,
    ) -> Score:
        reasons: list[str] = []
        components: list[float] = []

        # --- Stop-loss present ------------------------------------------
        if entry.sl is None:
            if self.cfg.require_sl:
                return Score(0.0, False, 0.0, ["no stop-loss → skipped"])
            reasons.append("no SL (penalised)")
            components.append(0.2)
        else:
            components.append(1.0)

        mid = entry.entry_mid if entry.entry_mid is not None else market_price
        sl_dist = abs(mid - entry.sl) if entry.sl is not None else None

        # --- Risk : reward ----------------------------------------------
        rr = self._risk_reward(entry, mid, sl_dist)
        if rr is not None:
            if rr < self.cfg.min_rr:
                reasons.append(f"R:R {rr:.2f} < min {self.cfg.min_rr}")
                if self.cfg.require_min_rr:
                    return Score(round(_mean(components) * 0.5, 3), False, 0.0,
                                 reasons + ["risk:reward too low → skipped"])
                components.append(0.3)
            else:
                reasons.append(f"R:R {rr:.2f}")
                components.append(min(1.0, rr / 2.0 + 0.4))

        # --- Not chasing -------------------------------------------------
        chase = self._chase_factor(entry, market_price, sl_dist)
        if chase is not None:
            comp, why, skip = chase
            reasons.append(why)
            if skip:
                return Score(round(_mean(components) * 0.5, 3), False, 0.0,
                             reasons + ["price ran too far → skipped"])
            components.append(comp)

        # --- Recent edge -------------------------------------------------
        edge = self._edge_factor(perf)
        if edge is not None:
            comp, why = edge
            reasons.append(why)
            components.append(comp)

        value = _mean(components)
        take = value >= self.cfg.min_signal_score
        # Map score to a conservative size factor in [min_size, 1.0].
        size_factor = self.cfg.min_size_factor + (1 - self.cfg.min_size_factor) * value
        size_factor = round(min(1.0, max(self.cfg.min_size_factor, size_factor)), 3)
        if not take:
            reasons.append(f"score {value:.2f} < min {self.cfg.min_signal_score} → skipped")
        return Score(round(value, 3), take, size_factor if take else 0.0, reasons)

    # ------------------------------------------------------------------ #
    def _risk_reward(self, entry: EntrySignal, mid: float,
                     sl_dist: float | None) -> float | None:
        if not sl_dist or sl_dist <= 0:
            return None
        tps = entry.concrete_tps
        if not tps:
            # Open runner only — assume the channel's hinted distance if any.
            runner = next((t for t in entry.take_profits if t.min_pips), None)
            if runner and runner.min_pips:
                reward = runner.min_pips * self.pip_size
                return round(reward / sl_dist, 2)
            return None
        nearest = min(tps, key=lambda t: abs(t.price - mid))
        reward = abs(nearest.price - mid)
        return round(reward / sl_dist, 2)

    def _chase_factor(self, entry: EntrySignal, market: float,
                      sl_dist: float | None):
        """How far has price moved past the entry toward target already?"""

        lo, hi = entry.entry_low, entry.entry_high
        if lo is None or hi is None or not sl_dist:
            return None
        is_buy = entry.direction.value == "BUY"
        # Distance market is beyond the favourable edge of the zone, toward TP.
        if is_buy:
            beyond = market - hi      # >0 means price already above zone
        else:
            beyond = lo - market      # >0 means price already below zone
        if beyond <= 0:
            return (1.0, "price in/at entry zone", False)
        ratio = beyond / sl_dist
        if ratio >= self.cfg.max_chase_ratio:
            return (0.0, f"price {ratio:.1f}×SL past entry", True)
        return (max(0.2, 1 - ratio), f"chasing {ratio:.1f}×SL", False)

    def _edge_factor(self, perf: dict | None):
        if not perf or perf.get("trades", 0) < self.cfg.min_trades_for_edge:
            return None
        wr = perf.get("win_rate", 0.0)
        pf = perf.get("profit_factor")
        # Centre on 0.5 win rate / 1.0 profit factor.
        comp = 0.5
        if pf is not None:
            comp = max(0.1, min(1.0, pf / 2.0))
        comp = (comp + wr) / 2
        return (round(comp, 3), f"recent edge wr={wr:.0%} pf={pf}")


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0
