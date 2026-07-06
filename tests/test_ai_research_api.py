# -*- coding: utf-8 -*-
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

from flask import Flask

import app_security
import ai_research_store as store
from web.ai_trader import bp as ai_trader_bp


def _app(uid=101):
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app_security.init_security(app)
    app.register_blueprint(ai_trader_bp)

    @app.before_request
    def _login_user():
        from flask import session

        session["user_id"] = uid
        session["username"] = f"u{uid}"
        session["is_admin"] = False

    return app


def test_research_memory_endpoint_is_user_scoped_and_masks_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "research.db")
    store.init_research_db()
    store.record_decision(101, {"as_of_date": "2026-07-03", "code": "000001", "action": "BUY", "confidence": 80, "features": {"change_pct": 5}})
    store.record_decision(202, {"as_of_date": "2026-07-03", "code": "000001", "action": "SELL", "confidence": 10, "features": {"change_pct": -5}})

    data = _app(101).test_client().get("/api/ai_trader/research/memory?code=000001").get_json()

    assert data["ok"] is True
    assert len(data["memories"]) == 1
    assert data["memories"][0]["decision"]["action"] == "BUY"
    assert "api_key" not in str(data).lower()


def test_research_backtest_starts_async_job_without_blocking(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "research.db")
    store.init_research_db()
    store.record_decision(101, {"as_of_date": "2026-07-01", "code": "000001", "action": "BUY", "confidence": 80})
    store.record_trade_outcome(101, "000001", {"as_of_date": "2026-07-02", "pnl_pct": 3.0})
    client = _app(101).test_client()

    t0 = time.time()
    data = client.post("/api/ai_trader/research/backtest", json={"start": "2026-07-01", "end": "2026-07-05", "top": 5}).get_json()
    elapsed = time.time() - t0

    assert data["ok"] is True
    assert data["job_id"]
    assert data["status"] in {"queued", "running", "done"}
    assert elapsed < 1.0

    status = client.get(f"/api/ai_trader/research/backtest/{data['job_id']}").get_json()
    assert status["ok"] is True
    assert status["job_id"] == data["job_id"]
    for _ in range(20):
        status = client.get(f"/api/ai_trader/research/backtest/{data['job_id']}").get_json()
        if status.get("status") == "done":
            break
        time.sleep(0.05)
    assert status["status"] == "done"
    assert status["result"]["decision_count"] == 1
    assert status["result"]["avg_pnl_pct"] == 3.0
