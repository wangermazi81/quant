import sys
import types
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

import analytics.tushare_provider as tp


def _complete_baostock_lifecycle() -> list[dict]:
    return [
        {
            "code": "000001",
            "ts_code": "000001.SZ",
            "name": "SZ Listed",
            "list_date": "19910403",
            "delist_date": "",
            "list_status": "L",
            "exchange": "SZ",
        },
        {
            "code": "000002",
            "ts_code": "000002.SZ",
            "name": "SZ Delisted",
            "list_date": "19910129",
            "delist_date": "20200101",
            "list_status": "D",
            "exchange": "SZ",
        },
        {
            "code": "600000",
            "ts_code": "600000.SH",
            "name": "SH Listed",
            "list_date": "19991110",
            "delist_date": "",
            "list_status": "L",
            "exchange": "SH",
        },
        {
            "code": "600001",
            "ts_code": "600001.SH",
            "name": "SH Delisted",
            "list_date": "19980122",
            "delist_date": "20091229",
            "list_status": "D",
            "exchange": "SH",
        },
    ]


def test_fetch_security_master_uses_baostock_lifecycle_even_with_tushare_delisted(
    monkeypatch,
):
    lifecycle_calls = []

    def fake_query(api_name, params, **kwargs):
        status = params["list_status"]
        rows = {
            "L": [
                {
                    "ts_code": "000001.SZ",
                    "name": "Tushare Listed",
                    "industry": "Bank",
                    "list_status": "L",
                },
                {
                    "ts_code": "430047.BJ",
                    "name": "BJ Profile",
                    "list_status": "L",
                },
            ],
            "D": [
                {
                    "ts_code": "600001.SH",
                    "name": "Tushare Delisted",
                    "industry": "Steel",
                    "list_status": "D",
                    "delist_date": "20091229",
                }
            ],
            "P": [],
        }[status]
        return rows

    def fake_lifecycle():
        lifecycle_calls.append(True)
        return _complete_baostock_lifecycle()

    monkeypatch.setattr(tp, "query_replay_api", fake_query)
    monkeypatch.setattr(
        tp,
        "_fetch_baostock_security_lifecycle",
        fake_lifecycle,
    )
    monkeypatch.setattr(
        tp,
        "fetch_trade_calendar",
        lambda *args, **kwargs: ["2026-07-30"],
    )

    rows = tp.fetch_security_master(token="tok", mode="replay")
    by_code = {row["code"]: row for row in rows}

    assert len(lifecycle_calls) == 1
    assert set(by_code) == {"000001", "000002", "600000", "600001"}
    assert by_code["000001"]["industry"] == "Bank"
    assert by_code["600001"]["industry"] == "Steel"
    assert by_code["600001"]["list_date"] == "19980122"
    assert by_code["600001"]["delist_date"] == "20091229"
    assert "430047" not in by_code
    assert all(
        row["lifecycle_source"] == "baostock_stock_basic"
        for row in rows
    )
    assert all(row["universe_scope"] == "sh_sz_a_share" for row in rows)


def test_fetch_security_master_fails_closed_when_baostock_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(
        tp,
        "query_replay_api",
        lambda api_name, params, **kwargs: [
            {
                "ts_code": "600001.SH",
                "name": "Tushare Delisted",
                "list_status": "D",
                "delist_date": "20091229",
            }
        ],
    )
    monkeypatch.setattr(
        tp,
        "_fetch_baostock_security_lifecycle",
        lambda: (_ for _ in ()).throw(
            RuntimeError("baostock lifecycle unavailable")
        ),
    )
    monkeypatch.setattr(
        tp,
        "fetch_trade_calendar",
        lambda *args, **kwargs: ["2026-07-30"],
    )

    with pytest.raises(RuntimeError, match="baostock lifecycle"):
        tp.fetch_security_master(token="tok", mode="replay")


@pytest.mark.parametrize(
    "invalid_rows",
    [
        [
            row
            for row in _complete_baostock_lifecycle()
            if not (
                row["exchange"] == "SH"
                and row["list_status"] == "D"
            )
        ],
        [
            *_complete_baostock_lifecycle(),
            _complete_baostock_lifecycle()[0],
        ],
        [
            {
                **row,
                "delist_date": "19900101",
            }
            if row["code"] == "600001"
            else row
            for row in _complete_baostock_lifecycle()
        ],
    ],
    ids=[
        "missing_exchange_status",
        "duplicate_code",
        "delist_before_list",
    ],
)
def test_baostock_lifecycle_validation_rejects_incomplete_or_invalid_rows(
    invalid_rows,
):
    with pytest.raises(RuntimeError, match="baostock lifecycle"):
        tp._validate_baostock_security_lifecycle(invalid_rows)


def test_fetch_security_master_uses_lifecycle_statuses_and_optional_profiles(
    monkeypatch,
):
    calls = []
    codes = {"L": "000001", "D": "600001", "P": "600000"}

    def fake_query(api_name, params, **kwargs):
        calls.append((api_name, dict(params)))
        status = params["list_status"]
        code = codes[status]
        return [
            {
                "ts_code": f"{code}.SZ",
                "symbol": code,
                "name": status,
                "list_date": "20200101",
                "delist_date": "20260301" if status == "D" else "",
                "list_status": status,
            }
        ]

    monkeypatch.setattr(tp, "query_replay_api", fake_query)
    monkeypatch.setattr(
        tp,
        "_fetch_baostock_security_lifecycle",
        _complete_baostock_lifecycle,
    )
    monkeypatch.setattr(
        tp,
        "fetch_trade_calendar",
        lambda *args, **kwargs: ["2026-07-30"],
    )

    rows = tp.fetch_security_master(token="tok", mode="replay")

    assert [
        (params["list_status"], params["limit"], params["offset"])
        for _api, params in calls
    ] == [
        ("L", 1000, 0),
        ("L", 2000, 0),
        ("D", 1000, 0),
        ("D", 2000, 0),
        ("P", 1000, 0),
        ("P", 2000, 0),
    ]
    assert all(api_name == "stock_basic" for api_name, _params in calls)
    assert {row["list_status"] for row in rows} == {"L", "D"}
    by_code = {row["code"]: row for row in rows}
    assert by_code["600000"]["list_status"] == "L"
    assert by_code["600000"]["name"] == "P"
    assert by_code["600001"]["delist_date"] == "20091229"
    assert by_code["600001"]["list_date"] == "19980122"
    assert all(row.get("security_master_snapshot_complete") is True for row in rows)
    assert all(row.get("security_master_page_requests") == 1 for row in rows)
    assert all(
        row.get("tushare_profile_page_requests") == 6
        for row in rows
    )
    assert {
        row.get("security_master_snapshot_trade_date")
        for row in rows
    } == {"20260730"}


