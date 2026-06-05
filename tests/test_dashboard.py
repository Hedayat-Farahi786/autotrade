"""Dashboard API tests: data endpoints, auth gating, and control actions.

Points the server's file paths at a temp dir (seeded with demo data) so the
tests are isolated and need no running bot.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

import web.server as srv


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Seed isolated demo artifacts.
    trades = [
        {"signal_id": 1, "direction": "BUY", "label": "TP1", "profit": 50.0,
         "risk_amount": 30.0, "close_price": 4472, "open_price": 4467,
         "reason": "tp", "closed_at": 1000, "r_multiple": 1.67, "volume": 0.1},
        {"signal_id": 1, "direction": "BUY", "label": "TP2", "profit": -20.0,
         "risk_amount": 30.0, "close_price": 4464, "open_price": 4467,
         "reason": "sl", "closed_at": 1100, "r_multiple": -0.67, "volume": 0.1},
    ]
    tf = tmp_path / "trades.jsonl"
    tf.write_text("\n".join(json.dumps(t) for t in trades) + "\n")
    status = tmp_path / "status.json"
    status.write_text(json.dumps({
        "ts": 1, "dry_run": True, "balance": 10000, "equity": 10030,
        "open_signals": 1, "open_positions": 2, "mt5_connected": True,
    }))
    signals = tmp_path / "signals.json"
    signals.write_text(json.dumps({"next_id": 2, "signals": {}}))

    monkeypatch.setattr(srv, "TRADES_FILE", str(tf))
    monkeypatch.setattr(srv, "STATUS_FILE", str(status))
    monkeypatch.setattr(srv, "STATE_FILE", str(signals))
    monkeypatch.setattr(srv, "STOP_FILE", str(tmp_path / "stop.flag"))
    monkeypatch.setattr(srv, "PAUSE_FILE", str(tmp_path / "pause.flag"))
    monkeypatch.setattr(srv, "COMMAND_FILE", str(tmp_path / "cmd.jsonl"))
    monkeypatch.setattr(srv, "TOKEN", None)
    return TestClient(srv.app)


def test_status_and_performance(client):
    st = client.get("/api/status").json()
    assert st["online"] is True and st["balance"] == 10000
    perf = client.get("/api/performance").json()
    assert perf["performance"]["trades"] == 2
    assert perf["performance"]["wins"] == 1 and perf["performance"]["losses"] == 1
    assert len(perf["equity_curve"]) == 2
    assert len(perf["recent"]) == 2


def test_feed_and_signals(client):
    assert client.get("/api/signals").status_code == 200
    assert client.get("/api/feed").status_code == 200


def test_controls(client):
    # Emergency stop on/off.
    assert client.post("/api/control/emergency-stop", json={"enabled": True}).json()["emergency_stop"]
    assert not client.post("/api/control/emergency-stop", json={"enabled": False}).json()["emergency_stop"]
    # Pause toggles the flag + queues a command.
    assert client.post("/api/control/pause", json={"enabled": True}).json()["paused"]
    assert os.path.exists(srv.PAUSE_FILE)
    assert client.post("/api/control/pause", json={"enabled": False}).json()["paused"] is False
    # Close-all is queued to the command bus.
    assert client.post("/api/control/close-all").json()["queued"] is True
    assert os.path.exists(srv.COMMAND_FILE)


def test_static_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "GTMO" in r.text


def test_bot_lifecycle_demo(tmp_path, monkeypatch):
    import time as _t

    # Point the demo bot's artifacts at the temp dir too.
    import web.sim_bot as sim
    status = str(tmp_path / "status.json")
    monkeypatch.setattr(sim._cfg, "status_file", status)
    monkeypatch.setattr(sim._cfg, "trades_file", str(tmp_path / "trades.jsonl"))
    monkeypatch.setattr(sim._cfg, "state_file", str(tmp_path / "signals.json"))
    monkeypatch.setattr(sim._cfg, "review_file", str(tmp_path / "review.jsonl"))
    monkeypatch.setattr(sim._cfg, "log_dir", str(tmp_path))
    # The server must read the same status file the demo bot writes.
    monkeypatch.setattr(srv, "STATUS_FILE", status)

    c = TestClient(srv.app)
    assert c.get("/api/bot/status").json()["running"] is False
    started = c.post("/api/bot/start", json={"mode": "demo"}).json()
    assert started["running"] is True and started["mode"] == "demo"
    try:
        # Within a couple seconds the boot sequence reports connections.
        connected = False
        for _ in range(40):
            st = c.get("/api/bot/status").json()
            if st["telegram_connected"] and st["mt5_connected"]:
                connected = True
                break
            _t.sleep(0.1)
        assert connected, "demo bot did not report connected"
    finally:
        stopped = c.post("/api/bot/stop").json()
        assert stopped["running"] is False
    assert sim.get_demo_bot().running is False


def test_bot_start_rejects_bad_mode():
    c = TestClient(srv.app)
    assert c.post("/api/bot/start", json={"mode": "nope"}).status_code == 400


def test_auth_gating(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "TOKEN", "secret")
    monkeypatch.setattr(srv, "TRADES_FILE", str(tmp_path / "none.jsonl"))
    monkeypatch.setattr(srv, "STATUS_FILE", str(tmp_path / "none.json"))
    c = TestClient(srv.app)
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer secret"}).status_code == 200
    # Index stays public so the login page can load.
    assert c.get("/").status_code == 200
    assert c.get("/api/auth").json() == {"required": True, "ok": False}
