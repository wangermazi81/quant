# -*- coding: utf-8 -*-
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

from flask import Flask, jsonify

import app_security as sec


def _isolated_security(monkeypatch, tmp_path):
    db_path = tmp_path / "security.db"
    monkeypatch.setattr(sec, "SECURITY_DB", str(db_path))
    monkeypatch.setattr(sec, "_ANTI_CRAWL_ENABLED", True)
    monkeypatch.setattr(sec, "_ANTI_SENSITIVE_MAX", 2)
    monkeypatch.setattr(sec, "_ANTI_BLOCK_SECONDS", 60)
    monkeypatch.setattr(sec, "_ANTI_AUTH_FAIL_MAX", 2)
    sec._buckets.clear()
    sec._suspicious_hits.clear()
    sec._fail_log.clear()
    sec._lock_until.clear()
    sec._ip_blocks.clear()
    sec._ensure_security_tables()


def test_sensitive_probe_is_hidden_and_audited(monkeypatch, tmp_path):
    _isolated_security(monkeypatch, tmp_path)
    app = Flask(__name__)
    sec.init_security(app)

    r = app.test_client().get("/.env", headers={"X-Real-IP": "198.51.100.10"})

    assert r.status_code == 404
    events = sec.list_security_events(limit=10)
    assert events[0]["event_type"] == "sensitive_path"
    assert events[0]["ip"] == "198.51.100.10"


def test_business_api_named_data_is_not_hidden_by_sensitive_guard(monkeypatch, tmp_path):
    _isolated_security(monkeypatch, tmp_path)
    app = Flask(__name__)
    sec.init_security(app)

    @app.route("/api/lhb/data")
    def lhb_data():
        return jsonify({"ok": True, "stocks": []})

    @app.route("/api/admin/logs")
    def admin_logs():
        return jsonify({"ok": True, "items": []})

    @app.route("/api/data/stocks")
    def data_stocks():
        return jsonify({"ok": True, "items": []})

    r = app.test_client().get("/api/lhb/data", headers={"X-Real-IP": "198.51.100.11"})
    logs_r = app.test_client().get("/api/admin/logs", headers={"X-Real-IP": "198.51.100.11"})
    data_r = app.test_client().get("/api/data/stocks", headers={"X-Real-IP": "198.51.100.11"})

    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert logs_r.status_code == 200
    assert data_r.status_code == 200
    assert sec.list_security_events(limit=10) == []


def test_sensitive_api_file_probe_still_hidden(monkeypatch, tmp_path):
    _isolated_security(monkeypatch, tmp_path)
    app = Flask(__name__)
    sec.init_security(app)

    r = app.test_client().get("/api/download/users.db", headers={"X-Real-IP": "198.51.100.13"})

    assert r.status_code == 404
    assert sec.list_security_events(limit=10)[0]["event_type"] == "sensitive_path"


def test_real_data_file_probe_still_hidden(monkeypatch, tmp_path):
    _isolated_security(monkeypatch, tmp_path)
    app = Flask(__name__)
    sec.init_security(app)

    r = app.test_client().get("/data/users.db", headers={"X-Real-IP": "198.51.100.12"})

    assert r.status_code == 404
    assert sec.list_security_events(limit=10)[0]["event_type"] == "sensitive_path"


def test_offensive_user_agent_is_blocked(monkeypatch, tmp_path):
    _isolated_security(monkeypatch, tmp_path)
    app = Flask(__name__)
    sec.init_security(app)

    @app.route("/api/auth/me")
    def me():
        return jsonify({"ok": True})

    r = app.test_client().get(
        "/api/auth/me",
        headers={"X-Real-IP": "203.0.113.20", "User-Agent": "sqlmap/1.7"},
    )

    assert r.status_code == 403
    assert sec.is_ip_blocked("203.0.113.20")[0] is True


def test_repeated_unauthorized_probe_blocks_ip(monkeypatch, tmp_path):
    _isolated_security(monkeypatch, tmp_path)
    app = Flask(__name__)
    sec.init_security(app)

    @app.route("/api/admin/users")
    def admin_users():
        return jsonify({"ok": False}), 401

    client = app.test_client()
    headers = {"X-Real-IP": "203.0.113.30"}

    assert client.get("/api/admin/users", headers=headers).status_code == 401
    assert client.get("/api/admin/users", headers=headers).status_code == 401
    assert sec.is_ip_blocked("203.0.113.30")[0] is True
    assert client.get("/api/auth/me", headers=headers).status_code == 403


def test_admin_security_event_helpers(monkeypatch, tmp_path):
    _isolated_security(monkeypatch, tmp_path)
    sec.record_security_event(
        "rate_limited",
        severity="medium",
        ip="203.0.113.40",
        path="/api/ai/analyze",
        method="POST",
        user_agent="test",
        detail="too fast",
    )
    sec.block_ip("203.0.113.40", seconds=30, reason="test block")

    assert sec.list_security_events(limit=1)[0]["event_type"] == "rate_limited"
    assert sec.list_ip_blocks()[0]["ip"] == "203.0.113.40"
    sec.release_ip_block("203.0.113.40")
    assert sec.list_ip_blocks() == []
