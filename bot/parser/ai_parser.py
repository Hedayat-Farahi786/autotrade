"""Claude (Anthropic) LLM-backed signal parser.

Asks Claude to return a strict, schema-validated list of intents via tool-use,
mapped onto the bot's :class:`~bot.models.Intent` objects by the shared
:mod:`bot.parser.intent_builder`.

Latency: defaults to the fastest model (Haiku) and caches the large system
prompt via ``cache_control`` so repeat calls are quick and cheap. See
:class:`bot.parser.gemini_parser.GeminiParser` for an often-faster alternative.
"""
from __future__ import annotations

import time
from typing import List, Optional

from ..logger import audit, get_logger
from ..models import Intent
from .intent_builder import INTENTS_SCHEMA, SYSTEM_PROMPT, payload_to_intents

log = get_logger("ai_parser")

try:
    import anthropic  # type: ignore

    _SDK = True
except Exception:  # noqa: BLE001
    anthropic = None  # type: ignore
    _SDK = False


TOOL = {
    "name": "emit_intents",
    "description": "Emit the structured list of trading intents extracted from the message.",
    "input_schema": INTENTS_SCHEMA,
}


class AIParser:
    provider = "anthropic"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-haiku-4-5-20251001",
        default_symbol: str = "XAUUSD",
        max_tokens: int = 600,
        timeout: float = 8.0,
    ) -> None:
        if not _SDK:
            raise RuntimeError(
                "anthropic package not installed. `pip install anthropic` to use "
                "the Claude parser."
            )
        self.model = model
        self.default_symbol = default_symbol
        self.max_tokens = max_tokens
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout)

    async def parse(self, text: Optional[str], message_id: Optional[int] = None) -> List[Intent]:
        if not text or not text.strip():
            return []
        text = text.strip()
        t0 = time.time()
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=0,
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[TOOL],
                tool_choice={"type": "tool", "name": "emit_intents"},
                messages=[{"role": "user", "content": f"MESSAGE:\n{text}"}],
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Claude parse failed (%s); message left unparsed.", exc)
            audit("ai_parse_error", provider="anthropic", error=str(exc),
                  message_id=message_id)
            return []

        payload = self._extract_tool_input(resp)
        latency_ms = round((time.time() - t0) * 1000)
        if payload is None:
            log.warning("Claude returned no tool call.")
            return []
        intents = payload_to_intents(payload, text, message_id,
                                     self.default_symbol, "ai")
        audit("ai_parse", provider="anthropic", message_id=message_id,
              latency_ms=latency_ms, count=len(intents),
              types=[i.type.value for i in intents])
        log.debug("Claude parse %sms → %s", latency_ms,
                  [i.type.value for i in intents])
        return intents

    @staticmethod
    def _extract_tool_input(resp) -> Optional[dict]:
        for block in getattr(resp, "content", []) or []:
            if getattr(block, "type", None) == "tool_use":
                return block.input
        return None
