from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
ANALYTICS = ROOT / "analytics"
if str(ANALYTICS) not in sys.path:
    sys.path.insert(0, str(ANALYTICS))

from analytics import data_manager as dm  # noqa: E402
from analytics import factor_history_quarantine  # noqa: E402
from analytics import st_status_history  # noqa: E402
from analytics.factors_zoo import scorer  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_factor_history_quarantine(tmp_path, monkeypatch):
    monkeypatch.setattr(
        factor_history_quarantine,
        "QUARANTINE_PATH",
        tmp_path / "factor_history_quarantine.json",
    )

    def expected_trade_date(now=None):
        current = now or datetime(2026, 7, 30, 17, 10)
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
        dm,
        "expected_completed_trade_date",
        expected_trade_date,
    )


def _daily_frame(start: str, periods: int) -> pd.DataFrame:
    dates = pd.bdate_range(start=start, periods=periods)
    frame = pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "open": range(10, 10 + periods),
            "close": range(11, 11 + periods),
            "high": range(12, 12 + periods),
            "low": range(9, 9 + periods),
            "volume": range(1000, 1000 + periods),
        }
    )
    frame["amount"] = frame["close"] * frame["volume"] * 100 + 123
    return frame


def _trusted_master_audit_fields(
    row_count: int,
    trade_date: str = "20260730",
) -> dict:
    return {
        "security_master_pagination_complete": True,
        "security_master_page_requests": 6,
        "security_master_row_count": row_count,
        "security_master_snapshot_complete": True,
        "security_master_snapshot_trade_date": trade_date,
    }


def _trusted_security_master(rows: list[dict]) -> list[dict]:
    coverage_rows = [
        {
            "code": "000998",
            "list_date": "20990101",
            "delist_date": "",
            "list_status": "L",
        },
        {
            "code": "000999",
            "list_date": "19900101",
            "delist_date": "19900102",
            "list_status": "D",
        },
        {
            "code": "600998",
            "list_date": "20990101",
            "delist_date": "",
            "list_status": "L",
        },
        {
            "code": "600999",
            "list_date": "19900101",
            "delist_date": "19900102",
            "list_status": "D",
        },
    ]
    normalized = [dict(row) for row in [*rows, *coverage_rows]]
    row_count = len(normalized)
    for row in normalized:
        code = str(row["code"]).zfill(6)
        exchange = "SH" if code.startswith(("5", "6", "9")) else "SZ"
        row["code"] = code
        row.setdefault("ts_code", f"{code}.{exchange}")
        row.setdefault("lifecycle_source", "baostock_stock_basic")
        row.setdefault("lifecycle_snapshot_complete", True)
        row.setdefault("universe_scope", "sh_sz_a_share")
        row.setdefault("snapshot_at", "2026-07-30T17:10:00")
        row.update(_trusted_master_audit_fields(row_count))
    return normalized


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


def test_factor_history_cache_requires_continuous_prices_and_real_amount(
    tmp_path,
):
    path = tmp_path / "000001.csv"
    frame = _daily_frame("2026-01-01", 80)
    security_start = pd.Timestamp(frame["date"].iloc[0])
    security_end = pd.Timestamp(frame["date"].iloc[-1])

    frame.drop(columns=["amount"]).to_csv(path, index=False)
    assert dm._factor_history_cache_complete(
        path,
        security_start=security_start,
        security_end=security_end,
        minimum_rows=60,
        require_end_coverage=True,
    ) is False

    discontinuous = frame.copy()
    discontinuous.loc[40, ["open", "close", "high", "low"]] *= 30
    discontinuous.to_csv(path, index=False)
    assert dm._factor_history_cache_complete(
        path,
        security_start=security_start,
        security_end=security_end,
        minimum_rows=60,
        require_end_coverage=True,
    ) is False

    suspended = frame.copy()
    suspended.loc[40, ["volume", "amount"]] = float("nan")
    suspended.to_csv(path, index=False)
    assert dm._factor_history_cache_complete(
        path,
        security_start=security_start,
        security_end=security_end,
        minimum_rows=60,
        require_end_coverage=True,
    ) is True

    frame.to_csv(path, index=False)
    assert dm._factor_history_cache_complete(
        path,
        security_start=security_start,
        security_end=security_end,
        minimum_rows=60,
        require_end_coverage=True,
    ) is True


def test_refresh_security_master_backfills_missing_delisted_history(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    master_path = tmp_path / "security_master.csv"
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(dm, "SECURITY_MASTER_FILE", master_path)
    monkeypatch.setattr(dm, "_load_meta", lambda: {})
    monkeypatch.setattr(dm, "_save_meta", lambda meta: None)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_security_master",
        lambda **kwargs: _trusted_security_master([
            {
                "code": "000001",
                "ts_code": "000001.SZ",
                "name": "delisted",
                "list_date": "20200101",
                    "delist_date": "20260301",
                    "list_status": "D",
                    "source": "tushare_stock_basic",
                    "lifecycle_snapshot_complete": True,
                }
        ]),
    )
    history = _daily_frame("2025-07-30", 120)
    history.loc[len(history) - 1, "date"] = "2026-03-01"
    calls = []

    def fake_fetch(code, start_date, end_date, **kwargs):
        calls.append((code, start_date, end_date))
        return history

    monkeypatch.setattr(dm.tushare_provider, "fetch_kline_qfq", fake_fetch)

    result = dm.refresh_factor_security_master_history(
        days=120,
        now=datetime(2026, 7, 30, 17, 10),
        token="tok",
        mode="replay",
    )

    assert result["ok"] is True
    assert result["master_count"] == 5
    assert result["history_backfilled"] == 1
    assert calls[0][0] == "000001"
    assert calls[0][2] == "20260301"
    assert master_path.exists()
    assert (daily_dir / "000001.csv").exists()


