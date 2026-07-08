import sys
from pathlib import Path

from flask import Flask

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

import web.ai as ai_web


def test_strategy_templates_falls_back_without_leaking_server_path(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_web, "BASE_DIR", tmp_path)

    app = Flask(__name__)
    app.register_blueprint(ai_web.bp)

    payload = app.test_client().get("/api/strategy_templates").get_json()

    assert payload["ok"] is True
    assert payload["templates"]
    assert len(payload["templates"]) >= 15
    assert {item["id"] for item in payload["templates"]} >= {"ma_cross", "boll_squeeze", "second_third_board"}
    assert payload["source"] == "builtin"
    assert "error" not in payload
    assert all("/opt/" not in str(item) for item in payload["templates"])


def test_strategy_templates_falls_back_for_malformed_file(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "strategy_templates.json").write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(ai_web, "BASE_DIR", tmp_path)

    app = Flask(__name__)
    app.register_blueprint(ai_web.bp)

    payload = app.test_client().get("/api/strategy_templates").get_json()

    assert payload["ok"] is True
    assert payload["templates"]
    assert payload["source"] == "builtin"
    assert "error" not in payload
