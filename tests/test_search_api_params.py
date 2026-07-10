import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, session


def _make_app(monkeypatch):
    import web.search as search_web

    app = Flask(__name__)
    app.secret_key = "test"

    @app.before_request
    def _login():
        session["user_id"] = 1

    monkeypatch.setattr(search_web.dm, "get_stock_name", lambda code: "平安银行")
    app.register_blueprint(search_web.bp)
    return app, search_web


def test_search_news_bad_limit_uses_default(monkeypatch):
    app, search_web = _make_app(monkeypatch)
    calls = []

    def fake_search_stock_news(code, name="", limit=8, force_refresh=False):
        calls.append((code, name, limit, force_refresh))
        return [{"title": "n"}]

    monkeypatch.setattr(
        search_web,
        "search_mgr",
        types.SimpleNamespace(search_stock_news=fake_search_stock_news),
    )

    response = app.test_client().get("/api/search/news/000001?limit=bad")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert calls == [("000001", "平安银行", 8, False)]


def test_search_news_limit_is_clamped(monkeypatch):
    app, search_web = _make_app(monkeypatch)
    calls = []

    def fake_search_stock_news(code, name="", limit=8, force_refresh=False):
        calls.append(limit)
        return []

    monkeypatch.setattr(
        search_web,
        "search_mgr",
        types.SimpleNamespace(search_stock_news=fake_search_stock_news),
    )

    response = app.test_client().get("/api/search/news/000001?limit=999999")

    assert response.status_code == 200
    assert calls == [50]


def test_search_web_bad_limit_uses_default(monkeypatch):
    app, search_web = _make_app(monkeypatch)
    calls = []

    def fake_search_web(query, topic="general", limit=10, force_refresh=False):
        calls.append((query, topic, limit, force_refresh))
        return [{"title": "r"}]

    monkeypatch.setattr(
        search_web,
        "search_mgr",
        types.SimpleNamespace(search_web=fake_search_web),
    )

    response = app.test_client().post(
        "/api/search/web",
        json={"query": "AI交易", "topic": "research", "limit": "bad"},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert calls == [("AI交易", "research", 10, False)]


def test_twelvedata_kline_bad_days_uses_default_and_clamps(monkeypatch):
    app, search_web = _make_app(monkeypatch)
    calls = []

    def fake_td_get_kline(code, days=60, interval="1day"):
        calls.append((code, days, interval))
        return [{"close": 1}]

    monkeypatch.setattr(
        search_web,
        "search_mgr",
        types.SimpleNamespace(td_get_kline=fake_td_get_kline),
    )

    bad = app.test_client().get("/api/search/twelvedata/kline/000001?days=bad")
    huge = app.test_client().get("/api/search/twelvedata/kline/000001?days=999999")

    assert bad.status_code == 200
    assert huge.status_code == 200
    assert calls == [("000001", 60, "1day"), ("000001", 3650, "1day")]
