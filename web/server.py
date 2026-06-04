"""FastAPI backend for the bot dashboard.

Fully decoupled from the running bot: it reads the same artifacts the bot
produces — ``state/status.json`` (heartbeat), ``state/active_signals.json``
(positions) and ``logs/audit.jsonl`` (event feed) — and writes the emergency-
stop file for the kill switch. This means the dashboard works whether the bot
runs in the same process, a separate process, or not at all.

Live updates are pushed over a WebSocket by tailing the audit log and polling
the status snapshot.

Run:  ``python main.py --web``  (or ``uvicorn web.server:app``)
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import (Depends, FastAPI, Header, HTTPException, Query, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bot.analytics.performance import load_records, summarize
from bot.config import get_config
from bot.control import send_command

STATIC_DIR = Path(__file__).parent / "static"

cfg = get_config(require_secrets=False)
AUDIT_FILE = os.path.join(cfg.log_dir, "audit.jsonl")
STATUS_FILE = cfg.status_file
STATE_FILE = cfg.state_file
STOP_FILE = cfg.emergency_stop_file
PAUSE_FILE = cfg.control.pause_file
COMMAND_FILE = cfg.control.command_file
TRADES_FILE = cfg.trades_file
REVIEW_FILE = cfg.review_file
TOKEN = cfg.dashboard.token


# --------------------------------------------------------------------------- #
#  Auth — optional bearer token / password protecting the API + WS
# --------------------------------------------------------------------------- #
def _token_ok(provided: Optional[str]) -> bool:
    return (not TOKEN) or (provided == TOKEN)


def require_token(authorization: Optional[str] = Header(None),
                  token: Optional[str] = Query(None)) -> None:
    if not TOKEN:
        return
    provided = None
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:]
    if not _token_ok(provided or token):
        raise HTTPException(status_code=401, detail="unauthorized")


def read_performance() -> Dict[str, Any]:
    return summarize(load_records(TRADES_FILE))


def equity_curve(limit: int = 250) -> List[Dict[str, Any]]:
    """Cumulative-profit series for the dashboard chart."""

    recs = [r for r in load_records(TRADES_FILE) if r.get("close_price") is not None]
    recs = recs[-limit:]
    out, cum = [], 0.0
    for r in recs:
        cum += float(r.get("profit", 0.0))
        out.append({"t": r.get("closed_at"), "equity": round(cum, 2)})
    return out


def read_review_count() -> int:
    if not os.path.exists(REVIEW_FILE):
        return 0
    try:
        with open(REVIEW_FILE, "r", encoding="utf-8") as fh:
            return sum(1 for ln in fh if ln.strip())
    except Exception:  # noqa: BLE001
        return 0

app = FastAPI(title="GTMO XAUUSD Dashboard", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------- #
#  Data access helpers
# --------------------------------------------------------------------------- #
def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def read_status() -> Dict[str, Any]:
    data = _read_json(STATUS_FILE)
    if not data:
        return {"online": False}
    data["online"] = True
    return data


def read_signals() -> List[Dict[str, Any]]:
    data = _read_json(STATE_FILE) or {}
    out: List[Dict[str, Any]] = []
    for sig in (data.get("signals") or {}).values():
        positions = sig.get("positions", [])
        open_pos = [p for p in positions if not p.get("closed")]
        if sig.get("closed") and not open_pos:
            continue
        out.append({
            "signal_id": sig.get("signal_id"),
            "direction": sig.get("direction"),
            "symbol": sig.get("symbol"),
            "entry_low": sig.get("entry_low"),
            "entry_high": sig.get("entry_high"),
            "sl": sig.get("sl"),
            "order_kind": sig.get("order_kind"),
            "created_at": sig.get("created_at"),
            "tps_hit": sig.get("tps_hit", []),
            "positions": [
                {
                    "ticket": p.get("ticket"),
                    "tp_label": p.get("tp_label"),
                    "tp_price": p.get("tp_price"),
                    "volume": p.get("volume"),
                    "sl": p.get("sl"),
                    "breakeven": p.get("breakeven", False),
                    "is_open_runner": p.get("is_open_runner", False),
                }
                for p in open_pos
            ],
        })
    out.sort(key=lambda s: s.get("created_at") or 0, reverse=True)
    return out


_FEED_EVENTS = {
    "telegram_message", "intent", "mt5_open", "mt5_modify", "mt5_close",
    "entry_rejected", "entry_invalid", "intent_error", "bot_start", "bot_stop",
}


def _feed_item(rec: dict) -> Optional[Dict[str, Any]]:
    event = rec.get("event")
    if event not in _FEED_EVENTS:
        return None
    base = {"ts": rec.get("ts"), "event": event}
    if event == "telegram_message":
        base.update(kind="message", message_id=rec.get("message_id"),
                    text=rec.get("text", ""), has_media=rec.get("has_media"),
                    edited=rec.get("edited"))
    elif event == "intent":
        base.update(kind="intent", message_id=rec.get("message_id"),
                    type=rec.get("type"), rule=rec.get("rule"),
                    detail=rec.get("detail", ""))
    elif event in {"mt5_open", "mt5_modify", "mt5_close"}:
        base.update(kind="action", action=event.replace("mt5_", ""),
                    sim=rec.get("sim"), ok=rec.get("ok", True),
                    ticket=rec.get("ticket"), volume=rec.get("volume"),
                    price=rec.get("price"), sl=rec.get("sl"), tp=rec.get("tp"),
                    direction=rec.get("direction"))
    elif event in {"entry_rejected", "entry_invalid", "intent_error"}:
        base.update(kind="warn", reason=rec.get("reason") or rec.get("error"),
                    detail=rec.get("detail", ""))
    elif event in {"bot_start", "bot_stop"}:
        base.update(kind="system")
    return base


def read_feed(limit: int = 80) -> List[Dict[str, Any]]:
    if not os.path.exists(AUDIT_FILE):
        return []
    tail: deque = deque(maxlen=limit * 3)
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                tail.append(line)
    except Exception:  # noqa: BLE001
        return []
    items: List[Dict[str, Any]] = []
    for line in tail:
        try:
            item = _feed_item(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
        if item:
            items.append(item)
    return items[-limit:]


# --------------------------------------------------------------------------- #
#  WebSocket live updates
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(self) -> None:
        self.clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def join(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.clients.add(ws)

    async def leave(self, ws: WebSocket) -> None:
        async with self._lock:
            self.clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            await self.leave(ws)


hub = Hub()


async def _audit_tailer() -> None:
    """Tail the audit log and broadcast new feed items + status changes."""

    last_size = 0
    last_status_ts: Optional[float] = None
    # Start at end of file so we only stream genuinely new events.
    if os.path.exists(AUDIT_FILE):
        last_size = os.path.getsize(AUDIT_FILE)

    while True:
        try:
            # New audit lines.
            if os.path.exists(AUDIT_FILE):
                size = os.path.getsize(AUDIT_FILE)
                if size < last_size:  # rotated/truncated
                    last_size = 0
                if size > last_size:
                    with open(AUDIT_FILE, "r", encoding="utf-8") as fh:
                        fh.seek(last_size)
                        for line in fh:
                            try:
                                item = _feed_item(json.loads(line))
                            except Exception:  # noqa: BLE001
                                continue
                            if item:
                                await hub.broadcast({"type": "feed", "item": item})
                    last_size = size

            # Status heartbeat changes.
            status = read_status()
            if status.get("ts") != last_status_ts:
                last_status_ts = status.get("ts")
                await hub.broadcast({"type": "status", "status": status})
                await hub.broadcast({"type": "signals", "signals": read_signals()})
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(1.0)


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_audit_tailer())


# --------------------------------------------------------------------------- #
#  REST API
# --------------------------------------------------------------------------- #
@app.get("/api/auth")
async def api_auth(authorization: Optional[str] = Header(None),
                   token: Optional[str] = Query(None)) -> JSONResponse:
    """Report whether auth is required and (if a token was sent) whether it's valid."""

    provided = None
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:]
    return JSONResponse({"required": bool(TOKEN), "ok": _token_ok(provided or token)})


