"""Regex building blocks for the signal parser.

Kept separate from the parsing logic so new channel phrasings can be added by
editing data here without touching control flow. Every pattern is compiled
case-insensitively and tolerant of the messy, emoji-laden text seen in the
GTMO VIP channel.
"""
from __future__ import annotations

import re

# A price for XAUUSD. Accepts "4470", "4470.5", "2,470.50". The comma-grouped
# form is tried first (it's more specific); the plain form requires 3-6 digits
# so a 4-digit gold price like "4462" is never clipped to "446".
PRICE = r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{3,6}(?:\.\d{1,2})?)"

# Direction keywords.
RE_BUY = re.compile(r"\b(buy|long|bull)\b", re.I)
RE_SELL = re.compile(r"\b(sell|short|bear)\b", re.I)

# The symbol — gold goes by many names.
RE_SYMBOL = re.compile(r"\b(gold|xau\s*/?\s*usd|xauusd|xau)\b", re.I)

# Order kind hints.
RE_MARKET = re.compile(r"\b(now|market|instant)\b", re.I)
RE_LIMIT = re.compile(r"\b(limit|pullback|retrace|wait|zone\s+buy)\b", re.I)
RE_STOP = re.compile(r"\b(stop|breakout|break\s*out)\b", re.I)

# Entry line: "Gold buy now 4470 - 4467" or "buy 4470/4467" or "buy now 4470".
RE_ENTRY = re.compile(
    r"(?:gold\s+)?"
    r"(?P<dir>buy|sell|long|short)\s+"
    r"(?:now\s+|limit\s+|stop\s+|market\s+)?"
    rf"(?P<p1>{PRICE})"
    rf"(?:\s*(?:-|to|/|\.\.\.?|–|—)\s*(?P<p2>{PRICE}))?",
    re.I,
)

# Stop-loss in an entry block: "SL: 4464", "SL 4464", "stop loss 4464".
RE_SL = re.compile(
    rf"\b(?:s\.?l\.?|stop\s*loss|stoploss)\s*[:=\-]?\s*(?P<sl>{PRICE})",
    re.I,
)

# SL adjustment: "Adjust SL to 4462", "move sl to", "change sl 4462", "sl to".
RE_SL_MODIFY = re.compile(
    r"\b(?:adjust|move|moving|change|shift|update|set|trail)\b"
    rf"[^0-9\n]{{0,30}}?(?:s\.?l\.?|stop\s*loss)\s*(?:to|at|=|:)?\s*(?P<sl>{PRICE})",
    re.I,
)
# Looser fallback: "SL to 4462" / "SL now 4462".
RE_SL_TO = re.compile(
    rf"\b(?:s\.?l\.?|stop\s*loss)\s*(?:to|at|now|=|:)\s*(?P<sl>{PRICE})",
    re.I,
)

# Take-profit levels. Captures an optional index ("TP1", "TP 2") and a price,
# or the open-runner form "TP: open (100+ pips)".
RE_TP = re.compile(
    rf"\bt\.?p\.?\s*(?P<idx>\d+)?\s*[:=\-]?\s*(?P<price>{PRICE})",
    re.I,
)
RE_TP_OPEN = re.compile(
    r"\bt\.?p\.?\s*\d*\s*[:=\-]?\s*open\b(?:[^0-9]*?(?P<pips>\d{1,4})\s*\+?\s*pips)?",
    re.I,
)

# A generic "100+ pips" / "70 pips" capture for open targets / progress notes.
RE_PIPS = re.compile(r"(?P<pips>\d{1,4})\s*\+?\s*pips?", re.I)

# --- Update / management intents -------------------------------------------

# Breakeven. Note: we deliberately do NOT match a bare "be" — it collides with
# the English word ("gotta be"). Only the slashed/dotted "b/e"/"b.e" form counts.
RE_BREAKEVEN = re.compile(
    r"(break\s*even|breakeven|\bb[\./]e\b|zero\s*risk|risk[\s\-]*free|"
    r"secure\s+the\s+trade)",
    re.I,
)

# Partial profit taking. Allows filler words between the verb and "profits"
# ("take some more profits"), plus standalone "partials"/"close half".
RE_PARTIAL = re.compile(
    r"\b(?:take|secure|book|grab|bank|lock)\b[^.\n]{0,25}?\bprofits?\b"
    r"|\bpartials?\b"
    r"|\bclose\s+(?:half|some|partial|a\s+bit)\b"
    r"|\btake\s+(?:some\s+)?off\b"
    r"|\btake\s+(?:some\s+)?profits?\b",
    re.I,
)

# Verbs meaning "a TP was reached". "smas+h?\w*" catches "smashed" and the
# exuberant "smasssheddd"; "check\w*" catches "checkkk".
_HIT_VERB = r"(touch\w*|hit|smas+h?\w*|check\w*|done|reach\w*|secured|bag\w*|complete\w*|nailed)"

# TP touched / smashed / checked. Captures the referenced TP number.
RE_TP_HIT = re.compile(
    rf"\bt\.?p\.?\s*(?P<idx>\d+)?\s*(?:has\s+been\s+)?{_HIT_VERB}\b",
    re.I,
)
# Reverse word order: "smashed tp2", "touched tp1".
RE_TP_HIT_REV = re.compile(
    rf"\b{_HIT_VERB}\s+t\.?p\.?\s*(?P<idx>\d+)?",
    re.I,
)

RE_CLOSE_ALL = re.compile(
    r"\b(close\s+(?:all|everything|the\s+trade|positions?|now)|"
    r"exit\s+(?:all|now|everything)|flat\s+now|cut\s+(?:it|all)|"
    r"get\s+out\s+now|cancel\s+(?:all|the\s+trade))\b",
    re.I,
)

# Phrases that explicitly target "all entries" → apply to every open position.
RE_ALL_ENTRIES = re.compile(r"\ball\s+entries\b", re.I)

# Strong noise indicators — disclaimers, pure motivation. Used to fast-path the
# common chatter so it never trips a trading rule.
RE_DISCLAIMER = re.compile(
    r"(no\s+financial\s+advice|trade\s+at\s+your\s+own\s+risk|"
    r"not\s+financial\s+advice|ready\s+signal)",
    re.I,
)
