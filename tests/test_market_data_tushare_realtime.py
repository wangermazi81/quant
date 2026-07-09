import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

import pandas as pd

import market_data as md


def test_realtime_quotes_use_tushare_when_selected(monkeypatch):
    monkeypatch.setattr(md.datasource_config, "get_internal_config", lambda: {
        "provider": "tushare",
        "tushare_enabled": True,
        "tushare_mode": "replay",
        "tushare_token": "tok",
    })
    monkeypatch.setattr(md.tushare_provider, "fetch_realtime_quotes", lambda codes, token=None, mode=None: [{
        "code": "000001",
        "name": "TsOne",
        "price": 10.49,
        "change_pct": -1.04,
        "last_close": 10.6,
        "open": 10.55,
        "high": 10.7,
        "low": 10.4,
        "vol": 123400,
        "amount": 1294500,
    }])
    monkeypatch.setattr(md.ak, "stock_zh_a_spot_em", lambda: (_ for _ in ()).throw(AssertionError("akshare should not run")))

    df = md.get_realtime_quotes(["000001"])

    assert df.to_dict("records") == [{
        "\u4ee3\u7801": "000001",
        "\u540d\u79f0": "TsOne",
        "\u6700\u65b0\u4ef7": 10.49,
        "\u6da8\u8dcc\u5e45": -1.04,
        "\u6628\u6536": 10.6,
        "\u4eca\u5f00": 10.55,
        "\u6700\u9ad8": 10.7,
        "\u6700\u4f4e": 10.4,
        "\u6210\u4ea4\u91cf": 123400,
        "\u6210\u4ea4\u989d": 1294500,
    }]


def test_minute_kline_uses_tushare_when_selected(monkeypatch, tmp_path):
    monkeypatch.setattr(md, "MINUTE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(md.datasource_config, "get_internal_config", lambda: {
        "provider": "tushare",
        "tushare_enabled": True,
        "tushare_mode": "replay",
        "tushare_token": "tok",
    })
    frame = pd.DataFrame({
        "datetime": pd.to_datetime(["2026-07-09 10:01:00"]),
        "open": [10.4],
        "high": [10.5],
        "low": [10.3],
        "close": [10.45],
        "volume": [1000.0],
        "amount": [10450.0],
        "is_complete": [True],
    })
    monkeypatch.setattr(md.tushare_provider, "fetch_minute_kline", lambda code, freq="1MIN", limit=None, token=None, mode=None: frame)
    monkeypatch.setattr(md.ak, "stock_zh_a_hist_min_em", lambda **kwargs: (_ for _ in ()).throw(AssertionError("akshare should not run")))

    result = md.get_minute_kline("000001", "20260709")

    assert result == {
        "datetime": ["2026-07-09 10:01:00"],
        "open": [10.4],
        "high": [10.5],
        "low": [10.3],
        "close": [10.45],
        "volume": [1000.0],
        "amount": [10450.0],
        "is_complete": [True],
    }
