"""Gemini (Google) LLM-backed signal parser.

Gemini Flash models are extremely fast and cheap, which makes them a great fit
for latency-sensitive signal parsing. We use JSON mode (``response_mime_type``
+ ``response_schema``) so the model returns exactly the structured shape the
shared :mod:`bot.parser.intent_builder` understands.

Uses the modern ``google-genai`` SDK with its native async client
(``client.aio``). Install with ``pip install google-genai``.
"""
from __future__ import annotations

import json
import time
from typing import List, Optional

from ..logger import audit, get_logger
from ..models import Intent, IntentType
from .intent_builder import INTENTS_SCHEMA, SYSTEM_PROMPT, payload_to_intents

log = get_logger("gemini_parser")

try:
    from google import genai  # type: ignore
    from google.genai import types as genai_types  # type: ignore

    _SDK = True
except Exception:  # noqa: BLE001
    genai = None  # type: ignore
    genai_types = None  # type: ignore
    _SDK = False


class GeminiParser:
    provider = "gemini"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-2.5-flash",
        default_symbol: str = "XAUUSD",
        max_tokens: int = 800,
        timeout: float = 8.0,
    ) -> None:
        if not _SDK:
            raise RuntimeError(
                "google-genai package not installed. `pip install google-genai` "
                "to use the Gemini parser."
            )
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is required for the Gemini parser.")
        self.model = model
        self.default_symbol = default_symbol
        # HTTP options carry the request timeout (milliseconds).
        http_opts = genai_types.HttpOptions(timeout=int(timeout * 1000))
        self._client = genai.Client(api_key=api_key, http_options=http_opts)
        self._gen_config = genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0,
            max_output_tokens=max_tokens,
            response_mime_type="application/json",
            response_schema=INTENTS_SCHEMA,
        )

    async def parse(self, text: Optional[str], message_id: Optional[int] = None) -> List[Intent]:
        if not text or not text.strip():
            return []
        text = text.strip()
        t0 = time.time()
        try:
            resp = await self._client.aio.models.generate_content(
                model=self.model,
                contents=f"MESSAGE:\n{text}",
                config=self._gen_config,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Gemini parse failed (%s); message left unparsed.", exc)
            audit("ai_parse_error", provider="gemini", error=str(exc),
                  message_id=message_id)
            return []

        latency_ms = round((time.time() - t0) * 1000)
        payload = self._extract_json(resp)
        if payload is None:
            log.warning("Gemini returned unparseable output.")
            return []
        intents = payload_to_intents(payload, text, message_id,
                                     self.default_symbol, "gemini")
        audit("ai_parse", provider="gemini", message_id=message_id,
              latency_ms=latency_ms, count=len(intents),
              types=[i.type.value for i in intents])
        log.debug("Gemini parse %sms → %s", latency_ms,
                  [i.type.value for i in intents])
        return intents

    @staticmethod
    def _extract_json(resp) -> Optional[dict]:
        raw = getattr(resp, "text", None)
        if not raw:
            # Fall back to walking candidate parts.
            try:
                raw = resp.candidates[0].content.parts[0].text
            except Exception:  # noqa: BLE001
                return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # Be tolerant of stray prose around the JSON.
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(raw[start:end + 1])
                except json.JSONDecodeError:
                    return None
            return None
