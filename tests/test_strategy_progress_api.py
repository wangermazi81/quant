import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

import main
from strategy_engine import strategy_manager as sm
import web.strategies as strategies_web
import web.trade as trade_web


def _login(client, uid=7):
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["username"] = f"u{uid}"
        sess["is_admin"] = False


def test_async_strategy_progress_uses_strategy_manager_progress(monkeypatch):
    task_id = "task123"
    with strategies_web._strat_tasks_lock:
        strategies_web._strat_tasks.clear()
        strategies_web._strat_tasks[task_id] = {"status": "running", "uid": 7, "fn": "demo"}

    monkeypatch.setattr(sm, "get_scan_progress", lambda: {
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
    monkeypatch.setattr(strategies_web, "_deduct", lambda *args, **kwargs: calls.append(("deduct", args)) or None)
    monkeypatch.setattr(sm, "run", lambda *args, **kwargs: calls.append(("run", args, kwargs)) or {"ok": True})

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
    monkeypatch.setattr(strategies_web, "_deduct", lambda *args, **kwargs: calls.append(("deduct", args)) or None)
    monkeypatch.setattr(sm, "run", lambda *args, **kwargs: calls.append(("run", args, kwargs)) or {"ok": True})
    with strategies_web._strat_tasks_lock:
        strategies_web._strat_tasks.clear()

    client = main.app.test_client()
    _login(client, uid=9)

    resp = client.post("/api/strategies/run_async", json={})
    data = resp.get_json()

    assert resp.status_code == 400
    assert data["ok"] is False
    assert "filename" in data["error"]
    assert calls == []
    with strategies_web._strat_tasks_lock:
        assert strategies_web._strat_tasks == {}


def test_strategy_activate_starts_monitoring_for_selected_strategy(monkeypatch):
    state = {"running": False, "active_strategy": None}
    persisted = []
    logs = []

    monkeypatch.setattr(strategies_web, "get_user_state", lambda uid: state)
    monkeypatch.setattr(strategies_web, "_persist_state", lambda uid, **fields: persisted.append((uid, fields)))
    monkeypatch.setattr(strategies_web, "_add_log", lambda uid, msg, level="INFO": logs.append((uid, msg, level)))
    monkeypatch.setattr(sm, "list_strategies", lambda: [
        {"file": "demo_strategy.py", "name": '演示策略'}
    ])

    client = main.app.test_client()
    _login(client, uid=10)

    resp = client.post("/api/strategies/activate", json={"filename": "demo_strategy.py"})
    data = resp.get_json()

    assert resp.status_code == 200
    assert data == {"ok": True, "active": "demo_strategy.py"}
    assert state["active_strategy"] == "demo_strategy.py"
    assert state["running"] is True
    assert any(fields.get("active_strategy") == "demo_strategy.py" for _, fields in persisted)
    assert any(fields.get("running") == 1 for _, fields in persisted)
    assert any('演示策略' in msg and '监控' in msg for _, msg, _ in logs)


def test_status_reports_vibe_activation_as_monitoring(monkeypatch):
    state = {"running": False, "active_strategy": None, "scan_progress": {"running": False}}
    monkeypatch.setattr(trade_web, "get_user_state", lambda uid: state)

    class FakeActivator:
        @staticmethod
        def get(uid):
            return {"enabled": True, "factor_id": "demo_factor"}

    import sys
    import types
    fake_pkg = types.ModuleType("analytics.factors_zoo")
    fake_pkg.activator = FakeActivator
    monkeypatch.setitem(sys.modules, "analytics.factors_zoo", fake_pkg)

    client = main.app.test_client()
    _login(client, uid=11)

    resp = client.get("/api/status")
    data = resp.get_json()

    assert resp.status_code == 200
    assert data["running"] is False
    assert data["vibe_active"] is True
    assert data["vibe_factor"] == "demo_factor"
