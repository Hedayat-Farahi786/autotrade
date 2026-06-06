"""Backend for the in-app Setup guide — connect MT5, AI keys, and trading mode.

Mirrors the Telegram login flow for the rest of the configuration: it persists
UI-entered settings to the unified runtime file (so the bot subprocess picks
them up) and can *test* the real MT5 connection before you trust it.
"""
from __future__ import annotations

from bot.config import MT5Config, get_config, save_runtime
from bot.logger import get_logger

log = get_logger("setup")


def _cfg():
    return get_config(require_secrets=False)


# --------------------------------------------------------------------------- #
#  MetaTrader 5
# --------------------------------------------------------------------------- #
async def test_mt5(login, password, server, terminal_path=None,
                   symbol=None, save: bool = True) -> dict:
    """Validate (and optionally persist) an MT5 connection with given creds."""

    from bot.mt5.executor import _MT5_AVAILABLE, MT5Executor

    if save:
        save_runtime("mt5", {
            "login": int(login) if login else None,
            "password": password, "server": server,
            "terminal_path": terminal_path or None,
            "symbol": (symbol or "XAUUSD"),
        })

    if not _MT5_AVAILABLE:
        return {
            "ok": False, "available": False,
            "error": "MetaTrader5 package not installed — it is Windows-only. "
                     "Run the bot on a Windows host/VPS, or continue in dry-run.",
        }
    if not (login and password and server):
        return {"ok": False, "available": True, "error": "Login, password and server are required."}

    cfg = MT5Config(login=int(login), password=password, server=server,
                    terminal_path=terminal_path or None, symbol=symbol or "XAUUSD")
    ex = MT5Executor(cfg, dry_run=False)
    try:
        if not await ex.connect():
            return {"ok": False, "available": True,
                    "error": "Connection/login failed — check login, password and server."}
        bal = await ex.account_balance()
        eq = await ex.account_equity()
        return {"ok": True, "available": True, "symbol": ex.symbol,
                "balance": round(bal, 2), "equity": round(eq, 2)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "available": True, "error": str(exc)[:160]}
    finally:
        await ex.shutdown()


def save_mt5(login, password, server, terminal_path=None, symbol=None) -> dict:
    sec = save_runtime("mt5", {
        "login": int(login) if login else None, "password": password,
        "server": server, "terminal_path": terminal_path or None,
        "symbol": (symbol or "XAUUSD"),
    })
    return {"ok": True, "configured": bool(sec.get("login") and sec.get("server"))}


# --------------------------------------------------------------------------- #
#  AI parser
# --------------------------------------------------------------------------- #
def save_ai(mode=None, provider=None, gemini_api_key=None,
            anthropic_api_key=None) -> dict:
    updates = {"mode": mode, "provider": provider}
    if gemini_api_key:
        updates["gemini_api_key"] = gemini_api_key
    if anthropic_api_key:
        updates["anthropic_api_key"] = anthropic_api_key
    save_runtime("parser", updates)
    cfg = _cfg()
    return {"ok": True, "mode": cfg.parser.mode, "provider": cfg.parser.provider,
            "has_key": bool(cfg.parser.gemini_api_key or cfg.parser.anthropic_api_key)}


async def test_ai() -> dict:
    """Do a real sample parse with the configured provider (costs one call)."""

    cfg = _cfg()
    from bot.parser import build_parser
    p = build_parser(cfg.parser.mode, provider=cfg.parser.provider,
                     gemini_api_key=cfg.parser.gemini_api_key,
                     anthropic_api_key=cfg.parser.anthropic_api_key)
    name = type(p).__name__
    if name == "RegexParserAsync":
        return {"ok": True, "parser": "regex",
                "note": "Using the fast regex parser (no AI key)."}
    try:
        intents = await p.parse("Gold buy now 4470 - 4467\nSL: 4464\nTP: 4472", 0)
        ok = any(i.type.value == "ENTRY" for i in intents)
        return {"ok": ok, "parser": getattr(p, "provider", cfg.parser.provider),
                "intents": [i.type.value for i in intents]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160]}


def set_dry_run(dry_run: bool) -> dict:
    save_runtime("", {"dry_run": bool(dry_run)})
    return {"ok": True, "dry_run": bool(dry_run)}


# --------------------------------------------------------------------------- #
#  Unified setup state (drives the guide + readiness check)
# --------------------------------------------------------------------------- #
async def setup_state(tg_state: dict) -> dict:
    from bot.mt5.executor import _MT5_AVAILABLE

    cfg = _cfg()
    tg_ok = bool(tg_state.get("authorized"))
    channel_ok = bool(tg_state.get("channel"))
    mt5_configured = bool(cfg.mt5.login and cfg.mt5.password and cfg.mt5.server)
    ai_has_key = bool(cfg.parser.gemini_api_key or cfg.parser.anthropic_api_key)

    # Live trading needs MT5; dry-run can run on the simulator.
    mt5_ready = mt5_configured or cfg.dry_run
    ready = tg_ok and channel_ok and mt5_ready

    return {
        "telegram": {"available": tg_state.get("telethon", False),
                     "connected": tg_ok, "channel": tg_state.get("channel", ""),
                     "me": tg_state.get("me")},
        "mt5": {"available": _MT5_AVAILABLE, "configured": mt5_configured,
                "symbol": cfg.mt5.symbol, "server": cfg.mt5.server},
        "ai": {"mode": cfg.parser.mode, "provider": cfg.parser.provider,
               "has_key": ai_has_key},
        "dry_run": cfg.dry_run,
        "ready": ready,
    }
