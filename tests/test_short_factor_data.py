from __future__ import annotations

import json

import pandas as pd
import pytest

from analytics import short_factor_data


def test_codes_accepts_baostock_exchange_prefix():
    assert short_factor_data._codes(
        ["sz.000001", "sh.600000"]
    ) == ["000001", "600000"]


def test_baostock_blank_turnover_is_a_missing_day_not_code_failure():
    assert short_factor_data._baostock_turnover_value("") is None
    assert short_factor_data._baostock_turnover_value("1.2500") == 1.25
    with pytest.raises(ValueError):
        short_factor_data._baostock_turnover_value("-1")


def _history_rows(codes, start, end, kind):
    rows = []
    for code in codes:
        base = {
            "code": str(code).zfill(6),
            "date": pd.Timestamp(end),
        }
        if kind == "daily_basic":
            base.update(
                {
                    "turnover_rate": 1.0,
                    "turnover_rate_f": 1.2,
                    "volume_ratio": 0.9,
                    "float_share": 1000.0,
                    "total_share": 1500.0,
                    "float_mv": 2000.0,
                    "circ_mv": 1800.0,
                }
            )
        else:
            base.update(
                {
                    "auction_volume": 100.0,
                    "auction_price": 10.0,
                    "auction_amount": 1000.0,
                    "pre_close": 9.8,
                    "auction_turnover_rate": 0.1,
                    "auction_volume_ratio": 1.1,
                    "float_share": 1000.0,
                }
            )
        rows.append(base)
    return pd.DataFrame(rows)


