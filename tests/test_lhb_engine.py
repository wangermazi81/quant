import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

import lhb_engine


def test_lhb_api_refresh_forwards_force_refresh(monkeypatch):
    from flask import Flask
    import web.lhb as lhb_web

    calls = []

    def fake_get_lhb_data(date=None, force_refresh=False):
        calls.append((date, force_refresh))
        return {"ok": True, "date": date, "stocks": [], "total": 0}

    fake_engine = types.SimpleNamespace(
        get_lhb_data=fake_get_lhb_data,
        get_lhb_dates=lambda: ["2026-07-08"],
    )
    monkeypatch.setitem(sys.modules, "lhb_engine", fake_engine)

    app = Flask(__name__)
    app.register_blueprint(lhb_web.bp)

    payload = app.test_client().get("/api/lhb/data?date=2026-07-08&refresh=1").get_json()

    assert payload["ok"] is True
    assert calls == [("2026-07-08", True)]


def test_preload_today_force_refreshes_and_reports_success(monkeypatch):
    calls = []

    def fake_get_lhb_data(date=None, force_refresh=False):
        calls.append((date, force_refresh))
        return {"ok": True, "date": "2026-07-08", "stocks": [{"code": "000001"}]}

    monkeypatch.setattr(lhb_engine, "get_lhb_data", fake_get_lhb_data)

    assert lhb_engine.preload_today() is True
    assert calls == [(None, True)]


def test_preload_today_reports_pending_when_not_published(monkeypatch):
    monkeypatch.setattr(
        lhb_engine,
        "get_lhb_data",
        lambda date=None, force_refresh=False: {"ok": False, "date": "2026-07-08", "stocks": []},
    )

    assert lhb_engine.preload_today() is False