@app.get("/api/status", dependencies=[Depends(require_token)])
async def api_status() -> JSONResponse:
    status = read_status()
    status["emergency_stop"] = os.path.exists(STOP_FILE)
    status["paused"] = os.path.exists(PAUSE_FILE)
    # Authoritative performance straight from the trade journal.
    status.setdefault("performance", read_performance())
    status.setdefault("review_queue", read_review_count())
    return JSONResponse(status)


@app.get("/api/performance", dependencies=[Depends(require_token)])
async def api_performance() -> JSONResponse:
    return JSONResponse({
        "performance": read_performance(),
        "recent": load_records(TRADES_FILE)[-25:],
        "equity_curve": equity_curve(),
        "review_queue": read_review_count(),
    })


@app.get("/api/signals", dependencies=[Depends(require_token)])
async def api_signals() -> JSONResponse:
    return JSONResponse({"signals": read_signals()})


@app.get("/api/feed", dependencies=[Depends(require_token)])
async def api_feed(limit: int = 80) -> JSONResponse:
    return JSONResponse({"feed": read_feed(limit=limit)})


@app.post("/api/control/emergency-stop", dependencies=[Depends(require_token)])
async def api_emergency_stop(payload: Dict[str, Any]) -> JSONResponse:
    enabled = bool(payload.get("enabled"))
    if enabled:
        Path(STOP_FILE).write_text("stopped via dashboard\n", encoding="utf-8")
    else:
        try:
            os.remove(STOP_FILE)
        except FileNotFoundError:
            pass
    state = os.path.exists(STOP_FILE)
    await hub.broadcast({"type": "emergency", "enabled": state})
    return JSONResponse({"emergency_stop": state})


