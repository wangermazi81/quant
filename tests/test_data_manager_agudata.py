from datetime import datetime

import pandas as pd

from analytics import data_manager as manager


def _config(**overrides):
    data = {
        "provider": "agudata",
        "agudata_enabled": True,
        "agudata_api_key": "key",
        "agudata_base_url": "https://data.example/openapi/v1",
        "tushare_enabled": True,
        "tushare_token": "old-key",
        "tushare_mode": "replay",
        "fallback_enabled": False,
    }
    data.update(overrides)
    return data


def _history(end_date: str, periods: int = 80) -> pd.DataFrame:
    dates = pd.bdate_range(end=end_date, periods=periods)
    return pd.DataFrame(
        {
            "date": dates,
            "open": [10.0] * periods,
            "high": [10.5] * periods,
            "low": [9.5] * periods,
            "close": [10.2] * periods,
            "volume": [1000.0] * periods,
            "amount": [10200.0] * periods,
        }
    )


def test_get_kline_by_config_uses_agudata(monkeypatch, tmp_path):
    expected = _history("2026-07-31")
    monkeypatch.setattr(
        manager.datasource_config,
        "get_internal_config",
        lambda: _config(),
    )
    monkeypatch.setattr(
        manager.agudata_provider,
        "fetch_kline_qfq",
        lambda *args, **kwargs: expected,
    )
    monkeypatch.setattr(
        manager.tushare_provider,
        "fetch_kline_qfq",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("old Tushare must not be used")
        ),
    )
    monkeypatch.setattr(manager, "DATA_DIR", tmp_path / "daily")
    monkeypatch.setattr(manager, "META_FILE", tmp_path / "metadata.json")

    result = manager.get_kline_by_config(
        "000001",
        days=60,
        min_rows=10,
        update_cache=False,
    )

    assert result["ok"] is True
    assert result["source"] == "agudata"
    assert result["fallback_used"] is False
    assert result["df"]["date"].max() == pd.Timestamp("2026-07-31")


def test_refresh_factor_daily_cache_uses_agudata_batch(
    monkeypatch,
    tmp_path,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    stale = _history("2026-07-29")
    stale.to_csv(daily_dir / "000001.csv", index=False)

    monkeypatch.setattr(manager, "DATA_DIR", daily_dir)
    monkeypatch.setattr(manager, "META_FILE", tmp_path / "metadata.json")
    monkeypatch.setattr(manager, "_load_meta", lambda: {})
    monkeypatch.setattr(manager, "_save_meta", lambda metadata: None)
    monkeypatch.setattr(
        manager.datasource_config,
        "get_internal_config",
        lambda: _config(),
    )

    market_row = pd.DataFrame(
        {
            "code": ["000001"],
            "date": pd.to_datetime(["2026-07-30"]),
            "open": [10.1],
            "high": [10.6],
            "low": [9.8],
            "close": [10.3],
            "volume": [1100.0],
            "amount": [11330.0],
        }
    )

    def fake_daily(trade_date, **kwargs):
        return (
            market_row
            if str(trade_date).replace("-", "") == "20260730"
            else pd.DataFrame()
        )

    def fake_factors(trade_date, **kwargs):
        compact = str(trade_date).replace("-", "")
        if compact not in {"20260730", "20260729"}:
            return pd.DataFrame()
        return pd.DataFrame(
            {
                "code": ["000001"],
                "date": pd.to_datetime([compact]),
                "adj_factor": [100.0],
            }
        )

    monkeypatch.setattr(
        manager.agudata_provider,
        "fetch_full_market_daily",
        fake_daily,
    )
    monkeypatch.setattr(
        manager.agudata_provider,
        "fetch_full_market_adj_factors",
        fake_factors,
    )
    monkeypatch.setattr(
        manager.agudata_provider,
        "fetch_kline_qfq",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("full QFQ repair should not be needed")
        ),
    )
    monkeypatch.setattr(
        manager.tushare_provider,
        "fetch_full_market_daily",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("old Tushare must not be used")
        ),
    )

    result = manager.refresh_factor_daily_cache(
        days=180,
        min_required=1,
        now=datetime(2026, 7, 31, 10, 30),
    )

    assert result["ok"] is True, result
    assert result["source"] == "agudata_daily"
    assert result["agudata_count"] == 1
    saved = pd.read_csv(
        daily_dir / "000001.csv",
        parse_dates=["date"],
    )
    assert saved["date"].max() == pd.Timestamp("2026-07-30")
