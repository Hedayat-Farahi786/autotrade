"""In-app Telegram login flow tests (with a mocked telethon).

Proves the wizard backend works end-to-end: credentials → code → (2FA) →
authorized → list channels → select, plus persistence the bot reads on start.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests import fake_telethon

fake_telethon.install()

from web.telegram_login import TelegramLoginError, TelegramLoginManager  # noqa: E402


def _cfg():
    tg = SimpleNamespace(api_id=123, api_hash="abc", session_name="test_sess",
                         channel="", phone=None)
    return SimpleNamespace(telegram=tg)


def _mgr(tmp_path, monkeypatch):
    import bot.config as C
    monkeypatch.setattr(C, "RUNTIME_FILE", str(tmp_path / "runtime.json"))
    return TelegramLoginManager(_cfg)


async def test_fresh_login_then_select_channel(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)

    st = await mgr.state()
    assert st["telethon"] is True and st["has_credentials"] is True
    assert st["authorized"] is False

    r = await mgr.connect("123", "abc", "+15551234567")
    assert r == {"step": "code"}
    assert mgr._client.code_sent_to == "+15551234567"

    r = await mgr.submit_code("12345")
    assert r["authorized"] is True and r["me"]["username"] == "me"

    # List channels the user can listen to.
    mgr._client.dialogs = [
        fake_telethon._Entity(title="GTMO VIP", username="gtmovip"),
        fake_telethon._Entity(title="Private chan", username=None, id=777),
    ]
    dialogs = await mgr.dialogs()
    assert any(d["title"] == "GTMO VIP" and d["peer"] == "@gtmovip" for d in dialogs)
    # No username → resolvable -100 peer.
    assert any(d["peer"] == "-100777" for d in dialogs)

    res = await mgr.select_channel("@gtmovip")
    assert res == {"ok": True, "channel": "@gtmovip"}


async def test_login_with_2fa(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    await mgr.connect("123", "abc", "+1")
    mgr._client.require_2fa = True

    r = await mgr.submit_code("12345")
    assert r == {"step": "password"}

    r = await mgr.submit_password("pw")
    assert r["authorized"] is True


async def test_invalid_code_raises(tmp_path, monkeypatch):
    from telethon.errors import PhoneCodeInvalidError
    mgr = _mgr(tmp_path, monkeypatch)
    await mgr.connect("123", "abc", "+1")
    with pytest.raises(PhoneCodeInvalidError):
        await mgr.submit_code("00000")


async def test_dialogs_requires_signin(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    await mgr.connect("123", "abc", "+1")  # code not submitted → not authorized
    with pytest.raises(TelegramLoginError):
        await mgr.dialogs()


def test_runtime_settings_persist_to_config(tmp_path, monkeypatch):
    import bot.config as C
    path = str(tmp_path / "telegram.json")
    monkeypatch.setattr(C, "RUNTIME_FILE", path)
    # Ensure env doesn't shadow the file.
    for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_CHANNEL"):
        monkeypatch.delenv(k, raising=False)

    C.save_runtime_telegram({"api_id": 111, "api_hash": "hh",
                             "channel": "GTMO VIP"}, path=path)
    cfg = C.get_config(require_secrets=False)
    assert cfg.telegram.api_id == 111
    assert cfg.telegram.channel == "GTMO VIP"
    C.reset_cache()
