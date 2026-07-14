# -*- coding: utf-8 -*-
from tests._module_stubs import stub_project_module
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))


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

    result = dm.refresh_factor_daily_cache(days=180, min_required=2)

    assert result["ok"] is False
    assert result["source_trade_date"] == "2026-07-13"
    assert result["latest_count"] == 1
    assert result["refreshed"] == 1
    assert result["failed"] == 1
    saved = pd.read_csv(data_dir / "000001.csv", parse_dates=["date"])
    assert saved["date"].max() == pd.Timestamp("2026-07-13")