def test_refresh_auxiliary_history_is_batched_and_resumable(
    tmp_path,
    monkeypatch,
):
    calls = []

    def fake_daily(codes, start, end, **kwargs):
        calls.append(("daily_basic", list(codes), start, end, kwargs))
        return _history_rows(codes, start, end, "daily_basic")

    def fake_auction(codes, start, end, **kwargs):
        calls.append(("auction", list(codes), start, end, kwargs))
        return _history_rows(codes, start, end, "auction")

    monkeypatch.setattr(
        short_factor_data.agudata_provider,
        "fetch_daily_basic_history",
        fake_daily,
    )
    monkeypatch.setattr(
        short_factor_data.agudata_provider,
        "fetch_auction_history",
        fake_auction,
    )
    codes = [f"{value:06d}" for value in range(1, 52)]

    first = short_factor_data.refresh_agudata_auxiliary_history(
        codes,
        "2026-01-01",
        "2026-07-31",
        root=tmp_path,
        api_key="key",
        base_url="https://data.example/openapi/v1",
        batch_size=50,
    )

    assert first["ok"] is True
    assert [
        (kind, len(batch))
        for kind, batch, *_ in calls
    ] == [
        ("daily_basic", 50),
        ("daily_basic", 1),
        ("auction", 50),
        ("auction", 1),
    ]
    assert first["daily_basic_rows"] == 51
    assert first["auction_rows"] == 51
    assert (tmp_path / "daily_basic.csv").is_file()
    assert (tmp_path / "auction.csv").is_file()

    calls.clear()
    second = short_factor_data.refresh_agudata_auxiliary_history(
        codes,
        "2026-01-01",
        "2026-07-31",
        root=tmp_path,
        api_key="key",
        base_url="https://data.example/openapi/v1",
        batch_size=50,
    )

    assert second["ok"] is True
    assert calls == []
    manifest = json.loads(
        (tmp_path / "coverage.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == 1
    assert manifest["daily_basic"]["000001"] == [
        {"start": "20260101", "end": "20260731"}
    ]


def test_refresh_auxiliary_history_fetches_only_new_date_gap(
    tmp_path,
    monkeypatch,
):
    calls = []

    def fake_daily(codes, start, end, **kwargs):
        calls.append(("daily_basic", list(codes), start, end))
        return _history_rows(codes, start, end, "daily_basic")

    def fake_auction(codes, start, end, **kwargs):
        calls.append(("auction", list(codes), start, end))
        return _history_rows(codes, start, end, "auction")

    monkeypatch.setattr(
        short_factor_data.agudata_provider,
        "fetch_daily_basic_history",
        fake_daily,
    )
    monkeypatch.setattr(
        short_factor_data.agudata_provider,
        "fetch_auction_history",
        fake_auction,
    )
    codes = ["000001", "600000"]
    short_factor_data.refresh_agudata_auxiliary_history(
        codes,
        "2026-01-01",
        "2026-07-30",
        root=tmp_path,
        api_key="key",
    )
    calls.clear()

    short_factor_data.refresh_agudata_auxiliary_history(
        codes,
        "2026-01-02",
        "2026-07-31",
        root=tmp_path,
        api_key="key",
    )

    assert calls == [
        (
            "daily_basic",
            ["000001", "600000"],
            "20260731",
            "20260731",
        ),
        (
            "auction",
            ["000001", "600000"],
            "20260731",
            "20260731",
        ),
    ]
    manifest = json.loads(
        (tmp_path / "coverage.json").read_text(encoding="utf-8")
    )
    assert manifest["daily_basic"]["000001"] == [
        {"start": "20260101", "end": "20260731"}
    ]


def test_load_auxiliary_history_returns_fixed_empty_frames(tmp_path):
    loaded = short_factor_data.load_auxiliary_history(root=tmp_path)

    assert loaded["daily_basic"].empty
    assert loaded["auction"].empty
    assert list(loaded["daily_basic"].columns) == [
        "code",
        "date",
        "turnover_rate",
        "turnover_rate_f",
        "volume_ratio",
        "float_share",
        "total_share",
        "float_mv",
        "circ_mv",
    ]


def test_refresh_baostock_turnover_history_is_incremental(
    tmp_path,
    monkeypatch,
):
    calls = []

    def fake_fetch(codes, start, end):
        calls.append((list(codes), start, end))
        rows = [
            {
                "code": code,
                "date": pd.Timestamp(end),
                "turnover_rate": 1.5,
            }
            for code in codes
        ]
        return {
            "frame": pd.DataFrame(rows),
            "successful_codes": list(codes),
            "errors": [],
        }

    monkeypatch.setattr(
        short_factor_data,
        "_fetch_baostock_turnover_batch",
        fake_fetch,
    )

    first = short_factor_data.refresh_baostock_turnover_history(
        ["000001", "600000"],
        "20260701",
        "20260730",
        root=tmp_path,
    )
    second = short_factor_data.refresh_baostock_turnover_history(
        ["000001", "600000"],
        "20260701",
        "20260731",
        root=tmp_path,
    )

    assert first["coverage"] == 1.0
    assert second["coverage"] == 1.0
    assert calls == [
        (["000001", "600000"], "20260701", "20260730"),
        (["000001", "600000"], "20260731", "20260731"),
    ]
    stored = pd.read_csv(tmp_path / "baostock_turnover.csv")
    assert len(stored) == 4


def test_load_auxiliary_history_fills_turnover_from_baostock(
    tmp_path,
):
    daily = pd.DataFrame(
        [
            {
                "code": "000001",
                "date": "2026-07-31",
                "turnover_rate": 2.0,
            }
        ]
    )
    fallback = pd.DataFrame(
        [
            {
                "code": "000001",
                "date": "2026-07-31",
                "turnover_rate": 9.0,
            },
            {
                "code": "600000",
                "date": "2026-07-31",
                "turnover_rate": 1.5,
            },
        ]
    )
    daily.to_csv(tmp_path / "daily_basic.csv", index=False)
    fallback.to_csv(
        tmp_path / "baostock_turnover.csv",
        index=False,
    )

    loaded = short_factor_data.load_auxiliary_history(root=tmp_path)
    values = loaded["daily_basic"].set_index("code")[
        "turnover_rate"
    ]

    assert values.loc["000001"] == 2.0
    assert values.loc["600000"] == 1.5


def test_load_auxiliary_history_filters_scope_with_chunked_reads(
    tmp_path,
    monkeypatch,
):
    rows = [
        {
            "code": "000001",
            "date": "2026-07-30",
            "turnover_rate": 1.0,
        },
        {
            "code": "000001",
            "date": "2026-07-31",
            "turnover_rate": 1.1,
        },
        {
            "code": "000001",
            "date": "2026-08-01",
            "turnover_rate": 1.2,
        },
        {
            "code": "600000",
            "date": "2026-07-31",
            "turnover_rate": 2.0,
        },
    ]
    daily_basic = pd.DataFrame(rows)
    for column in short_factor_data.DAILY_BASIC_COLUMNS:
        if column not in daily_basic:
            daily_basic[column] = pd.NA
    daily_basic.to_csv(tmp_path / "daily_basic.csv", index=False)
    pd.DataFrame(rows)[
        short_factor_data.BAOSTOCK_TURNOVER_COLUMNS
    ].to_csv(tmp_path / "baostock_turnover.csv", index=False)
    auction = pd.DataFrame(rows).rename(
        columns={"turnover_rate": "auction_turnover_rate"}
    )
    for column in short_factor_data.AUCTION_COLUMNS:
        if column not in auction:
            auction[column] = pd.NA
    auction.to_csv(tmp_path / "auction.csv", index=False)

    chunksizes = []
    original_read_csv = pd.read_csv

    def tracked_read_csv(*args, **kwargs):
        chunksizes.append(kwargs.get("chunksize"))
        return original_read_csv(*args, **kwargs)

    monkeypatch.setattr(short_factor_data.pd, "read_csv", tracked_read_csv)

    loaded = short_factor_data.load_auxiliary_history(
        root=tmp_path,
        codes=["000001"],
        start_date="20260731",
        end_date="20260731",
    )

    assert chunksizes == [50_000, 50_000, 50_000]
    assert loaded["daily_basic"][["code", "date"]].to_dict("records") == [
        {"code": "000001", "date": pd.Timestamp("2026-07-31")}
    ]
    assert loaded["auction"][["code", "date"]].to_dict("records") == [
        {"code": "000001", "date": pd.Timestamp("2026-07-31")}
    ]