def test_fetch_security_master_uses_baostock_lifecycle_when_replay_has_no_delisted(
    monkeypatch,
):
    def fake_query(api_name, params, **kwargs):
        if params["list_status"] != "L" or params.get("offset", 0):
            return []
        return [
            {
                "ts_code": "000001.SZ",
                "symbol": "000001",
                "name": "Ping An Bank",
                "industry": "Bank",
                "list_date": "19910403",
                "delist_date": "",
                "list_status": "L",
            },
            {
                "ts_code": "430047.BJ",
                "symbol": "430047",
                "name": "BJ Stock",
                "list_date": "20211115",
                "delist_date": "",
                "list_status": "L",
            },
        ]

    monkeypatch.setattr(tp, "query_replay_api", fake_query)
    monkeypatch.setattr(
        tp,
        "_fetch_baostock_security_lifecycle",
        _complete_baostock_lifecycle,
    )
    monkeypatch.setattr(
        tp,
        "fetch_trade_calendar",
        lambda *args, **kwargs: ["2026-07-30"],
    )

    rows = tp.fetch_security_master(token="tok", mode="replay")
    by_code = {row["code"]: row for row in rows}

    assert set(by_code) == {"000001", "000002", "600000", "600001"}
    assert by_code["000001"]["industry"] == "Bank"
    assert by_code["600001"]["delist_date"] == "20091229"
    assert {row["list_status"] for row in rows} == {"L", "D"}
    assert all(row["lifecycle_snapshot_complete"] is True for row in rows)
    assert all(row["lifecycle_source"] == "baostock_stock_basic" for row in rows)
    assert all(row["universe_scope"] == "sh_sz_a_share" for row in rows)


def test_fetch_security_master_rejects_missing_independent_lifecycle(
    monkeypatch,
):
    monkeypatch.setattr(
        tp,
        "query_replay_api",
        lambda api_name, params, **kwargs: (
            [
                {
                    "ts_code": "000001.SZ",
                    "symbol": "000001",
                    "name": "Ping An Bank",
                    "list_date": "19910403",
                    "delist_date": "",
                    "list_status": "L",
                }
            ]
            if params["list_status"] == "L" and not params.get("offset", 0)
            else []
        ),
    )
    monkeypatch.setattr(
        tp,
        "_fetch_baostock_security_lifecycle",
        lambda: [],
    )
    monkeypatch.setattr(
        tp,
        "fetch_trade_calendar",
        lambda *args, **kwargs: ["2026-07-30"],
    )

    with pytest.raises(RuntimeError, match="baostock lifecycle"):
        tp.fetch_security_master(token="tok", mode="replay")


def test_fetch_security_master_expands_limit_until_snapshot_stabilizes(
    monkeypatch,
):
    calls = []
    available = [
        {
            "ts_code": f"00000{index}.SZ",
            "symbol": f"00000{index}",
            "name": f"Stock {index}",
            "list_date": "19910703",
            "list_status": "L",
        }
        for index in range(1, 6)
    ]

    def fake_query(api_name, params, **kwargs):
        calls.append(dict(params))
        return available[: params["limit"]]

    monkeypatch.setattr(tp, "STOCK_BASIC_PAGE_SIZE", 2, raising=False)
    monkeypatch.setattr(tp, "STOCK_BASIC_MAX_LIMIT", 16, raising=False)
    monkeypatch.setattr(tp, "query_replay_api", fake_query)
    monkeypatch.setattr(
        tp,
        "_fetch_baostock_security_lifecycle",
        _complete_baostock_lifecycle,
    )
    monkeypatch.setattr(
        tp,
        "fetch_trade_calendar",
        lambda *args, **kwargs: ["2026-07-30"],
    )

    rows = tp.fetch_security_master(
        token="tok",
        mode="replay",
        statuses=("L",),
    )

    assert [call.get("offset") for call in calls] == [0, 0, 0, 0]
    assert [call.get("limit") for call in calls] == [2, 4, 8, 16]
    assert [row["code"] for row in rows] == [
        "000001",
        "000002",
        "600000",
        "600001",
    ]
    assert all(row.get("security_master_pagination_complete") is True for row in rows)
    assert all(row.get("security_master_page_requests") == 1 for row in rows)
    assert all(
        row.get("tushare_profile_page_requests") == 4
        for row in rows
    )
    assert all(row.get("security_master_row_count") == 4 for row in rows)


def test_stable_tushare_server_cap_cannot_shrink_baostock_master(
    monkeypatch,
):
    lifecycle = [
        *_complete_baostock_lifecycle(),
        {
            "code": "000003",
            "ts_code": "000003.SZ",
            "name": "SZ Extra",
            "list_date": "19910703",
            "delist_date": "",
            "list_status": "L",
            "exchange": "SZ",
        },
        {
            "code": "600002",
            "ts_code": "600002.SH",
            "name": "SH Extra",
            "list_date": "19920327",
            "delist_date": "",
            "list_status": "L",
            "exchange": "SH",
        },
    ]
    profile_codes = [
        "000001",
        "000002",
        "000003",
        "600000",
        "600001",
    ]
    profiles = [
        {
            "ts_code": (
                f"{code}.SH"
                if code.startswith("6")
                else f"{code}.SZ"
            ),
            "name": f"Profile {code}",
            "list_status": "L",
        }
        for code in profile_codes
    ]
    calls = []

    def fake_query(api_name, params, **kwargs):
        calls.append(params["limit"])
        return profiles[: min(params["limit"], 5)]

    monkeypatch.setattr(tp, "STOCK_BASIC_PAGE_SIZE", 2, raising=False)
    monkeypatch.setattr(tp, "STOCK_BASIC_MAX_LIMIT", 16, raising=False)
    monkeypatch.setattr(tp, "query_replay_api", fake_query)
    monkeypatch.setattr(
        tp,
        "_fetch_baostock_security_lifecycle",
        lambda: lifecycle,
    )
    monkeypatch.setattr(
        tp,
        "fetch_trade_calendar",
        lambda *args, **kwargs: ["2026-07-30"],
    )

    rows = tp.fetch_security_master(
        token="tok",
        mode="replay",
        statuses=("L",),
    )

    assert calls == [2, 4, 8, 16]
    assert {row["code"] for row in rows} == {
        "000001",
        "000002",
        "000003",
        "600000",
        "600001",
        "600002",
    }
    extra = next(row for row in rows if row["code"] == "600002")
    assert extra["source"] == "baostock_stock_basic"
    assert extra["name"] == "SH Extra"
    assert all(
        row["tushare_profile_snapshot_complete"] is False
        for row in rows
    )


