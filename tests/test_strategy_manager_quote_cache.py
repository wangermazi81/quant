from tests._module_stubs import stub_project_module
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

    class EmptyClient:
        _ok = True

        def get_quotes(self, codes):
            return []

    from market import ths_quote

    monkeypatch.setattr(ths_quote, "get_client", lambda *args, **kwargs: EmptyClient())

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

    class PartialClient:
        _ok = True

        def get_quotes(self, codes):
            assert codes == ["000001", "000002"]
            return [{
                "code": "000001",
                "name": "LiveOne",
                "price": 11.0,
                "change_pct": 9.9,
                "last_close": 10.0,
                "vol": 3000,
            }]

    from market import ths_quote

    monkeypatch.setattr(ths_quote, "get_client", lambda *args, **kwargs: PartialClient())

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

    class EmptyTdxClient:
        _ok = True

        def get_all_stock_codes(self):
            return []

    class BaoLogin:
        error_code = "0"
        error_msg = ""

    class BaoQuery:
        error_code = "0"
        error_msg = ""

        def __init__(self):
            self._rows = [["sz.000001"], ["sh.600000"]]
            self._idx = -1

        def next(self):
            self._idx += 1
            return self._idx < len(self._rows)

        def get_row_data(self):
            return self._rows[self._idx]

    def ak_down():
        raise RuntimeError("eastmoney spot unavailable")

    def ef_quotes():
        return pd.DataFrame(
            {
                "\u80a1\u7968\u4ee3\u7801": ["000001", "600000"],
                "\u80a1\u7968\u540d\u79f0": ["PingAn", "Pufa"],
                "\u6700\u65b0\u4ef7": [12.3, 9.8],
                "\u6da8\u8dcc\u5e45": [1.2, -0.4],
                "\u6628\u65e5\u6536\u76d8": [12.0, 9.9],
                "\u6210\u4ea4\u91cf": [2000, 3000],
            }
        )

    stub_project_module(
        monkeypatch,
        "ths_quote",
        types.SimpleNamespace(get_client=lambda *args, **kwargs: EmptyTdxClient()),
    )
    monkeypatch.setitem(sys.modules, "akshare", types.SimpleNamespace(stock_zh_a_spot_em=ak_down))
    monkeypatch.setitem(
        sys.modules,
        "baostock",
        types.SimpleNamespace(login=lambda: BaoLogin(), query_all_stock=lambda: BaoQuery(), logout=lambda: None),
    )
    monkeypatch.setitem(sys.modules, "efinance", types.SimpleNamespace(stock=types.SimpleNamespace(get_realtime_quotes=ef_quotes)))

    active = sm._refresh_market_cache()

    assert [q["code"] for q in active] == ["600000", "000001"]
    assert active[0]["price"] == 9.8
    assert active[0]["vol"] == 3000