def test_refresh_security_master_rejects_untrusted_lifecycle_without_overwrite(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    master_path = tmp_path / "security_master.csv"
    master_path.write_text("sentinel", encoding="utf-8")
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(dm, "SECURITY_MASTER_FILE", master_path)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_security_master",
        lambda **kwargs: _trusted_security_master([
            {
                "code": "000001",
                "list_date": "19910403",
                "delist_date": "",
                "list_status": "L",
                "lifecycle_snapshot_complete": False,
            }
        ]),
    )
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("untrusted master must fail before history fetch")
        ),
    )

    result = dm.refresh_factor_security_master_history(
        days=120,
        now=datetime(2026, 7, 30, 17, 10),
        token="tok",
        mode="replay",
    )

    assert result["ok"] is False
    assert result["error"] == "security master lifecycle snapshot is incomplete"
    assert master_path.read_text(encoding="utf-8") == "sentinel"


def test_refresh_security_master_repairs_existing_incomplete_history(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    master_path = tmp_path / "security_master.csv"
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(dm, "SECURITY_MASTER_FILE", master_path)
    monkeypatch.setattr(dm, "_load_meta", lambda: {})
    monkeypatch.setattr(dm, "_save_meta", lambda meta: None)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_security_master",
        lambda **kwargs: _trusted_security_master([
            {
                "code": "000001",
                "ts_code": "000001.SZ",
                "name": "listed",
                "list_date": "20200101",
                    "delist_date": "",
                    "list_status": "L",
                    "source": "tushare_stock_basic",
                    "lifecycle_snapshot_complete": True,
                }
        ]),
    )
    _daily_frame("2026-07-01", 5).to_csv(
        daily_dir / "000001.csv",
        index=False,
    )
    repaired = _daily_frame("2025-07-01", 280)
    repaired["date"] = pd.bdate_range(end="2026-07-30", periods=len(repaired))
    calls = []

    def fake_fetch(code, start_date, end_date, **kwargs):
        calls.append((code, start_date, end_date))
        return repaired

    monkeypatch.setattr(dm.tushare_provider, "fetch_kline_qfq", fake_fetch)

    result = dm.refresh_factor_security_master_history(
        days=120,
        now=datetime(2026, 7, 30, 17, 10),
        token="tok",
        mode="replay",
    )

    assert result["ok"] is True
    assert result["history_backfilled"] == 1
    assert calls and calls[0][0] == "000001"
    assert len(pd.read_csv(daily_dir / "000001.csv")) >= 120


def test_refresh_security_master_uses_agudata_master_and_history(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(
        dm,
        "SECURITY_MASTER_FILE",
        tmp_path / "security_master.csv",
    )
    monkeypatch.setattr(dm, "_load_meta", lambda: {})
    monkeypatch.setattr(dm, "_save_meta", lambda meta: None)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_security_master",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("old Tushare master must not be used")
        ),
    )
    master_calls = []
    monkeypatch.setattr(
        dm.agudata_provider,
        "fetch_security_master",
        lambda **kwargs: (
            master_calls.append(kwargs)
            or _trusted_security_master(
                [
                    {
                        "code": "000001",
                        "list_date": "20200101",
                        "delist_date": "",
                        "list_status": "L",
                        "lifecycle_snapshot_complete": True,
                    }
                ]
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("old Tushare history must not be used")
        ),
    )
    repaired = _daily_frame("2025-07-01", 280)
    repaired["date"] = pd.bdate_range(
        end="2026-07-30",
        periods=len(repaired),
    )
    calls = []
    monkeypatch.setattr(
        dm.agudata_provider,
        "fetch_kline_qfq",
        lambda code, start, end, **kwargs: (
            calls.append((code, start, end, kwargs))
            or repaired
        ),
    )

    result = dm.refresh_factor_security_master_history(
        days=120,
        now=datetime(2026, 7, 30, 17, 10),
        history_provider="agudata",
        agudata_api_key="agudata-key",
        agudata_base_url="https://data.example/openapi/v1",
    )

    assert result["ok"] is True, result
    assert result["history_backfilled"] == 1
    assert master_calls == [
        {
            "api_key": "agudata-key",
            "base_url": "https://data.example/openapi/v1",
        }
    ]
    assert calls[0][0] == "000001"
    assert calls[0][3] == {
        "api_key": "agudata-key",
        "base_url": "https://data.example/openapi/v1",
    }


def test_refresh_security_master_falls_back_to_baostock_qfq(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(
        dm,
        "SECURITY_MASTER_FILE",
        tmp_path / "security_master.csv",
    )
    monkeypatch.setattr(dm, "_load_meta", lambda: {})
    monkeypatch.setattr(dm, "_save_meta", lambda meta: None)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_security_master",
        lambda **kwargs: _trusted_security_master([
            {
                "code": "000001",
                "list_date": "20200101",
                "delist_date": "",
                "list_status": "L",
                "lifecycle_snapshot_complete": True,
            }
        ]),
    )
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda *args, **kwargs: None,
    )
    repaired = _daily_frame("2025-07-01", 280)
    repaired["date"] = pd.bdate_range(end="2026-07-30", periods=len(repaired))
    baostock_calls = []
    monkeypatch.setattr(
        dm,
        "_fetch_kline_baostock",
        lambda code, start, end: (
            baostock_calls.append((code, start, end))
            or repaired
        ),
    )

    result = dm.refresh_factor_security_master_history(
        days=120,
        now=datetime(2026, 7, 30, 17, 10),
        token="tok",
        mode="replay",
    )

    assert result["ok"] is True, result
    assert result["history_backfilled"] == 1
    assert baostock_calls and baostock_calls[0][0] == "000001"
    saved = pd.read_csv(daily_dir / "000001.csv")
    assert bool((saved["amount"] > 0).all()) is True