def test_fetch_stock_basic_expands_limit_when_replay_ignores_offset(monkeypatch):
    rows = [
        {
            "ts_code": f"00000{index}.SZ",
            "symbol": f"00000{index}",
            "name": f"Stock {index}",
            "list_date": "20200101",
            "list_status": "L",
        }
        for index in range(1, 6)
    ]
    calls = []

    def fake_query(api_name, params, **kwargs):
        calls.append(dict(params))
        return rows[: params["limit"]]

    monkeypatch.setattr(tp, "STOCK_BASIC_PAGE_SIZE", 2, raising=False)
    monkeypatch.setattr(tp, "STOCK_BASIC_MAX_LIMIT", 16, raising=False)
    monkeypatch.setattr(tp, "query_replay_api", fake_query)

    fetched, request_count = tp._fetch_stock_basic_pages(
        "L",
        "ts_code,symbol,name,list_date,list_status",
        "tok",
        "replay",
    )

    assert [row["symbol"] for row in fetched] == [
        "000001",
        "000002",
        "000003",
        "000004",
        "000005",
    ]
    assert [call["limit"] for call in calls] == [2, 4, 8, 16]
    assert all(call.get("offset", 0) == 0 for call in calls)
    assert request_count == 4


def test_fetch_stock_basic_rejects_repeated_full_page_when_limit_grows(
    monkeypatch,
):
    page = [
        {
            "ts_code": "000001.SZ",
            "symbol": "000001",
            "name": "One",
            "list_date": "19910403",
            "list_status": "L",
        },
        {
            "ts_code": "000002.SZ",
            "symbol": "000002",
            "name": "Two",
            "list_date": "19910129",
            "list_status": "L",
        },
    ]

    monkeypatch.setattr(tp, "STOCK_BASIC_PAGE_SIZE", 2, raising=False)
    monkeypatch.setattr(
        tp,
        "query_replay_api",
        lambda api_name, params, **kwargs: page,
    )
    with pytest.raises(RuntimeError, match="stock_basic pagination"):
        tp._fetch_stock_basic_pages(
            "L",
            "ts_code,symbol,name,list_date,list_status",
            "tok",
            "replay",
        )


def test_fetch_stock_basic_rejects_duplicate_code_within_page(monkeypatch):
    page = [
        {
            "ts_code": "000001.SZ",
            "symbol": "000001",
            "name": "One",
            "list_date": "19910403",
            "list_status": "L",
        },
        {
            "ts_code": "000001.SZ",
            "symbol": "000001",
            "name": "Duplicate One",
            "list_date": "19910403",
            "list_status": "L",
        },
    ]

    monkeypatch.setattr(tp, "STOCK_BASIC_PAGE_SIZE", 5, raising=False)
    monkeypatch.setattr(
        tp,
        "query_replay_api",
        lambda api_name, params, **kwargs: page,
    )
    with pytest.raises(RuntimeError, match="stock_basic pagination"):
        tp._fetch_stock_basic_pages(
            "L",
            "ts_code,symbol,name,list_date,list_status",
            "tok",
            "replay",
        )


def test_fetch_security_master_ignores_duplicate_optional_profiles_across_statuses(
    monkeypatch,
):
    def fake_query(api_name, params, **kwargs):
        if params.get("offset", 0):
            return []
        status = params["list_status"]
        return [
            {
                "ts_code": "000001.SZ",
                "symbol": "000001",
                "name": status,
                "list_date": "19910403",
                "delist_date": "20200101" if status == "D" else "",
                "list_status": status,
            }
        ]

    monkeypatch.setattr(tp, "query_replay_api", fake_query)
    monkeypatch.setattr(
        tp,
        "_fetch_baostock_security_lifecycle",
        _complete_baostock_lifecycle,
    )
    monkeypatch.setattr(
        tp,
        "fetch_trade_calendar",
        lambda *args, **kwargs: ["2026-07-30"],
    )

    rows = tp.fetch_security_master(
        token="tok",
        mode="replay",
        statuses=("L", "D"),
    )

    assert {row["code"] for row in rows} == {
        "000001",
        "000002",
        "600000",
        "600001",
    }
    assert all(row["lifecycle_snapshot_complete"] is True for row in rows)


def test_fetch_security_master_uses_latest_completed_trade_day_for_snapshot(
    monkeypatch,
):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 31, 14, 59, 0)

    calendar_calls = []

    def fake_calendar(start_date, end_date, **kwargs):
        calendar_calls.append((start_date, end_date))
        return ["2026-07-29", "2026-07-30"]

    def fake_query(api_name, params, **kwargs):
        if params.get("offset", 0):
            return []
        status = params["list_status"]
        return [
            {
                "ts_code": "000001.SZ" if status == "L" else "600001.SH",
                "symbol": "000001" if status == "L" else "600001",
                "name": status,
                "list_date": "19910403",
                "delist_date": "20200101" if status == "D" else "",
                "list_status": status,
            }
        ]

    monkeypatch.setattr(tp, "datetime", FrozenDatetime)
    monkeypatch.setattr(tp, "fetch_trade_calendar", fake_calendar)
    monkeypatch.setattr(tp, "query_replay_api", fake_query)
    monkeypatch.setattr(
        tp,
        "_fetch_baostock_security_lifecycle",
        _complete_baostock_lifecycle,
    )

    rows = tp.fetch_security_master(
        token="tok",
        mode="replay",
        statuses=("L", "D"),
    )

    assert calendar_calls
    assert calendar_calls[0][1] == "20260730"
    assert {
        row.get("security_master_snapshot_trade_date")
        for row in rows
    } == {"20260730"}
    assert all(row.get("security_master_snapshot_complete") is True for row in rows)


