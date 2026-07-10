from tests._module_stubs import stub_project_module
import sys
import types
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics import review_engine


def test_lhb_preloader_retries_until_today_is_cached(monkeypatch):
    calls = []

    def fake_preload_today():
        calls.append(len(calls) + 1)
        return len(calls) >= 2

    stub_project_module(monkeypatch, "lhb_engine", types.SimpleNamespace(preload_today=fake_preload_today))
    state = {}

    first = review_engine._maybe_preload_lhb(
        datetime(2026, 7, 8, 15, 31, 0), state, now_ts=1000.0
    )
    too_soon = review_engine._maybe_preload_lhb(
        datetime(2026, 7, 8, 15, 31, 30), state, now_ts=1030.0
    )
    second = review_engine._maybe_preload_lhb(
        datetime(2026, 7, 8, 15, 32, 1), state, now_ts=1061.0
    )
    after_success = review_engine._maybe_preload_lhb(
        datetime(2026, 7, 8, 15, 45, 0), state, now_ts=1900.0
    )

    assert first is False
    assert too_soon is False
    assert second is True
    assert after_success is True
    assert calls == [1, 2]
    assert state["done_date"] == "2026-07-08"


def test_lhb_preloader_retries_quickly_right_after_publish_window(monkeypatch):
    calls = []

    def fake_preload_today():
        calls.append(len(calls) + 1)
        return len(calls) >= 2

    stub_project_module(monkeypatch, "lhb_engine", types.SimpleNamespace(preload_today=fake_preload_today))
    state = {}

    first = review_engine._maybe_preload_lhb(
        datetime(2026, 7, 8, 15, 25, 0), state, now_ts=1000.0
    )
    too_soon = review_engine._maybe_preload_lhb(
        datetime(2026, 7, 8, 15, 25, 30), state, now_ts=1030.0
    )
    retry = review_engine._maybe_preload_lhb(
        datetime(2026, 7, 8, 15, 26, 1), state, now_ts=1061.0
    )

    assert first is False
    assert too_soon is False
    assert retry is True
    assert calls == [1, 2]


def test_lhb_preloader_keeps_retrying_until_late_evening(monkeypatch):
    calls = []

    def fake_preload_today():
        calls.append(len(calls) + 1)
        return False

    stub_project_module(monkeypatch, "lhb_engine", types.SimpleNamespace(preload_today=fake_preload_today))
    state = {}

    first = review_engine._maybe_preload_lhb(
        datetime(2026, 7, 8, 16, 31, 0), state, now_ts=2000.0
    )
    quick_retry = review_engine._maybe_preload_lhb(
        datetime(2026, 7, 8, 16, 34, 0), state, now_ts=2180.0
    )
    late_retry = review_engine._maybe_preload_lhb(
        datetime(2026, 7, 8, 22, 30, 0), state, now_ts=2301.0
    )
    after_window = review_engine._maybe_preload_lhb(
        datetime(2026, 7, 8, 23, 1, 0), state, now_ts=2700.0
    )

    assert first is False
    assert quick_retry is False
    assert late_retry is False
    assert after_window is False
    assert calls == [1, 2]


def test_lhb_preloader_keeps_one_minute_retry_until_finalization(monkeypatch):
    calls = []

    def fake_preload_today():
        calls.append(len(calls) + 1)
        return False

    stub_project_module(monkeypatch, "lhb_engine", types.SimpleNamespace(preload_today=fake_preload_today))
    state = {}

    first = review_engine._maybe_preload_lhb(
        datetime(2026, 7, 8, 16, 31, 0), state, now_ts=2000.0
    )
    retry = review_engine._maybe_preload_lhb(
        datetime(2026, 7, 8, 16, 32, 1), state, now_ts=2061.0
    )

    assert first is False
    assert retry is False
    assert calls == [1, 2]


def test_lhb_backfill_runs_after_close_and_throttles(monkeypatch):
    calls = []

    def fake_backfill_recent(today=None, days=5, limit=2):
        calls.append((today.strftime("%Y-%m-%d"), days, limit))
        return {"checked": ["2026-07-07"], "saved": ["2026-07-07"], "pending": [], "skipped": []}

    stub_project_module(monkeypatch, "lhb_engine", types.SimpleNamespace(backfill_recent=fake_backfill_recent))
    state = {}

    before_window = review_engine._maybe_backfill_lhb(
        datetime(2026, 7, 8, 16, 29, 0), state, now_ts=1000.0
    )
    first = review_engine._maybe_backfill_lhb(
        datetime(2026, 7, 8, 16, 31, 0), state, now_ts=1060.0
    )
    too_soon = review_engine._maybe_backfill_lhb(
        datetime(2026, 7, 8, 16, 45, 0), state, now_ts=1900.0
    )
    retry = review_engine._maybe_backfill_lhb(
        datetime(2026, 7, 8, 17, 2, 0), state, now_ts=2920.0
    )

    assert before_window is False
    assert first is True
    assert too_soon is False
    assert retry is True
    assert calls == [("2026-07-08", 5, 2), ("2026-07-08", 5, 2)]


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
    stub_project_module(monkeypatch, "review_engine", fake_review_engine)
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
    stub_project_module(monkeypatch, "review_engine", fake_review_engine)
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