def test_refresh_security_master_rejects_still_incomplete_backfill(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(dm, "SECURITY_MASTER_FILE", tmp_path / "security_master.csv")
    monkeypatch.setattr(dm, "_load_meta", lambda: {})
    monkeypatch.setattr(dm, "_save_meta", lambda meta: None)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_security_master",
        lambda **kwargs: _trusted_security_master([
            {
                "code": "000001",
                    "list_date": "20200101",
                    "delist_date": "",
                    "list_status": "L",
                    "lifecycle_snapshot_complete": True,
                }
        ]),
    )
    truncated = _daily_frame("2026-01-01", 60)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda *args, **kwargs: truncated,
    )

    result = dm.refresh_factor_security_master_history(
        days=120,
        now=datetime(2026, 7, 30, 17, 10),
        token="tok",
        mode="replay",
    )

    assert result["ok"] is False
    assert result["history_backfilled"] == 0
    assert result["history_failed"] == 1
    assert "incomplete" in result["errors"][0]


def test_refresh_security_master_reports_total_failures_not_error_sample_size(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(dm, "SECURITY_MASTER_FILE", tmp_path / "security_master.csv")
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_security_master",
        lambda **kwargs: _trusted_security_master([
            {
                    "code": f"{index:06d}",
                    "list_date": "20200101",
                    "delist_date": "",
                    "list_status": "L",
                    "lifecycle_snapshot_complete": True,
                }
            for index in range(1, 26)
        ]),
    )
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("source unavailable")
        ),
    )
    monkeypatch.setattr(dm, "_fetch_kline_baostock", lambda *args, **kwargs: None)

    result = dm.refresh_factor_security_master_history(
        days=60,
        now=datetime(2026, 7, 30, 17, 10),
        token="tok",
        mode="replay",
    )

    assert result["ok"] is False
    assert result["history_failed"] == 25
    assert len(result["errors"]) == 20


def test_refresh_security_master_backfills_only_point_in_time_pool(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(dm, "SECURITY_MASTER_FILE", tmp_path / "security_master.csv")
    monkeypatch.setattr(dm, "_load_meta", lambda: {})
    monkeypatch.setattr(dm, "_save_meta", lambda meta: None)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_security_master",
        lambda **kwargs: _trusted_security_master([
            {
                    "code": f"{index:06d}",
                    "list_date": "20200101",
                    "delist_date": "",
                    "list_status": "L",
                    "lifecycle_snapshot_complete": True,
                }
            for index in range(1, 6)
        ]),
    )
    history = _daily_frame("2025-07-01", 120)
    calls = []

    def fake_fetch(code, *args, **kwargs):
        calls.append(code)
        return history

    monkeypatch.setattr(dm.tushare_provider, "fetch_kline_qfq", fake_fetch)

    result = dm.refresh_factor_security_master_history(
        days=120,
        n_stocks=2,
        now=datetime(2026, 7, 30, 17, 10),
        token="tok",
        mode="replay",
    )

    assert result["ok"] is True
    assert result["history_required"] == 3
    assert result["history_backfilled"] == 3
    assert set(calls) == {"000001", "000003", "000004"}


def test_refresh_security_master_requires_every_sampled_history(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(
        dm,
        "SECURITY_MASTER_FILE",
        tmp_path / "security_master.csv",
    )
    monkeypatch.setattr(dm, "_load_meta", lambda: {})
    monkeypatch.setattr(dm, "_save_meta", lambda meta: None)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_security_master",
        lambda **kwargs: _trusted_security_master([
            {
                "code": code,
                "list_date": "20200101",
                "delist_date": "",
                "list_status": "L",
                "lifecycle_snapshot_complete": True,
            }
            for code in ("000009", "000004", "000006")
        ]),
    )
    history = _daily_frame("2025-07-01", 280)
    history["date"] = pd.bdate_range(end="2026-07-30", periods=len(history))
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda code, *args, **kwargs: (
            None if code == "000009" else history
        ),
    )
    monkeypatch.setattr(dm, "_fetch_kline_baostock", lambda *args, **kwargs: None)

    result = dm.refresh_factor_security_master_history(
        days=120,
        n_stocks=2,
        now=datetime(2026, 7, 30, 17, 10),
        token="tok",
        mode="replay",
    )

    assert result["ok"] is True, result
    assert result["history_required"] == 3
    assert result["history_usable"] == 2
    assert result["history_failed"] == 1
    assert result["minimum_usable"] == 2
    assert result["history_quarantined"] == 1
    assert result["quarantined_codes"] == ["000009"]


