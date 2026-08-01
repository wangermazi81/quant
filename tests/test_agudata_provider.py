from datetime import datetime

import pandas as pd
import pytest

from analytics import agudata_provider as provider


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


def test_query_api_unwraps_nested_fields_and_items(monkeypatch):
    calls = []
    payload = {
        "code": 0,
        "data": {
            "code": 200,
            "data": {
                "data": {
                    "fields": ["ts_code", "trade_date", "close"],
                    "items": [["000001.SZ", "20260731", 12.34]],
                }
            },
        },
    }

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(payload)

    monkeypatch.setattr(provider.requests, "get", fake_get)

    rows = provider.query_api(
        "daily",
        {"ts_code": "000001.SZ"},
        api_key="test-secret",
        base_url="https://data.example/openapi/v1",
    )

    assert rows == [
        {"ts_code": "000001.SZ", "trade_date": "20260731", "close": 12.34}
    ]
    assert calls[0][0] == "https://data.example/openapi/v1/tushare/daily"
    assert calls[0][1]["headers"]["X-API-Key"] == "test-secret"
    assert calls[0][1]["params"] == {"ts_code": "000001.SZ"}


def test_query_api_reports_permission_error_without_leaking_key(monkeypatch):
    monkeypatch.setattr(
        provider.requests,
        "get",
        lambda *args, **kwargs: _Response(
            {
                "code": 403,
                "message": "API_PERMISSION_DENIED",
                "data": {"message": "permission denied"},
            }
        ),
    )

    with pytest.raises(provider.AgudataPermissionError) as exc_info:
        provider.query_api("stk-auction", api_key="never-log-this")

    assert "API_PERMISSION_DENIED" in str(exc_info.value)
    assert "never-log-this" not in str(exc_info.value)


def test_query_api_classifies_gateway_limiter_outage_as_retryable(
    monkeypatch,
):
    monkeypatch.setattr(
        provider.requests,
        "get",
        lambda *args, **kwargs: _Response(
            {
                "code": 500,
                "message": "限流服务不可用",
            }
        ),
    )

    with pytest.raises(provider.AgudataRateLimitError):
        provider.query_api(
            "daily-basic",
            api_key="test-secret",
        )


def test_query_api_classifies_business_5xx_as_transient(
    monkeypatch,
):
    monkeypatch.setattr(
        provider.requests,
        "get",
        lambda *args, **kwargs: _Response(
            {
                "code": 500,
                "message": "请求摘要暂不可用，请稍后重试",
            }
        ),
    )

    with pytest.raises(provider.AgudataTransientError):
        provider.query_api(
            "daily-basic",
            api_key="test-secret",
        )


def test_query_api_classifies_non_json_http_5xx_as_transient(
    monkeypatch,
):
    class NonJsonResponse(_Response):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(
        provider.requests,
        "get",
        lambda *args, **kwargs: NonJsonResponse(
            "<html>timeout</html>",
            status_code=524,
        ),
    )

    with pytest.raises(provider.AgudataTransientError) as exc_info:
        provider.query_api(
            "daily-basic",
            api_key="test-secret",
        )

    assert "HTTP 524" in str(exc_info.value)


def test_fetch_kline_qfq_applies_adjustment_factors(monkeypatch):
    def fake_paged(endpoint, params, **kwargs):
        if endpoint == "daily":
            return [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260730",
                    "open": 30,
                    "high": 31,
                    "low": 29,
                    "close": 30,
                    "vol": 100,
                    "amount": 1000,
                },
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260731",
                    "open": 20,
                    "high": 22,
                    "low": 18,
                    "close": 20,
                    "vol": 200,
                    "amount": 4000,
                },
            ]
        if endpoint == "adj-factor":
            return [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260730",
                    "adj_factor": 1,
                },
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260731",
                    "adj_factor": 2,
                },
            ]
        raise AssertionError(endpoint)

    monkeypatch.setattr(provider, "_paged_query", fake_paged)

    frame = provider.fetch_kline_qfq(
        "000001",
        "20260730",
        "20260731",
        api_key="key",
    )

    assert list(frame.columns) == [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]
    assert frame["close"].tolist() == [15.0, 20.0]
    assert frame["volume"].tolist() == [100, 200]
    assert frame["amount"].tolist() == [1000, 4000]