def test_news_feed_sanitizes_limit_and_offset(monkeypatch):
    from flask import Flask
    import web.review as review_web

    calls = []

    def fake_query(**kwargs):
        calls.append(kwargs)
        return [{"title": "ok"}]

    fake_news_center = types.SimpleNamespace(
        query=fake_query,
        fetch_quick=lambda: None,
        maybe_fetch_async=lambda min_gap=60: None,
    )
    monkeypatch.setitem(sys.modules, "news_center", fake_news_center)

    app = Flask(__name__)
    app.register_blueprint(review_web.bp)
    client = app.test_client()

    invalid = client.get("/api/news/feed?limit=abc&offset=abc")
    clamped = client.get("/api/news/feed?limit=999999&offset=-20")

    assert invalid.status_code == 200
    assert clamped.status_code == 200
    assert calls[0]["limit"] == 80
    assert calls[0]["offset"] == 0
    assert calls[1]["limit"] == 200
    assert calls[1]["offset"] == 0


def test_market_review_uses_today_archive_after_close(monkeypatch):
    import web.market as market_web

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 8, 15, 20, 0)

    archive_review = {
        "date": "2026-07-08",
        "time": "15:10",
        "archived": True,
        "breadth": {"source": "eastmoney_three_market", "up": 1593, "down": 3790, "flat": 136, "total": 5519},
        "turnover": {"source": "eastmoney_index", "today": 25635.0, "degraded": False},
        "sectors": {"top_gainers": [], "top_losers": []},
    }
    monkeypatch.setattr(market_web, "datetime", FakeDateTime)
    monkeypatch.setattr(market_web, "_load_archive_review", lambda date_text=None: archive_review)
    monkeypatch.setattr(
        market_web,
        "_fetch_industry_sector_rotation",
        lambda: (_ for _ in ()).throw(AssertionError("after-close archive should bypass live sector fetch")),
    )

    review = market_web._build_market_review()

    assert review["date"] == "2026-07-08"
    assert review["archived"] is True
    assert review["turnover"]["today"] == 25635.0


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
            },
            "turnover": {"source": "eastmoney_three_market", "today": 31124, "amount": 31124.43, "degraded": False},
        },
    }
    fake_review_engine = types.SimpleNamespace(load=lambda date_str: archive if date_str == "2026-07-06" else None)
    fake_market_stats = types.SimpleNamespace(
        get_verified_market_snapshot=lambda date_text: None,
        normalize_breadth_quality=lambda breadth: {**breadth, "expected_count": 5190, "coverage": round(4634 / 5190, 3), "degraded": True},
        get_market_breadth=lambda date_text=None: {"source": "eastmoney_three_market", "up": 1876, "down": 3541, "flat": 102, "total": 5519, "degraded": False},
        get_market_turnover=lambda date_text=None: {"source": "official_exchange", "today": 30944.0, "amount": 30944.46, "degraded": False},
    )
    stub_project_module(monkeypatch, "review_engine", fake_review_engine)
    stub_project_module(monkeypatch, "market_stats", fake_market_stats)

    review = market_web._build_market_review("2026-07-06")

    assert review["date"] == "2026-07-06"
    assert review.get("archived") is not True
    assert review["breadth"]["source"] == "eastmoney_three_market"
    assert review["breadth"]["total"] == 5519
    assert review["turnover"]["source"] == "official_exchange"
    assert review["turnover"]["today"] == 30944.0


def test_market_review_force_refresh_bypasses_preopen_archive(monkeypatch):
    import web.market as market_web

    monkeypatch.setattr(market_web, "_should_use_archive_for_live", lambda now=None: True)
    monkeypatch.setattr(market_web, "_load_archive_review", lambda date_text=None: (_ for _ in ()).throw(AssertionError("archive should be bypassed")))

    class FakeMarketStats:
        @staticmethod
        def get_market_breadth(date_text=None):
            return {"source": "eastmoney_spot", "up": 1, "down": 0, "flat": 0, "total": 1}

    stub_project_module(monkeypatch, "market_stats", FakeMarketStats)

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
    stub_project_module(monkeypatch, "market_stats", fake_market_stats)
    stub_project_module(monkeypatch, "ths_quote", fake_ths_quote)
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)

    review = market_web._build_market_review(allow_archive=False)

    assert calls == [None]
    assert review["breadth"]["source"] == "eastmoney_three_market"
    assert review["breadth"]["total"] == 5351
    assert review["turnover"]["today"] == 30567
    assert review["turnover"]["trend"][-1]["amount"] == 30567