def test_refresh_security_master_repairs_missing_reserve_even_when_target_is_usable(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(
        dm,
        "SECURITY_MASTER_FILE",
        tmp_path / "security_master.csv",
    )
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_security_master",
        lambda **kwargs: _trusted_security_master([
            {
                "code": code,
                "list_date": "20200101",
                "delist_date": "",
                "list_status": "L",
                "lifecycle_snapshot_complete": True,
            }
            for code in ("000009", "000004", "000006")
        ]),
    )
    history = _daily_frame("2025-07-01", 280)
    history["date"] = pd.bdate_range(end="2026-07-30", periods=len(history))
    history.to_csv(daily_dir / "000009.csv", index=False)
    history.to_csv(daily_dir / "000004.csv", index=False)
    calls = []
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda *args, **kwargs: calls.append(args) or None,
    )
    monkeypatch.setattr(
        dm,
        "_fetch_kline_baostock",
        lambda *args, **kwargs: calls.append(args) or None,
    )

    result = dm.refresh_factor_security_master_history(
        days=120,
        n_stocks=2,
        now=datetime(2026, 7, 30, 17, 10),
        token="tok",
        mode="replay",
    )

    assert result["ok"] is True, result
    assert result["history_usable"] == 2
    assert result["history_incomplete"] == 1
    assert result["history_failed"] == 1
    assert result["minimum_usable"] == 2
    assert result["history_quarantined"] == 1
    assert result["quarantined_codes"] == ["000006"]
    assert calls


def test_refresh_security_master_sample_ignores_pre_window_delists_and_recent_ipos(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(dm, "SECURITY_MASTER_FILE", tmp_path / "security_master.csv")
    monkeypatch.setattr(dm, "_load_meta", lambda: {})
    monkeypatch.setattr(dm, "_save_meta", lambda meta: None)
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_security_master",
        lambda **kwargs: _trusted_security_master([
            {
                "code": "000001",
                "list_date": "20200101",
                "delist_date": "",
                "list_status": "L",
                "lifecycle_snapshot_complete": True,
            },
            {
                "code": "000002",
                "list_date": "20200101",
                "delist_date": "",
                "list_status": "L",
                "lifecycle_snapshot_complete": True,
            },
            {
                "code": "301677",
                "list_date": "20260730",
                "delist_date": "",
                "list_status": "L",
                "lifecycle_snapshot_complete": True,
            },
            {
                "code": "600565",
                "list_date": "20020723",
                "delist_date": "20240807",
                "list_status": "D",
                "lifecycle_snapshot_complete": True,
            },
        ]),
    )
    history = _daily_frame("2025-07-30", 120)
    calls = []

    def fake_fetch(code, *args, **kwargs):
        calls.append(code)
        return history

    monkeypatch.setattr(dm.tushare_provider, "fetch_kline_qfq", fake_fetch)

    result = dm.refresh_factor_security_master_history(
        days=120,
        n_stocks=1,
        now=datetime(2026, 7, 30, 17, 10),
        token="tok",
        mode="replay",
    )

    assert result["ok"] is True
    assert result["history_required"] == 2
    assert set(calls) == {"000001", "000002"}


def test_refresh_security_master_zero_sample_requires_no_history(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(dm, "SECURITY_MASTER_FILE", tmp_path / "security_master.csv")
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_security_master",
        lambda **kwargs: _trusted_security_master([
            {
                    "code": "000001",
                    "list_date": "20200101",
                    "delist_date": "",
                    "list_status": "L",
                    "lifecycle_snapshot_complete": True,
                }
        ]),
    )
    calls = []
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda code, *args, **kwargs: calls.append(code),
    )

    result = dm.refresh_factor_security_master_history(
        days=120,
        n_stocks=0,
        now=datetime(2026, 7, 30, 17, 10),
        token="tok",
        mode="replay",
    )

    assert result["ok"] is True
    assert result["history_required"] == 0
    assert result["history_backfilled"] == 0
    assert calls == []


