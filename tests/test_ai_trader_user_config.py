# -*- coding: utf-8 -*-
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

from flask import Flask

import app_security
import ai_analyst
import ai_trader
import db_migration
import user_manager as um
from web.ai_trader import bp as ai_trader_bp
from web.ai import bp as platform_ai_bp


def _app_with_user(uid: int) -> Flask:
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


def _platform_ai_app(uid: int) -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app_security.init_security(app)
    app.register_blueprint(platform_ai_bp)

    @app.before_request
    def _login_admin():
        from flask import session

        session["user_id"] = uid
        session["username"] = f"admin{uid}"
        session["is_admin"] = True

    return app


def _init_user_db(tmp_path, monkeypatch):
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(um, "DB_PATH", db)
    monkeypatch.setattr(db_migration, "USERS_DB", db)
    um.init_db()
    db_migration.migrate_users_db()


def test_ai_trader_config_is_saved_per_logged_in_user(tmp_path, monkeypatch):
    _init_user_db(tmp_path, monkeypatch)
    monkeypatch.setattr(ai_trader, "TRADER_CFG_FILE", tmp_path / "global_ai_trader_config.json")
    ai_trader.save_trader_config({
        "provider": "owner",
        "api_key": "owner-secret-key",
        "base_url": "https://owner.example/v1",
        "model": "owner-model",
        "enabled": True,
    })

    c101 = _app_with_user(101).test_client()
    r = c101.post("/api/ai_trader/config", json={
        "provider": "deepseek",
        "api_key": "user101-secret-key",
        "base_url": "https://u101.example/v1",
        "model": "u101-model",
        "enabled": True,
    }).get_json()
    assert r["ok"] is True
    assert r["config"]["has_key"] is True

    cfg101 = c101.get("/api/ai_trader/config").get_json()["config"]
    assert cfg101["provider"] == "deepseek"
    assert cfg101["base_url"] == "https://u101.example/v1"
    assert cfg101["model"] == "u101-model"
    assert cfg101["enabled"] is True
    assert cfg101["has_key"] is True
    assert "user101-secret-key" not in str(cfg101)
    assert "owner-secret-key" not in str(cfg101)

    cfg202 = _app_with_user(202).test_client().get("/api/ai_trader/config").get_json()["config"]
    assert cfg202["provider"] != "owner"
    assert cfg202["base_url"] != "https://owner.example/v1"
    assert cfg202["model"] != "owner-model"
    assert cfg202["has_key"] is False
    assert cfg202["enabled"] is False


def test_ai_trader_engine_refuses_to_use_global_key_for_user_without_config(tmp_path, monkeypatch):
    _init_user_db(tmp_path, monkeypatch)
    monkeypatch.setattr(ai_trader, "TRADER_CFG_FILE", tmp_path / "global_ai_trader_config.json")
    ai_trader.save_trader_config({
        "provider": "owner",
        "api_key": "owner-secret-key",
        "base_url": "https://owner.example/v1",
        "model": "owner-model",
        "enabled": True,
    })
    monkeypatch.setattr(ai_trader._Engine, "_run_round", lambda self, st: None)
    ai_trader._engines.clear()

    res = ai_trader.get_engine(202).run_once()

    assert res["ok"] is False
    assert res["msg"]


def test_ai_trader_status_explains_waiting_outside_market_time(monkeypatch):
    monkeypatch.setitem(sys.modules, "market_data", types.SimpleNamespace(is_trading_time=lambda: False))
    engine = ai_trader._Engine(101)
    engine.running = True

    st = engine.status()

    assert st["market_open"] is False
    assert st["analysis_ready"] is False
    assert "非A股连续竞价时段" in st["waiting_reason"]


def test_platform_ai_config_does_not_fallback_to_ai_trader_user_key(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_analyst, "CONFIG_FILE", tmp_path / "ai_config.json")
    ai_analyst.save_config({
        "provider": "platform",
        "api_key": "",
        "base_url": "https://platform.example/v1",
        "model": "platform-model",
        "enabled": False,
    })

    resolved = ai_analyst.resolve_config({
        "provider": "deepseek",
        "api_key": "user-trader-secret-key",
        "base_url": "https://user.example/v1",
        "model": "user-model",
        "enabled": True,
    })

    assert resolved["provider"] == "platform"
    assert resolved["api_key"] == ""
    assert resolved["base_url"] == "https://platform.example/v1"
    assert resolved["model"] == "platform-model"
    assert resolved["enabled"] is False


def test_saving_platform_ai_config_keeps_user_ai_trader_config(tmp_path, monkeypatch):
    _init_user_db(tmp_path, monkeypatch)
    monkeypatch.setattr(ai_analyst, "CONFIG_FILE", tmp_path / "ai_config.json")

    um.save_user_ai_config(
        101,
        provider="deepseek",
        api_key="user101-secret-key",
        base_url="https://u101.example/v1",
        model="u101-model",
        enabled=True,
    )

    r = _platform_ai_app(1).test_client().post("/api/ai/config", json={
        "provider": "platform",
        "api_key": "platform-secret-key",
        "base_url": "https://platform.example/v1",
        "model": "platform-model",
        "enabled": True,
    }).get_json()

    assert r["ok"] is True
    saved = um.get_user_ai_config(101)
    assert saved["api_key"] == "user101-secret-key"
    assert saved["base_url"] == "https://u101.example/v1"
    assert saved["model"] == "u101-model"
    assert saved["enabled"] == 1
