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

    df = tp.fetch_kline_qfq("000001", "2026-07-01", "20260708", token="tok")

    assert calls[0]["ts_code"] == "000001.SZ"
    assert calls[0]["api"] is fake_api
    assert calls[0]["adj"] == "qfq"
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-07-07", "2026-07-08"]
    assert df["volume"].tolist() == [1000.0, 1200.0]


def test_test_connection_uses_daily_endpoint(monkeypatch):
    class FakePro:
        def daily(self, **kwargs):
            assert kwargs["ts_code"] == "000001.SZ"
            return pd.DataFrame({"ts_code": ["000001.SZ"]})

    fake_ts = types.SimpleNamespace(pro_api=lambda token: FakePro())
    monkeypatch.setitem(sys.modules, "tushare", fake_ts)

    result = tp.test_connection("tok")

    assert result["ok"] is True
    assert result["rows"] == 1
