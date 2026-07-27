from tests._module_stubs import stub_project_module
import sys
import threading
import time
import types
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from market import market_stats
from market import tushare_replay


def test_parse_legu_activity_uses_complete_breadth_counts():
    df = pd.DataFrame(
        {
            "item": [
                "\u4e0a\u6da8",
                "\u6da8\u505c",
                "\u771f\u5b9e\u6da8\u505c",
                "\u4e0b\u8dcc",
                "\u8dcc\u505c",
                "\u771f\u5b9e\u8dcc\u505c",
                "\u5e73\u76d8",
                "\u505c\u724c",
                "\u6d3b\u8dc3\u5ea6",
                "\u7edf\u8ba1\u65e5\u671f",
            ],
            "value": [3527.0, 159.0, 139.0, 1584.0, 28.0, 25.0, 82.0, 11.0, "67.77%", "2026-07-03 15:00:00"],
        }
    )

    breadth = market_stats.parse_legu_activity(df)

    assert breadth["up"] == 3527
    assert breadth["down"] == 1584
    assert breadth["flat"] == 82
    assert breadth["suspended"] == 11
    assert breadth["total"] == 5193
    assert breadth["listed_total"] == 5204
    assert breadth["limit_up"] == 139
    assert breadth["limit_dn"] == 25
    assert breadth["source"] == "legu"
    assert breadth["degraded"] is False


def test_fetch_tushare_replay_market_snapshot_uses_project_module(monkeypatch, tmp_path):
    snapshot = {
        "source": "tushare_replay_daily",
        "stat_date": "2026-07-08",
        "turnover": {"amount": 25000, "source": "tushare_replay_daily_info", "degraded": False},
        "breadth": {"up": 3000, "down": 2000, "flat": 100, "total": 5100, "source": "tushare_replay_daily", "degraded": False},
    }

    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(tushare_replay, "fetch_daily_market_snapshot", lambda date_text=None: snapshot)

    result = market_stats.fetch_tushare_replay_market_snapshot("20260708")

    assert result["source"] == "tushare_replay_daily"
    assert (tmp_path / "20260708.json").exists()


def test_quote_breadth_marks_low_coverage_as_degraded():
    quotes = [{"change_pct": 1.0}] * 3190 + [{"change_pct": -1.0}] * 1441 + [{"change_pct": 0.0}] * 82

    breadth = market_stats.breadth_from_quotes(quotes, expected_count=5193)

    assert breadth["total"] == 4713
    assert breadth["expected_count"] == 5193
    assert breadth["coverage"] == 0.908
    assert breadth["degraded"] is True
    assert "coverage" in breadth["reason"]


def test_quote_breadth_uses_default_expected_count_when_unspecified():
    quotes = [{"change_pct": 1.0}] * 4634

    breadth = market_stats.breadth_from_quotes(quotes)

    assert breadth["total"] == 4634
    assert breadth["expected_count"] == market_stats.DEFAULT_EXPECTED_STOCK_COUNT
    assert breadth["coverage"] == round(4634 / market_stats.DEFAULT_EXPECTED_STOCK_COUNT, 3)
    assert breadth["degraded"] is True


def test_normalize_breadth_quality_repairs_old_tdx_archive_metadata():
    old_archive_breadth = {
        "source": "tdx_quotes",
        "up": 1594,
        "down": 2954,
        "flat": 86,
        "total": 4634,
        "expected_count": 4634,
        "coverage": 1.0,
        "degraded": False,
    }

    breadth = market_stats.normalize_breadth_quality(old_archive_breadth)

    assert breadth["expected_count"] == market_stats.DEFAULT_EXPECTED_STOCK_COUNT
    assert breadth["coverage"] == round(4634 / market_stats.DEFAULT_EXPECTED_STOCK_COUNT, 3)
    assert breadth["degraded"] is True
    assert "coverage" in breadth["reason"]


def test_breadth_falls_back_to_spot_when_legu_and_strategy_snapshot_unavailable(monkeypatch):
    spot = pd.DataFrame(
        {
            "\u4ee3\u7801": ["000001", "000002", "000003", "000004", "000005"],
            "\u6da8\u8dcc\u5e45": [1.2, -0.5, 0.0, 10.1, -10.0],
        }
    )

    def legu_down():
        raise RuntimeError("legu unavailable")

    fake_ak = types.SimpleNamespace(
        stock_market_activity_legu=legu_down,
        stock_zh_a_spot_em=lambda: spot,
    )
    monkeypatch.setattr(market_stats, "fetch_eastmoney_three_market_snapshot", lambda date_text=None: (_ for _ in ()).throw(RuntimeError("three-market unavailable")))
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    stub_project_module(monkeypatch, "strategy_manager", types.SimpleNamespace())

    breadth = market_stats.get_market_breadth(expected_count=5)

    assert breadth["source"] == "eastmoney_spot"
    assert breadth["up"] == 2
    assert breadth["down"] == 2
    assert breadth["flat"] == 1
    assert breadth["limit_up"] == 1
    assert breadth["limit_dn"] == 1
    assert breadth["total"] == 5
    assert breadth["degraded"] is False


