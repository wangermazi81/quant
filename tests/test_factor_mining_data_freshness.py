from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
ANALYTICS = ROOT / "analytics"
if str(ANALYTICS) not in sys.path:
    sys.path.insert(0, str(ANALYTICS))

import data_manager as dm  # noqa: E402
from analytics.factors_zoo import scorer  # noqa: E402


def _daily_frame(start: str, periods: int) -> pd.DataFrame:
    dates = pd.bdate_range(start=start, periods=periods)
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "open": range(10, 10 + periods),
            "close": range(11, 11 + periods),
            "high": range(12, 12 + periods),
            "low": range(9, 9 + periods),
            "volume": range(1000, 1000 + periods),
        }
    )


def test_download_one_does_not_overwrite_newer_cache_with_stale_source(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dm, "_load_meta", lambda: {})
    monkeypatch.setattr(dm, "_save_meta", lambda meta: None)
    monkeypatch.setattr(dm, "get_stock_name", lambda code: code)
    monkeypatch.setattr(dm.datasource_config, "get_internal_config", lambda: {
        "provider": "tushare",
        "tushare_enabled": True,
        "tushare_token": "token",
        "tushare_mode": "replay",
        "fallback_enabled": False,
    })
    monkeypatch.setattr(dm, "datetime", type("FixedDatetime", (), {
        "today": staticmethod(lambda: datetime(2026, 7, 9)),
        "now": staticmethod(lambda: datetime(2026, 7, 9, 16, 0)),
    }))

    cached = _daily_frame("2026-06-01", 28)
    cached.loc[len(cached) - 1, "date"] = "2026-07-08"
    cached.to_csv(tmp_path / "000002.csv", index=False)

    stale = _daily_frame("2026-06-01", 12)
    stale.loc[len(stale) - 1, "date"] = "2026-06-16"
    monkeypatch.setattr(dm.tushare_provider, "fetch_kline_qfq", lambda *args, **kwargs: stale)

    result = dm._download_one("000002", "2026-06-01")
    written = pd.read_csv(tmp_path / "000002.csv")

    assert result["ok"] is False
    assert "早于现有缓存" in result["msg"]
    assert str(written["date"].max()) == "2026-07-08"


def test_build_panel_from_cache_prefers_freshest_files_before_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)

    for code in ("000001", "000002"):
        old = _daily_frame("2026-01-01", 70)
        old.loc[len(old) - 1, "date"] = "2026-07-07"
        old.to_csv(tmp_path / f"{code}.csv", index=False)

    for code in ("300001", "600001"):
        fresh = _daily_frame("2026-01-01", 70)
        fresh.loc[len(fresh) - 1, "date"] = "2026-07-09"
        fresh.to_csv(tmp_path / f"{code}.csv", index=False)

    panel, codes = scorer.build_panel_from_cache(n_stocks=2, days=60, min_history=60)

    assert codes == ["300001", "600001"]
    assert set(panel["close"].columns) == {"300001", "600001"}


def test_build_panel_from_cache_require_latest_rejects_stale_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)

    for code in ("000001", "000002"):
        old = _daily_frame("2026-01-01", 70)
        old.loc[len(old) - 1, "date"] = "2026-07-07"
        old.to_csv(tmp_path / f"{code}.csv", index=False)

    fresh = _daily_frame("2026-01-01", 70)
    fresh.loc[len(fresh) - 1, "date"] = "2026-07-09"
    fresh.to_csv(tmp_path / "300001.csv", index=False)

    with pytest.raises(ValueError, match="Refresh data before generating live signals"):
        scorer.build_panel_from_cache(n_stocks=2, days=60, min_history=60, require_latest=True)
