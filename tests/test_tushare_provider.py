import sys
import types

import pandas as pd

import analytics.tushare_provider as tp


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
                    "fields": ["ts_code", "trade_date", "open", "high", "low", "close", "vol"],
                    "items": [
                        ["000001.SZ", "20240103", 9.2, 9.3, 9.1, 9.25, 1000.0],
                        ["000001.SZ", "20240102", 9.0, 9.4, 8.9, 9.2, 900.0],
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
    assert calls[0]["params"]["fields"] == "ts_code,trade_date,open,high,low,close,vol"
    assert calls[1]["url"].endswith("/adj_factor")
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == ["2024-01-02", "2024-01-03"]
    assert df["close"].round(3).tolist() == [4.6, 9.25]
    assert df["volume"].tolist() == [900.0, 1000.0]


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
        calls.append({"url": url, "params": params, "headers": headers})
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
        calls.append({"url": url, "params": params, "headers": headers})
        return FakeResponse()

    monkeypatch.setattr(tp.requests, "get", fake_get)

    rows = tp.fetch_realtime_quotes(["000001"], token="api-key", mode="replay")

    assert calls[0]["url"] == "https://ai-tool.indevs.in/tushare/pro/rt_tick"
    assert calls[0]["params"]["ts_code"] == "000001.SZ"
    assert rows == [{
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
    }]


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
        calls.append({"url": url, "params": params, "headers": headers})
        return FakeResponse()

    monkeypatch.setattr(tp.requests, "get", fake_get)

    df = tp.fetch_minute_kline("000001", freq="1MIN", token="api-key", mode="replay")

    assert calls[0]["url"] == "https://ai-tool.indevs.in/tushare/pro/rt_min"
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
