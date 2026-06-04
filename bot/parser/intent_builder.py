"""Shared mapping from an LLM JSON payload to :class:`Intent` objects.

Both the Claude and Gemini parsers ask the model for the *same* JSON shape and
funnel it through here, so adding/altering provider backends never duplicates
the (somewhat fiddly) normalisation logic.
"""
from __future__ import annotations

from typing import List, Optional

from ..logger import get_logger
from ..models import (
    Direction,
    EntrySignal,
    Intent,
    IntentType,
    OrderKind,
    TakeProfit,
)

log = get_logger("parser.builder")


# JSON schema for the structured output (OpenAPI-subset; works for both
# Anthropic tool-use input_schema and Gemini response_schema).
INTENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "intents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "ENTRY", "MODIFY_SL", "BREAKEVEN", "PARTIAL_CLOSE",
                            "TP_HIT", "CLOSE_ALL", "NOISE",
                        ],
                    },
                    "direction": {"type": "string", "enum": ["BUY", "SELL"]},
                    "order_kind": {"type": "string", "enum": ["MARKET", "LIMIT", "STOP"]},
                    "entry_low": {"type": "number"},
                    "entry_high": {"type": "number"},
                    "sl": {"type": "number"},
                    "take_profits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "price": {"type": "number"},
                                "min_pips": {"type": "number"},
                                "label": {"type": "string"},
                            },
                        },
                    },
                    "new_sl": {"type": "number"},
                    "close_fraction": {"type": "number"},
                    "tp_index": {"type": "integer"},
                    "applies_all": {"type": "boolean"},
                    "confidence": {"type": "number"},
                },
                "required": ["type"],
            },
        }
    },
    "required": ["intents"],
}


def payload_to_intents(
    payload: dict,
    text: str,
    message_id: Optional[int],
    default_symbol: str,
    source: str,
) -> List[Intent]:
    out: List[Intent] = []
    for raw in (payload or {}).get("intents", []):
        try:
            out.append(_one(raw, text, message_id, default_symbol, source))
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed %s intent %s: %s", source, raw, exc)
    if not out:
        return [Intent(type=IntentType.NOISE, raw_text=text, message_id=message_id,
                       matched_rule=source, confidence=0.0)]
    return out


def _val(raw: dict, key: str):
    """Return a value, treating null/empty-string as missing."""

    v = raw.get(key)
    if v is None or v == "":
        return None
    return v


def _one(raw: dict, text: str, message_id: Optional[int], default_symbol: str,
         source: str) -> Intent:
    itype = IntentType(raw["type"])
    intent = Intent(
        type=itype,
        raw_text=text,
        message_id=message_id,
        matched_rule=source,
        confidence=float(_val(raw, "confidence") or 0.9),
    )
    if itype is IntentType.ENTRY:
        tps: List[TakeProfit] = []
        for tp in raw.get("take_profits") or []:
            price = tp.get("price")
            tps.append(
                TakeProfit(
                    price=price if price not in (None, "") else None,
                    min_pips=tp.get("min_pips") or None,
                    label=tp.get("label") or None,
                )
            )
        direction = Direction(_val(raw, "direction") or "BUY")
        order_kind = OrderKind(_val(raw, "order_kind") or "MARKET")
        lo, hi = _val(raw, "entry_low"), _val(raw, "entry_high")
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        intent.entry = EntrySignal(
            direction=direction,
            symbol=default_symbol,
            order_kind=order_kind,
            entry_low=lo,
            entry_high=hi if hi is not None else lo,
            sl=_val(raw, "sl"),
            take_profits=tps,
        )
    elif itype is IntentType.MODIFY_SL:
        intent.new_sl = _val(raw, "new_sl") or _val(raw, "sl")
    elif itype is IntentType.PARTIAL_CLOSE:
        frac = _val(raw, "close_fraction")
        intent.close_fraction = float(frac) if frac else 0.5
    elif itype is IntentType.TP_HIT:
        idx = _val(raw, "tp_index")
        intent.tp_index = int(idx) if idx is not None else 0
    return intent


# The system prompt is shared verbatim across providers.
SYSTEM_PROMPT = """You are a deterministic trading-signal extraction engine for \
the "GTMO VIP" Telegram channel, which posts XAUUSD (Gold) signals and live \
management updates. Your ONLY job is to convert a single channel message into a \
JSON object {"intents": [...]} describing the trading intents it contains. Never \
trade on your own opinion; only extract what the message states.

Domain knowledge:
- The instrument is always Gold / XAUUSD unless stated otherwise.
- Gold "pips" are 0.1 in price terms (70 pips = a 7.0 move).
- Entry example: "Gold buy now 4470 - 4467" means a BUY with an entry zone from \
4467 to 4470, executed at market ("now"). "limit" => pending LIMIT, "stop" => \
pending STOP.
- "SL: 4464" is the stop loss. Multiple "TP:" lines are take-profit targets in \
order (TP1, TP2, ...). "TP: open (100+ pips)" is an open-ended runner with no \
fixed price (price omitted, min_pips set to the number if given).

Intent types:
- ENTRY: a NEW trade (include direction; plus entry_low/entry_high, sl, \
take_profits, order_kind when present). Entry messages are self-contained: if a \
message is an entry, output ONLY the ENTRY intent.
- MODIFY_SL: move stop loss to a stated price (set new_sl), e.g. "Adjust SL to \
4462".
- BREAKEVEN: move stop to entry for zero risk ("set breakeven", "zero risk", \
"risk free").
- PARTIAL_CLOSE: take partial profit ("take some profits", "secure profits", \
"book some"). Default close_fraction 0.5 unless a fraction/percent is stated.
- TP_HIT: a target was reached/touched/smashed/checked (informational). Set \
tp_index to the number if stated, else 0.
- CLOSE_ALL: close/exit the whole trade now.
- NOISE: motivation, disclaimers, chart/screenshot captions, commentary with no \
actionable instruction.

Rules:
- One message may contain SEVERAL management intents (e.g. "TP2 smashed take \
some more profits and set breakeven now" => TP_HIT(2) + PARTIAL_CLOSE + \
BREAKEVEN). Output each.
- If the message references "all entries"/"all positions", set applies_all true.
- If nothing is actionable, output exactly one NOISE intent.
- Prices must be plausible XAUUSD values (hundreds to thousands). Ignore numbers \
that are not prices (view counts, timestamps; pip counts unless they define a \
target).
- Output ONLY the JSON object, nothing else."""
