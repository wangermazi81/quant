import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

import main


def _login(client, uid=7):
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["username"] = f"u{uid}"
        sess["is_admin"] = False


def test_async_strategy_progress_uses_strategy_manager_progress(monkeypatch):
    task_id = "task123"
    with main._strat_tasks_lock:
        main._strat_tasks.clear()
        main._strat_tasks[task_id] = {"status": "running", "uid": 7, "fn": "demo"}

    monkeypatch.setattr(main.sm, "get_scan_progress", lambda: {
        "running": True,
        "stage": "strategy",
        "current": 17,
        "total": 88,
        "code": "000001",
    })

    client = main.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 7

    resp = client.get(f"/api/strategies/progress/{task_id}")

    assert resp.status_code == 200
    assert resp.get_json() == {
        "ok": True,
        "task_id": task_id,
        "status": "running",
        "scan_current": 17,
        "scan_total": 88,
        "scan_code": "000001",
        "scan_stage": "strategy",
    }


def test_strategy_run_rejects_missing_filename_before_deducting(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_deduct", lambda *args, **kwargs: calls.append(("deduct", args)) or None)
    monkeypatch.setattr(main.sm, "run", lambda *args, **kwargs: calls.append(("run", args, kwargs)) or {"ok": True})

    client = main.app.test_client()
    _login(client, uid=8)

    resp = client.post("/api/strategies/run", json={})
    data = resp.get_json()

    assert resp.status_code == 400
    assert data["ok"] is False
    assert "filename" in data["error"]
    assert calls == []


def test_strategy_run_async_rejects_missing_filename_before_deducting(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_deduct", lambda *args, **kwargs: calls.append(("deduct", args)) or None)
    monkeypatch.setattr(main.sm, "run", lambda *args, **kwargs: calls.append(("run", args, kwargs)) or {"ok": True})
    with main._strat_tasks_lock:
        main._strat_tasks.clear()

    client = main.app.test_client()
    _login(client, uid=9)

    resp = client.post("/api/strategies/run_async", json={})
    data = resp.get_json()

    assert resp.status_code == 400
    assert data["ok"] is False
    assert "filename" in data["error"]
    assert calls == []
    with main._strat_tasks_lock:
        assert main._strat_tasks == {}
