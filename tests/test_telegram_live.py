"""Telegram listener tests — the real listen → parse → execute path.

Installs a fake ``telethon`` so ``bot.telegram.listener`` runs without telethon
or credentials, then drives simulated channel messages through the listener into
the parser and trader, asserting trades actually execute. Also covers channel
resolution, the remote command handler, alerts, and reconnection.
"""
from __future__ import annotations

# Install the fake telethon BEFORE importing the listener.
from tests import fake_telethon

fake_telethon.install()

from bot.parser.signal_parser import SignalParser  # noqa: E402
from bot.telegram.listener import TelegramListener  # noqa: E402
from tests.test_pipeline import _make  # noqa: E402


def _listener(on_message):
    from bot.config import TelegramConfig
    cfg = TelegramConfig(api_id=1, api_hash="h", channel="GTMO VIP",
                         session_name="t", phone="+100")
    return TelegramListener(cfg, on_message)


# --------------------------------------------------------------------------- #
#  Connection / channel resolution
# --------------------------------------------------------------------------- #
async def test_start_connects_and_registers_handlers():
    seen = []

    async def on_msg(text, mid):
        seen.append((text, mid))

    lis = _listener(on_msg)
    await lis.start()
    assert lis._client.started is True
    # NewMessage + MessageEdited handlers registered.
    kinds = {f.kind for f, _ in lis._client.handlers}
    assert {"new", "edit"} <= kinds


async def test_channel_resolution_by_dialog_title():
    lis = _listener(lambda t, m: None)
    # Direct get_entity fails → fall back to scanning dialogs by title.
    lis._client.raise_on_get_entity = True
    lis._client.dialogs = [fake_telethon._Entity(title="Random"),
                           fake_telethon._Entity(title="GTMO VIP")]
    await lis.start()
    assert getattr(lis._entity, "title", None) == "GTMO VIP"


# --------------------------------------------------------------------------- #
#  The full pipeline: a channel message → an executed trade
# --------------------------------------------------------------------------- #
async def test_message_triggers_real_execution(tmp_path):
    cfg, ex, state, risk, trader = _make(tmp_path)
    await ex.connect()
    risk.start_day(await ex.account_balance())
    parser = SignalParser()

    async def on_msg(text, mid):
        await trader.handle(parser.parse(text, mid))

    lis = _listener(on_msg)
    await lis.start()

    # Simulate the channel posting an entry signal (4 TPs + open runner).
    await lis._client.fire(
        "Gold buy now 4470 - 4467\nSL: 4464\n"
        "TP: 4472\nTP: 4474\nTP: 4476\nTP: 4478\nTP: open (100+ pips)",
        1001)
    sig = state.latest_active()
    assert sig is not None and len(sig.open_positions()) == 5

    # A management message manages the open trade.
    await lis._client.fire("Adjust SL to 4462", 1002)
    assert state.latest_active().sl == 4462

    # Noise must not create or disturb trades.
    before = len(state.active_signals())
    await lis._client.fire("We in blueeee 😎😎😎", 1003)
    assert len(state.active_signals()) == before


async def test_empty_and_media_messages_ignored():
    seen = []

    async def on_msg(text, mid):
        seen.append(mid)

    lis = _listener(on_msg)
    await lis.start()
    await lis._client.fire("", 1, media=None)            # empty text
    await lis._client.fire("   ", 2)                       # whitespace
    await lis._client.fire("real signal text", 3)
    assert seen == [3]


# --------------------------------------------------------------------------- #
#  Remote control over Telegram + alerts
# --------------------------------------------------------------------------- #
async def test_command_handler_replies(tmp_path):
    cfg, ex, state, risk, trader = _make(tmp_path)
    await ex.connect()
    risk.start_day(await ex.account_balance())
    from bot.control import Controller
    ctrl = Controller(cfg, ex, state, trader, trader.tracker, notifier=None)

    lis = _listener(lambda t, m: None)
    await lis.start()
    target = await lis.resolve(None)             # → Saved Messages (me)
    lis.add_command_handler(target, ctrl.handle_command)

    ev = await lis._client.fire("/status", 5, kind="new")
    assert ev.replies and "GTMO" in ev.replies[0]

    # A non-command message is ignored by the command handler.
    ev2 = await lis._client.fire("just chatting", 6, kind="new")
    assert ev2.replies == []


async def test_notifier_sends_via_client():
    from bot.control import Notifier
    lis = _listener(lambda t, m: None)
    await lis.start()
    target = await lis.resolve("me")
    notifier = Notifier(enabled=True)
    notifier.bind(lis.client, target)
    await notifier.notify("hello")
    assert lis._client.sent and lis._client.sent[-1][1] == "hello"


async def test_resolve_defaults_to_saved_messages():
    lis = _listener(lambda t, m: None)
    await lis.start()
    me = await lis.resolve(None)
    assert me.username == "me"
