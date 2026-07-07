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

    def fake_build_review(date_text=None, allow_archive=True):
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

    def fake_build_review(date_text=None, allow_archive=True):
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


def test_market_review_uses_latest_archive_before_market_open(monkeypatch):
    import web.market as market_web

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 7, 0, 20, 0)

    latest_archive = {
        "ok": True,
        "date": "2026-07-06",
        "phase": "evening",
        "data": {
            "date": "2026-07-06",
            "market": {
                "date": "2026-07-06",
                "time": "15:00",
                "indices": {},
                "breadth": {
                    "source": "tdx_quotes",
                    "up": 1594,
                    "down": 2954,
                    "flat": 86,
                    "total": 4634,
                    "expected_count": 4634,
                    "coverage": 1.0,
                    "degraded": False,
                },
                "sectors": {"top_gainers": [], "top_losers": []},
            },
        },
    }
    fake_review_engine = types.SimpleNamespace(
        load=lambda date_str: latest_archive if date_str == "2026-07-06" else None,
        list_dates=lambda limit=10: ["2026-07-06", "2026-07-03"],
    )
    monkeypatch.setitem(sys.modules, "review_engine", fake_review_engine)
    monkeypatch.setattr(market_web, "datetime", FakeDateTime)
    monkeypatch.setattr(
        market_web,
        "mctx",
        types.SimpleNamespace(get_market_regime=lambda force_refresh=False: (_ for _ in ()).throw(AssertionError("should not collect live"))),
    )

    review = market_web._build_market_review()

    assert review["date"] == "2026-07-06"
    assert review["archived"] is True
    assert review["breadth"]["expected_count"] == 5190
    assert review["breadth"]["degraded"] is True


def test_market_review_rebuilds_degraded_explicit_archive_from_three_market_source(monkeypatch):
    import web.market as market_web

    archive = {
        "ok": True,
        "date": "2026-07-06",
        "phase": "evening",
        "data": {
            "market": {
                "date": "2026-07-06",
                "time": "15:00",
                "breadth": {
                    "source": "tdx_quotes",
                    "up": 1594,
                    "down": 2954,
                    "flat": 86,
                    "total": 4634,
                    "expected_count": 4634,
                    "coverage": 1.0,
                    "degraded": False,
                },
                "sectors": {"top_gainers": [], "top_losers": []},
            }
        },
    }
    fake_review_engine = types.SimpleNamespace(load=lambda date_str: archive if date_str == "2026-07-06" else None)
    fake_market_stats = types.SimpleNamespace(
        get_verified_market_snapshot=lambda date_text: None,
        normalize_breadth_quality=lambda breadth: {**breadth, "expected_count": 5190, "coverage": round(4634 / 5190, 3), "degraded": True},
        get_market_breadth=lambda date_text=None: {"source": "eastmoney_three_market", "up": 1876, "down": 3541, "flat": 102, "total": 5519, "degraded": False},
        get_market_turnover=lambda date_text=None: {"source": "eastmoney_three_market", "today": 31124, "amount": 31124.43, "degraded": False},
    )
    monkeypatch.setitem(sys.modules, "review_engine", fake_review_engine)
    monkeypatch.setitem(sys.modules, "market_stats", fake_market_stats)

    review = market_web._build_market_review("2026-07-06")

    assert review["date"] == "2026-07-06"
    assert review.get("archived") is not True
    assert review["breadth"]["source"] == "eastmoney_three_market"
    assert review["breadth"]["total"] == 5519
    assert review["turnover"]["source"] == "eastmoney_three_market"
    assert review["turnover"]["today"] == 31124


def test_market_review_force_refresh_bypasses_preopen_archive(monkeypatch):
    import web.market as market_web

    monkeypatch.setattr(market_web, "_should_use_archive_for_live", lambda now=None: True)
    monkeypatch.setattr(market_web, "_load_archive_review", lambda date_text=None: (_ for _ in ()).throw(AssertionError("archive should be bypassed")))

    class FakeMarketStats:
        @staticmethod
        def get_market_breadth(date_text=None):
            return {"source": "eastmoney_spot", "up": 1, "down": 0, "flat": 0, "total": 1}

    monkeypatch.setitem(sys.modules, "market_stats", FakeMarketStats)

    review = market_web._get_market_review(force_refresh=True)

    assert review["breadth"]["source"] == "eastmoney_spot"


def test_market_review_reuses_single_three_market_snapshot(monkeypatch):
    import web.market as market_web

    calls = []
    snapshot = {
        "source": "eastmoney_three_market",
        "stat_date": "2026-07-07",
        "turnover": {"today": 30567, "amount": 30567.4, "source": "eastmoney_three_market", "degraded": False},
        "breadth": {"source": "eastmoney_three_market", "up": 2450, "down": 2810, "flat": 91, "total": 5351, "degraded": False},
    }

    def fetch_once(date_text=None):
        calls.append(date_text)
        return snapshot

    fake_market_stats = types.SimpleNamespace(
        get_verified_market_snapshot=lambda date_text: None,
        normalize_breadth_quality=lambda breadth: breadth or {},
        get_market_snapshot_metrics=lambda date_text=None: {
            "breadth": fetch_once(date_text)["breadth"],
            "turnover": {**snapshot["turnover"], "trend": [{"date": "2026-07-07", "amount": snapshot["turnover"]["today"]}]},
        },
    )
    fake_ths_quote = types.SimpleNamespace(get_client=lambda: types.SimpleNamespace(_ok=False))
    fake_akshare = types.SimpleNamespace(stock_zh_index_spot_em=lambda: None, stock_board_industry_name_em=lambda: None)
    monkeypatch.setitem(sys.modules, "market_stats", fake_market_stats)
    monkeypatch.setitem(sys.modules, "ths_quote", fake_ths_quote)
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)

    review = market_web._build_market_review(allow_archive=False)

    assert calls == [None]
    assert review["breadth"]["source"] == "eastmoney_three_market"
    assert review["breadth"]["total"] == 5351
    assert review["turnover"]["today"] == 30567
    assert review["turnover"]["trend"][-1]["amount"] == 30567