def test_refresh_factor_daily_cache_limits_master_history_to_required_sample(
    monkeypatch,
):
    captured = {}
    enabled = iter((True, False))
    monkeypatch.setattr(dm.datasource_config, "get_internal_config", lambda: {})
    monkeypatch.setattr(dm, "_factor_tushare_enabled", lambda config: next(enabled))

    def fake_refresh_master(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(dm, "refresh_factor_security_master_history", fake_refresh_master)
    monkeypatch.setattr(
        dm,
        "_refresh_factor_daily_cache_tdx",
        lambda **kwargs: {"ok": True, "refreshed": 0},
    )

    result = dm.refresh_factor_daily_cache(
        days=120,
        min_required=37,
        now=datetime(2026, 7, 30, 17, 10),
        refresh_security_master=True,
    )

    assert result["ok"] is False
    assert result["error"] == "daily cache is empty"
    assert captured["n_stocks"] == 37


def test_refresh_factor_daily_cache_limits_daily_work_to_point_in_time_sample(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    master_path = tmp_path / "security_master.csv"
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    monkeypatch.setattr(dm, "SECURITY_MASTER_FILE", master_path)
    monkeypatch.setattr(dm, "_load_meta", lambda: {})
    monkeypatch.setattr(dm, "_save_meta", lambda meta: None)
    monkeypatch.setattr(
        dm.datasource_config,
        "get_internal_config",
        lambda: {
            "provider": "tushare",
            "tushare_enabled": True,
            "tushare_token": "token",
            "tushare_mode": "replay",
            "fallback_enabled": False,
        },
    )
    monkeypatch.setattr(
        dm,
        "refresh_factor_security_master_history",
        lambda **kwargs: {"ok": True},
    )

    current = _daily_frame("2026-01-01", 80)
    current["date"] = pd.bdate_range(end="2026-07-30", periods=80)
    current.to_csv(daily_dir / "000001.csv", index=False)
    for code in ("301677", "600565"):
        outside_sample = current.copy()
        outside_sample.loc[40, ["open", "close", "high", "low"]] *= 30
        outside_sample.to_csv(daily_dir / f"{code}.csv", index=False)

    pd.DataFrame(
        [
            {
                "code": "000001",
                "list_date": "20200101",
                "delist_date": "",
                "list_status": "L",
                "lifecycle_snapshot_complete": True,
                "lifecycle_source": "baostock_stock_basic",
                "universe_scope": "sh_sz_a_share",
                "snapshot_at": "2026-07-30T17:10:00",
            },
            {
                "code": "301677",
                "list_date": "20260730",
                "delist_date": "",
                "list_status": "L",
                "lifecycle_snapshot_complete": True,
                "lifecycle_source": "baostock_stock_basic",
                "universe_scope": "sh_sz_a_share",
                "snapshot_at": "2026-07-30T17:10:00",
            },
            {
                "code": "600565",
                "list_date": "20020723",
                "delist_date": "20240807",
                "list_status": "D",
                "lifecycle_snapshot_complete": True,
                "lifecycle_source": "baostock_stock_basic",
                "universe_scope": "sh_sz_a_share",
                "snapshot_at": "2026-07-30T17:10:00",
            },
        ]
    ).assign(**_trusted_master_audit_fields(3)).to_csv(
        master_path,
        index=False,
    )

    market_rows = []
    for code in ("000001", "301677", "600565"):
        market_rows.append(
            {
                "code": code,
                "date": pd.Timestamp("2026-07-30"),
                "open": 20.0,
                "high": 20.5,
                "low": 19.5,
                "close": 20.2,
                "volume": 1000.0,
                "amount": 20200.0,
            }
        )
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_daily",
        lambda *args, **kwargs: pd.DataFrame(market_rows),
    )
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_full_market_adj_factors",
        lambda trade_date, **kwargs: pd.DataFrame(
            {
                "code": ["000001", "301677", "600565"],
                "date": pd.to_datetime([str(trade_date)] * 3),
                "adj_factor": [100.0, 100.0, 100.0],
            }
        ),
    )
    qfq_calls = []
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda code, *args, **kwargs: qfq_calls.append(code) or None,
    )

    result = dm.refresh_factor_daily_cache(
        days=320,
        min_required=1,
        now=datetime(2026, 7, 30, 17, 10),
        refresh_security_master=True,
    )

    assert result["ok"] is True, result
    assert result["total"] == 1
    assert result["latest_count"] == 1
    assert qfq_calls == []


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


def test_build_panel_from_cache_uses_stable_sample_instead_of_longest_histories(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)

    for index, code in enumerate(("000001", "000002", "000003", "000004", "000005", "000006")):
        frame = _daily_frame("2026-01-01", 60 + index * 10)
        frame.loc[len(frame) - 1, "date"] = "2026-07-29"
        frame.to_csv(tmp_path / f"{code}.csv", index=False)

    panel, codes = scorer.build_panel_from_cache(n_stocks=2, days=60, min_history=60, require_latest=True)

    assert codes == ["000004", "000006"]
    assert set(panel["close"].columns) == {"000004", "000006"}


def test_build_panel_from_cache_skips_discontinuous_price_history_and_refills_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)

    for code in ("000001", "000002", "000003", "000004", "000005", "000006"):
        frame = _daily_frame("2026-01-01", 70)
        frame.loc[len(frame) - 1, "date"] = "2026-07-29"
        if code == "000004":
            spike = len(frame) - 3
            frame.loc[spike, ["open", "close", "high", "low"]] *= 30
        frame.to_csv(tmp_path / f"{code}.csv", index=False)

    panel, codes = scorer.build_panel_from_cache(
        n_stocks=2,
        days=60,
        min_history=60,
        require_latest=True,
    )

    assert codes == ["000003", "000006"]
    assert set(panel["close"].columns) == {"000003", "000006"}


def test_build_panel_from_cache_preserves_real_turnover_amount(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)
    frame = _daily_frame("2026-01-01", 70)
    frame.to_csv(tmp_path / "000001.csv", index=False)

    panel, _codes = scorer.build_panel_from_cache(n_stocks=1, days=60, min_history=60)

    expected = float(frame.tail(60).iloc[-1]["amount"])
    assert float(panel["amount"].iloc[-1]["000001"]) == expected


def test_build_panel_from_cache_keeps_stocks_with_missing_amount_as_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)
    real = _daily_frame("2026-01-01", 70)
    proxy = _daily_frame("2026-01-01", 70).drop(columns=["amount"])
    real.to_csv(tmp_path / "000001.csv", index=False)
    proxy.to_csv(tmp_path / "000002.csv", index=False)

    panel, codes = scorer.build_panel_from_cache(n_stocks=2, days=60, min_history=60)

    assert codes == ["000001", "000002"]
    assert set(panel["close"].columns) == {"000001", "000002"}
    assert set(panel["amount"].columns) == {"000001", "000002"}
    assert bool(panel["amount_real"]["000001"].all()) is True
    assert bool(panel["amount_real"]["000002"].any()) is False
    expected_proxy = panel["close"]["000002"] * panel["volume"]["000002"]
    pd.testing.assert_series_equal(
        panel["amount"]["000002"],
        expected_proxy,
        check_names=False,
    )