def test_verified_eastmoney_snapshot_matches_20260703_three_market_overview():
    snapshot = market_stats.get_verified_market_snapshot("20260703")

    assert snapshot["source"] == "eastmoney_three_market_verified"
    assert snapshot["stat_date"] == "2026-07-03"
    assert snapshot["turnover"]["today"] == 32055
    assert snapshot["turnover"]["amount"] == 32055
    assert snapshot["turnover"]["previous_same_time_delta"] == -2687
    assert snapshot["turnover"]["previous_same_time_pct"] == -7.73
    assert snapshot["turnover"]["avg_60d"] == 29641
    assert snapshot["turnover"]["avg_60d_pct"] == 8.14
    assert snapshot["breadth"]["up"] == 3804
    assert snapshot["breadth"]["flat"] == 84
    assert snapshot["breadth"]["down"] == 1628
    assert snapshot["breadth"]["total"] == 5516
    assert snapshot["breadth"]["distribution"] == {
        "limit_up": 160,
        "up_5_to_limit": 443,
        "up_1_to_5": 2380,
        "up_0_to_1": 821,
        "flat": 84,
        "down_0_to_1": 428,
        "down_1_to_5": 886,
        "down_5_to_limit": 290,
        "limit_down": 24,
    }


def test_market_turnover_prefers_verified_three_market_snapshot_for_20260703():
    turnover = market_stats.get_market_turnover("20260703")

    assert turnover["today"] == 32055
    assert turnover["amount"] == 32055
    assert turnover["source"] == "eastmoney_three_market_verified"
    assert turnover["stat_date"] == "2026-07-03"
    assert turnover["degraded"] is False


def test_market_breadth_prefers_verified_three_market_snapshot_for_20260703():
    breadth = market_stats.get_market_breadth(date_text="20260703")

    assert breadth["up"] == 3804
    assert breadth["flat"] == 84
    assert breadth["down"] == 1628
    assert breadth["total"] == 5516
    assert breadth["source"] == "eastmoney_three_market_verified"
    assert breadth["degraded"] is False


def test_three_market_snapshot_from_eastmoney_rows_aggregates_breadth_and_turnover():
    rows = [
        {"f3": 10.01, "f6": 1_200_000_000, "f12": "688001", "f124": 1783324800},
        {"f3": 6.2, "f6": 600_000_000, "f12": "301001", "f124": 1783324800},
        {"f3": 1.2, "f6": 400_000_000, "f12": "600001", "f124": 1783324800},
        {"f3": 0.4, "f6": 300_000_000, "f12": "000001", "f124": 1783324800},
        {"f3": 0.0, "f6": 100_000_000, "f12": "430001", "f124": 1783324800},
        {"f3": -0.7, "f6": 200_000_000, "f12": "830001", "f124": 1783324800},
        {"f3": -2.5, "f6": 500_000_000, "f12": "002001", "f124": 1783324800},
        {"f3": -6.5, "f6": 700_000_000, "f12": "300001", "f124": 1783324800},
        {"f3": -10.0, "f6": 900_000_000, "f12": "600002", "f124": 1783324800},
        {"f3": "-", "f6": "-", "f12": "600003", "f124": 1783324800},
    ]

    snapshot = market_stats.three_market_snapshot_from_rows(rows, date_text="20260706", raw_total=10)

    assert snapshot["source"] == "eastmoney_three_market"
    assert snapshot["stat_date"] == "2026-07-06"
    assert snapshot["turnover"]["today"] == 49
    assert snapshot["turnover"]["amount"] == 49.0
    assert snapshot["turnover"]["degraded"] is False
    assert snapshot["breadth"]["up"] == 4
    assert snapshot["breadth"]["flat"] == 1
    assert snapshot["breadth"]["down"] == 4
    assert snapshot["breadth"]["total"] == 9
    assert snapshot["breadth"]["listed_total"] == 10
    assert snapshot["breadth"]["coverage"] == 0.9
    assert snapshot["breadth"]["limit_up"] == 1
    assert snapshot["breadth"]["limit_dn"] == 1
    assert snapshot["breadth"]["distribution"] == {
        "limit_up": 1,
        "up_5_to_limit": 1,
        "up_1_to_5": 1,
        "up_0_to_1": 1,
        "flat": 1,
        "down_0_to_1": 1,
        "down_1_to_5": 1,
        "down_5_to_limit": 1,
        "limit_down": 1,
    }


def test_fetch_pages_concurrently_runs_more_than_one_page_at_once():
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fetch_page(page):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return [{"page": page}]

    rows = market_stats._fetch_pages_concurrently(fetch_page, [2, 3, 4, 5], max_workers=4)

    assert max_active > 1
    assert [row["page"] for row in rows] == [2, 3, 4, 5]


def test_market_breadth_uses_dynamic_three_market_snapshot_and_turnover_uses_two_market_index(monkeypatch, tmp_path):
    snapshot = {
        "source": "eastmoney_three_market",
        "stat_date": "2026-07-06",
        "turnover": {"today": 31124, "amount": 31124.43, "source": "eastmoney_three_market", "degraded": False},
        "breadth": {"source": "eastmoney_three_market", "up": 1876, "down": 3541, "flat": 102, "total": 5519, "degraded": False},
    }
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(
        market_stats,
        "fetch_tushare_replay_market_snapshot",
        lambda date_text=None: (_ for _ in ()).throw(RuntimeError("tushare unavailable")),
    )
    monkeypatch.setattr(market_stats, "fetch_eastmoney_three_market_snapshot", lambda date_text=None: snapshot, raising=False)
    monkeypatch.setattr(market_stats, "_turnover_from_eastmoney_index", lambda date_text=None: {"today": 30949, "amount": 30948.86, "source": "eastmoney_index", "stat_date": "2026-07-06", "degraded": False})
    monkeypatch.setattr(market_stats, "_turnover_trend_from_index_daily", lambda: [])

    breadth = market_stats.get_market_breadth(date_text="20260706")
    turnover = market_stats.get_market_turnover("20260706")

    assert breadth["source"] == "eastmoney_three_market"
    assert breadth["total"] == 5519
    assert turnover["source"] == "eastmoney_index"
    assert turnover["today"] == 30949


def test_market_snapshot_metrics_reuse_one_three_market_snapshot(monkeypatch, tmp_path):
    calls = []
    snapshot = {
        "source": "eastmoney_three_market",
        "stat_date": "2026-07-06",
        "turnover": {"today": 31124, "amount": 31124.43, "source": "eastmoney_three_market", "degraded": False},
        "breadth": {"source": "eastmoney_three_market", "up": 1876, "down": 3541, "flat": 102, "total": 5519, "degraded": False},
    }

    def fake_snapshot(date_text=None):
        calls.append(date_text)
        return snapshot

    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(market_stats, "fetch_eastmoney_three_market_snapshot", fake_snapshot, raising=False)
    monkeypatch.setattr(market_stats, "_turnover_from_eastmoney_index", lambda date_text=None: {"today": 30949, "amount": 30948.86, "source": "eastmoney_index", "stat_date": "2026-07-06", "degraded": False})
    monkeypatch.setattr(market_stats, "_turnover_trend_from_index_daily", lambda: [{"date": "2026-07-06", "amount": 0}])

    metrics = market_stats.get_market_snapshot_metrics("2026-07-06")

    assert calls == ["2026-07-06"]
    assert metrics["breadth"]["total"] == 5519
    assert metrics["turnover"]["today"] == 30949
    assert metrics["turnover"]["trend"][-1]["amount"] == 30949


def test_market_snapshot_metrics_respects_tushare_only_source_order(monkeypatch, tmp_path):
    fresh_snapshot = {
        "source": "tushare_replay_daily",
        "stat_date": "2026-07-06",
        "turnover": {"today": 30944, "amount": 30944.46, "source": "tushare_replay_daily_info", "degraded": False},
        "breadth": {"source": "tushare_replay_daily", "up": 1900, "down": 3500, "flat": 119, "total": 5519, "coverage": 1.0, "degraded": False},
    }
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(market_stats.dg, "get_source_order", lambda: ["tushare"])
    monkeypatch.setattr(market_stats, "fetch_tushare_replay_market_snapshot", lambda date_text=None: fresh_snapshot)
    monkeypatch.setattr(
        market_stats,
        "fetch_eastmoney_three_market_snapshot",
        lambda date_text=None: (_ for _ in ()).throw(AssertionError("current snapshot must not be used when fallback is disabled")),
        raising=False,
    )
    monkeypatch.setattr(
        market_stats,
        "_turnover_from_eastmoney_index",
        lambda date_text=None: (_ for _ in ()).throw(AssertionError("current turnover must not be used when fallback is disabled")),
    )
    monkeypatch.setattr(
        market_stats,
        "_safe_turnover_trend",
        lambda: (_ for _ in ()).throw(AssertionError("current trend must not be used when fallback is disabled")),
    )

    metrics = market_stats.get_market_snapshot_metrics("2026-07-06")

    assert metrics["source"] == "tushare_replay_daily"
    assert metrics["breadth"]["source"] == "tushare_replay_daily"
    assert metrics["turnover"]["source"] == "tushare_replay_daily_info"
    assert metrics["turnover"]["trend"] == []


def test_market_snapshot_metrics_fetches_snapshot_and_trend_concurrently(monkeypatch, tmp_path):
    snapshot_started = threading.Event()
    trend_started = threading.Event()
    snapshot = {
        "source": "eastmoney_three_market",
        "stat_date": "2026-07-06",
        "turnover": {"today": 31124, "amount": 31124.43, "source": "eastmoney_three_market", "degraded": False},
        "breadth": {"source": "eastmoney_three_market", "up": 1876, "down": 3541, "flat": 102, "total": 5519, "degraded": False},
    }

    def fake_snapshot(date_text=None):
        snapshot_started.set()
        assert trend_started.wait(0.5), "turnover trend did not start while snapshot was in flight"
        return snapshot

    def fake_trend():
        trend_started.set()
        assert snapshot_started.wait(0.5), "snapshot did not start while turnover trend was in flight"
        return [{"date": "2026-07-06", "amount": 0}]

    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(market_stats, "fetch_eastmoney_three_market_snapshot", fake_snapshot, raising=False)
    monkeypatch.setattr(market_stats, "_turnover_from_eastmoney_index", lambda date_text=None: {"today": 30949, "amount": 30948.86, "source": "eastmoney_index", "stat_date": "2026-07-06", "degraded": False})
    monkeypatch.setattr(market_stats, "_turnover_trend_from_index_daily", fake_trend)

    metrics = market_stats.get_market_snapshot_metrics("2026-07-06")

    assert metrics["breadth"]["total"] == 5519
    assert metrics["turnover"]["trend"][-1]["amount"] == 30949


def test_market_snapshot_metrics_ignores_low_trust_cache_and_fetches_tushare(monkeypatch, tmp_path):
    snapshot = {
        "source": "eastmoney_three_market",
        "stat_date": "2026-07-06",
        "turnover": {"today": 31124, "amount": 31124.43, "source": "eastmoney_three_market", "degraded": False},
        "breadth": {"source": "eastmoney_three_market", "up": 1876, "down": 3541, "flat": 102, "total": 5519, "degraded": False},
    }
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(market_stats.dg, "get_source_order", lambda: ["tushare", "current"])
    market_stats.save_market_snapshot(snapshot)
    fresh_snapshot = {
        "source": "tushare_replay_daily",
        "stat_date": "2026-07-06",
        "turnover": {"today": 30944, "amount": 30944.46, "source": "tushare_replay_daily_info", "degraded": False},
        "breadth": {"source": "tushare_replay_daily", "up": 1900, "down": 3500, "flat": 119, "total": 5519, "coverage": 1.0, "degraded": False},
    }
    monkeypatch.setattr(market_stats, "fetch_tushare_replay_market_snapshot", lambda date_text=None: fresh_snapshot)
    monkeypatch.setattr(
        market_stats,
        "_turnover_from_eastmoney_index",
        lambda date_text=None: (_ for _ in ()).throw(RuntimeError("index history unavailable")),
    )
    monkeypatch.setattr(market_stats, "_turnover_from_official_exchange", lambda date_text=None: {"today": 30944, "amount": 30944.46, "source": "official_exchange", "stat_date": "2026-07-06", "degraded": False})
    monkeypatch.setattr(market_stats, "_turnover_trend_from_index_daily", lambda: [])

    metrics = market_stats.get_market_snapshot_metrics("2026-07-06")

    assert metrics["breadth"]["source"] == "tushare_replay_daily"
    assert metrics["breadth"]["total"] == 5519
    assert metrics["turnover"]["source"] == "tushare_replay_daily_info"
    assert metrics["turnover"]["amount"] == 30944.46