def test_fetch_security_master_fails_closed_without_completed_trade_day(
    monkeypatch,
):
    from market import trading_calendar

    monkeypatch.setattr(
        trading_calendar,
        "has_verified_calendar",
        lambda year: False,
    )
    monkeypatch.setattr(
        trading_calendar,
        "authoritative_year",
        lambda year: None,
    )
    monkeypatch.setattr(
        tp,
        "query_replay_api",
        lambda api_name, params, **kwargs: [
            {
                "ts_code": "000001.SZ",
                "symbol": "000001",
                "name": "One",
                "list_date": "19910403",
                "list_status": params["list_status"],
            }
        ],
    )
    monkeypatch.setattr(tp, "fetch_trade_calendar", lambda *args, **kwargs: [])
    monkeypatch.setattr(tp, "_fetch_baostock_security_lifecycle", lambda: [])

    with pytest.raises(RuntimeError, match="latest completed trade date"):
        tp.fetch_security_master(
            token="tok",
            mode="replay",
            statuses=("L",),
        )



def test_fetch_security_master_falls_back_to_verified_local_calendar_when_remote_calendar_fails(
    monkeypatch,
):
    from market import trading_calendar

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 4, 16, 0, 0)

    monkeypatch.setattr(tp, "datetime", FrozenDatetime)
    monkeypatch.setattr(
        tp,
        "fetch_trade_calendar",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("403 Forbidden")
        ),
    )
    monkeypatch.setattr(tp, "query_replay_api", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        tp,
        "_fetch_baostock_security_lifecycle",
        _complete_baostock_lifecycle,
    )
    monkeypatch.setattr(
        trading_calendar,
        "has_verified_calendar",
        lambda year: int(year) == 2026,
    )
    monkeypatch.setattr(
        trading_calendar,
        "is_trading_day",
        lambda value: value == date(2026, 8, 4),
    )

    rows = tp.fetch_security_master(
        token="expired-token",
        mode="replay",
        statuses=(),
    )

    assert {row["security_master_snapshot_trade_date"] for row in rows} == {
        "20260804"
    }
    assert {row["security_master_calendar_source"] for row in rows} == {
        "verified_local_trading_calendar"
    }


def test_fetch_baostock_security_master_uses_only_verified_local_calendar(
    monkeypatch,
):
    from market import trading_calendar

    monkeypatch.setattr(
        tp,
        "_fetch_baostock_security_lifecycle",
        _complete_baostock_lifecycle,
    )
    monkeypatch.setattr(
        trading_calendar,
        "has_verified_calendar",
        lambda year: int(year) == 2026,
    )
    monkeypatch.setattr(
        trading_calendar,
        "is_trading_day",
        lambda value: value == date(2026, 8, 4),
    )
    monkeypatch.setattr(
        tp,
        "query_replay_api",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Baostock-only master must not call a remote profile API")
        ),
    )

    rows = tp.fetch_baostock_security_master(
        snapshot_now=datetime(2026, 8, 4, 17, 10),
    )

    assert len(rows) == 4
    assert {row["source"] for row in rows} == {"baostock_stock_basic"}
    assert {row["security_master_snapshot_trade_date"] for row in rows} == {
        "20260804"
    }
    assert {row["security_master_calendar_source"] for row in rows} == {
        "verified_local_trading_calendar"
    }


def test_local_calendar_fallback_accepts_valid_authoritative_commitment(
    monkeypatch,
):
    from market import trading_calendar

    authority = types.SimpleNamespace(
        closed_days=frozenset({"2026-10-01"}),
    )
    monkeypatch.setattr(
        trading_calendar,
        "has_verified_calendar",
        lambda year: False,
    )
    monkeypatch.setattr(
        trading_calendar,
        "authoritative_year",
        lambda year: authority if int(year) == 2026 else None,
    )
    monkeypatch.setattr(
        trading_calendar,
        "is_trading_day",
        lambda value: (_ for _ in ()).throw(
            AssertionError("mutable calendar must not be used")
        ),
    )

    trade_date = tp._latest_completed_trade_date_from_verified_local_calendar(
        datetime(2026, 8, 4, 17, 10),
    )

    assert trade_date == "20260804"


def test_fetch_security_master_fails_closed_when_remote_and_local_calendars_are_untrusted(
    monkeypatch,
):
    from market import trading_calendar

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 4, 16, 0, 0)

    monkeypatch.setattr(tp, "datetime", FrozenDatetime)
    monkeypatch.setattr(
        tp,
        "fetch_trade_calendar",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("403 Forbidden")
        ),
    )
    monkeypatch.setattr(
        trading_calendar,
        "has_verified_calendar",
        lambda year: False,
    )
    monkeypatch.setattr(
        trading_calendar,
        "authoritative_year",
        lambda year: None,
    )
    monkeypatch.setattr(
        tp,
        "_fetch_baostock_security_lifecycle",
        _complete_baostock_lifecycle,
    )

    with pytest.raises(RuntimeError, match="verified local trading calendar"):
        tp.fetch_security_master(
            token="expired-token",
            mode="replay",
            statuses=(),
        )


def test_baostock_security_lifecycle_normalizes_exchange_prefixed_codes(
    monkeypatch,
):
    class FakeResult:
        error_code = "0"
        error_msg = "success"
        fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]

        def __init__(self):
            self._rows = iter(
                [
                    ["sh.600000", "浦发银行", "1999-11-10", "", "1", "1"],
                    ["sh.600001", "邯郸钢铁", "1998-01-22", "2009-12-29", "1", "0"],
                    ["sz.000001", "SZ Listed", "1991-04-03", "", "1", "1"],
                    ["sz.000002", "SZ Delisted", "1991-01-29", "2020-01-01", "1", "0"],
                    ["sh.000001", "上证指数", "1991-07-15", "", "2", "1"],
                ]
            )
            self._current = None

        def next(self):
            try:
                self._current = next(self._rows)
                return True
            except StopIteration:
                return False

        def get_row_data(self):
            return self._current

    login_result = types.SimpleNamespace(error_code="0", error_msg="success")
    fake_bs = types.SimpleNamespace(
        login=lambda: login_result,
        logout=lambda: None,
        query_stock_basic=lambda: FakeResult(),
    )
    monkeypatch.setitem(sys.modules, "baostock", fake_bs)

    rows = tp._fetch_baostock_security_lifecycle()

    by_code = {row["code"]: row for row in rows}
    assert set(by_code) == {"000001", "000002", "600000", "600001"}
    assert by_code["600000"]["ts_code"] == "600000.SH"
    assert by_code["600001"]["list_status"] == "D"
    assert by_code["600001"]["delist_date"] == "20091229"