def test_fetch_full_market_daily_pages_and_normalizes(monkeypatch):
    offsets = []

    def fake_query(endpoint, params, **kwargs):
        assert endpoint == "daily"
        offsets.append(params["offset"])
        if params["offset"] == 0:
            return [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260731",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "vol": 100,
                    "amount": 1050,
                },
                {
                    "ts_code": "600000.SH",
                    "trade_date": "20260731",
                    "open": 20,
                    "high": 21,
                    "low": 19,
                    "close": 20.5,
                    "vol": 200,
                    "amount": 4100,
                },
            ]
        return []

    monkeypatch.setattr(provider, "query_api", fake_query)

    frame = provider.fetch_full_market_daily(
        "20260731",
        api_key="key",
        page_size=2,
    )

    assert offsets == [0, 2]
    assert frame["code"].tolist() == ["000001", "600000"]
    assert frame["date"].dt.strftime("%Y%m%d").tolist() == [
        "20260731",
        "20260731",
    ]


def test_fetch_full_market_daily_uses_safe_default_page_size(monkeypatch):
    limits = []

    def fake_query(endpoint, params, **kwargs):
        limits.append(params["limit"])
        return [
            {
                "ts_code": "000001.SZ",
                "trade_date": "20260731",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "vol": 100,
                "amount": 1050,
            }
        ]

    monkeypatch.setattr(provider, "query_api", fake_query)

    frame = provider.fetch_full_market_daily(
        "20260731",
        api_key="key",
    )

    assert len(frame) == 1
    assert limits == [500]


def test_paged_query_reuses_one_http_session(monkeypatch):
    sessions = []

    class FakeSession:
        def __init__(self):
            self.calls = []
            sessions.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs["params"]["offset"]))
            offset = kwargs["params"]["offset"]
            rows = (
                [["000001.SZ"]]
                if offset == 0
                else []
            )
            return _Response(
                {
                    "code": 0,
                    "data": {
                        "data": {
                            "data": {
                                "fields": ["ts_code"],
                                "items": rows,
                            }
                        }
                    },
                }
            )

    monkeypatch.setattr(provider.requests, "Session", FakeSession)
    monkeypatch.setattr(
        provider.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("pagination must use its shared session")
        ),
    )

    rows = provider._paged_query(
        "daily",
        {"trade_date": "20260731"},
        api_key="key",
        page_size=1,
        max_rows=2,
    )

    assert rows == [{"ts_code": "000001.SZ"}]
    assert len(sessions) == 1
    assert [offset for _, offset in sessions[0].calls] == [0, 1]


