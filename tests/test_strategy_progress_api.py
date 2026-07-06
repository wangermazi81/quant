import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

import main


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