def test_fetch_namechange_history_normalizes_and_sorts_records(monkeypatch):
    captured = {}

    def fake_query(api_name, params, **kwargs):
        captured.update({"api_name": api_name, "params": params, **kwargs})
        return [
            {
                "ts_code": "000010.SZ",
                "name": "*ST美丽",
                "start_date": "20260711",
                "end_date": "",
                "ann_date": "20260710",
                "change_reason": "*ST",
            },
            {
                "ts_code": "000010.SZ",
                "name": "美丽生态",
                "start_date": "20200101",
                "end_date": "20260710",
                "ann_date": "20191231",
                "change_reason": "其他",
            },
        ]

    monkeypatch.setattr(tp, "query_replay_api", fake_query)

    rows = tp.fetch_namechange_history("000010", token="tok", mode="replay")

    assert captured["api_name"] == "namechange"
    assert captured["params"]["ts_code"] == "000010.SZ"
    assert rows[0]["start_date"] == "20200101"
    assert rows[-1]["name"] == "*ST美丽"


def test_fetch_kline_qfq_normalizes_pro_bar(monkeypatch):
    calls = []

    def fake_pro_bar(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame({
            "trade_date": ["20260708", "20260707"],
            "open": [10.0, 9.5],
            "high": [10.5, 9.8],
            "low": [9.8, 9.2],
            "close": [10.2, 9.6],
            "vol": [1200.0, 1000.0],
        })

    fake_api = object()
    fake_ts = types.SimpleNamespace(pro_api=lambda token: fake_api, pro_bar=fake_pro_bar)
    monkeypatch.setitem(sys.modules, "tushare", fake_ts)

    df = tp.fetch_kline_qfq("000001", "2026-07-01", "20260708", token="tok", mode="sdk")

    assert calls[0]["ts_code"] == "000001.SZ"
    assert calls[0]["api"] is fake_api
    assert calls[0]["adj"] == "qfq"
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-07-07", "2026-07-08"]
    assert df["volume"].tolist() == [1000.0, 1200.0]



def test_fetch_kline_qfq_uses_replay_http_api(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        if url.endswith("/daily"):
            return FakeResponse({
                "code": 0,
                "data": {
                    "fields": [
                        "ts_code",
                        "trade_date",
                        "open",
                        "high",
                        "low",
                        "close",
                        "vol",
                        "amount",
                        ],
                        "items": [
                            ["000001.SZ", "20240103", 9.2, 9.3, 9.1, 9.25, 1000.0, 9250.0],
                            ["000001.SZ", "20240102", 18.0, 18.8, 17.8, 18.4, 900.0, 16560.0],
                    ],
                },
            })
        return FakeResponse({
            "code": 0,
            "data": {
                "fields": ["ts_code", "trade_date", "adj_factor"],
                "items": [
                    ["000001.SZ", "20240102", 100.0],
                    ["000001.SZ", "20240103", 200.0],
                ],
            },
        })

    monkeypatch.setattr(tp.requests, "get", fake_get)

    df = tp.fetch_kline_qfq("000001", "2024-01-01", "2024-01-10", token="api-key", mode="replay")

    assert calls[0]["url"] == "https://ai-tool.indevs.in/tushare/pro/daily"
    assert calls[0]["headers"]["X-API-Key"] == "api-key"
    assert calls[0]["params"]["fields"] == (
        "ts_code,trade_date,open,high,low,close,vol,amount"
    )
    assert calls[1]["url"].endswith("/adj_factor")
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == ["2024-01-02", "2024-01-03"]
    assert df["close"].round(3).tolist() == [9.2, 9.25]
    assert df["volume"].tolist() == [900.0, 1000.0]
    assert df["amount"].tolist() == [16560.0, 9250.0]


def test_replay_qfq_preserves_same_amount_as_full_market_daily(monkeypatch):
    tp.clear_kline_cache()
    calls = []
    daily_rows = [
        {
            "ts_code": "000777.SZ",
            "trade_date": "20260729",
            "open": 40.0,
            "high": 40.8,
            "low": 39.6,
            "close": 40.4,
            "vol": 1000.0,
            "amount": 10123.5,
        },
        {
            "ts_code": "000777.SZ",
            "trade_date": "20260730",
            "open": 20.0,
            "high": 20.6,
            "low": 19.8,
            "close": 20.4,
            "vol": 2000.0,
            "amount": 40567.25,
        },
    ]

    def fake_replay_get(api_name, token, params, **kwargs):
        calls.append((api_name, dict(params)))
        if api_name == "daily":
            if params.get("trade_date"):
                return [daily_rows[-1]]
            return daily_rows
        return [
            {"ts_code": "000777.SZ", "trade_date": "20260729", "adj_factor": 100.0},
            {"ts_code": "000777.SZ", "trade_date": "20260730", "adj_factor": 200.0},
        ]

    monkeypatch.setattr(tp, "_replay_get", fake_replay_get)

    qfq = tp.fetch_kline_qfq(
        "000777",
        "20260729",
        "20260730",
        token="api-key",
        mode="replay",
    )
    full_market = tp.fetch_full_market_daily(
        "20260730",
        token="api-key",
        mode="replay",
    )

    qfq_daily_params = next(
        params
        for api_name, params in calls
        if api_name == "daily" and "start_date" in params
    )
    assert qfq_daily_params["fields"] == (
        "ts_code,trade_date,open,high,low,close,vol,amount"
    )
    assert qfq["close"].round(2).tolist() == [20.2, 20.4]
    assert qfq["amount"].tolist() == [10123.5, 40567.25]
    assert float(qfq.iloc[-1]["amount"]) == float(full_market.iloc[0]["amount"])


def test_replay_qfq_rejects_transient_corrupt_adjustment_factor(monkeypatch):
    tp.clear_kline_cache()

    def fake_replay_get(api_name, token, params, **kwargs):
        if api_name == "daily":
            return [
                {
                    "ts_code": "000014.SZ",
                    "trade_date": trade_date,
                    "open": close,
                    "high": close + 0.1,
                    "low": close - 0.1,
                    "close": close,
                    "vol": 1000.0,
                    "amount": 10000.0,
                }
                for trade_date, close in (
                    ("20260728", 10.0),
                    ("20260729", 10.1),
                    ("20260730", 10.2),
                )
            ]
        return [
            {
                "ts_code": "000014.SZ",
                "trade_date": trade_date,
                "adj_factor": factor,
            }
            for trade_date, factor in (
                ("20260728", 1.0),
                ("20260729", 10.0),
                ("20260730", 1.0),
            )
        ]

    monkeypatch.setattr(tp, "_replay_get", fake_replay_get)

    frame = tp.fetch_kline_qfq(
        "000014",
        "20260728",
        "20260730",
        token="api-key",
        mode="replay",
    )

    assert frame is None


def test_replay_qfq_rejects_daily_bars_when_adjustment_factor_fetch_fails(
    monkeypatch,
):
    tp.clear_kline_cache()

    def fake_replay_get(api_name, token, params, **kwargs):
        if api_name == "adj_factor":
            raise RuntimeError("factor source unavailable")
        return [
            {
                "ts_code": "000014.SZ",
                "trade_date": "20260730",
                "open": 10.0,
                "high": 10.2,
                "low": 9.9,
                "close": 10.1,
                "vol": 1000.0,
                "amount": 10100.0,
            }
        ]

    monkeypatch.setattr(tp, "_replay_get", fake_replay_get)

    frame = tp.fetch_kline_qfq(
        "000014",
        "20260730",
        "20260730",
        token="api-key",
        mode="replay",
    )

    assert frame is None


def test_replay_qfq_rejects_daily_bars_when_adjustment_factors_are_empty(
    monkeypatch,
):
    tp.clear_kline_cache()

    def fake_replay_get(api_name, token, params, **kwargs):
        if api_name == "adj_factor":
            return []
        return [
            {
                "ts_code": "000014.SZ",
                "trade_date": "20260730",
                "open": 10.0,
                "high": 10.2,
                "low": 9.9,
                "close": 10.1,
                "vol": 1000.0,
                "amount": 10100.0,
            }
        ]

    monkeypatch.setattr(tp, "_replay_get", fake_replay_get)

    frame = tp.fetch_kline_qfq(
        "000014",
        "20260730",
        "20260730",
        token="api-key",
        mode="replay",
    )

    assert frame is None


def test_replay_qfq_rejects_history_when_latest_daily_factor_is_missing(
    monkeypatch,
):
    tp.clear_kline_cache()

    def fake_replay_get(api_name, token, params, **kwargs):
        if api_name == "adj_factor":
            return [
                {
                    "ts_code": "000014.SZ",
                    "trade_date": "20260729",
                    "adj_factor": 2.0,
                }
            ]
        return [
            {
                "ts_code": "000014.SZ",
                "trade_date": trade_date,
                "open": close,
                "high": close + 0.2,
                "low": close - 0.1,
                "close": close,
                "vol": 1000.0,
                "amount": 10100.0,
            }
            for trade_date, close in (
                ("20260729", 10.0),
                ("20260730", 10.1),
            )
        ]

    monkeypatch.setattr(tp, "_replay_get", fake_replay_get)

    frame = tp.fetch_kline_qfq(
        "000014",
        "20260729",
        "20260730",
        token="api-key",
        mode="replay",
    )

    assert frame is None


def test_fetch_kline_qfq_chunks_long_replay_ranges_without_gaps(monkeypatch):
    tp.clear_kline_cache()
    calls = []

    def fake_replay_get(api_name, token, params, **kwargs):
        calls.append((api_name, dict(params)))
        trade_date = params["start_date"]
        if api_name == "daily":
            return [
                {
                    "ts_code": params["ts_code"],
                    "trade_date": trade_date,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "vol": 1000.0,
                }
            ]
        return [
            {
                "ts_code": params["ts_code"],
                "trade_date": trade_date,
                "adj_factor": 100.0,
            }
        ]

    monkeypatch.setattr(tp, "_replay_get", fake_replay_get)

    frame = tp.fetch_kline_qfq(
        "000001",
        "20240101",
        "20260730",
        token="tok",
        mode="replay",
    )

    daily_calls = [params for api_name, params in calls if api_name == "daily"]
    factor_calls = [params for api_name, params in calls if api_name == "adj_factor"]
    assert len(daily_calls) >= 3
    assert len(factor_calls) == len(daily_calls)
    assert daily_calls[0]["start_date"] == "20240101"
    assert daily_calls[-1]["end_date"] == "20260730"
    for current, following in zip(daily_calls, daily_calls[1:]):
        current_end = datetime.strptime(current["end_date"], "%Y%m%d")
        following_start = datetime.strptime(following["start_date"], "%Y%m%d")
        assert following_start == current_end + timedelta(days=1)
        assert current_end - datetime.strptime(current["start_date"], "%Y%m%d") <= timedelta(days=366)
    assert len(frame) == len(daily_calls)


def test_apply_qfq_rejects_missing_factor_instead_of_forward_filling():
    daily = pd.DataFrame(
        {
            "trade_date": ["20260701", "20260702", "20260703"],
            "open": [10.0, 11.0, 12.0],
            "high": [10.5, 11.5, 12.5],
            "low": [9.5, 10.5, 11.5],
            "close": [10.2, 11.2, 12.2],
            "vol": [1000.0, 1100.0, 1200.0],
        }
    )
    factors = pd.DataFrame(
        {
            "trade_date": ["20260701", "20260702"],
            "adj_factor": [100.0, 200.0],
        }
    )

    with pytest.raises(ValueError, match="one valid adjustment factor"):
        tp._apply_qfq(daily, factors)


@pytest.mark.parametrize("invalid_factor", [0.0, -1.0, float("nan"), float("inf"), "bad"])
def test_apply_qfq_rejects_non_finite_or_non_positive_factor(invalid_factor):
    daily = pd.DataFrame(
        {
            "trade_date": ["20260701", "20260702"],
            "open": [10.0, 11.0],
            "high": [10.5, 11.5],
            "low": [9.5, 10.5],
            "close": [10.2, 11.2],
            "vol": [1000.0, 1100.0],
        }
    )
    factors = pd.DataFrame(
        {
            "trade_date": ["20260701", "20260702"],
            "adj_factor": [100.0, invalid_factor],
        }
    )

    with pytest.raises(ValueError, match="one valid adjustment factor"):
        tp._apply_qfq(daily, factors)


@pytest.mark.parametrize("duplicate_factor", [100.0, 200.0])
def test_apply_qfq_rejects_duplicate_factor_date(duplicate_factor):
    daily = pd.DataFrame(
        {
            "trade_date": ["20260701", "20260702"],
            "open": [10.0, 11.0],
            "high": [10.5, 11.5],
            "low": [9.5, 10.5],
            "close": [10.2, 11.2],
            "vol": [1000.0, 1100.0],
        }
    )
    factors = pd.DataFrame(
        {
            "trade_date": ["20260701", "20260702", "20260702"],
            "adj_factor": [100.0, 100.0, duplicate_factor],
        }
    )

    with pytest.raises(ValueError, match="one valid adjustment factor"):
        tp._apply_qfq(daily, factors)


def test_test_connection_uses_daily_endpoint(monkeypatch):
    class FakePro:
        def daily(self, **kwargs):
            assert kwargs["ts_code"] == "000001.SZ"
            return pd.DataFrame({"ts_code": ["000001.SZ"]})

    fake_ts = types.SimpleNamespace(pro_api=lambda token: FakePro())
    monkeypatch.setitem(sys.modules, "tushare", fake_ts)

    result = tp.test_connection("tok", mode="sdk")

    assert result["ok"] is True
    assert result["rows"] == 1



def test_test_connection_uses_replay_mode(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 0, "data": {"fields": ["ts_code", "trade_date"], "items": [["000001.SZ", "20240102"]]}}

    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(tp.requests, "get", fake_get)

    result = tp.test_connection("api-key", mode="replay")

    assert result["ok"] is True
    assert result["mode"] == "replay"
    assert result["rows"] == 1
    assert calls[0]["headers"]["X-API-Key"] == "api-key"


def test_fetch_realtime_quotes_uses_replay_rt_tick(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "request_id": "req-1",
                "code": 0,
                "api_name": "rt_tick",
                "data": {
                    "fields": [
                        "ts_code", "name", "trade_time", "price", "pre_price",
                        "high_price", "open_price", "low_price", "volume", "amount",
                        "ask_price1", "ask_volume1", "bid_price1", "bid_volume1",
                    ],
                    "items": [[
                        "000001.SZ", "Ping An", "2026-07-09 10:01:02", 10.49, 10.60,
                        10.70, 10.55, 10.40, 123400.0, 1294500.0,
                        10.50, 6800.0, 10.49, 9100.0,
                    ]],
                },
            }

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(tp.requests, "get", fake_get)

    rows = tp.fetch_realtime_quotes(["000001"], token="api-key", mode="replay")

    assert calls[0]["url"] == "https://ai-tool.indevs.in/tushare/pro/rt_tick"
    assert calls[0]["timeout"] <= 5
    assert calls[0]["params"]["ts_code"] == "000001.SZ"
    assert len(rows) == 1
    row = rows[0]
    expected = {
        "code": "000001",
        "ts_code": "000001.SZ",
        "name": "Ping An",
        "trade_time": "2026-07-09 10:01:02",
        "price": 10.49,
        "last_close": 10.60,
        "open": 10.55,
        "high": 10.70,
        "low": 10.40,
        "vol": 123400.0,
        "amount": 1294500.0,
        "change_pct": -1.0377,
        "bid1": 10.49,
        "bid1_vol": 9100.0,
        "ask1": 10.50,
        "ask1_vol": 6800.0,
        "source": "tushare_rt_tick",
        "level": "L1-5",
    }
    for key, value in expected.items():
        assert row[key] == value
    assert row["bid2"] == 0.0
    assert row["ask5_vol"] == 0.0



def test_fetch_tick_series_uses_replay_a_share_ticks(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "api_name": "a_share_ticks",
                "code": 0,
                "data": [
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20260709",
                        "time": "2026-07-09 09:30:02",
                        "price": 10.51,
                        "vol": 1200.0,
                        "amount": 12612.0,
                        "prev_price": 10.50,
                        "kind": "U",
                    },
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20260709",
                        "time": "2026-07-09 09:30:01",
                        "price": 10.50,
                        "vol": 800.0,
                        "amount": 8400.0,
                        "prev_price": 10.49,
                        "kind": "D",
                    },
                ],
            }

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(tp.requests, "get", fake_get)

    df = tp.fetch_tick_series("000001", trade_date="20260709", limit=2, token="api-key", mode="replay")

    assert calls[0]["url"] == "https://ai-tool.indevs.in/tushare/pro/a_share_ticks"
    assert calls[0]["timeout"] <= 5
    assert calls[0]["params"] == {"ts_code": "000001.SZ", "trade_date": "20260709", "limit": 2}
    assert df.to_dict("records") == [
        {
            "datetime": pd.Timestamp("2026-07-09 09:30:01"),
            "price": 10.50,
            "volume": 800.0,
            "amount": 8400.0,
            "prev_price": 10.49,
            "kind": "D",
        },
        {
            "datetime": pd.Timestamp("2026-07-09 09:30:02"),
            "price": 10.51,
            "volume": 1200.0,
            "amount": 12612.0,
            "prev_price": 10.50,
            "kind": "U",
        },
    ]


