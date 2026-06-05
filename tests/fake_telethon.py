"""A minimal fake of the ``telethon`` package, injected into ``sys.modules``.

Lets ``bot.telegram.listener`` import and run on a machine without telethon (and
without real credentials), so we can drive the listen→parse→execute path and the
command handler with simulated Telegram events.
"""
from __future__ import annotations

import sys
import types
from typing import Any


# --- event filters --------------------------------------------------------
class _EventFilter:
    def __init__(self, kind, chats=None, **kw):
        self.kind = kind
        self.chats = chats


class _Events:
    @staticmethod
    def NewMessage(chats=None, **kw):
        return _EventFilter("new", chats, **kw)

    @staticmethod
    def MessageEdited(chats=None, **kw):
        return _EventFilter("edit", chats, **kw)


# --- fake message / event -------------------------------------------------
class FakeMessage:
    def __init__(self, text, mid, media=None):
        self.message = text
        self.id = mid
        self.media = media


class FakeEvent:
    def __init__(self, text, mid, media=None):
        self.message = FakeMessage(text, mid, media)
        self.replies: list[str] = []

    async def reply(self, text):
        self.replies.append(text)


class _Entity:
    def __init__(self, title="GTMO VIP", username=None, id=-1001):
        self.title = title
        self.username = username
        self.id = id
        self.first_name = title


# --- fake client ----------------------------------------------------------
class FakeTelegramClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.handlers: list[tuple[Any, Any]] = []
        self.sent: list[tuple[Any, str]] = []
        self._connected = True
        self.started = False
        self.disconnected = False
        # Configurable behaviour for tests.
        self.me = _Entity("Me", username="me", id=42)
        self.entities = {}          # target → entity
        self.dialogs: list = []
        self.raise_on_get_entity = False

    async def start(self, phone=None):
        self.started = True
        self.start_phone = phone
        return self

    async def get_me(self):
        return self.me

    async def get_entity(self, target):
        if self.raise_on_get_entity:
            raise ValueError("cannot resolve")
        if target in self.entities:
            return self.entities[target]
        return _Entity(title=str(target), id=-1002)

    async def iter_dialogs(self):
        for d in self.dialogs:
            yield types.SimpleNamespace(entity=d)

    def on(self, event_filter):
        def deco(fn):
            self.handlers.append((event_filter, fn))
            return fn
        return deco

    def is_connected(self):
        return self._connected

    async def connect(self):
        self._connected = True

    async def disconnect(self):
        self.disconnected = True
        self._connected = False

    async def send_message(self, target, text):
        self.sent.append((target, text))

    async def run_until_disconnected(self):
        return None

    async def iter_messages(self, entity, limit=0):
        for m in []:
            yield m

    # --- test helpers ----------------------------------------------------
    async def fire(self, text, mid, kind="new", media=None):
        ev = FakeEvent(text, mid, media)
        for flt, fn in self.handlers:
            if flt.kind == kind:
                await fn(ev)
        return ev


def install(client_factory=None):
    """Install the fake telethon into sys.modules. Returns the events module."""

    telethon = types.ModuleType("telethon")
    telethon.TelegramClient = client_factory or FakeTelegramClient
    telethon.events = _Events()

    errors = types.ModuleType("telethon.errors")

    class FloodWaitError(Exception):
        def __init__(self, seconds=1):
            self.seconds = seconds

    errors.FloodWaitError = FloodWaitError

    tl = types.ModuleType("telethon.tl")
    tl_types = types.ModuleType("telethon.tl.types")
    tl_types.Channel = _Entity
    tl_types.Chat = _Entity

    telethon.errors = errors
    telethon.tl = tl
    tl.types = tl_types

    sys.modules["telethon"] = telethon
    sys.modules["telethon.errors"] = errors
    sys.modules["telethon.tl"] = tl
    sys.modules["telethon.tl.types"] = tl_types
    return telethon
