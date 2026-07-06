import sys
import types
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

import review_engine


def test_daily_review_without_date_uses_latest_archive_before_market_open(monkeypatch):
    from flask import Flask
    import web.review as review_web

    latest_archive = {"ok": True, "date": "2026-07-06", "phase": "evening", "data": {"date": "2026-07-06"}}

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 7, 0, 10, 0)

    fake_review_engine = types.SimpleNamespace(
        load=lambda date_str: latest_archive if date_str == "2026-07-06" else None,
        list_dates=lambda: ["2026-07-06", "2026-07-03"],
        collect=lambda date_str: (_ for _ in ()).throw(AssertionError("should not live collect before open")),
    )
    monkeypatch.setitem(sys.modules, "review_engine", fake_review_engine)
    monkeypatch.setattr(review_web, "datetime", FakeDateTime)

    app = Flask(__name__)
    app.register_blueprint(review_web.bp)

    response = app.test_client().get("/api/review/daily")
    payload = response.get_json()

    assert payload["ok"] is True
    assert payload["date"] == "2026-07-06"
    assert payload["phase"] == "evening"


def test_market_review_api_reuses_short_cache(monkeypatch):
    from flask import Flask
    import web.market as market_web

    calls = []

    def fake_build_review(date_text=None):
        calls.append(date_text)
        return {"date": "2026-07-06", "breadth": {"up": len(calls)}}

    monkeypatch.setattr(market_web, "_MARKET_REVIEW_CACHE", {})
    monkeypatch.setattr(market_web, "_MARKET_REVIEW_CACHE_TTL", 60)
    monkeypatch.setattr(market_web, "_build_market_review", fake_build_review)

    app = Flask(__name__)
    app.register_blueprint(market_web.bp)
    client = app.test_client()

    first = client.get("/api/market/review").get_json()
    second = client.get("/api/market/review").get_json()

    assert first["breadth"]["up"] == 1
    assert second["breadth"]["up"] == 1
    assert calls == [None]


def test_market_review_cache_does_not_miss_on_minute_boundary(monkeypatch):
    import web.market as market_web

    calls = []
    now_values = iter([100.0, 110.0])
    bucket_values = iter(["202607062359", "202607070000"])

    def fake_build_review(date_text=None):
        calls.append(date_text)
        return {"date": "2026-07-06", "breadth": {"up": len(calls)}}

    monkeypatch.setattr(market_web, "_MARKET_REVIEW_CACHE", {})
    monkeypatch.setattr(market_web, "_MARKET_REVIEW_CACHE_TTL", 60)
    monkeypatch.setattr(market_web, "time", lambda: next(now_values))
    monkeypatch.setattr(market_web, "_dt_cache_bucket", lambda date_text: next(bucket_values))
    monkeypatch.setattr(market_web, "_build_market_review", fake_build_review)

    first = market_web._get_market_review()
    second = market_web._get_market_review()

    assert first["breadth"]["up"] == 1
    assert second["breadth"]["up"] == 1
    assert calls == [None]


def test_market_review_historical_verified_snapshot_does_not_mix_live_indices():
    import web.market as market_web

    review = market_web._build_market_review("2026-07-03")

    assert review["date"] == "2026-07-03"
    assert review["time"] == "15:00"
    assert review["indices"] == {}
    assert review["breadth"]["up"] == 3804
    assert review["breadth"]["source"] == "eastmoney_three_market_verified"


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


def test_daily_review_api_verified_snapshot_overrides_stale_historical_archive(monkeypatch):
    from flask import Flask
    from web.review import bp

    stale_archive = {
        "ok": True,
        "date": "2026-07-03",
        "phase": "manual",
        "data": {
            "date": "2026-07-03",
            "market": {
                "date": "2026-07-06",
                "breadth": {"up": 3527, "down": 1584, "flat": 82, "total": 5193},
            },
            "turnover": {"today": 31853.0, "amount": 31852.82},
            "zt": {"zt_count": 108, "dt_count": 19},
        },
        "ai_summary": "old summary with stale market numbers",
    }
    monkeypatch.setattr(review_engine, "load", lambda date_str: stale_archive)
    monkeypatch.setitem(sys.modules, "review_engine", review_engine)

    app = Flask(__name__)
    app.register_blueprint(bp)

    response = app.test_client().get("/api/review/daily?date=2026-07-03")
    payload = response.get_json()

    assert payload["ok"] is True
    assert payload["phase"] == "verified_snapshot"
    assert payload["data"]["market"]["date"] == "2026-07-03"
    assert payload["data"]["turnover"]["today"] == 32055
    assert payload["data"]["market"]["breadth"]["up"] == 3804
    assert payload["data"]["market"]["breadth"]["down"] == 1628
    assert payload["data"]["zt"]["zt_count"] == 160
    assert payload["data"]["zt"]["dt_count"] == 24


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


def test_market_review_api_passes_requested_date(monkeypatch):
    from flask import Flask
    import web.market as market_web

    calls = []

    def fake_market_review(date_text=None, force_refresh=False):
        calls.append((date_text, force_refresh))
        return {"date": "2026-07-03", "breadth": {"up": 3804}}

    monkeypatch.setattr(market_web, "_get_market_review", fake_market_review)

    app = Flask(__name__)
    app.register_blueprint(market_web.bp)

    response = app.test_client().get("/api/market/review?date=2026-07-03")
    payload = response.get_json()

    assert payload["ok"] is True
    assert calls == [("2026-07-03", False)]
    assert payload["breadth"]["up"] == 3804