def test_market_review_uses_historical_archive_when_no_verified_snapshot(monkeypatch):
    import web.market as market_web

    archive = {
        "ok": True,
        "date": "2026-07-02",
        "phase": "evening",
        "data": {
            "market": {
                "date": "2026-07-02",
                "time": "15:00",
                "breadth": {"source": "eastmoney_spot", "up": 3000, "down": 2000, "flat": 90, "total": 5090},
            }
        },
    }
    fake_review_engine = types.SimpleNamespace(load=lambda date_str: archive if date_str == "2026-07-02" else None)
    monkeypatch.setitem(sys.modules, "review_engine", fake_review_engine)

    review = market_web._build_market_review("2026-07-02")

    assert review["date"] == "2026-07-02"
    assert review["archived"] is True
    assert review["breadth"]["up"] == 3000


def test_market_review_historical_verified_snapshot_does_not_mix_live_indices():
    import web.market as market_web

    review = market_web._build_market_review("2026-07-03")

    assert review["date"] == "2026-07-03"
    assert review["time"] == "15:00"
    assert review["indices"] == {}
    assert review["turnover"]["today"] == 32055
    assert review["turnover"]["source"] == "eastmoney_three_market_verified"
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


def test_daily_review_api_repairs_degraded_historical_archive(monkeypatch):
    from flask import Flask
    from web.review import bp

    stale_archive = {
        "ok": True,
        "date": "2026-07-06",
        "phase": "evening",
        "data": {
            "date": "2026-07-06",
            "market": {
                "date": "2026-07-06",
                "breadth": {"source": "tdx_quotes", "up": 1594, "down": 2954, "flat": 86, "total": 4634, "expected_count": 4634},
            },
            "turnover": {"source": "official_exchange", "today": 30944.0, "amount": 30944.46},
            "zt": {"zt_count": 64, "dt_count": 46},
        },
        "ai_summary": "old ai summary",
    }
    fake_market_stats = types.SimpleNamespace(
        get_verified_market_snapshot=lambda date_text: None,
        normalize_breadth_quality=lambda breadth: {**breadth, "expected_count": 5190, "coverage": round(4634 / 5190, 3), "degraded": True},
        fetch_eastmoney_three_market_snapshot=lambda date_text: (_ for _ in ()).throw(RuntimeError("direct source unavailable in this test")),
        get_market_breadth=lambda date_text=None: {"source": "eastmoney_three_market", "up": 1876, "down": 3541, "flat": 102, "total": 5519, "degraded": False},
        get_market_turnover=lambda date_text=None: {"source": "eastmoney_three_market", "today": 31124, "amount": 31124.43, "degraded": False},
    )
    monkeypatch.setattr(review_engine, "load", lambda date_str: stale_archive)
    monkeypatch.setattr(review_engine, "collect", lambda date_str: (_ for _ in ()).throw(AssertionError("collect should not be needed for archived market metric repair")))
    monkeypatch.setitem(sys.modules, "review_engine", review_engine)
    monkeypatch.setitem(sys.modules, "market_stats", fake_market_stats)

    app = Flask(__name__)
    app.register_blueprint(bp)

    response = app.test_client().get("/api/review/daily?date=2026-07-06")
    payload = response.get_json()

    assert payload["ok"] is True
    assert payload["phase"] == "evening"
    assert payload["data"]["market"]["breadth"]["source"] == "eastmoney_three_market"
    assert payload["data"]["turnover"]["source"] == "eastmoney_three_market"
    assert payload["ai_summary"] == "old ai summary"



def test_generate_refreshes_market_snapshot_before_collect(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(review_engine, "REVIEW_DIR", tmp_path)
    monkeypatch.setattr(review_engine, "load", lambda date_str: {})
    monkeypatch.setattr(review_engine, "summarize", lambda data, phase: "")

    def fake_refresh(date_str):
        calls.append(("refresh", date_str))
        return True

    def fake_collect(date_str):
        calls.append(("collect", date_str))
        return {
            "date": date_str,
            "trading_day": True,
            "zt": {"zt_count": 1, "dt_count": 0, "zb_count": 0},
            "market": {"breadth": {"up": 1, "down": 0, "total": 1}},
            "turnover": {"today": 1},
        }

    monkeypatch.setattr(review_engine, "_refresh_market_snapshot", fake_refresh, raising=False)
    monkeypatch.setattr(review_engine, "collect", fake_collect)

    result = review_engine.generate("2026-07-06", phase="close", with_ai=False)

    assert result["ok"] is True
    assert calls[:2] == [("refresh", "2026-07-06"), ("collect", "2026-07-06")]

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
