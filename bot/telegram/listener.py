"""Real-time Telegram listener built on Telethon (asyncio).

Connects as a user account, resolves the target channel (by id, @username or
title) and streams new messages to an async callback with minimal latency.
Includes robust auto-reconnect: Telethon reconnects internally, and we also
guard the run loop so transient disconnects never kill the process.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional, Union

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat

from ..config import TelegramConfig
from ..logger import audit, get_logger

log = get_logger("telegram")

MessageHandler = Callable[[str, int], Awaitable[None]]


class TelegramListener:
    def __init__(self, cfg: TelegramConfig, on_message: MessageHandler) -> None:
        self.cfg = cfg
        self._on_message = on_message
        self._client = TelegramClient(
            cfg.session_name, cfg.api_id, cfg.api_hash,
            connection_retries=None,      # retry forever
            retry_delay=2,
            auto_reconnect=True,
            request_retries=5,
        )
        self._entity = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        await self._client.start(phone=self.cfg.phone)
        me = await self._client.get_me()
        log.info("Telegram connected as %s (id=%s).",
                 getattr(me, "username", None) or getattr(me, "first_name", "?"),
                 getattr(me, "id", "?"))
        self._entity = await self._resolve_channel()
        if self._entity is None:
            raise RuntimeError(
                f"Could not resolve channel '{self.cfg.channel}'. Use the numeric "
                f"id (-100...), @username, or exact title, and ensure this account "
                f"is a member."
            )
        title = getattr(self._entity, "title", str(self._entity))
        log.info("Listening to channel: %s", title)
        self._register_handlers()

    async def _resolve_channel(self):
        target = self.cfg.channel.strip()
        # Numeric id (possibly -100 prefixed).
        try:
            if target.lstrip("-").isdigit():
                return await self._client.get_entity(int(target))
        except Exception as exc:  # noqa: BLE001
            log.debug("Numeric entity resolve failed: %s", exc)
        # @username or t.me link.
        try:
            return await self._client.get_entity(target)
        except Exception as exc:  # noqa: BLE001
            log.debug("Direct entity resolve failed: %s", exc)
        # Fall back to scanning dialogs by title (case-insensitive).
        try:
            async for dialog in self._client.iter_dialogs():
                ent = dialog.entity
                if isinstance(ent, (Channel, Chat)):
                    if (getattr(ent, "title", "") or "").strip().lower() == target.lower():
                        return ent
        except Exception as exc:  # noqa: BLE001
            log.debug("Dialog scan failed: %s", exc)
        return None

    def _register_handlers(self) -> None:
        @self._client.on(events.NewMessage(chats=self._entity))
        async def _handler(event):  # noqa: ANN001
            await self._handle_event(event)

        # Edited messages sometimes carry the final instruction; treat them too.
        @self._client.on(events.MessageEdited(chats=self._entity))
        async def _edit_handler(event):  # noqa: ANN001
            await self._handle_event(event, edited=True)

    async def _handle_event(self, event, edited: bool = False) -> None:
        msg = event.message
        text = msg.message or ""
        mid = msg.id
        has_media = bool(getattr(msg, "media", None))
        audit("telegram_message", message_id=mid, edited=edited,
              has_media=has_media, text=text)
        log.info("MSG #%s%s%s: %s", mid, " (edited)" if edited else "",
                 " [media]" if has_media else "",
                 (text[:120] + "…") if len(text) > 120 else text)
        if not text.strip():
            return  # pure image/chart → nothing to parse
        try:
            await self._on_message(text, mid)
        except Exception as exc:  # noqa: BLE001
            log.exception("on_message handler error: %s", exc)

    async def run_forever(self) -> None:
        """Block until disconnected/stopped, surviving transient errors."""

        backoff = 2
        while not self._stop.is_set():
            try:
                await self._client.run_until_disconnected()
                if self._stop.is_set():
                    break
                log.warning("Telegram disconnected; reconnecting in %ss…", backoff)
            except FloodWaitError as exc:
                log.error("FloodWait: sleeping %ss", exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
                continue
            except Exception as exc:  # noqa: BLE001
                log.exception("Listener loop error: %s", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            try:
                if not self._client.is_connected():
                    await self._client.connect()
                backoff = 2
            except Exception as exc:  # noqa: BLE001
                log.error("Reconnect attempt failed: %s", exc)

    async def stop(self) -> None:
        self._stop.set()
        try:
            await self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass
