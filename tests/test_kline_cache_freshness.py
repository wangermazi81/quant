# -*- coding: utf-8 -*-
from tests._module_stubs import stub_project_module
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _deterministic_completed_trade_date(monkeypatch):
    import analytics.data_manager as dm

    def expected_trade_date(now=None):
        current = now or datetime(2026, 7, 31, 17, 10)
        close_guard = current.replace(
            hour=15,
            minute=30,
            second=0,
            microsecond=0,
        )
        candidate = current.date()
        if current < close_guard:
            candidate -= timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate.isoformat()

    monkeypatch.setattr(
        dm,
        "expected_completed_trade_date",
        expected_trade_date,
    )


def _kline_frame(end_date, periods=30, start_price=10.0):
    dates = pd.bdate_range(end=end_date, periods=periods)
    prices = [start_price + i * 0.1 for i in range(periods)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": prices,
            "high": [p + 0.2 for p in prices],
            "low": [p - 0.2 for p in prices],
            "close": [p + 0.1 for p in prices],
            "volume": [1000 + i for i in range(periods)],
        }
    )


def test_factor_prefetch_refreshes_stale_local_cache(monkeypatch):
    import analytics.factor_manager as fm

    stale = _kline_frame(date.today() - timedelta(days=45), periods=30)
    fresh = _kline_frame(date.today(), periods=30, start_price=20.0)
    calls = []

    fake_dm = types.SimpleNamespace(get_local=lambda code: stale.copy())
    stub_project_module(monkeypatch, "data_manager", fake_dm)
    monkeypatch.setattr(fm, "_init_kline_pool", lambda n_workers: [])

    def fallback_get_kline(code, days):
        calls.append((code, days))
        return fresh.tail(days).reset_index(drop=True)

    cache = fm._prefetch_klines_parallel(["000001"], fallback_get_kline, days=20, n_workers=1)

    assert calls == [("000001", 20)]
    # 断言"已刷新到 fresh 数据"（对比 fresh 自身最新日期而非 date.today()：
    # bdate_range 周末/节假日生成不出当天K线，原断言在非交易日必挂）
    cached_max = pd.to_datetime(cache["000001"]["date"]).max()
    assert cached_max == pd.to_datetime(fresh["date"]).max()
    assert cached_max > pd.to_datetime(stale["date"]).max()


def test_merge_kline_cache_preserves_history_when_writing_short_refresh(monkeypatch, tmp_path):
    import analytics.data_manager as dm

    monkeypatch.setattr(dm, "DATA_DIR", tmp_path)
    code = "000001"
    old = _kline_frame("2026-06-11", periods=140)
    old.to_csv(tmp_path / f"{code}.csv", index=False, encoding="utf-8")

    refresh = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-06-10", "2026-06-11", "2026-07-07"]),
            "open": [30.0, 31.0, 32.0],
            "high": [30.5, 31.5, 32.5],
            "low": [29.5, 30.5, 31.5],
            "close": [30.2, 31.2, 32.2],
            "volume": [3000, 3100, 3200],
        }
    )

    merged = dm.merge_kline_cache(code, refresh)

    saved = pd.read_csv(tmp_path / f"{code}.csv", parse_dates=["date"])
    assert len(saved) == len(old) + 1
    assert saved["date"].min() == old["date"].min()
    assert saved["date"].max() == pd.Timestamp("2026-07-07")
    assert saved.loc[saved["date"] == pd.Timestamp("2026-06-11"), "close"].iloc[0] == 31.2
    assert len(merged) == len(saved)