def test_market_breadth_and_turnover_ignore_low_trust_cached_three_market_snapshot(monkeypatch, tmp_path):
    snapshot = {
        "source": "eastmoney_three_market",
        "stat_date": "2026-07-06",
        "turnover": {"today": 31124, "amount": 31124.43, "source": "eastmoney_three_market", "degraded": False},
        "breadth": {"source": "eastmoney_three_market", "up": 1876, "down": 3541, "flat": 102, "total": 5519, "degraded": False},
    }
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(market_stats.dg, "get_source_order", lambda: ["tushare", "current"])
    market_stats.save_market_snapshot(snapshot)
    fresh_snapshot = {
        "source": "tushare_replay_daily",
        "stat_date": "2026-07-06",
        "turnover": {"today": 30949, "amount": 30948.86, "source": "tushare_replay_daily_info", "degraded": False},
        "breadth": {"source": "tushare_replay_daily", "up": 1876, "down": 3541, "flat": 102, "total": 5519, "coverage": 1.0, "degraded": False},
    }
    monkeypatch.setattr(market_stats, "fetch_tushare_replay_market_snapshot", lambda date_text=None: fresh_snapshot)
    monkeypatch.setattr(
        market_stats,
        "fetch_eastmoney_three_market_snapshot",
        lambda date_text=None: (_ for _ in ()).throw(RuntimeError("latest-only source unavailable for historical date")),
        raising=False,
    )
    monkeypatch.setattr(market_stats, "_turnover_from_eastmoney_index", lambda date_text=None: {"today": 30949, "amount": 30948.86, "source": "eastmoney_index_daily", "stat_date": "2026-07-06", "degraded": False})
    monkeypatch.setattr(market_stats, "_turnover_trend_from_index_daily", lambda: [])

    breadth = market_stats.get_market_breadth(date_text="2026-07-06")
    turnover = market_stats.get_market_turnover("2026-07-06")

    assert breadth["source"] == "tushare_replay_daily"
    assert breadth["total"] == 5519
    assert turnover["source"] == "tushare_replay_daily_info"
    assert turnover["today"] == 30949


def test_explicit_today_ignores_cached_zero_turnover_or_degraded_breadth(monkeypatch, tmp_path):
    from datetime import datetime

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 7, 16, 10, 0)

    snapshot = {
        "source": "eastmoney_three_market",
        "stat_date": "2026-07-07",
        "turnover": {"today": 0, "amount": 0, "source": "eastmoney_three_market", "degraded": False},
        "breadth": {"source": "eastmoney_three_market", "up": 969, "down": 1030, "flat": 532, "total": 2531, "degraded": True},
    }
    monkeypatch.setattr(market_stats, "datetime", FakeDateTime)
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    market_stats.save_market_snapshot(snapshot)

    assert market_stats.load_cached_market_snapshot("2026-07-07") is None


def test_explicit_today_breadth_date_mismatch_does_not_use_previous_day_live_fallback(monkeypatch, tmp_path):
    from datetime import datetime

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 7, 2, 15, 0)

    monkeypatch.setattr(market_stats, "datetime", FakeDateTime)
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(
        market_stats,
        "fetch_eastmoney_three_market_snapshot",
        lambda date_text=None: (_ for _ in ()).throw(ValueError("eastmoney latest date 2026-07-06 does not match requested 2026-07-07")),
    )
    monkeypatch.setattr(
        market_stats,
        "_turnover_from_eastmoney_index",
        lambda date_text=None: (_ for _ in ()).throw(ValueError("eastmoney index date 2026-07-06 does not match requested 2026-07-07")),
    )
    fake_ak = types.SimpleNamespace(
        stock_market_activity_legu=lambda: (_ for _ in ()).throw(AssertionError("previous-day legu must not be used for explicit today")),
        stock_zh_a_spot_em=lambda: (_ for _ in ()).throw(AssertionError("spot fallback must not be used after date mismatch")),
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    stub_project_module(monkeypatch, "strategy_manager", types.SimpleNamespace(_get_full_market_snapshot=lambda: (_ for _ in ()).throw(AssertionError("quote fallback must not be used after date mismatch"))))

    breadth = market_stats.get_market_breadth(date_text="2026-07-07")

    assert breadth["source"] == "none"
    assert breadth["degraded"] is True
    assert "date mismatch" in breadth["reason"]


def test_explicit_today_turnover_date_mismatch_does_not_use_previous_day_live_fallback(monkeypatch, tmp_path):
    from datetime import datetime

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 7, 2, 15, 0)

    monkeypatch.setattr(market_stats, "datetime", FakeDateTime)
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(
        market_stats,
        "fetch_eastmoney_three_market_snapshot",
        lambda date_text=None: (_ for _ in ()).throw(ValueError("eastmoney latest date 2026-07-06 does not match requested 2026-07-07")),
    )
    monkeypatch.setattr(
        market_stats,
        "_turnover_from_eastmoney_index",
        lambda date_text=None: (_ for _ in ()).throw(ValueError("eastmoney index date 2026-07-06 does not match requested 2026-07-07")),
    )
    monkeypatch.setattr(
        market_stats,
        "_turnover_from_tdx_index",
        lambda: (_ for _ in ()).throw(AssertionError("live TDX must not be used after date mismatch")),
    )
    monkeypatch.setattr(market_stats, "_turnover_trend_from_index_daily", lambda: [])

    turnover = market_stats.get_market_turnover("2026-07-07")

    assert turnover["source"] == "none"
    assert turnover["degraded"] is True
    assert "date mismatch" in turnover["reason"]