def test_point_in_time_panel_refills_invalid_member_from_stable_reserve(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)
    master_path = tmp_path / "security_master.csv"
    monkeypatch.setattr(scorer, "_SECURITY_MASTER_PATH", master_path)

    dates = pd.bdate_range(end="2026-07-30", periods=70)
    for code in ("000009", "000004", "000006"):
        frame = _daily_frame("2026-01-01", len(dates))
        frame["date"] = dates
        if code == "000009":
            frame.loc[65, ["open", "close", "high", "low"]] *= 30
        frame.to_csv(tmp_path / f"{code}.csv", index=False)
    pd.DataFrame(
        [
            {
                "code": code,
                "list_date": "20200101",
                "delist_date": "",
                "list_status": "L",
                "lifecycle_snapshot_complete": True,
                "lifecycle_source": "baostock_stock_basic",
                "universe_scope": "sh_sz_a_share",
                "snapshot_at": "2026-07-30T17:10:00",
            }
            for code in ("000009", "000004", "000006")
        ]
        + [
            {
                "code": "600565",
                "list_date": "20020723",
                "delist_date": "20240807",
                "list_status": "D",
                "lifecycle_snapshot_complete": True,
                "lifecycle_source": "baostock_stock_basic",
                "universe_scope": "sh_sz_a_share",
                "snapshot_at": "2026-07-30T17:10:00",
            }
        ]
    ).assign(**_trusted_master_audit_fields(4)).to_csv(
        master_path,
        index=False,
    )

    panel, _codes = scorer.build_panel_from_cache(
        n_stocks=2,
        days=60,
        min_history=10,
        require_latest=True,
        point_in_time=True,
        require_security_master=True,
    )

    latest = panel["universe_eligible"].iloc[-1]
    assert int(latest.sum()) == 2
    assert bool(latest["000004"]) is True
    assert bool(latest["000006"]) is True
    assert bool(latest.get("000009", False)) is False


def test_point_in_time_panel_accepts_target_when_only_reserve_history_is_missing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)
    master_path = tmp_path / "security_master.csv"
    monkeypatch.setattr(scorer, "_SECURITY_MASTER_PATH", master_path)
    dates = pd.bdate_range(end="2026-07-30", periods=70)
    for code in ("000009", "000004"):
        frame = _daily_frame("2026-01-01", len(dates))
        frame["date"] = dates
        frame.to_csv(tmp_path / f"{code}.csv", index=False)
    pd.DataFrame(
        [
            {
                "code": code,
                "list_date": "20200101",
                "delist_date": "",
                "list_status": "L",
                "lifecycle_snapshot_complete": True,
                "lifecycle_source": "baostock_stock_basic",
                "universe_scope": "sh_sz_a_share",
                "snapshot_at": "2026-07-30T17:10:00",
            }
            for code in ("000009", "000004", "000006")
        ]
        + [
            {
                "code": "600565",
                "list_date": "20020723",
                "delist_date": "20240807",
                "list_status": "D",
                "lifecycle_snapshot_complete": True,
                "lifecycle_source": "baostock_stock_basic",
                "universe_scope": "sh_sz_a_share",
                "snapshot_at": "2026-07-30T17:10:00",
            }
        ]
    ).assign(**_trusted_master_audit_fields(4)).to_csv(
        master_path,
        index=False,
    )

    panel, codes = scorer.build_panel_from_cache(
        n_stocks=2,
        days=60,
        min_history=10,
        require_latest=True,
        point_in_time=True,
        require_security_master=True,
    )

    assert codes == ["000004", "000009"]
    assert int(panel["universe_eligible"].iloc[-1].sum()) == 2


def test_point_in_time_panel_quarantines_only_from_first_price_break(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)
    master_path = tmp_path / "security_master.csv"
    monkeypatch.setattr(scorer, "_SECURITY_MASTER_PATH", master_path)
    pd.DataFrame(
        [
            {"code": "000001", "list_date": "20200101", "delist_date": ""},
            {"code": "000002", "list_date": "20200101", "delist_date": ""},
        ]
    ).to_csv(master_path, index=False)
    stable = _daily_frame("2026-01-01", 70)
    broken = _daily_frame("2026-01-01", 70)
    break_index = len(broken) - 5
    broken.loc[break_index, ["open", "close", "high", "low"]] *= 30
    stable.to_csv(tmp_path / "000001.csv", index=False)
    broken.to_csv(tmp_path / "000002.csv", index=False)

    panel, codes = scorer.build_panel_from_cache(
        n_stocks=None,
        days=60,
        min_history=10,
        point_in_time=True,
    )

    break_date = pd.Timestamp(broken.loc[break_index, "date"])
    prior_date = pd.Timestamp(broken.loc[break_index - 1, "date"])
    assert "000002" in codes
    assert bool(panel["universe_eligible"].loc[prior_date, "000002"]) is True
    assert bool(panel["universe_eligible"].loc[break_date, "000002"]) is False
    assert pd.isna(panel["close"].loc[break_date, "000002"])


