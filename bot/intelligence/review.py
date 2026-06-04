"""Parser self-learning queue.

The parser can never be "finished" — the channel invents new phrasings. Rather
than silently dropping anything ambiguous, we log it for review so the system
*evolves*: messages that look trade-related but the parser was unsure about (or
where the fast regex and the AI disagree) are appended to a queue a human can
skim and turn into a new pattern or a test case.

This is the feedback loop that keeps the parser improving over time.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from ..logger import audit, get_logger
from ..models import Intent, IntentType

log = get_logger("review")

# Does the text look like it *should* have been actionable?
_TRADE_HINT = re.compile(
    r"\b(buy|sell|long|short|gold|xau|sl|stop\s*loss|tp|take\s*profit|"
    r"break\s*even|breakeven|entry|entries|close|partial|secure|profit|"
    r"target|pips?)\b",
    re.I,
)


class ReviewLogger:
    def __init__(self, review_file: str, min_confidence: float = 0.5) -> None:
        self.review_file = review_file
        self.min_confidence = min_confidence
        self._lock = threading.Lock()
        self._count = 0

    def consider(
        self,
        text: str,
        intents: list[Intent],
        message_id: int | None,
        ai_intents: list[Intent] | None = None,
    ) -> bool:
        """Queue the message if the parse looks uncertain. Returns True if queued."""

        reason = self._uncertainty_reason(text, intents, ai_intents)
        if reason is None:
            return False
        entry = {
            "ts": round(time.time(), 3),
            "message_id": message_id,
            "text": text,
            "reason": reason,
            "parsed": [self._brief(i) for i in intents],
            "resolved": False,
        }
        if ai_intents is not None:
            entry["ai_parsed"] = [self._brief(i) for i in ai_intents]
        self._append(entry)
        self._count += 1
        audit("review_queued", message_id=message_id, reason=reason, text=text)
        log.info("Queued for review (%s): %.60s", reason, text.replace("\n", " "))
        return True

    def _uncertainty_reason(self, text: str, intents: list[Intent],
                            ai_intents: list[Intent] | None) -> str | None:
        all_noise = bool(intents) and all(i.type is IntentType.NOISE for i in intents)
        looks_trade = bool(_TRADE_HINT.search(text or ""))

        # 1) Regex/AI disagree about whether it's actionable.
        if ai_intents is not None:
            ai_actionable = any(i.type is not IntentType.NOISE for i in ai_intents)
            rx_actionable = any(i.type is not IntentType.NOISE for i in intents)
            if ai_actionable != rx_actionable:
                return "parser_disagreement"

        # 2) Looked like a trade message but produced only noise.
        if all_noise and looks_trade:
            return "trade_hint_but_noise"

        # 3) Low-confidence actionable parse.
        for i in intents:
            if i.type is not IntentType.NOISE and i.confidence < self.min_confidence:
                return "low_confidence"
        return None

    @staticmethod
    def _brief(i: Intent) -> dict:
        return {"type": i.type.value, "rule": i.matched_rule,
                "confidence": i.confidence}

    def _append(self, entry: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.review_file) or ".", exist_ok=True)
            with self._lock:
                with open(self.review_file, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to write review entry: %s", exc)

    @property
    def count(self) -> int:
        return self._count