def test_fetch_tick_series_accepts_exchange_qualified_codes(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 0, "data": []}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params.copy())
        return FakeResponse()

    monkeypatch.setattr(tp.requests, "get", fake_get)

    for code in ("000001.SZ", "SZ000001", "sz000001", "600000.SH", "SH600000"):
        tp.fetch_tick_series(code, trade_date="20260709", limit=2, token="api-key", mode="replay")

    assert [call["ts_code"] for call in calls] == [
        "000001.SZ",
        "000001.SZ",
        "000001.SZ",
        "600000.SH",
        "600000.SH",
    ]


def test_fetch_tick_series_does_not_retry_all_replay_hosts(monkeypatch):
    calls = []
    monkeypatch.setattr(tp, "_replay_base_urls", lambda: ("https://one.example/tushare/pro", "https://two.example/tushare/pro"))

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        raise tp.requests.Timeout("slow")

    monkeypatch.setattr(tp.requests, "get", fake_get)

    with pytest.raises(RuntimeError):
        tp.fetch_tick_series("000001", trade_date="20260709", limit=2, token="api-key", mode="replay")

    assert calls == ["https://one.example/tushare/pro/a_share_ticks"]


def test_fetch_order_book_normalizes_five_levels(monkeypatch):
    monkeypatch.setattr(tp, "fetch_realtime_quotes", lambda codes, token=None, mode=None: [{
        "code": "000001",
        "ts_code": "000001.SZ",
        "name": "Ping An",
        "trade_time": "2026-07-09 10:01:02",
        "price": 10.49,
        "last_close": 10.60,
        "open": 10.55,
        "high": 10.70,
        "low": 10.40,
        "amount": 1294500.0,
        "change_pct": -1.0377,
        "bid1": 10.49,
        "bid1_vol": 9100.0,
        "ask1": 10.50,
        "ask1_vol": 6800.0,
        "bid2": 10.48,
        "bid2_vol": 4300.0,
        "ask2": 10.51,
        "ask2_vol": 3900.0,
    }])

    book = tp.fetch_order_book("000001", token="api-key", mode="replay")

    assert book["code"] == "000001"
    assert book["price"] == 10.49
    assert book["bids"][:2] == [
        {"level": 1, "price": 10.49, "volume": 9100.0},
        {"level": 2, "price": 10.48, "volume": 4300.0},
    ]
    assert book["asks"][:2] == [
        {"level": 1, "price": 10.50, "volume": 6800.0},
        {"level": 2, "price": 10.51, "volume": 3900.0},
    ]

