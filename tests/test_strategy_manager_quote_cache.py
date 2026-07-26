import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from strategy_engine import strategy_manager as sm


def test_strategy_quotes_reuse_market_cache_when_tdx_quote_fetch_unavailable(monkeypatch):
    sm._mkt_cache["sorted_quotes"] = [
        {
            "code": "000001",
            "name": "CacheOne",
            "price": 10.5,
            "change_pct": 9.8,
            "last_close": 9.55,
            "vol": 12345,
            "open": 10.0,
            "low": 9.9,
        }
    ]
    sm._mkt_cache["ts"] = 1

    # 统一网关重构（8e06ac6）后 _build_data_fns._get_quotes 先走 data_gateway
    # （strategy_manager.py:431），只打桩 market.ths_quote 已经拦不住实时行情。
    monkeypatch.setattr(
        sm, "data_gateway",
        types.SimpleNamespace(get_realtime_quotes=lambda *a, **k: []),
    )

    _, get_quotes = sm._build_data_fns(["000001"])
    quotes = get_quotes(["000001"])

    assert isinstance(quotes, pd.DataFrame)
    assert quotes.to_dict("records") == [
        {
            "\u4ee3\u7801": "000001",
            "\u540d\u79f0": "CacheOne",
            "\u6700\u65b0\u4ef7": 10.5,
            "\u6da8\u8dcc\u5e45": 9.8,
            "\u6628\u6536": 9.55,
            "\u6210\u4ea4\u91cf": 12345,
            "\u91cf\u6bd4": 1.0,
            "open": 10.0,
            "low": 9.9,
            "limit_up": 10.51,
        }
    ]


def test_strategy_quotes_merge_partial_live_quotes_with_market_cache(monkeypatch):
    sm._mkt_cache["sorted_quotes"] = [
        {
            "code": "000001",
            "name": "LiveOneOld",
            "price": 10.0,
            "change_pct": 5.0,
            "last_close": 9.5,
            "vol": 1000,
        },
        {
            "code": "000002",
            "name": "CacheTwo",
            "price": 20.5,
            "change_pct": 8.0,
            "last_close": 19.0,
            "vol": 2000,
        },
    ]
    sm._mkt_cache["ts"] = 1

    # 网关只回其中一只（partial），另一只必须由 _mkt_cache 补齐
    def _partial_gateway(codes=None, *a, **k):
        assert list(codes) == ["000001", "000002"]
        return [{
            "code": "000001",
            "name": "LiveOne",
            "price": 11.0,
            "change_pct": 9.9,
            "last_close": 10.0,
            "vol": 3000,
        }]

    monkeypatch.setattr(
        sm, "data_gateway",
        types.SimpleNamespace(get_realtime_quotes=_partial_gateway),
    )

    _, get_quotes = sm._build_data_fns(["000001", "000002"])
    quotes = get_quotes(["000001", "000002"])

    records = {row["\u4ee3\u7801"]: row for row in quotes.to_dict("records")}
    assert list(records) == ["000001", "000002"]
    assert records["000001"]["\u540d\u79f0"] == "LiveOne"
    assert records["000001"]["\u6700\u65b0\u4ef7"] == 11.0
    assert records["000002"]["\u540d\u79f0"] == "CacheTwo"
    assert records["000002"]["\u6700\u65b0\u4ef7"] == 20.5


def test_market_cache_does_not_accept_baostock_code_only_stubs(monkeypatch):
    sm._mkt_cache["sorted_quotes"] = []
    sm._mkt_cache["ts"] = 0

    # \u7edf\u4e00\u7f51\u5173\u91cd\u6784\uff088e06ac6\uff09\u540e _refresh_market_cache \u53ea\u8d70 data_gateway\uff0c
    # \u539f\u6765\u7684 TDX\u2192akshare\u2192baostock\u2192efinance \u964d\u7ea7\u94fe\u5df2\u4ece\u751f\u4ea7\u4ee3\u7801\u5220\u9664\uff0c
    # \u6545\u76f4\u63a5\u6253\u6869\u7f51\u5173\u3002\u7528\u4f8b\u539f\u610f\u4fdd\u7559\u4e14\u66f4\u805a\u7126\uff1a
    # \u300c\u53ea\u6709\u4ee3\u7801\u3001\u6ca1\u6709\u4ef7\u91cf\u7684\u5360\u4f4d\u884c\u60c5\uff08baostock \u5f0f\uff09\u4e0d\u5f97\u8fdb\u5165\u7b56\u7565\u884c\u60c5\u7f13\u5b58\u300d\u2014\u2014
    # \u73b0\u5728\u7531 strategy_manager.py:463-466 \u7684 price>3 / vol>100 / last_close>0 \u8fc7\u6ee4\u5668\u8d1f\u8d23\u3002
    gateway_rows = [
        {"code": "000001", "name": "PingAn", "price": 12.3, "change_pct": 1.2,
         "last_close": 12.0, "vol": 2000},
        {"code": "600000", "name": "Pufa", "price": 9.8, "change_pct": -0.4,
         "last_close": 9.9, "vol": 3000},
        # baostock \u5f0f code-only \u5360\u4f4d\u884c\u60c5\uff1a\u65e0\u4ef7\u65e0\u91cf\uff0c\u5fc5\u987b\u88ab\u8fc7\u6ee4\u6389
        {"code": "000002", "name": "", "price": 0, "change_pct": 0, "last_close": 0, "vol": 0},
        {"code": "601398", "name": "", "price": 0, "change_pct": 0, "last_close": 0, "vol": 0},
    ]
    monkeypatch.setattr(
        sm, "data_gateway",
        types.SimpleNamespace(get_realtime_quotes=lambda *a, **k: list(gateway_rows)),
    )

    active = sm._refresh_market_cache()

    assert [q["code"] for q in active] == ["600000", "000001"]
    assert active[0]["price"] == 9.8
    assert active[0]["vol"] == 3000
    assert sm._mkt_cache["sorted_quotes"] == active
