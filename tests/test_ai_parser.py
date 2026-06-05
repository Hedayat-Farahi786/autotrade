"""Verify the AI parser code paths work end-to-end with *injected* clients.

These tests do not need real API keys or network: they feed each parser the
exact response shape the real SDK returns (Gemini JSON mode; Anthropic tool_use)
and assert the message is mapped to the correct intents. If these pass, the
parser will work the moment you drop in a real GEMINI_API_KEY / ANTHROPIC_API_KEY.

They also exercise the shared ``payload_to_intents`` mapping for every intent
type, which both providers funnel through.
"""
from __future__ import annotations

import json

import pytest

from bot.models import Direction, IntentType, OrderKind
from bot.parser.ai_parser import TOOL, AIParser
from bot.parser.gemini_parser import GeminiParser
from bot.parser.intent_builder import INTENTS_SCHEMA, payload_to_intents

ENTRY_PAYLOAD = {
    "intents": [{
        "type": "ENTRY", "direction": "BUY", "order_kind": "MARKET",
        "entry_low": 4467, "entry_high": 4470, "sl": 4464,
        "take_profits": [
            {"price": 4472, "label": "TP1"},
            {"price": 4474, "label": "TP2"},
            {"price": None, "min_pips": 100, "label": "TP_OPEN"},
        ],
        "confidence": 0.95,
    }]
}

COMPOUND_PAYLOAD = {
    "intents": [
        {"type": "TP_HIT", "tp_index": 2},
        {"type": "PARTIAL_CLOSE", "close_fraction": 0.5},
        {"type": "BREAKEVEN", "applies_all": True},
    ]
}


# --------------------------------------------------------------------------- #
#  Shared mapping (the contract both providers produce)
# --------------------------------------------------------------------------- #
def test_payload_to_intents_entry():
    intents = payload_to_intents(ENTRY_PAYLOAD, "raw", 1, "XAUUSD", "ai")
    assert len(intents) == 1
    e = intents[0].entry
    assert intents[0].type is IntentType.ENTRY
    assert e.direction is Direction.BUY and e.order_kind is OrderKind.MARKET
    assert e.entry_low == 4467 and e.entry_high == 4470 and e.sl == 4464
    assert [tp.price for tp in e.concrete_tps] == [4472, 4474]
    assert any(tp.is_open and tp.min_pips == 100 for tp in e.take_profits)


def test_payload_to_intents_compound():
    intents = payload_to_intents(COMPOUND_PAYLOAD, "raw", 1, "XAUUSD", "ai")
    types = [i.type for i in intents]
    assert types == [IntentType.TP_HIT, IntentType.PARTIAL_CLOSE, IntentType.BREAKEVEN]
    assert intents[0].tp_index == 2
    assert intents[1].close_fraction == 0.5


def test_payload_to_intents_each_type():
    cases = [
        ({"type": "MODIFY_SL", "new_sl": 4462}, IntentType.MODIFY_SL),
        ({"type": "CLOSE_ALL"}, IntentType.CLOSE_ALL),
        ({"type": "NOISE"}, IntentType.NOISE),
    ]
    for raw, expected in cases:
        out = payload_to_intents({"intents": [raw]}, "raw", 1, "XAUUSD", "ai")
        assert out[0].type is expected
    sl = payload_to_intents({"intents": [{"type": "MODIFY_SL", "new_sl": 4462}]},
                            "raw", 1, "XAUUSD", "ai")
    assert sl[0].new_sl == 4462


def test_schema_shapes_match():
    # The Anthropic tool input_schema reuses the shared schema → stays in sync.
    assert TOOL["input_schema"] is INTENTS_SCHEMA
    assert "intents" in INTENTS_SCHEMA["properties"]


# --------------------------------------------------------------------------- #
#  Gemini parse() with an injected fake client (JSON mode)
# --------------------------------------------------------------------------- #
class _FakeGeminiResp:
    def __init__(self, payload):
        self.text = json.dumps(payload)


class _FakeGeminiClient:
    def __init__(self, payload):
        self._payload = payload

        class _Models:
            async def generate_content(_self, model, contents, config):
                return _FakeGeminiResp(payload)

        class _Aio:
            models = _Models()

        self.aio = _Aio()


def _make_gemini(payload) -> GeminiParser:
    # Bypass __init__ (which needs the SDK + key); inject a fake client.
    p = object.__new__(GeminiParser)
    p.model = "gemini-2.5-flash"
    p.default_symbol = "XAUUSD"
    p._gen_config = None
    p._gen_config_fallback = None
    p._schema_ok = True
    p._client = _FakeGeminiClient(payload)
    return p


@pytest.mark.asyncio
async def test_gemini_parse_entry_with_fake_client():
    p = _make_gemini(ENTRY_PAYLOAD)
    intents = await p.parse("Gold buy now 4470 - 4467 SL 4464", 1)
    assert intents[0].type is IntentType.ENTRY
    assert intents[0].entry.sl == 4464


@pytest.mark.asyncio
async def test_gemini_parse_compound_with_fake_client():
    p = _make_gemini(COMPOUND_PAYLOAD)
    intents = await p.parse("TP2 smashed, take profit, breakeven", 1)
    assert {i.type for i in intents} == {
        IntentType.TP_HIT, IntentType.PARTIAL_CLOSE, IntentType.BREAKEVEN}


# --------------------------------------------------------------------------- #
#  Anthropic parse() with an injected fake client (tool_use)
# --------------------------------------------------------------------------- #
class _FakeBlock:
    type = "tool_use"

    def __init__(self, payload):
        self.input = payload


class _FakeAnthropicResp:
    def __init__(self, payload):
        self.content = [_FakeBlock(payload)]
        self.usage = None


class _FakeAnthropicClient:
    def __init__(self, payload):
        self._payload = payload

        class _Messages:
            async def create(_self, **kw):
                return _FakeAnthropicResp(payload)

        self.messages = _Messages()


def _make_anthropic(payload) -> AIParser:
    p = object.__new__(AIParser)
    p.model = "claude-haiku-4-5-20251001"
    p.default_symbol = "XAUUSD"
    p.max_tokens = 600
    p._client = _FakeAnthropicClient(payload)
    return p


@pytest.mark.asyncio
async def test_anthropic_parse_entry_with_fake_client():
    p = _make_anthropic(ENTRY_PAYLOAD)
    intents = await p.parse("Gold buy now 4470 - 4467 SL 4464", 1)
    assert intents[0].type is IntentType.ENTRY
    assert [tp.price for tp in intents[0].entry.concrete_tps] == [4472, 4474]


@pytest.mark.asyncio
async def test_anthropic_parse_compound_with_fake_client():
    p = _make_anthropic(COMPOUND_PAYLOAD)
    intents = await p.parse("TP2 smashed, take profit, breakeven", 1)
    assert {i.type for i in intents} == {
        IntentType.TP_HIT, IntentType.PARTIAL_CLOSE, IntentType.BREAKEVEN}
