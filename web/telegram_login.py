"""In-app Telegram login flow (Telethon), driven from the dashboard.

Runs the real authentication a user would otherwise do in a terminal:
phone → code → (2FA password) → authorized, then lists the user's channels so
they can pick which one to listen to. Credentials and the chosen channel are
persisted to ``state/telegram.json`` so the bot process picks them up, and the
created ``.session`` file means the bot starts non-interactively afterwards.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from bot.config import save_runtime_telegram
from bot.logger import get_logger

log = get_logger("tg_login")


def telethon_available() -> bool:
    try:
        import telethon  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _me(user) -> dict:
    if user is None:
        return {}
    return {
        "id": getattr(user, "id", None),
        "username": getattr(user, "username", None),
        "first_name": getattr(user, "first_name", None),
        "phone": getattr(user, "phone", None),
    }


class TelegramLoginError(Exception):
    pass


class TelegramLoginManager:
    """Stateful, single-session Telegram login orchestrator."""

    def __init__(self, get_cfg: Callable[[], Any]) -> None:
        self._get_cfg = get_cfg
        self._client = None
        self._phone: str | None = None
        self._hash: str | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    async def _client_for(self, api_id=None, api_hash=None):
        from telethon import TelegramClient

        cfg = self._get_cfg()
        aid = int(api_id) if api_id else cfg.telegram.api_id
        ah = api_hash or cfg.telegram.api_hash
        if not aid or not ah:
            raise TelegramLoginError("API ID and API Hash are required.")
        if self._client is None:
            self._client = TelegramClient(cfg.telegram.session_name, aid, ah)
        if not self._client.is_connected():
            await self._client.connect()
        return self._client

    async def state(self) -> dict:
        if not telethon_available():
            return {"telethon": False, "has_credentials": False,
                    "authorized": False, "me": None, "channel": ""}
        cfg = self._get_cfg()
        has_creds = bool(cfg.telegram.api_id and cfg.telegram.api_hash)
        authorized, me = False, None
        if has_creds:
            try:
                client = await self._client_for()
                authorized = await client.is_user_authorized()
                if authorized:
                    me = _me(await client.get_me())
            except Exception as exc:  # noqa: BLE001
                log.debug("state() probe failed: %s", exc)
        return {"telethon": True, "has_credentials": has_creds,
                "authorized": authorized, "me": me,
                "channel": cfg.telegram.channel, "phone": cfg.telegram.phone}

    async def connect(self, api_id, api_hash, phone) -> dict:
        async with self._lock:
            if not telethon_available():
                raise TelegramLoginError(
                    "telethon is not installed. Run `pip install telethon`.")
            save_runtime_telegram({"api_id": int(api_id), "api_hash": api_hash,
                                   "phone": phone})
            client = await self._client_for(api_id, api_hash)
            if await client.is_user_authorized():
                return {"authorized": True, "me": _me(await client.get_me())}
            sent = await client.send_code_request(phone)
            self._phone, self._hash = phone, sent.phone_code_hash
            log.info("Verification code sent to %s", phone)
            return {"step": "code"}

    async def submit_code(self, code) -> dict:
        from telethon.errors import SessionPasswordNeededError
        async with self._lock:
            client = await self._client_for()
            try:
                await client.sign_in(self._phone, str(code).strip(),
                                     phone_code_hash=self._hash)
            except SessionPasswordNeededError:
                return {"step": "password"}
            return {"authorized": True, "me": _me(await client.get_me())}

    async def submit_password(self, password) -> dict:
        async with self._lock:
            client = await self._client_for()
            await client.sign_in(password=password)
            return {"authorized": True, "me": _me(await client.get_me())}

    async def dialogs(self, limit: int = 200) -> list[dict]:
        from telethon.tl.types import Channel, Chat
        client = await self._client_for()
        if not await client.is_user_authorized():
            raise TelegramLoginError("Not signed in yet.")
        out: list[dict] = []
        async for d in client.iter_dialogs(limit=limit):
            ent = d.entity
            if not isinstance(ent, (Channel, Chat)):
                continue
            username = getattr(ent, "username", None)
            is_broadcast = getattr(ent, "broadcast", False)
            is_mega = getattr(ent, "megagroup", False)
            if username:
                peer = "@" + username
            elif isinstance(ent, Channel):
                peer = f"-100{ent.id}"
            else:
                peer = str(-ent.id)
            out.append({
                "title": getattr(ent, "title", "") or username or str(ent.id),
                "username": username, "peer": peer,
                "type": "channel" if is_broadcast else ("group" if is_mega or isinstance(ent, Chat) else "channel"),
                "participants": getattr(ent, "participants_count", None),
            })
        return out

    async def select_channel(self, channel: str) -> dict:
        channel = str(channel).strip()
        if not channel:
            raise TelegramLoginError("No channel provided.")
        save_runtime_telegram({"channel": channel})
        log.info("Channel selected: %s", channel)
        return {"ok": True, "channel": channel}

    async def logout(self) -> dict:
        async with self._lock:
            if self._client is not None:
                try:
                    await self._client.log_out()
                except Exception:  # noqa: BLE001
                    pass
                self._client = None
            self._phone = self._hash = None
            return {"ok": True}

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
