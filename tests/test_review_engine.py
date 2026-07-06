import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

import review_engine


def test_daily_review_api_uses_verified_snapshot_when_historical_archive_missing(monkeypatch):
    from flask import Flask
    from web.review import bp

    monkeypatch.setattr(review_engine, "load", lambda date_str: None)
    monkeypatch.setitem(sys.modules, "review_engine", review_engine)

    app = Flask(__name__)
    app.register_blueprint(bp)

    response = app.test_client().get("/api/review/daily?date=2026-07-03")
    payload = response.get_json()

    assert payload["ok"] is True
    assert payload["phase"] == "verified_snapshot"
    assert payload["data"]["turnover"]["today"] == 32055
    assert payload["data"]["market"]["breadth"]["up"] == 3804
    assert payload["data"]["zt"]["zt_count"] == 160


def test_collect_turnover_uses_requested_compact_date(monkeypatch):
    calls = []

    class FakeMarketStats:
        @staticmethod
        def get_market_turnover(date_text=None):
            calls.append(date_text)
            return {"today": 123.0, "trend": [], "source": "test"}

    monkeypatch.setitem(sys.modules, "market_stats", FakeMarketStats)

    turnover = review_engine._collect_turnover("20260703")

    assert turnover["today"] == 123.0
    assert calls == ["20260703"]


def test_collect_passes_requested_date_to_market_review(monkeypatch):
    calls = []

    def fake_market_review(date_text=None):
        calls.append(date_text)
        return {"breadth": {"up": 3804, "down": 1628, "flat": 84, "total": 5516}}

    monkeypatch.setitem(sys.modules, "web.market", types.SimpleNamespace(_get_market_review=fake_market_review))
    monkeypatch.setattr(
        review_engine,
        "_collect_zt",
        lambda compact: {"zt_count": 1, "dt_count": 0, "zb_count": 0, "zb_rate": None, "prev_zt_avg": None},
    )
    monkeypatch.setattr(review_engine, "_collect_turnover", lambda compact: {"today": 32055, "trend": []})
    monkeypatch.setattr(review_engine, "_collect_sectors", lambda: {})
    monkeypatch.setitem(sys.modules, "news_center", types.SimpleNamespace(important_digest=lambda date, limit=40: []))

    data = review_engine.collect("2026-07-03")

    assert calls == ["20260703"]
    assert data["market"]["breadth"]["up"] == 3804