def test_fetch_minute_kline_uses_replay_rt_min(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "api_name": "rt_min",
                "code": 0,
                "data": [
                    {
                        "ts_code": "000001.SZ",
                        "freq": "1MIN",
                        "trade_date": "20260709",
                        "time": "2026-07-09 10:01:00",
                        "open": 10.4,
                        "high": 10.5,
                        "low": 10.3,
                        "close": 10.45,
                        "vol": 1000.0,
                        "amount": 10450.0,
                        "is_complete": True,
                    }
                ],
            }

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(tp.requests, "get", fake_get)

    df = tp.fetch_minute_kline("000001", freq="1MIN", token="api-key", mode="replay")

    assert calls[0]["url"] == "https://ai-tool.indevs.in/tushare/pro/rt_min"
    assert calls[0]["timeout"] <= 5
    assert calls[0]["params"]["ts_code"] == "000001.SZ"
    assert calls[0]["params"]["freq"] == "1MIN"
    assert df.to_dict("records") == [{
        "datetime": pd.Timestamp("2026-07-09 10:01:00"),
        "open": 10.4,
        "high": 10.5,
        "low": 10.3,
        "close": 10.45,
        "volume": 1000.0,
        "amount": 10450.0,
        "is_complete": True,
    }]


def test_fetch_minute_kline_prefers_fast_rt_min_with_large_limit(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "api_name": "rt_min",
                "code": 0,
                "data": [{
                    "ts_code": "000001.SZ",
                    "freq": "1MIN",
                    "trade_date": "20260709",
                    "time": "2026-07-09 10:01:00",
                    "open": 10.4,
                    "high": 10.5,
                    "low": 10.3,
                    "close": 10.45,
                    "vol": 1000.0,
                    "amount": 10450.0,
                    "is_complete": True,
                }],
            }

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(tp.requests, "get", fake_get)

    df = tp.fetch_minute_kline("000001", freq="1MIN", limit=240, token="api-key", mode="replay")

    assert calls[0]["url"] == "https://ai-tool.indevs.in/tushare/pro/rt_min"
    assert calls[0]["params"] == {"ts_code": "000001.SZ", "freq": "1MIN"}
    assert df["close"].tolist() == [10.45]


