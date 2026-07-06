import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

import pandas as pd

import strategy_manager as sm


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

    import ths_quote

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

    import ths_quote

    monkeypatch.setattr(ths_quote, "get_client", lambda *args, **kwargs: PartialClient())

    _, get_quotes = sm._build_data_fns(["000001", "000002"])
    quotes = get_quotes(["000001", "000002"])

    records = {row["\u4ee3\u7801"]: row for row in quotes.to_dict("records")}
    assert list(records) == ["000001", "000002"]
    assert records["000001"]["\u540d\u79f0"] == "LiveOne"
    assert records["000001"]["\u6700\u65b0\u4ef7"] == 11.0
    assert records["000002"]["\u540d\u79f0"] == "CacheTwo"
    assert records["000002"]["\u6700\u65b0\u4ef7"] == 20.5