@app.post("/api/control/pause", dependencies=[Depends(require_token)])
async def api_pause(payload: Dict[str, Any]) -> JSONResponse:
    """Toggle the pause flag directly (works even if the bot is offline)."""

    enabled = bool(payload.get("enabled"))
    if enabled:
        os.makedirs(os.path.dirname(PAUSE_FILE) or ".", exist_ok=True)
        Path(PAUSE_FILE).write_text("paused via dashboard\n", encoding="utf-8")
        send_command(COMMAND_FILE, "pause", source="dashboard")
    else:
        try:
            os.remove(PAUSE_FILE)
        except FileNotFoundError:
            pass
        send_command(COMMAND_FILE, "resume", source="dashboard")
    state = os.path.exists(PAUSE_FILE)
    await hub.broadcast({"type": "paused", "enabled": state})
    return JSONResponse({"paused": state})


@app.post("/api/control/close-all", dependencies=[Depends(require_token)])
async def api_close_all() -> JSONResponse:
    """Ask the running bot to flatten everything (via the command bus)."""

    send_command(COMMAND_FILE, "close_all", source="dashboard")
    return JSONResponse({"queued": True})


@app.websocket("/ws")
async def ws(ws: WebSocket, token: Optional[str] = Query(None)) -> None:
    if TOKEN and not _token_ok(token):
        await ws.close(code=4401)
        return
    await hub.join(ws)
    try:
        # Send an initial snapshot on connect.
        await ws.send_json({"type": "status", "status": read_status()})
        await ws.send_json({"type": "signals", "signals": read_signals()})
        await ws.send_json({"type": "feed_snapshot", "feed": read_feed()})
        while True:
            await ws.receive_text()  # keepalive / ignore client messages
    except WebSocketDisconnect:
        pass
    finally:
        await hub.leave(ws)


# --------------------------------------------------------------------------- #
#  Static frontend
# --------------------------------------------------------------------------- #
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