def test_fetch_full_market_daily_uses_one_replay_request(monkeypatch):
    calls = []

    def fake_replay_get(api_name, token, params, timeout=25, max_attempts=None):
        calls.append((api_name, token, params, timeout, max_attempts))
        return [
            {
                "ts_code": "000001.SZ",
                "trade_date": "20260713",
                "open": 10.42,
                "high": 10.55,
                "low": 10.38,
                "close": 10.54,
                "vol": 924746.24,
                "amount": 978410.52,
            },
            {
                "ts_code": "600000.SH",
                "trade_date": "20260713",
                "open": 9.0,
                "high": 9.2,
                "low": 8.9,
                "close": 9.1,
                "vol": 361767.0,
                "amount": 329208.0,
            },
        ]

    monkeypatch.setattr(tp, "_replay_get", fake_replay_get)

    df = tp.fetch_full_market_daily("2026-07-13", token="api-key", mode="replay")

    assert calls == [
        (
            "daily",
            "api-key",
            {
                "trade_date": "20260713",
                "limit": 7000,
                "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
            },
            60,
            1,
        )
    ]
    assert df["code"].tolist() == ["000001", "600000"]
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-07-13", "2026-07-13"]
    assert df["close"].tolist() == [10.54, 9.1]
    assert df["volume"].tolist() == [924746.24, 361767.0]
    assert df["amount"].tolist() == [978410.52, 329208.0]


def test_fetch_full_market_adj_factors_uses_trade_date(monkeypatch):
    calls = []

    def fake_replay_get(api_name, token, params, timeout=25, max_attempts=None):
        calls.append((api_name, token, params, timeout, max_attempts))
        return [
            {"ts_code": "000001.SZ", "trade_date": "20260713", "adj_factor": 139.008},
            {"ts_code": "600000.SH", "trade_date": "20260713", "adj_factor": 10.5},
        ]

    monkeypatch.setattr(tp, "_replay_get", fake_replay_get)

    df = tp.fetch_full_market_adj_factors("20260713", token="api-key", mode="replay")

    assert calls[0][0] == "adj_factor"
    assert calls[0][2]["trade_date"] == "20260713"
    assert calls[0][2]["limit"] == 7000
    assert df.to_dict("records") == [
        {"code": "000001", "date": pd.Timestamp("2026-07-13"), "adj_factor": 139.008},
        {"code": "600000", "date": pd.Timestamp("2026-07-13"), "adj_factor": 10.5},
    ]


def test_fetch_full_market_realtime_daily_uses_rt_k_without_trade_date(monkeypatch):
    calls = []

    def fake_replay_request(api_name, token, params, timeout=25, max_attempts=None):
        calls.append((api_name, token, params, timeout, max_attempts))
        return {
            "code": 0,
            "params": {"source_trade_date": "20260714"},
            "data": [
                {
                    "ts_code": "000001.SZ",
                    "open": 10.53,
                    "high": 10.64,
                    "low": 10.48,
                    "close": 10.63,
                    "vol": 51001161.0,
                    "amount": 539712087.96,
                }
            ],
        }

    monkeypatch.setattr(tp, "_replay_request", fake_replay_request, raising=False)

    df = tp.fetch_full_market_realtime_daily(token="api-key", mode="replay")

    assert calls == [("rt_k", "api-key", {}, 60, 1)]
    assert df["code"].tolist() == ["000001"]
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-07-14"]
    assert df["close"].tolist() == [10.63]
    assert df["volume"].tolist() == pytest.approx([510011.61])
    assert df["amount"].tolist() == pytest.approx([539712.08796])
    assert (
        float(df.iloc[0]["amount"]) * 1000
        / (float(df.iloc[0]["volume"]) * 100)
    ) == pytest.approx(539712087.96 / 51001161.0)