def test_refresh_factor_daily_cache_updates_latest_coverage(monkeypatch, tmp_path):
    import analytics.data_manager as dm
    import market.ths_quote as ths_quote

    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", data_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    _disable_tushare_factor_source(monkeypatch, dm)

    stale = _kline_frame("2026-07-09", periods=80)
    stale.to_csv(data_dir / "000001.csv", index=False, encoding="utf-8")
    stale.to_csv(data_dir / "000002.csv", index=False, encoding="utf-8")

    fresh = _kline_frame("2026-07-13", periods=80, start_price=20.0)

    class FakeClient:
        def get_index_kline(self, code, market, days):
            return [{"date": "2026-07-13"}]

        def get_kline(self, code, days):
            if code == "000002":
                return []
            return fresh.to_dict("records")

    monkeypatch.setattr(ths_quote, "get_client", lambda: FakeClient())

    result = dm.refresh_factor_daily_cache(
        days=180,
        min_required=2,
        now=datetime(2026, 7, 13, 17, 10),
    )

    assert result["ok"] is False
    assert result["source_trade_date"] == "2026-07-13"
    assert result["latest_count"] == 1
    assert result["refreshed"] == 1
    assert result["failed"] == 1
    saved = pd.read_csv(data_dir / "000001.csv", parse_dates=["date"])
    assert saved["date"].max() == pd.Timestamp("2026-07-13")


def test_refresh_factor_daily_cache_ignores_unfinished_intraday_bar(monkeypatch, tmp_path):
    import analytics.data_manager as dm
    import market.ths_quote as ths_quote

    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", data_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    _disable_tushare_factor_source(monkeypatch, dm)

    stale = _kline_frame("2026-07-09", periods=80)
    stale.to_csv(data_dir / "000001.csv", index=False, encoding="utf-8")
    bars_with_partial_today = _kline_frame("2026-07-14", periods=85, start_price=20.0)

    class FakeClient:
        def get_index_kline(self, code, market, days):
            return [{"date": "2026-07-14"}, {"date": "2026-07-13"}]

        def get_kline(self, code, days):
            return bars_with_partial_today.to_dict("records")

    monkeypatch.setattr(ths_quote, "get_client", lambda: FakeClient())

    result = dm.refresh_factor_daily_cache(
        days=180,
        min_required=1,
        now=datetime(2026, 7, 14, 10, 30),
    )

    assert result["ok"] is True
    assert result["source_trade_date"] == "2026-07-13"
    saved = pd.read_csv(data_dir / "000001.csv", parse_dates=["date"])
    assert saved["date"].max() == pd.Timestamp("2026-07-13")


def _disable_tushare_factor_source(monkeypatch, dm):
    monkeypatch.setattr(
        dm.datasource_config,
        "get_internal_config",
        lambda: {"provider": "tdx", "tushare_enabled": False},
    )


def _enable_tushare_factor_source(monkeypatch, dm):
    monkeypatch.setattr(
        dm.datasource_config,
        "get_internal_config",
        lambda: {
            "provider": "tushare",
            "tushare_enabled": True,
            "tushare_mode": "replay",
            "tushare_token": "api-key",
            "fallback_enabled": True,
        },
    )


def _market_daily_frame(trade_date, codes=("000001", "000002")):
    return pd.DataFrame(
        {
            "code": list(codes),
            "date": pd.to_datetime([trade_date] * len(codes)),
            "open": [10.42 + i for i in range(len(codes))],
            "high": [10.55 + i for i in range(len(codes))],
            "low": [10.38 + i for i in range(len(codes))],
            "close": [10.54 + i for i in range(len(codes))],
            "volume": [924746.24 + i for i in range(len(codes))],
        }
    )


def _market_factor_frame(trade_date, codes=("000001", "000002"), factors=(100.0, 100.0)):
    return pd.DataFrame(
        {
            "code": list(codes),
            "date": pd.to_datetime([trade_date] * len(codes)),
            "adj_factor": list(factors)[: len(codes)],
        }
    )


def test_refresh_factor_daily_cache_prefers_tushare_batch(monkeypatch, tmp_path):
    import analytics.data_manager as dm
    import market.ths_quote as ths_quote

    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", data_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    _enable_tushare_factor_source(monkeypatch, dm)

    stale = _kline_frame("2026-07-10", periods=80)
    stale.to_csv(data_dir / "000001.csv", index=False, encoding="utf-8")
    stale.to_csv(data_dir / "000002.csv", index=False, encoding="utf-8")
    daily_calls = []

    def fake_market_daily(trade_date, **kwargs):
        daily_calls.append(str(trade_date))
        return _market_daily_frame("2026-07-13") if str(trade_date).replace("-", "") == "20260713" else pd.DataFrame()

    monkeypatch.setattr(dm.tushare_provider, "fetch_full_market_daily", fake_market_daily, raising=False)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_adj_factors",
        lambda trade_date, **kwargs: _market_factor_frame(str(trade_date), factors=(100.0, 100.0)),
        raising=False,
    )
    monkeypatch.setattr(dm.tushare_provider, "fetch_kline_qfq", lambda *args, **kwargs: None)
    monkeypatch.setattr(ths_quote, "get_client", lambda: (_ for _ in ()).throw(AssertionError("TDX must not be used")))

    result = dm.refresh_factor_daily_cache(days=180, min_required=2, now=datetime(2026, 7, 14, 10, 30))

    assert daily_calls[0].replace("-", "") == "20260713"
    assert result["ok"] is True
    assert result["source"] == "tushare_replay_daily"
    assert result["primary_source"] == "tushare_replay_daily"
    assert result["source_trade_date"] == "2026-07-13"
    assert result["tushare_count"] == 2
    assert result["tdx_fallback_count"] == 0
    saved = pd.read_csv(data_dir / "000001.csv", parse_dates=["date"])
    latest = saved.loc[saved["date"] == pd.Timestamp("2026-07-13")].iloc[0]
    assert latest["close"] == 10.54
    assert latest["volume"] == 924746.24


def test_refresh_factor_daily_cache_writes_new_amount_for_unchanged_ohlcv(
    monkeypatch,
    tmp_path,
):
    import analytics.data_manager as dm
    import market.ths_quote as ths_quote

    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", data_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    _enable_tushare_factor_source(monkeypatch, dm)

    current = _kline_frame("2026-07-13", periods=80)
    current.to_csv(data_dir / "000001.csv", index=False, encoding="utf-8")
    source = current.tail(1).copy()
    source.insert(0, "code", "000001")
    source["amount"] = 987654.32

    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_daily",
        lambda trade_date, **kwargs: source if str(trade_date) == "20260713" else pd.DataFrame(),
    )

    def fake_factors(trade_date, **kwargs):
        compact = str(trade_date).replace("-", "")
        if compact == "20260713":
            return _market_factor_frame("2026-07-13", codes=("000001",), factors=(100.0,))
        if compact == "20260710":
            return _market_factor_frame("2026-07-10", codes=("000001",), factors=(100.0,))
        return pd.DataFrame()

    monkeypatch.setattr(dm.tushare_provider, "fetch_full_market_adj_factors", fake_factors)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("qfq should not be needed")),
    )
    monkeypatch.setattr(
        ths_quote,
        "get_client",
        lambda: (_ for _ in ()).throw(AssertionError("TDX must not be used")),
    )

    result = dm.refresh_factor_daily_cache(
        days=180,
        min_required=1,
        now=datetime(2026, 7, 14, 10, 30),
    )
    saved = pd.read_csv(data_dir / "000001.csv", parse_dates=["date"])
    latest = saved.loc[saved["date"] == pd.Timestamp("2026-07-13")].iloc[0]

    assert result["ok"] is True
    assert result["refreshed"] == 1
    assert result["already_current"] == 0
    assert latest["amount"] == 987654.32


def test_refresh_factor_daily_cache_uses_tushare_qfq_for_adjustment_change(monkeypatch, tmp_path):
    import analytics.data_manager as dm
    import market.ths_quote as ths_quote

    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", data_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    _enable_tushare_factor_source(monkeypatch, dm)

    stale = _kline_frame("2026-07-10", periods=80)
    stale.to_csv(data_dir / "000001.csv", index=False, encoding="utf-8")
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_daily",
        lambda trade_date, **kwargs: _market_daily_frame("2026-07-13", codes=("000001",)),
        raising=False,
    )

    def fake_factors(trade_date, **kwargs):
        compact = str(trade_date).replace("-", "")
        factor = 200.0 if compact == "20260713" else 100.0
        return _market_factor_frame(str(trade_date), codes=("000001",), factors=(factor,))

    qfq_calls = []
    qfq = _kline_frame("2026-07-13", periods=81, start_price=30.0)
    monkeypatch.setattr(dm.tushare_provider, "fetch_full_market_adj_factors", fake_factors, raising=False)
    monkeypatch.setattr(dm, "_fetch_kline_baostock", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda code, start, end, **kwargs: qfq_calls.append((code, start, end)) or qfq,
    )
    monkeypatch.setattr(ths_quote, "get_client", lambda: (_ for _ in ()).throw(AssertionError("TDX must not be used")))

    result = dm.refresh_factor_daily_cache(days=180, min_required=1, now=datetime(2026, 7, 14, 10, 30))

    assert result["ok"] is True
    assert result["tushare_qfq_count"] == 1
    assert qfq_calls and qfq_calls[0][0] == "000001"
    saved = pd.read_csv(data_dir / "000001.csv", parse_dates=["date"])
    assert saved["date"].max() == pd.Timestamp("2026-07-13")
    assert saved.loc[saved["date"] == pd.Timestamp("2026-07-13"), "close"].iloc[0] == qfq.iloc[-1]["close"]


def test_refresh_factor_daily_cache_repairs_historical_price_discontinuity(
    monkeypatch,
    tmp_path,
):
    import analytics.data_manager as dm
    import market.ths_quote as ths_quote

    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", data_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    _enable_tushare_factor_source(monkeypatch, dm)

    corrupt = _kline_frame("2026-07-10", periods=80)
    price_columns = ["open", "high", "low", "close"]
    corrupt.loc[:74, price_columns] = corrupt.loc[:74, price_columns] / 100.0
    corrupt.to_csv(data_dir / "000001.csv", index=False, encoding="utf-8")
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_daily",
        lambda trade_date, **kwargs: _market_daily_frame(
            "2026-07-13",
            codes=("000001",),
        ),
    )
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_adj_factors",
        lambda trade_date, **kwargs: _market_factor_frame(
            str(trade_date),
            codes=("000001",),
            factors=(100.0,),
        ),
    )
    repaired = _kline_frame("2026-07-13", periods=81)
    repaired["amount"] = [100000.0 + index for index in range(len(repaired))]
    qfq_calls = []
    monkeypatch.setattr(dm, "_fetch_kline_baostock", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda code, start, end, **kwargs: (
            qfq_calls.append((code, start, end)) or repaired
        ),
    )
    monkeypatch.setattr(
        ths_quote,
        "get_client",
        lambda: (_ for _ in ()).throw(AssertionError("TDX must not be used")),
    )

    result = dm.refresh_factor_daily_cache(
        days=180,
        min_required=1,
        now=datetime(2026, 7, 14, 10, 30),
    )

    saved = pd.read_csv(data_dir / "000001.csv", parse_dates=["date"])
    returns = saved["close"].pct_change(fill_method=None).abs()
    assert result["ok"] is True, result
    assert result["continuity_repair_count"] == 1
    assert result["continuity_repair_failed_count"] == 0
    assert qfq_calls == [
        (
            "000001",
            pd.Timestamp(corrupt["date"].min()).strftime("%Y%m%d"),
            "20260713",
        )
    ]
    assert float(returns.max()) <= 0.35
    assert saved["amount"].tolist() == repaired["amount"].tolist()


def test_refresh_factor_daily_cache_uses_baostock_when_replay_qfq_is_rejected(
    monkeypatch,
    tmp_path,
):
    import analytics.data_manager as dm
    import market.ths_quote as ths_quote

    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", data_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    _enable_tushare_factor_source(monkeypatch, dm)

    corrupt = _kline_frame("2026-07-10", periods=80)
    corrupt.loc[:74, ["open", "high", "low", "close"]] /= 100.0
    corrupt.to_csv(data_dir / "000001.csv", index=False, encoding="utf-8")
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_daily",
        lambda trade_date, **kwargs: _market_daily_frame(
            "2026-07-13",
            codes=("000001",),
        ),
    )
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_adj_factors",
        lambda trade_date, **kwargs: _market_factor_frame(
            str(trade_date),
            codes=("000001",),
            factors=(100.0,),
        ),
    )
    tushare_qfq_calls = []
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda *args, **kwargs: tushare_qfq_calls.append(args) or None,
    )
    repaired = _kline_frame("2026-07-13", periods=81)
    repaired["amount"] = [100000.0 + index for index in range(len(repaired))]
    baostock_calls = []
    monkeypatch.setattr(
        dm,
        "_fetch_kline_baostock",
        lambda code, start, end: (
            baostock_calls.append((code, start, end))
            or repaired
        ),
    )
    monkeypatch.setattr(
        ths_quote,
        "get_client",
        lambda: (_ for _ in ()).throw(AssertionError("TDX must not be used")),
    )

    result = dm.refresh_factor_daily_cache(
        days=180,
        min_required=1,
        now=datetime(2026, 7, 14, 10, 30),
    )

    saved = pd.read_csv(data_dir / "000001.csv", parse_dates=["date"])
    assert result["ok"] is True, result
    assert result["continuity_repair_count"] == 1
    assert result["continuity_repair_failed_count"] == 0
    assert baostock_calls and baostock_calls[0][0] == "000001"
    assert tushare_qfq_calls == []
    assert float(saved["close"].pct_change(fill_method=None).abs().max()) <= 0.35
    assert bool((saved["amount"] > 0).all()) is True


def test_refresh_factor_daily_cache_rejects_raw_append_when_qfq_repair_fails(monkeypatch, tmp_path):
    import analytics.data_manager as dm
    import market.ths_quote as ths_quote

    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", data_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    _enable_tushare_factor_source(monkeypatch, dm)

    stale = _kline_frame("2026-07-10", periods=80)
    stale.to_csv(data_dir / "000001.csv", index=False, encoding="utf-8")
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_daily",
        lambda trade_date, **kwargs: _market_daily_frame("2026-07-13", codes=("000001",)),
    )

    def fake_factors(trade_date, **kwargs):
        compact = str(trade_date).replace("-", "")
        factor = 200.0 if compact == "20260713" else 100.0
        return _market_factor_frame(str(trade_date), codes=("000001",), factors=(factor,))

    monkeypatch.setattr(dm.tushare_provider, "fetch_full_market_adj_factors", fake_factors)
    monkeypatch.setattr(dm.tushare_provider, "fetch_kline_qfq", lambda *args, **kwargs: None)
    monkeypatch.setattr(dm, "_fetch_kline_baostock", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ths_quote,
        "get_client",
        lambda: (_ for _ in ()).throw(AssertionError("TDX cannot repair QFQ history")),
    )

    result = dm.refresh_factor_daily_cache(
        days=180,
        min_required=1,
        now=datetime(2026, 7, 13, 17, 10),
    )

    assert result["ok"] is False
    assert result["latest_count"] == 0
    assert result["adjustment_repair_failed_count"] == 1
    saved = pd.read_csv(data_dir / "000001.csv", parse_dates=["date"])
    assert saved["date"].max() == pd.Timestamp("2026-07-10")


@pytest.mark.parametrize("missing_snapshot", ["current", "previous"])
def test_refresh_factor_daily_cache_rejects_raw_append_when_factor_snapshot_is_missing(
    monkeypatch,
    tmp_path,
    missing_snapshot,
):
    import analytics.data_manager as dm
    import market.ths_quote as ths_quote

    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", data_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    _enable_tushare_factor_source(monkeypatch, dm)

    stale = _kline_frame("2026-07-10", periods=80)
    stale.to_csv(data_dir / "000001.csv", index=False, encoding="utf-8")
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_daily",
        lambda trade_date, **kwargs: _market_daily_frame(
            "2026-07-13",
            codes=("000001",),
        ),
    )

    def fake_factors(trade_date, **kwargs):
        compact = str(trade_date).replace("-", "")
        if missing_snapshot == "current" and compact == "20260713":
            return pd.DataFrame()
        if missing_snapshot == "previous" and compact != "20260713":
            return pd.DataFrame()
        return _market_factor_frame(
            str(trade_date),
            codes=("000001",),
            factors=(100.0,),
        )

    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_adj_factors",
        fake_factors,
    )
    qfq_calls = []
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda *args, **kwargs: qfq_calls.append(args) or None,
    )
    monkeypatch.setattr(dm, "_fetch_kline_baostock", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ths_quote,
        "get_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("raw TDX cannot repair unverified QFQ history")
        ),
    )

    result = dm.refresh_factor_daily_cache(
        days=180,
        min_required=1,
        now=datetime(2026, 7, 13, 17, 10),
    )

    assert result["ok"] is False
    assert result["latest_count"] == 0
    assert result["factor_coverage_repair_failed_count"] == 1
    assert qfq_calls == [
        (
            "000001",
            pd.Timestamp(stale["date"].min()).strftime("%Y%m%d"),
            "20260713",
        )
    ]
    saved = pd.read_csv(data_dir / "000001.csv", parse_dates=["date"])
    assert saved["date"].max() == pd.Timestamp("2026-07-10")


def test_refresh_factor_daily_cache_repairs_only_symbol_missing_current_factor(
    monkeypatch,
    tmp_path,
):
    import analytics.data_manager as dm
    import market.ths_quote as ths_quote

    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", data_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    _enable_tushare_factor_source(monkeypatch, dm)

    stale = _kline_frame("2026-07-10", periods=80)
    stale.to_csv(data_dir / "000001.csv", index=False, encoding="utf-8")
    stale.to_csv(data_dir / "000002.csv", index=False, encoding="utf-8")
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_daily",
        lambda trade_date, **kwargs: _market_daily_frame("2026-07-13"),
    )

    def fake_factors(trade_date, **kwargs):
        compact = str(trade_date).replace("-", "")
        codes = ("000001",) if compact == "20260713" else ("000001", "000002")
        return _market_factor_frame(str(trade_date), codes=codes)

    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_adj_factors",
        fake_factors,
    )
    repaired = _kline_frame("2026-07-13", periods=81, start_price=20.0)
    qfq_calls = []
    monkeypatch.setattr(dm, "_fetch_kline_baostock", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda code, start, end, **kwargs: (
            qfq_calls.append((code, start, end))
            or (repaired if code == "000002" else None)
        ),
    )
    monkeypatch.setattr(
        ths_quote,
        "get_client",
        lambda: (_ for _ in ()).throw(AssertionError("TDX must not be used")),
    )

    result = dm.refresh_factor_daily_cache(
        days=180,
        min_required=2,
        now=datetime(2026, 7, 13, 17, 10),
    )

    assert result["ok"] is True, result
    assert result["factor_coverage_repair_count"] == 1
    assert qfq_calls == [
        (
            "000002",
            pd.Timestamp(stale["date"].min()).strftime("%Y%m%d"),
            "20260713",
        )
    ]
    first = pd.read_csv(data_dir / "000001.csv", parse_dates=["date"])
    second = pd.read_csv(data_dir / "000002.csv", parse_dates=["date"])
    assert first["date"].max() == pd.Timestamp("2026-07-13")
    assert second["date"].max() == pd.Timestamp("2026-07-13")
    assert float(second.iloc[-1]["close"]) == float(repaired.iloc[-1]["close"])


def test_refresh_factor_daily_cache_does_not_use_raw_tdx_when_factor_is_missing(
    monkeypatch,
    tmp_path,
):
    import analytics.data_manager as dm
    import market.ths_quote as ths_quote

    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", data_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    _enable_tushare_factor_source(monkeypatch, dm)

    stale = _kline_frame("2026-07-10", periods=80)
    stale.to_csv(data_dir / "000001.csv", index=False, encoding="utf-8")
    stale.to_csv(data_dir / "000002.csv", index=False, encoding="utf-8")
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_daily",
        lambda trade_date, **kwargs: _market_daily_frame("2026-07-13", codes=("000001",)),
        raising=False,
    )
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_adj_factors",
        lambda trade_date, **kwargs: _market_factor_frame(str(trade_date), codes=("000001",), factors=(100.0,)),
        raising=False,
    )
    monkeypatch.setattr(dm.tushare_provider, "fetch_kline_qfq", lambda *args, **kwargs: None)
    monkeypatch.setattr(dm, "_fetch_kline_baostock", lambda *args, **kwargs: None)
    tdx_calls = []
    fresh = _kline_frame("2026-07-13", periods=80, start_price=20.0)

    class FakeClient:
        _ok = True

        def get_kline(self, code, days):
            tdx_calls.append(code)
            return fresh.to_dict("records")

    monkeypatch.setattr(ths_quote, "get_client", lambda: FakeClient())

    result = dm.refresh_factor_daily_cache(days=180, min_required=1, now=datetime(2026, 7, 14, 10, 30))

    assert result["ok"] is True, result
    assert result["tushare_count"] == 1
    assert result["latest_count"] == 1
    assert result["failed"] == 1
    assert result["factor_coverage_repair_failed_count"] == 1
    assert result["tdx_fallback_count"] == 0
    assert tdx_calls == []
    rejected = pd.read_csv(data_dir / "000002.csv", parse_dates=["date"])
    assert rejected["date"].max() == pd.Timestamp("2026-07-10")