def test_master_sample_mask_scales_to_full_market_size():
    codes = [f"{index:06d}" for index in range(1, 5501)]
    master = pd.DataFrame(
        {
            "code": codes,
            "list_date": pd.Timestamp("2020-01-01"),
            "delist_date": pd.NaT,
        },
        index=codes,
    )
    dates = pd.bdate_range("2025-01-01", periods=320)

    started = time.perf_counter()
    selected = scorer._master_sample_mask(master, dates, 800)
    elapsed = time.perf_counter() - started

    assert selected.shape == (320, 5500)
    assert selected.sum(axis=1).eq(800).all()
    assert elapsed < 10.0


def test_master_sample_mask_excludes_recent_listing_until_warmup_completes():
    dates = pd.bdate_range("2026-07-27", periods=5)
    master = pd.DataFrame(
        {
            "code": ["000001", "301677"],
            "list_date": [pd.Timestamp("2020-01-01"), dates[0]],
            "delist_date": [pd.NaT, pd.NaT],
        },
        index=["000001", "301677"],
    )

    selected = scorer._master_sample_mask(
        master,
        dates,
        n_stocks=None,
        minimum_history=3,
    )

    assert selected["000001"].all()
    assert selected["301677"].tolist() == [False, False, True, True, True]


def test_point_in_time_panel_fails_closed_when_master_history_is_missing(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(scorer, "_DATA_DIR", daily_dir)
    master_path = tmp_path / "security_master.csv"
    monkeypatch.setattr(scorer, "_SECURITY_MASTER_PATH", master_path)

    available = _daily_frame("2026-01-01", 70)
    available.to_csv(daily_dir / "000002.csv", index=False)
    pd.DataFrame(
        [
            {
                "code": "000001",
                "list_date": "20200101",
                "delist_date": "20260301",
                "list_status": "D",
                "lifecycle_snapshot_complete": True,
                "lifecycle_source": "baostock_stock_basic",
                "universe_scope": "sh_sz_a_share",
                "snapshot_at": "2026-07-30T17:10:00",
            },
            {
                "code": "000002",
                "list_date": "20200101",
                "delist_date": "",
                "list_status": "L",
                "lifecycle_snapshot_complete": True,
                "lifecycle_source": "baostock_stock_basic",
                "universe_scope": "sh_sz_a_share",
                "snapshot_at": "2026-07-30T17:10:00",
            },
        ]
    ).assign(**_trusted_master_audit_fields(2)).to_csv(
        master_path,
        index=False,
    )

    with pytest.raises(ValueError, match="security master histories missing"):
        scorer.build_panel_from_cache(
            n_stocks=None,
            days=60,
            min_history=10,
            point_in_time=True,
            require_security_master=True,
        )


def test_point_in_time_panel_rejects_untrusted_lifecycle_snapshot(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(scorer, "_DATA_DIR", daily_dir)
    master_path = tmp_path / "security_master.csv"
    monkeypatch.setattr(scorer, "_SECURITY_MASTER_PATH", master_path)
    _daily_frame("2026-01-01", 70).to_csv(
        daily_dir / "000001.csv",
        index=False,
    )
    pd.DataFrame(
        [
            {
                "code": "000001",
                "list_date": "20200101",
                "delist_date": "",
                "list_status": "L",
                "lifecycle_snapshot_complete": False,
            }
        ]
    ).to_csv(master_path, index=False)

    with pytest.raises(ValueError, match="security master lifecycle snapshot is untrusted"):
        scorer.build_panel_from_cache(
            n_stocks=1,
            days=60,
            min_history=10,
            point_in_time=True,
            require_security_master=True,
        )


@pytest.mark.parametrize(
    ("audit_field", "audit_value"),
    [
        ("security_master_pagination_complete", False),
        ("security_master_snapshot_trade_date", "20260101"),
        ("lifecycle_source", "tushare_stock_basic"),
        ("universe_scope", "a_share"),
    ],
)
def test_point_in_time_panel_rejects_untrusted_master_audit(
    tmp_path,
    monkeypatch,
    audit_field,
    audit_value,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    monkeypatch.setattr(scorer, "_DATA_DIR", daily_dir)
    master_path = tmp_path / "security_master.csv"
    monkeypatch.setattr(scorer, "_SECURITY_MASTER_PATH", master_path)
    for code in ("000001", "000002"):
        _daily_frame("2026-01-01", 70).to_csv(
            daily_dir / f"{code}.csv",
            index=False,
        )
    rows = [
        {
            "code": "000001",
            "list_date": "20200101",
            "delist_date": "",
            "list_status": "L",
            "lifecycle_snapshot_complete": True,
            "lifecycle_source": "baostock_stock_basic",
            "universe_scope": "sh_sz_a_share",
            "snapshot_at": "2026-07-30T17:10:00",
        },
        {
            "code": "000002",
            "list_date": "20200101",
            "delist_date": "20260301",
            "list_status": "D",
            "lifecycle_snapshot_complete": True,
            "lifecycle_source": "baostock_stock_basic",
            "universe_scope": "sh_sz_a_share",
            "snapshot_at": "2026-07-30T17:10:00",
        },
    ]
    audit = _trusted_master_audit_fields(len(rows))
    audit[audit_field] = audit_value
    pd.DataFrame(rows).assign(**audit).to_csv(master_path, index=False)

    with pytest.raises(
        ValueError,
        match="security master lifecycle snapshot is untrusted",
    ):
        scorer.build_panel_from_cache(
            n_stocks=1,
            days=60,
            min_history=10,
            point_in_time=True,
            require_security_master=True,
        )


def test_build_panel_from_cache_preserves_point_in_time_st_status_without_filling(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)
    master_path = tmp_path / "security_master.csv"
    monkeypatch.setattr(scorer, "_SECURITY_MASTER_PATH", master_path)
    pd.DataFrame(
        [
            {"code": "000001", "list_date": "20200101", "delist_date": ""},
            {"code": "000002", "list_date": "20200101", "delist_date": ""},
        ]
    ).to_csv(master_path, index=False)
    explicit = _daily_frame("2026-01-01", 70)
    explicit["is_st"] = 0
    explicit.loc[len(explicit) - 2, "is_st"] = 1
    explicit.loc[len(explicit) - 1, "is_st"] = None
    named = _daily_frame("2026-01-01", 70)
    named["name"] = "平安银行"
    named.loc[len(named) - 2, "name"] = "*ST测试"
    named.loc[len(named) - 1, "name"] = ""
    explicit.to_csv(tmp_path / "000001.csv", index=False)
    named.to_csv(tmp_path / "000002.csv", index=False)

    panel, codes = scorer.build_panel_from_cache(
        n_stocks=None,
        days=60,
        min_history=10,
        point_in_time=True,
    )

    st_date = pd.Timestamp(explicit.loc[len(explicit) - 2, "date"])
    unknown_date = pd.Timestamp(explicit.loc[len(explicit) - 1, "date"])
    assert codes == ["000001", "000002"]
    assert bool(panel["is_st"].loc[st_date, "000001"]) is True
    assert bool(panel["is_st"].loc[st_date, "000002"]) is True
    assert bool(panel["st_status_known"].loc[st_date, "000001"]) is True
    assert bool(panel["st_status_known"].loc[st_date, "000002"]) is True
    assert bool(panel["st_status_known"].loc[unknown_date, "000001"]) is False
    assert bool(panel["st_status_known"].loc[unknown_date, "000002"]) is False


def test_build_panel_from_cache_uses_historical_name_intervals(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)
    master_path = tmp_path / "security_master.csv"
    monkeypatch.setattr(scorer, "_SECURITY_MASTER_PATH", master_path)
    pd.DataFrame(
        [{"code": "000010", "list_date": "20200101", "delist_date": ""}]
    ).to_csv(master_path, index=False)
    frame = _daily_frame("2026-01-01", 70)
    frame["is_st"] = 1
    frame["name"] = "*ST current snapshot"
    frame.to_csv(tmp_path / "000010.csv", index=False)
    st_date = pd.Timestamp(frame.loc[len(frame) - 2, "date"])
    previous_date = pd.Timestamp(frame.loc[len(frame) - 3, "date"])
    monkeypatch.setattr(
        st_status_history,
        "load_cache",
        lambda: {
            "codes": {
                "000010": {
                    "records": [
                        {
                            "name": "美丽生态",
                            "start_date": "20260101",
                            "end_date": previous_date.strftime("%Y%m%d"),
                            "ann_date": "20260101",
                        },
                        {
                            "name": "*ST美丽",
                            "start_date": st_date.strftime("%Y%m%d"),
                            "end_date": "",
                            "ann_date": st_date.strftime("%Y%m%d"),
                        },
                    ]
                }
            }
        },
    )

    panel, _codes = scorer.build_panel_from_cache(
        n_stocks=1,
        days=60,
        min_history=10,
        point_in_time=True,
    )

    assert bool(panel["is_st"].loc[previous_date, "000010"]) is False
    assert bool(panel["is_st"].loc[st_date, "000010"]) is True
    assert bool(panel["st_status_known"].loc[previous_date, "000010"]) is True
    assert bool(panel["st_status_known"].loc[st_date, "000010"]) is True


def test_historical_st_unknown_is_not_overwritten_by_current_csv_name():
    dates = pd.to_datetime(["2026-07-10", "2026-07-11", "2026-07-12"])
    frame = pd.DataFrame(
        {
            "name": ["*ST current snapshot"] * 3,
            "is_st": [1, 1, 1],
        },
        index=dates,
    )
    history_cache = {
        "codes": {
            "000010": {
                "records": [
                    {
                        "name": "*ST美丽",
                        "start_date": "20260711",
                        "end_date": "",
                        "ann_date": "20260712",
                    }
                ]
            }
        }
    }

    is_st, known = scorer._st_status_from_cache(
        frame,
        code="000010",
        history_cache=history_cache,
    )

    assert known.astype(bool).tolist() == [False, False, True]
    assert is_st.astype(bool).tolist() == [False, False, True]


def test_merge_kline_cache_preserves_amount_and_st_fields_on_ohlcv_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, "DATA_DIR", tmp_path)
    initial = _daily_frame("2026-07-01", 2)
    initial["name"] = ["测试股份", "*ST测试"]
    initial["is_st"] = [0, 1]
    first = dm.merge_kline_cache("000001", initial, update_metadata=False)
    refresh = initial.tail(1).drop(columns=["amount", "name", "is_st"]).copy()
    refresh["close"] = refresh["close"].astype(float) + 0.1

    merged = dm.merge_kline_cache("000001", refresh, update_metadata=False)
    saved = pd.read_csv(tmp_path / "000001.csv")

    assert {"amount", "name", "is_st"}.issubset(merged.columns)
    assert float(merged.iloc[-1]["amount"]) == float(first.iloc[-1]["amount"])
    assert merged.iloc[-1]["name"] == "*ST测试"
    assert int(merged.iloc[-1]["is_st"]) == 1
    assert {"amount", "name", "is_st"}.issubset(saved.columns)