def test_explicit_today_breadth_empty_official_source_does_not_use_previous_day_live_fallback(monkeypatch, tmp_path):
    from datetime import datetime

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 7, 8, 30, 0)

    monkeypatch.setattr(market_stats, "datetime", FakeDateTime)
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(
        market_stats,
        "fetch_eastmoney_three_market_snapshot",
        lambda date_text=None: (_ for _ in ()).throw(ValueError("empty eastmoney three-market rows")),
    )
    fake_ak = types.SimpleNamespace(
        stock_market_activity_legu=lambda: (_ for _ in ()).throw(AssertionError("previous-day legu must not be used for explicit today")),
        stock_zh_a_spot_em=lambda: (_ for _ in ()).throw(AssertionError("spot fallback must not be used after explicit today source is empty")),
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    stub_project_module(monkeypatch, "strategy_manager", types.SimpleNamespace(_get_full_market_snapshot=lambda: (_ for _ in ()).throw(AssertionError("quote fallback must not be used after explicit today source is empty"))))

    breadth = market_stats.get_market_breadth(date_text="2026-07-07")

    assert breadth["source"] == "none"
    assert breadth["degraded"] is True
    assert "unavailable" in breadth["reason"]


def test_explicit_today_turnover_empty_official_source_does_not_use_live_fallback(monkeypatch, tmp_path):
    from datetime import datetime

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 7, 8, 30, 0)

    monkeypatch.setattr(market_stats, "datetime", FakeDateTime)
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(
        market_stats,
        "fetch_eastmoney_three_market_snapshot",
        lambda date_text=None: (_ for _ in ()).throw(ValueError("eastmoney turnover unavailable for requested 2026-07-07")),
    )
    monkeypatch.setattr(
        market_stats,
        "_turnover_from_eastmoney_index",
        lambda date_text=None: (_ for _ in ()).throw(ValueError("eastmoney index turnover unavailable for requested 2026-07-07")),
    )
    monkeypatch.setattr(
        market_stats,
        "_turnover_from_tdx_index",
        lambda: (_ for _ in ()).throw(AssertionError("live TDX must not be used after explicit today turnover is unavailable")),
    )
    monkeypatch.setattr(
        market_stats,
        "_turnover_from_official_exchange",
        lambda date_text=None: (_ for _ in ()).throw(AssertionError("official fallback must not be used after explicit today turnover is unavailable")),
    )
    monkeypatch.setattr(market_stats, "_turnover_trend_from_index_daily", lambda: [])

    turnover = market_stats.get_market_turnover("2026-07-07")

    assert turnover["source"] == "none"
    assert turnover["degraded"] is True
    assert "unavailable" in turnover["reason"]

