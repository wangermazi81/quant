import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

import main
import web.research as web_research


def _login(client, uid=93):
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["username"] = f"u{uid}"
        sess["is_admin"] = False


def test_chain_graph_rejects_missing_name_before_deducting(monkeypatch):
    calls = []
    monkeypatch.setattr(web_research, "_deduct", lambda *args, **kwargs: calls.append(("deduct", args)) or None)

    client = main.app.test_client()
    _login(client)

    resp = client.post("/api/chain/graph", json={})
    data = resp.get_json()

    assert resp.status_code == 400
    assert data["ok"] is False
    assert "name" in data["error"]
    assert calls == []


def test_chain_graph_rejects_non_json_body_without_415(monkeypatch):
    calls = []
    monkeypatch.setattr(web_research, "_deduct", lambda *args, **kwargs: calls.append(("deduct", args)) or None)

    client = main.app.test_client()
    _login(client, uid=94)

    resp = client.post("/api/chain/graph", data="name=AI")
    data = resp.get_json()

    assert resp.status_code == 400
    assert data["ok"] is False
    assert "name" in data["error"]
    assert calls == []
