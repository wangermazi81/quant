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
    assert pd.to_datetime(cache["000001"]["date"]).max().date() == date.today()


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