def test_historical_breadth_does_not_use_live_only_fallbacks_when_snapshot_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(
        market_stats,
        "fetch_eastmoney_three_market_snapshot",
        lambda date_text=None: (_ for _ in ()).throw(ValueError("latest date does not match requested")),
    )
    monkeypatch.setattr(
        market_stats,
        "_turnover_from_eastmoney_index",
        lambda date_text=None: (_ for _ in ()).throw(RuntimeError("index historical source unavailable")),
    )

    fake_ak = types.SimpleNamespace(
        stock_market_activity_legu=lambda: (_ for _ in ()).throw(AssertionError("live legu must not be used for historical breadth")),
        stock_zh_a_spot_em=lambda: (_ for _ in ()).throw(AssertionError("live spot must not be used for historical breadth")),
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    stub_project_module(monkeypatch, "strategy_manager", types.SimpleNamespace(_get_full_market_snapshot=lambda: (_ for _ in ()).throw(AssertionError("live quotes must not be used for historical breadth"))))

    breadth = market_stats.get_market_breadth(date_text="2026-07-01")

    assert breadth["source"] == "none"
    assert breadth["degraded"] is True
    assert "historical" in breadth["reason"]


def test_historical_turnover_does_not_use_live_tdx_when_official_history_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(
        market_stats,
        "fetch_eastmoney_three_market_snapshot",
        lambda date_text=None: (_ for _ in ()).throw(ValueError("latest date does not match requested")),
    )
    monkeypatch.setattr(
        market_stats,
        "_turnover_from_official_exchange",
        lambda date_text=None: (_ for _ in ()).throw(RuntimeError("official historical source unavailable")),
    )
    monkeypatch.setattr(
        market_stats,
        "_turnover_from_tdx_index",
        lambda: (_ for _ in ()).throw(AssertionError("live TDX must not be used for historical turnover")),
    )
    monkeypatch.setattr(market_stats, "_turnover_trend_from_index_daily", lambda: [])

    turnover = market_stats.get_market_turnover("2026-07-01")

    assert turnover["source"] == "none"
    assert turnover["degraded"] is True
    assert "official" in turnover["reason"]


def test_official_turnover_combines_sse_yi_and_szse_yuan_units():
    sse = pd.DataFrame(
        [
            ["\u6302\u724c\u6570", 2350.0, 1699.0, 41.0, 610.0],
            ["\u5e02\u4ef7\u603b\u503c", 677302.79, 517964.55, 997.68, 158340.56],
            ["\u6d41\u901a\u5e02\u503c", 628431.36, 497639.85, 699.77, 130091.74],
            ["\u6210\u4ea4\u91d1\u989d", 14667.74, 9529.67, 1.15, 5136.92],
        ],
        columns=["\u5355\u65e5\u60c5\u51b5", "\u80a1\u7968", "\u4e3b\u677fA", "\u4e3b\u677fB", "\u79d1\u521b\u677f"],
    )
    szse = pd.DataFrame(
        [
            ["\u80a1\u7968", 2933, 1.718723e12],
            ["\u4e3b\u677fA\u80a1", 1496, 9.076941e11],
            ["\u4e3b\u677fB\u80a1", 38, 1.000183e8],
            ["\u521b\u4e1a\u677fA\u80a1", 1399, 8.109285e11],
        ],
        columns=["\u8bc1\u5238\u7c7b\u522b", "\u6570\u91cf", "\u6210\u4ea4\u91d1\u989d"],
    )

    turnover = market_stats.official_turnover_from_frames("20260703", sse, szse)

    assert turnover["today"] == 31853.0
    assert turnover["amount"] == 31852.82
    assert turnover["source"] == "official_exchange"
    assert turnover["stat_date"] == "2026-07-03"
    assert turnover["degraded"] is False


def test_index_turnover_uses_tdx_whole_market_indices_not_stock_sum():
    quotes = [
        {"code": "000001", "amount": 1465563086848.0},
        {"code": "399106", "amount": 1716000000000.0},
        {"code": "399001", "amount": 1716890370048.0},
    ]

    turnover = market_stats.turnover_from_index_quotes(quotes)

    assert turnover["today"] == 31825.0
    assert turnover["amount"] == 31824.53
    assert turnover["source"] == "tdx_index"
    assert turnover["degraded"] is False


def test_eastmoney_index_rows_match_two_market_turnover_quote():
    rows = [
        {"f12": "000001", "f6": 1196371087894.4, "f124": 1783411921},
        {"f12": "399001", "f6": 1384786224621.8315, "f124": 1783411896},
    ]

    turnover = market_stats.turnover_from_index_rows(rows, date_text="20260707")

    assert turnover["today"] == 25812
    assert turnover["amount"] == 25811.57
    assert turnover["source"] == "eastmoney_index"
    assert turnover["stat_date"] == "2026-07-07"
    assert turnover["degraded"] is False


def _sample_tushare_snapshot():
    from market.tushare_replay import snapshot_from_rt_k_rows

    return snapshot_from_rt_k_rows(
        [
            {"ts_code": "600001.SH", "trade_date": "20260708", "pct_chg": 10.0, "amount": 1_200_000_000_000},
            {"ts_code": "000001.SZ", "trade_date": "20260708", "pct_chg": 2.5, "amount": 2_000_000_000_000},
            {"ts_code": "430001.BJ", "trade_date": "20260708", "pct_chg": -10.0, "amount": 500_000_000_000},
            {"ts_code": "300001.SZ", "trade_date": "20260708", "pct_chg": 0.0, "amount": 0},
        ],
        date_text="20260708",
    )


def test_tushare_replay_rt_k_snapshot_counts_breadth_and_two_market_turnover():
    snapshot = _sample_tushare_snapshot()

    assert snapshot["source"] == "tushare_replay_rt_k"
    assert snapshot["stat_date"] == "2026-07-08"
    assert snapshot["breadth"]["up"] == 2
    assert snapshot["breadth"]["flat"] == 1
    assert snapshot["breadth"]["down"] == 1
    assert snapshot["breadth"]["limit_up"] == 1
    assert snapshot["breadth"]["limit_dn"] == 1
    assert snapshot["breadth"]["distribution"]["limit_up"] == 1
    assert snapshot["breadth"]["distribution"]["limit_down"] == 1
    assert snapshot["turnover"]["amount"] == 32000.0
    assert snapshot["turnover"]["source"] == "tushare_replay_rt_k"


def test_tushare_replay_daily_snapshot_uses_daily_source_and_daily_info_turnover():
    from market.tushare_replay import _daily_info_two_market_amount, snapshot_from_daily_rows

    rows = [
        {"ts_code": "600001.SH", "trade_date": "20260708", "pct_chg": 10.0, "amount": 1_200_000},
        {"ts_code": "000001.SZ", "trade_date": "20260708", "pct_chg": -2.5, "amount": 2_000_000},
        {"ts_code": "430001.BJ", "trade_date": "20260708", "pct_chg": 1.0, "amount": 500_000},
    ]
    info_rows = [
        {"ts_code": "SH_MARKET", "amount": 11956.51},
        {"ts_code": "SZ_MARKET", "amount": 13725.36},
    ]

    snapshot = snapshot_from_daily_rows(rows, "20260708")
    amount = _daily_info_two_market_amount(info_rows)

    assert snapshot["source"] == "tushare_replay_daily"
    assert snapshot["breadth"]["source"] == "tushare_replay_daily"
    assert snapshot["turnover"]["amount"] == 0.03
    assert amount == 25681.87


def test_low_trust_old_market_snapshot_cache_is_ignored(monkeypatch, tmp_path):
    old_snapshot = {
        "source": "eastmoney_three_market",
        "stat_date": "2026-07-06",
        "turnover": {"amount": 31124.43, "source": "eastmoney_three_market"},
        "breadth": {"coverage": 0.94, "source": "eastmoney_three_market", "degraded": False},
    }
    cache_path = tmp_path / "20260706.json"
    cache_path.write_text(__import__("json").dumps(old_snapshot), encoding="utf-8")
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)

    assert market_stats.load_cached_market_snapshot("20260706") is None