def test_market_review_uses_direct_sector_source_without_slow_akshare(monkeypatch):
    import web.market as market_web

    fake_market_stats = types.SimpleNamespace(
        get_verified_market_snapshot=lambda date_text: None,
        normalize_breadth_quality=lambda breadth: breadth or {},
        get_market_snapshot_metrics=lambda date_text=None: {
            "breadth": {"source": "eastmoney_three_market", "up": 1, "down": 0, "flat": 0, "total": 1},
            "turnover": {"source": "eastmoney_three_market", "today": 1, "trend": []},
        },
    )
    fake_ths_quote = types.SimpleNamespace(get_client=lambda: types.SimpleNamespace(_ok=False))
    fake_akshare = types.SimpleNamespace(
        stock_zh_index_spot_em=lambda: None,
        stock_board_industry_name_em=lambda: (_ for _ in ()).throw(AssertionError("slow akshare sector source should not be used")),
    )

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "diff": [
                        {"f14": "Strong A", "f3": 4.2},
                        {"f14": "Strong B", "f3": 3.1},
                        {"f14": "Strong C", "f3": 2.8},
                        {"f14": "Weak A", "f3": -2.4},
                        {"f14": "Weak B", "f3": -1.7},
                        {"f14": "Weak C", "f3": -1.2},
                    ]
                }
            }

    fake_requests = types.SimpleNamespace(get=lambda *args, **kwargs: FakeResponse())
    stub_project_module(monkeypatch, "market_stats", fake_market_stats)
    stub_project_module(monkeypatch, "ths_quote", fake_ths_quote)
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    review = market_web._build_market_review(allow_archive=False)

    assert review["sectors"]["top_gainers"] == ["Strong A", "Strong B", "Strong C"]
    assert review["sectors"]["top_losers"] == ["Weak A", "Weak B", "Weak C"]


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
            },
            "turnover": {"source": "official_exchange", "today": 28000.0, "amount": 28000.12, "degraded": False},
        },
    }
    fake_review_engine = types.SimpleNamespace(load=lambda date_str: archive if date_str == "2026-07-02" else None)
    stub_project_module(monkeypatch, "review_engine", fake_review_engine)

    review = market_web._build_market_review("2026-07-02")

    assert review["date"] == "2026-07-02"
    assert review["archived"] is True
    assert review["breadth"]["up"] == 3000
    assert review["turnover"]["source"] == "official_exchange"


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
    stub_project_module(monkeypatch, "review_engine", review_engine)

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
    stub_project_module(monkeypatch, "review_engine", review_engine)

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


def test_daily_review_api_repairs_stale_three_market_turnover_archive(monkeypatch):
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
                "breadth": {"source": "eastmoney_three_market", "up": 1876, "down": 3541, "flat": 102, "total": 5519, "degraded": False},
            },
            "turnover": {"source": "eastmoney_three_market", "today": 31124, "amount": 31124.43, "degraded": False},
            "zt": {"zt_count": 64, "dt_count": 46},
        },
        "ai_summary": "old ai summary",
    }
    fake_market_stats = types.SimpleNamespace(
        get_verified_market_snapshot=lambda date_text: None,
        normalize_breadth_quality=lambda breadth: {**breadth, "expected_count": 5519, "coverage": 1.0, "degraded": False},
        fetch_eastmoney_three_market_snapshot=lambda date_text: (_ for _ in ()).throw(RuntimeError("direct source unavailable in this test")),
        get_market_breadth=lambda date_text=None: {"source": "eastmoney_three_market", "up": 1876, "down": 3541, "flat": 102, "total": 5519, "degraded": False},
        get_market_turnover=lambda date_text=None: {"source": "official_exchange", "today": 30944.0, "amount": 30944.46, "degraded": False},
    )
    monkeypatch.setattr(review_engine, "load", lambda date_str: stale_archive)
    monkeypatch.setattr(review_engine, "collect", lambda date_str: (_ for _ in ()).throw(AssertionError("collect should not be needed for archived market metric repair")))
    stub_project_module(monkeypatch, "review_engine", review_engine)
    stub_project_module(monkeypatch, "market_stats", fake_market_stats)

    app = Flask(__name__)
    app.register_blueprint(bp)

    response = app.test_client().get("/api/review/daily?date=2026-07-06")
    payload = response.get_json()

    assert payload["ok"] is True
    assert payload["phase"] == "evening"
    assert payload["data"]["market"]["breadth"]["source"] == "eastmoney_three_market"
    assert payload["data"]["turnover"]["source"] == "official_exchange"
    assert payload["data"]["turnover"]["today"] == 30944.0
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

    stub_project_module(monkeypatch, "market_stats", FakeMarketStats)

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
