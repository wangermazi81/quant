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