def test_refresh_factor_daily_cache_falls_back_when_tushare_batch_fails(monkeypatch, tmp_path):
    import analytics.data_manager as dm
    import market.ths_quote as ths_quote

    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", data_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    _enable_tushare_factor_source(monkeypatch, dm)
    stale = _kline_frame("2026-07-10", periods=80)
    stale.to_csv(data_dir / "000001.csv", index=False, encoding="utf-8")

    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_daily",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("replay unavailable")),
        raising=False,
    )
    fresh = _kline_frame("2026-07-13", periods=80, start_price=20.0)

    class FakeClient:
        _ok = True

        def get_index_kline(self, code, market, days):
            return [{"date": "2026-07-13"}]

        def get_kline(self, code, days):
            return fresh.to_dict("records")

    monkeypatch.setattr(ths_quote, "get_client", lambda: FakeClient())

    result = dm.refresh_factor_daily_cache(days=180, min_required=1, now=datetime(2026, 7, 14, 10, 30))

    assert result["ok"] is True
    assert result["source"] == "tdx_daily"
    assert result["primary_source"] == "tdx_daily"
    assert result["tdx_fallback_count"] == 1
    assert "tushare_errors" in result


def test_refresh_factor_daily_cache_uses_rt_k_after_close_when_daily_is_empty(monkeypatch, tmp_path):
    import analytics.data_manager as dm
    import market.ths_quote as ths_quote

    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", data_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    _enable_tushare_factor_source(monkeypatch, dm)
    stale = _kline_frame("2026-07-13", periods=80)
    stale.to_csv(data_dir / "000001.csv", index=False, encoding="utf-8")
    monkeypatch.setattr(dm.tushare_provider, "fetch_full_market_daily", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_realtime_daily",
        lambda **kwargs: _market_daily_frame("2026-07-14", codes=("000001",)),
        raising=False,
    )
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_adj_factors",
        lambda trade_date, **kwargs: _market_factor_frame(str(trade_date), codes=("000001",), factors=(100.0,)),
    )
    monkeypatch.setattr(ths_quote, "get_client", lambda: (_ for _ in ()).throw(AssertionError("TDX must not be used")))

    result = dm.refresh_factor_daily_cache(days=180, min_required=1, now=datetime(2026, 7, 14, 17, 10))

    assert result["ok"] is True
    assert result["source"] == "tushare_replay_rt_k"
    assert result["source_trade_date"] == "2026-07-14"
    assert result["tdx_fallback_count"] == 0


def test_refresh_factor_daily_cache_rejects_previous_session_after_close(monkeypatch, tmp_path):
    import analytics.data_manager as dm

    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", data_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    monkeypatch.setattr(
        dm.datasource_config,
        "get_internal_config",
        lambda: {
            "provider": "tushare",
            "tushare_enabled": True,
            "tushare_mode": "replay",
            "tushare_token": "api-key",
            "fallback_enabled": False,
        },
    )

    stale = _kline_frame("2026-07-13", periods=80)
    stale.to_csv(data_dir / "000001.csv", index=False, encoding="utf-8")
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_daily",
        lambda trade_date, **kwargs: (
            _market_daily_frame("2026-07-13", codes=("000001",))
            if str(trade_date) == "20260713"
            else pd.DataFrame()
        ),
    )
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_realtime_daily",
        lambda **kwargs: pd.DataFrame(),
    )

    result = dm.refresh_factor_daily_cache(
        days=180,
        min_required=1,
        now=datetime(2026, 7, 14, 17, 10),
    )

    assert result["ok"] is False
    assert result["expected_trade_date"] == "2026-07-14"
    assert result["source_trade_date"] == "2026-07-13"
    assert result["error"] == (
        "factor daily source is stale: expected 2026-07-14, got 2026-07-13"
    )
