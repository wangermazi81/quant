import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

import main
import web.ai as web_ai


def _login(client, uid=91):
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["username"] = f"u{uid}"
        sess["is_admin"] = False


def test_ai_analyze_rejects_missing_code_before_deducting(monkeypatch):
    calls = []
    monkeypatch.setattr(web_ai, "_deduct", lambda *args, **kwargs: calls.append(("deduct", args)) or None)
    monkeypatch.setattr(web_ai.ai, "run_analysis", lambda *args, **kwargs: calls.append(("run", args, kwargs)) or {"ok": True})

    client = main.app.test_client()
    _login(client)

    resp = client.post("/api/ai/analyze", json={})
    data = resp.get_json()

    assert resp.status_code == 400
    assert data["ok"] is False
    assert "code" in data["error"]
    assert calls == []


def test_ai_analyze_async_rejects_missing_code_before_deducting(monkeypatch):
    calls = []
    monkeypatch.setattr(web_ai, "_deduct", lambda *args, **kwargs: calls.append(("deduct", args)) or None)
    with web_ai._ai_tasks_lock:
        web_ai._ai_tasks.clear()

    client = main.app.test_client()
    _login(client, uid=92)

    resp = client.post("/api/ai/analyze_async", json={})
    data = resp.get_json()

    assert resp.status_code == 400
    assert data["ok"] is False
    assert "code" in data["error"]
    assert calls == []
    with web_ai._ai_tasks_lock:
        assert web_ai._ai_tasks == {}
