import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, session


def _make_app(monkeypatch):
    import web.user as user_web

    app = Flask(__name__)
    app.secret_key = "test"

    @app.before_request
    def _login():
        session["user_id"] = 42

    app.register_blueprint(user_web.bp)
    return app, user_web


def test_user_logs_bad_limit_uses_default(monkeypatch):
    app, user_web = _make_app(monkeypatch)
    calls = []

    def fake_get_user_logs(uid, limit=200):
        calls.append((uid, limit))
        return [{"msg": "ok"}]

    monkeypatch.setattr(user_web.um, "get_user_logs", fake_get_user_logs)

    response = app.test_client().get("/api/user/logs?limit=bad")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert calls == [(42, 200)]


def test_user_logs_limit_is_clamped(monkeypatch):
    app, user_web = _make_app(monkeypatch)
    calls = []

    def fake_get_user_logs(uid, limit=200):
        calls.append(limit)
        return []

    monkeypatch.setattr(user_web.um, "get_user_logs", fake_get_user_logs)

    response = app.test_client().get("/api/user/logs?limit=999999")

    assert response.status_code == 200
    assert calls == [1000]
