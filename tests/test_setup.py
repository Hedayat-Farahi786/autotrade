"""Tests for the in-app Setup backend (MT5 / AI / mode / readiness)."""
from __future__ import annotations

import pytest

import bot.config as C
from web import setup as S


@pytest.fixture(autouse=True)
def _runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "RUNTIME_FILE", str(tmp_path / "runtime.json"))
    # Don't let real env shadow the runtime file.
    for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_CHANNEL",
              "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "GEMINI_API_KEY",
              "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "DRY_RUN", "PARSER_MODE"):
        monkeypatch.delenv(k, raising=False)
    C.reset_cache()
    yield
    C.reset_cache()


async def test_mt5_save_persists_to_config():
    res = S.save_mt5("12345678", "secret", "Broker-Server", symbol="XAUUSD")
    assert res["ok"] and res["configured"]
    cfg = C.get_config(require_secrets=False)
    assert cfg.mt5.login == 12345678 and cfg.mt5.server == "Broker-Server"
    assert cfg.mt5.password == "secret"


async def test_mt5_test_reports_windows_only_here():
    # MetaTrader5 isn't installed in CI/dev → friendly unavailable result,
    # but the credentials are still saved for the (Windows) bot host.
    r = await S.test_mt5("1", "p", "S")
    assert r["ok"] is False and r["available"] is False
    assert "Windows" in r["error"]
    assert C.get_config(require_secrets=False).mt5.server == "S"


async def test_ai_save_persists_key():
    r = S.save_ai(mode="hybrid", provider="gemini", gemini_api_key="KEY123")
    assert r["ok"] and r["has_key"] is True
    cfg = C.get_config(require_secrets=False)
    assert cfg.parser.gemini_api_key == "KEY123" and cfg.parser.mode == "hybrid"


def test_set_dry_run_persists():
    assert S.set_dry_run(False) == {"ok": True, "dry_run": False}
    assert C.get_config(require_secrets=False).dry_run is False
    S.set_dry_run(True)
    assert C.get_config(require_secrets=False).dry_run is True


async def test_setup_state_readiness():
    # Nothing configured, dry-run → ready needs Telegram + channel.
    st = await S.setup_state({"telethon": True, "authorized": False, "channel": ""})
    assert st["ready"] is False and st["telegram"]["connected"] is False

    # Telegram connected + channel + dry-run (no MT5 needed) → ready.
    st = await S.setup_state({"telethon": True, "authorized": True,
                              "channel": "@gtmovip", "me": {"username": "u"}})
    assert st["dry_run"] is True
    assert st["ready"] is True

    # Live mode without MT5 → not ready.
    S.set_dry_run(False)
    st = await S.setup_state({"telethon": True, "authorized": True, "channel": "@x"})
    assert st["ready"] is False
    # Add MT5 → ready.
    S.save_mt5("1", "p", "S")
    st = await S.setup_state({"telethon": True, "authorized": True, "channel": "@x"})
    assert st["mt5"]["configured"] is True and st["ready"] is True