def test_paged_query_retries_rate_limit_with_bounded_backoff(monkeypatch):
    attempts = []
    sleeps = []

    def fake_query(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise provider.AgudataRateLimitError("connection limit")
        return [{"ts_code": "000001.SZ"}]

    monkeypatch.setattr(provider, "query_api", fake_query)
    monkeypatch.setattr(
        provider.time,
        "sleep",
        lambda seconds: sleeps.append(seconds),
    )

    rows = provider._paged_query(
        "daily",
        {"trade_date": "20260731"},
        api_key="key",
        page_size=1000,
        max_rows=1000,
    )

    assert rows == [{"ts_code": "000001.SZ"}]
    assert len(attempts) == 3
    assert sleeps == [3.0, 10.0]


def test_paged_query_survives_sustained_rate_limit_window(
    monkeypatch,
):
    attempts = []
    sleeps = []

    def fake_query(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 5:
            raise provider.AgudataRateLimitError("too fast")
        return [{"ts_code": "000001.SZ"}]

    monkeypatch.setattr(provider, "query_api", fake_query)
    monkeypatch.setattr(provider.time, "sleep", sleeps.append)

    rows = provider._paged_query(
        "daily-basic",
        {"trade_date": "20260731"},
        api_key="key",
        page_size=1000,
        max_rows=1000,
    )

    assert rows == [{"ts_code": "000001.SZ"}]
    assert len(attempts) == 5
    assert sleeps == [3.0, 10.0, 30.0, 60.0]


def test_paged_query_paces_successive_full_pages(monkeypatch):
    offsets = []
    sleeps = []

    def fake_query(_endpoint, params, **_kwargs):
        offsets.append(params["offset"])
        if params["offset"] < 2:
            return [{"ts_code": f"{params['offset']:06d}.SZ"}]
        return []

    monkeypatch.setattr(provider, "query_api", fake_query)
    monkeypatch.setattr(provider.time, "sleep", sleeps.append)

    rows = provider._paged_query(
        "daily-basic",
        {},
        api_key="key",
        page_size=1,
        max_rows=3,
        request_interval_seconds=2.1,
    )

    assert len(rows) == 2
    assert offsets == [0, 1, 2]
    assert sleeps == [2.1, 2.1]


def test_paged_query_allows_provider_verified_5000_row_pages(
    monkeypatch,
):
    limits = []

    def fake_query(_endpoint, params, **_kwargs):
        limits.append(params["limit"])
        return []

    monkeypatch.setattr(provider, "query_api", fake_query)

    provider._paged_query(
        "daily-basic",
        {},
        api_key="key",
        page_size=5000,
        max_rows=5000,
    )

    assert limits == [5000]


def test_paged_query_retries_transient_5xx_with_bounded_backoff(
    monkeypatch,
):
    attempts = []
    sleeps = []

    def fake_query(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise provider.AgudataTransientError(
                "temporarily unavailable"
            )
        return [{"ts_code": "000001.SZ"}]

    monkeypatch.setattr(provider, "query_api", fake_query)
    monkeypatch.setattr(provider.time, "sleep", sleeps.append)

    rows = provider._paged_query(
        "daily-basic",
        {"trade_date": "20260731"},
        api_key="key",
        page_size=1000,
        max_rows=1000,
    )

    assert rows == [{"ts_code": "000001.SZ"}]
    assert len(attempts) == 3
    assert sleeps == [3.0, 10.0]


def test_fetch_minute_kline_uses_historical_endpoint_for_trade_date(monkeypatch):
    calls = []

    def fake_paged(endpoint, params, **kwargs):
        calls.append((endpoint, params))
        return [
            {
                "ts_code": "000001.SZ",
                "trade_time": "2026-07-31 09:30:00",
                "open": 10,
                "close": 10.1,
                "high": 10.2,
                "low": 9.9,
                "vol": 100,
                "amount": 1010,
            }
        ]

    monkeypatch.setattr(provider, "_paged_query", fake_paged)

    frame = provider.fetch_minute_kline(
        "000001",
        freq="1MIN",
        trade_date="2026-07-31",
        api_key="key",
    )

    assert calls == [
        (
            "stk-mins",
            {
                "ts_code": "000001.SZ",
                "freq": "1min",
                "start_date": "20260731 09:30:00",
                "end_date": "20260731 15:00:00",
                "fields": "ts_code,trade_time,open,close,high,low,vol,amount",
            },
        )
    ]
    assert frame["datetime"].dt.strftime("%H:%M:%S").tolist() == ["09:30:00"]
    assert frame["is_complete"].tolist() == [True]


def test_fetch_realtime_quotes_and_auction_snapshot(monkeypatch):
    def fake_paged(endpoint, params, **kwargs):
        if endpoint == "rt-k":
            return [
                {
                    "ts_code": "000001.SZ",
                    "name": "平安银行",
                    "trade_time": "2026-07-31 14:59:58",
                    "close": 12.0,
                    "pre_close": 11.5,
                    "open": 11.6,
                    "high": 12.1,
                    "low": 11.4,
                    "vol": 123400,
                    "amount": 1480800,
                }
            ]
        if endpoint == "stk-auction":
            return [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260731",
                    "vol": 1000,
                    "price": 11.8,
                    "amount": 11800,
                    "pre_close": 11.5,
                    "turnover_rate": 0.01,
                    "volume_ratio": 1.2,
                    "float_share": 1000000,
                }
            ]
        raise AssertionError(endpoint)

    monkeypatch.setattr(provider, "_paged_query", fake_paged)

    quotes = provider.fetch_realtime_quotes(["000001"], api_key="key")
    auction = provider.fetch_auction_snapshot(
        "000001",
        "20260731",
        api_key="key",
    )

    assert quotes[0]["code"] == "000001"
    assert quotes[0]["price"] == 12.0
    assert quotes[0]["change_pct"] == pytest.approx((12.0 / 11.5 - 1) * 100)
    assert quotes[0]["source"] == "agudata_rt_k"
    assert auction["price"] == 11.8
    assert auction["source"] == "agudata_stk_auction"


def test_fetch_daily_basic_history_batches_and_normalizes(monkeypatch):
    calls = []

    def fake_paged(endpoint, params, **kwargs):
        calls.append((endpoint, dict(params), kwargs))
        if len(calls) > 1:
            return []
        return [
            {
                "ts_code": "000002.SZ",
                "trade_date": "20260731",
                "turnover_rate": "2.5",
                "turnover_rate_f": "3.5",
                "volume_ratio": "1.2",
                "float_share": "2000",
                "total_share": "3000",
                "float_mv": "4000",
                "circ_mv": "5000",
            },
            {
                "ts_code": "000001.SZ",
                "trade_date": "20260730",
                "turnover_rate": "1.0",
                "turnover_rate_f": "1.1",
                "volume_ratio": "0.8",
                "float_share": "1000",
                "total_share": "1500",
                "float_mv": "2000",
                "circ_mv": "2500",
            },
            {
                "ts_code": "000001.SZ",
                "trade_date": "20260730",
                "turnover_rate": "1.5",
                "turnover_rate_f": "1.6",
                "volume_ratio": "0.9",
                "float_share": "1100",
                "total_share": "1600",
                "float_mv": "2100",
                "circ_mv": "2600",
            },
            {
                "ts_code": "invalid",
                "trade_date": "not-a-date",
                "turnover_rate": "bad",
            },
        ]

    monkeypatch.setattr(provider, "_paged_query", fake_paged)

    frame = provider.fetch_daily_basic_history(
        [f"{number:06d}" for number in range(1, 52)],
        "2026-07-01",
        "2026-07-31",
        api_key="key",
        base_url="https://data.example/openapi/v1",
    )

    assert [endpoint for endpoint, _, _ in calls] == [
        "daily-basic",
        "daily-basic",
    ]
    assert [len(params["ts_code"].split(",")) for _, params, _ in calls] == [
        50,
        1,
    ]
    assert calls[0][1]["ts_code"].split(",")[0] == "000001.SZ"
    assert calls[1][1]["ts_code"] == "000051.SZ"
    assert calls[0][1]["start_date"] == "20260701"
    assert calls[0][1]["end_date"] == "20260731"
    assert all(call[2]["page_size"] == 5000 for call in calls)
    assert all(
        call[2]["request_interval_seconds"] == 3.2
        for call in calls
    )
    assert list(frame.columns) == [
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
    assert frame["code"].tolist() == ["000001", "000002"]
    assert frame["date"].dt.strftime("%Y%m%d").tolist() == [
        "20260730",
        "20260731",
    ]
    assert frame["turnover_rate"].tolist() == [1.5, 2.5]
    assert all(
        pd.api.types.is_numeric_dtype(frame[column])
        for column in frame.columns[2:]
    )


def test_fetch_auction_history_normalizes_deduplicates_and_sorts(monkeypatch):
    calls = []

    def fake_paged(endpoint, params, **kwargs):
        calls.append((endpoint, dict(params), kwargs))
        return [
            {
                "ts_code": "600000.SH",
                "trade_date": "20260731",
                "vol": "200",
                "price": "12.5",
                "amount": "2500",
                "pre_close": "12.0",
                "turnover_rate": "0.2",
                "volume_ratio": "1.3",
                "float_share": "10000",
            },
            {
                "ts_code": "000001.SZ",
                "trade_date": "20260730",
                "vol": "100",
                "price": "10.0",
                "amount": "1000",
                "pre_close": "9.8",
                "turnover_rate": "0.1",
                "volume_ratio": "1.1",
                "float_share": "8000",
            },
            {
                "ts_code": "000001.SZ",
                "trade_date": "20260730",
                "vol": "150",
                "price": "10.2",
                "amount": "1530",
                "pre_close": "9.8",
                "turnover_rate": "0.15",
                "volume_ratio": "1.2",
                "float_share": "8000",
            },
        ]

    monkeypatch.setattr(provider, "_paged_query", fake_paged)

    frame = provider.fetch_auction_history(
        ["600000.SH", "000001"],
        "20260701",
        "20260731",
        api_key="key",
    )

    assert len(calls) == 1
    assert calls[0][0] == "stk-auction"
    assert calls[0][1]["ts_code"] == "600000.SH,000001.SZ"
    assert calls[0][2]["page_size"] == 5000
    assert calls[0][2]["request_interval_seconds"] == 3.2
    assert list(frame.columns) == [
        "code",
        "date",
        "auction_volume",
        "auction_price",
        "auction_amount",
        "pre_close",
        "auction_turnover_rate",
        "auction_volume_ratio",
        "float_share",
    ]
    assert frame["code"].tolist() == ["000001", "600000"]
    assert frame["auction_volume"].tolist() == [150, 200]
    assert frame["auction_price"].tolist() == [10.2, 12.5]
    assert all(
        pd.api.types.is_numeric_dtype(frame[column])
        for column in frame.columns[2:]
    )


def test_history_helpers_return_fixed_empty_frames_without_requests(monkeypatch):
    monkeypatch.setattr(
        provider,
        "_paged_query",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("empty input must not request Agudata")
        ),
    )

    daily = provider.fetch_daily_basic_history([], "20260701", "20260731")
    auction = provider.fetch_auction_history([], "20260701", "20260731")

    assert list(daily.columns) == [
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
    assert list(auction.columns) == [
        "code",
        "date",
        "auction_volume",
        "auction_price",
        "auction_amount",
        "pre_close",
        "auction_turnover_rate",
        "auction_volume_ratio",
        "float_share",
    ]
    assert daily.empty
    assert auction.empty


def test_fetch_security_master_merges_agudata_profiles_with_trusted_lifecycle(
    monkeypatch,
):
    lifecycle = [
        {
            "code": "000001",
            "ts_code": "000001.SZ",
            "name": "旧名称",
            "list_date": "19910403",
            "delist_date": "",
            "list_status": "L",
            "exchange": "SZ",
        },
        {
            "code": "000002",
            "ts_code": "000002.SZ",
            "name": "退市深股",
            "list_date": "19910129",
            "delist_date": "20200102",
            "list_status": "D",
            "exchange": "SZ",
        },
        {
            "code": "600000",
            "ts_code": "600000.SH",
            "name": "浦发银行",
            "list_date": "19991110",
            "delist_date": "",
            "list_status": "L",
            "exchange": "SH",
        },
        {
            "code": "600001",
            "ts_code": "600001.SH",
            "name": "退市沪股",
            "list_date": "19981231",
            "delist_date": "20200103",
            "list_status": "D",
            "exchange": "SH",
        },
    ]
    monkeypatch.setattr(
        provider,
        "_fetch_trusted_security_lifecycle",
        lambda: lifecycle,
        raising=False,
    )
    calls = []

    def fake_paged(endpoint, params, **kwargs):
        calls.append((endpoint, dict(params), kwargs))
        if endpoint == "trade-cal":
            return [
                {
                    "exchange": "SSE",
                    "cal_date": "20260730",
                    "is_open": 1,
                    "pretrade_date": "20260729",
                }
            ]
        if endpoint == "stock-basic" and params["list_status"] == "L":
            return [
                {
                    "ts_code": "000001.SZ",
                    "symbol": "000001",
                    "name": "平安银行",
                    "area": "深圳",
                    "industry": "银行",
                    "market": "主板",
                    "list_date": "19910403",
                    "delist_date": "",
                    "list_status": "L",
                }
            ]
        if endpoint == "stock-basic":
            return []
        raise AssertionError(endpoint)

    monkeypatch.setattr(provider, "_paged_query", fake_paged)
    monkeypatch.setattr(
        provider,
        "datetime",
        type(
            "FixedDatetime",
            (),
            {"now": staticmethod(lambda: datetime(2026, 7, 31, 10, 0))},
        ),
    )

    rows = provider.fetch_security_master(
        api_key="key",
        base_url="https://data.example/openapi/v1",
    )

    assert len(rows) == 4
    assert rows[0]["name"] == "平安银行"
    assert rows[0]["industry"] == "银行"
    assert rows[0]["source"] == (
        "agudata_stock_basic+baostock_stock_basic"
    )
    assert rows[1]["source"] == "baostock_stock_basic"
    assert all(
        row["security_master_snapshot_trade_date"] == "20260730"
        for row in rows
    )
    assert all(
        row["security_master_row_count"] == 4
        and row["lifecycle_snapshot_complete"] is True
        for row in rows
    )
    assert [
        params["list_status"]
        for endpoint, params, _ in calls
        if endpoint == "stock-basic"
    ] == ["L", "D", "P"]
