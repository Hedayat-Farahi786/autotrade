"""Unified async parser facade with selectable strategy and AI provider.

Modes (env ``PARSER_MODE``):

* ``regex``  — fast, deterministic, zero network latency. Best for the known
  GTMO formats.
* ``ai``     — every message goes to the configured LLM. Most robust to unseen
  phrasings; adds latency per message (Gemini Flash is typically the fastest).
* ``hybrid`` — (default, recommended) regex runs first (sub-millisecond); the AI
  parser is consulted **only** when regex is unsure (returned pure NOISE or low
  confidence while the text still looks trade-related). Speed of regex on the
  common path, robustness of AI on the long tail.

AI provider (env ``AI_PROVIDER``): ``gemini`` (default) or ``anthropic``.

All strategies expose ``async parse(text, message_id) -> List[Intent]``.
"""
from __future__ import annotations

import re

from ..logger import get_logger
from ..models import Intent, IntentType
from .signal_parser import SignalParser

log = get_logger("parser.facade")

# Cheap heuristic: does this text smell like it could be a trade instruction?
# Used by hybrid mode to decide whether an AI fallback is worth the latency.
_TRADE_HINT = re.compile(
    r"\b(buy|sell|long|short|gold|xau|sl|stop\s*loss|tp|take\s*profit|"
    r"break\s*even|breakeven|entry|entries|pips?|close|partial|secure|profit)\b",
    re.I,
)


class RegexParserAsync:
    """Async adapter over the synchronous :class:`SignalParser`."""

    def __init__(self, default_symbol: str = "XAUUSD") -> None:
        self._p = SignalParser(default_symbol=default_symbol)

    async def parse(self, text: str | None, message_id: int | None = None) -> list[Intent]:
        return self._p.parse(text, message_id)


class HybridParser:
    def __init__(self, regex_parser, ai_parser, min_confidence: float = 0.5) -> None:
        self._regex = regex_parser
        self._ai = ai_parser
        self.min_confidence = min_confidence

    async def parse(self, text: str | None, message_id: int | None = None) -> list[Intent]:
        intents = await self._regex.parse(text, message_id)
        if self._is_confident(intents):
            return intents
        if text and _TRADE_HINT.search(text):
            log.debug("Hybrid: regex unsure, consulting AI parser.")
            ai_intents = await self._ai.parse(text, message_id)
            if ai_intents and not self._all_noise(ai_intents):
                return ai_intents
        return intents

    def _is_confident(self, intents: list[Intent]) -> bool:
        if not intents:
            return False
        if self._all_noise(intents):
            return False
        return all(i.confidence >= self.min_confidence for i in intents)

    @staticmethod
    def _all_noise(intents: list[Intent]) -> bool:
        return all(i.type is IntentType.NOISE for i in intents)


def _build_ai_parser(provider: str, *, default_symbol: str, anthropic_api_key,
                     gemini_api_key, anthropic_model: str, gemini_model: str):
    """Instantiate the configured AI provider, or raise."""

    provider = (provider or "gemini").lower()
    if provider == "gemini":
        from .gemini_parser import GeminiParser

        return GeminiParser(api_key=gemini_api_key, model=gemini_model,
                            default_symbol=default_symbol)
    if provider in {"anthropic", "claude"}:
        from .ai_parser import AIParser

        return AIParser(api_key=anthropic_api_key, model=anthropic_model,
                        default_symbol=default_symbol)
    raise ValueError(f"Unknown AI_PROVIDER '{provider}' (use gemini|anthropic)")


def build_parser(
    mode: str,
    *,
    provider: str = "gemini",
    default_symbol: str = "XAUUSD",
    anthropic_api_key: str | None = None,
    gemini_api_key: str | None = None,
    anthropic_model: str = "claude-haiku-4-5-20251001",
    gemini_model: str = "gemini-2.5-flash",
):
    """Factory returning a parser object exposing ``async parse(...)``.

    Falls back to regex if AI is requested but the SDK/key is unavailable.
    """

    mode = (mode or "hybrid").lower()
    regex = RegexParserAsync(default_symbol=default_symbol)

    if mode == "regex":
        log.info("Parser mode: regex-only.")
        return regex

    try:
        ai = _build_ai_parser(
            provider, default_symbol=default_symbol,
            anthropic_api_key=anthropic_api_key, gemini_api_key=gemini_api_key,
            anthropic_model=anthropic_model, gemini_model=gemini_model,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("AI parser unavailable (%s). Falling back to regex-only.", exc)
        return regex

    if mode == "ai":
        log.info("Parser mode: AI-only (provider=%s).", getattr(ai, "provider", provider))
        return ai

    log.info("Parser mode: hybrid (regex → %s fallback).",
             getattr(ai, "provider", provider))
    return HybridParser(regex, ai)
