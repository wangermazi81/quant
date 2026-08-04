from datetime import datetime

import pandas as pd
import pytest

from analytics import data_manager as manager


@pytest.fixture(autouse=True)
def _deterministic_completed_trade_date(monkeypatch):
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
            candidate -= pd.Timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= pd.Timedelta(days=1)
        return candidate.isoformat()

    monkeypatch.setattr(
        manager,
        "expected_completed_trade_date",
        expected_trade_date,
    )


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



def _complete_security_master(trade_date: str = "20260804") -> list[dict]:
    raw = [
        ("000001", "SZ", "L", "19910403", ""),
        ("000002", "SZ", "D", "19910129", "20260731"),
        ("600000", "SH", "L", "19991110", ""),
        ("600001", "SH", "D", "19980122", "20260731"),
    ]
    snapshot_at = "2026-08-04T17:10:00"
    return [
        {
            "code": code,
            "ts_code": f"{code}.{exchange}",
            "name": code,
            "list_date": list_date,
            "delist_date": delist_date,
            "list_status": status,
            "exchange": exchange,
            "source": "baostock_stock_basic",
            "lifecycle_source": "baostock_stock_basic",
            "lifecycle_snapshot_complete": True,
            "universe_scope": "sh_sz_a_share",
            "snapshot_at": snapshot_at,
            "security_master_pagination_complete": True,
            "security_master_page_requests": 1,
            "security_master_row_count": 4,
            "security_master_snapshot_complete": True,
            "security_master_snapshot_trade_date": trade_date,
            "security_master_calendar_source": "verified_local_trading_calendar",
        }
        for code, exchange, status, list_date, delist_date in raw
    ]


def test_refresh_factor_daily_cache_uses_baostock_master_fallback_and_current_cache(
    monkeypatch,
    tmp_path,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    for row in _complete_security_master():
        _history("2026-08-04").to_csv(
            daily_dir / f"{row['code']}.csv",
            index=False,
        )

    monkeypatch.setattr(manager, "DATA_DIR", daily_dir)
    monkeypatch.setattr(
        manager,
        "SECURITY_MASTER_FILE",
        tmp_path / "security_master.csv",
    )
    monkeypatch.setattr(manager, "META_FILE", tmp_path / "metadata.json")
    monkeypatch.setattr(manager, "_load_meta", lambda: {})
    monkeypatch.setattr(manager, "_save_meta", lambda metadata: None)
    monkeypatch.setattr(
        manager.factor_history_quarantine,
        "active_codes",
        lambda: set(),
    )
    monkeypatch.setattr(
        manager.factor_history_quarantine,
        "record_failures",
        lambda failures: None,
    )
    monkeypatch.setattr(
        manager.factor_history_quarantine,
        "clear_codes",
        lambda codes: None,
    )
    monkeypatch.setattr(
        manager,
        "_factor_history_cache_complete",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        manager.datasource_config,
        "get_internal_config",
        lambda: _config(fallback_enabled=False),
    )
    monkeypatch.setattr(
        manager.agudata_provider,
        "fetch_security_master",
        lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("API key unavailable")
        ),
    )
    monkeypatch.setattr(
        manager.tushare_provider,
        "fetch_baostock_security_master",
        lambda **kwargs: _complete_security_master(),
    )
    monkeypatch.setattr(
        manager.agudata_provider,
        "fetch_full_market_daily",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("daily provider unavailable")
        ),
    )
    monkeypatch.setattr(
        manager.agudata_provider,
        "fetch_full_market_realtime_daily",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("realtime provider unavailable")
        ),
    )

    result = manager.refresh_factor_daily_cache(
        days=60,
        min_required=1,
        now=datetime(2026, 8, 4, 17, 10),
        refresh_security_master=True,
    )

    assert result["ok"] is True, result
    assert result["source"] == "existing_daily_cache"
    assert result["primary_source"] == "agudata_daily"
    assert result["fallback_used"] is True
    assert result["source_trade_date"] == "2026-08-04"
    assert result["latest_count"] >= 1
    assert result["security_master"]["fallback_used"] is True
    assert result["security_master"]["source"] == "baostock_security_master"