def test_market_breadth_prefers_tushare_replay_before_eastmoney(monkeypatch, tmp_path):
    snapshot = _sample_tushare_snapshot()
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(market_stats.dg, "get_source_order", lambda: ["tushare", "current"])
    monkeypatch.setattr(market_stats, "fetch_tushare_replay_market_snapshot", lambda date_text=None: snapshot)
    monkeypatch.setattr(
        market_stats,
        "fetch_eastmoney_three_market_snapshot",
        lambda date_text=None: (_ for _ in ()).throw(AssertionError("eastmoney should not be used when tushare replay works")),
    )

    breadth = market_stats.get_market_breadth(date_text="20260708")

    assert breadth["source"] == "tushare_replay_rt_k"
    assert breadth["up"] == 2


def test_market_turnover_prefers_tushare_replay_before_eastmoney(monkeypatch, tmp_path):
    snapshot = _sample_tushare_snapshot()
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(market_stats.dg, "get_source_order", lambda: ["tushare", "current"])
    monkeypatch.setattr(market_stats, "fetch_tushare_replay_market_snapshot", lambda date_text=None: snapshot)
    monkeypatch.setattr(market_stats, "_turnover_trend_from_index_daily", lambda: [])
    monkeypatch.setattr(
        market_stats,
        "_turnover_from_eastmoney_index",
        lambda date_text=None: (_ for _ in ()).throw(AssertionError("eastmoney turnover should not be used when tushare replay works")),
    )

    turnover = market_stats.get_market_turnover(date_text="20260708")

    assert turnover["source"] == "tushare_replay_rt_k"
    assert turnover["amount"] == 32000.0


def test_market_snapshot_metrics_keeps_tushare_turnover_from_same_snapshot(monkeypatch, tmp_path):
    snapshot = _sample_tushare_snapshot()
    monkeypatch.setattr(market_stats, "MARKET_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(market_stats.dg, "get_source_order", lambda: ["tushare", "current"])
    monkeypatch.setattr(market_stats, "fetch_tushare_replay_market_snapshot", lambda date_text=None: snapshot)
    monkeypatch.setattr(market_stats, "_turnover_trend_from_index_daily", lambda: [])
    monkeypatch.setattr(
        market_stats,
        "_turnover_from_eastmoney_index",
        lambda date_text=None: (_ for _ in ()).throw(AssertionError("metrics should keep tushare turnover")),
    )

    metrics = market_stats.get_market_snapshot_metrics("20260708")

    assert metrics["source"] == "tushare_replay_rt_k"
    assert metrics["breadth"]["source"] == "tushare_replay_rt_k"
    assert metrics["turnover"]["source"] == "tushare_replay_rt_k"
    assert metrics["turnover"]["amount"] == 32000.0
