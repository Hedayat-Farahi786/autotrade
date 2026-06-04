"""Trading-condition filters: sessions, news/volatility blackout, spread.

Pure, testable gating logic that decides whether *now* is an acceptable time to
open a new trade. Spread is checked separately by the trader (it needs a live
quote); sessions and blackout windows are evaluated here.
"""
from __future__ import annotations

import datetime as dt
from typing import List, Optional, Tuple

from .config import FiltersConfig
from .logger import get_logger

log = get_logger("filters")


def _parse_hhmm(s: str) -> Optional[int]:
    try:
        h, m = s.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:  # noqa: BLE001
        return None


class TradingFilters:
    def __init__(self, cfg: FiltersConfig) -> None:
        self.cfg = cfg
        self._sessions = self._parse_sessions(cfg.sessions)
        self._blackout = self._parse_blackout(cfg.news_blackout)

    def _parse_sessions(self, raw: List[str]) -> List[Tuple[int, int]]:
        out = []
        for item in raw:
            try:
                a, b = item.split("-")
                start, end = _parse_hhmm(a), _parse_hhmm(b)
                if start is not None and end is not None:
                    out.append((start, end))
            except Exception:  # noqa: BLE001
                log.warning("Ignoring malformed session window: %s", item)
        return out

    def _parse_blackout(self, raw: List[str]) -> List[Tuple[dt.datetime, dt.datetime]]:
        out = []
        for item in raw:
            try:
                a, b = item.split("/")
                start = dt.datetime.fromisoformat(a).replace(tzinfo=dt.timezone.utc)
                end = dt.datetime.fromisoformat(b).replace(tzinfo=dt.timezone.utc)
                out.append((start, end))
            except Exception:  # noqa: BLE001
                log.warning("Ignoring malformed blackout window: %s", item)
        return out

    def allowed(self, now: Optional[dt.datetime] = None) -> Tuple[bool, str]:
        """Is trading allowed at ``now`` (UTC)?  Returns (allowed, reason)."""

        if not self.cfg.enabled:
            return True, "filters disabled"
        now = now or dt.datetime.now(dt.timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=dt.timezone.utc)

        # News/volatility blackout.
        for start, end in self._blackout:
            if start <= now <= end:
                return False, f"news blackout until {end:%H:%M} UTC"

        # Session windows (if any configured).
        if self._sessions:
            minute = now.hour * 60 + now.minute
            for start, end in self._sessions:
                inside = (start <= minute <= end if start <= end
                          else minute >= start or minute <= end)  # wraps midnight
                if inside:
                    return True, "in session"
            return False, "outside trading session"

        return True, "ok"
