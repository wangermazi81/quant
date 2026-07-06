import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

import pandas as pd
import pytest


da_ban = pytest.importorskip("strategies.da_ban", reason="private strategy file is not committed")


def test_scan_uses_quote_fallback_when_tdx_returns_no_candidates(monkeypatch):
    def empty_success(_results):
        return True

    def get_quotes(codes):
        assert codes == ["000001"]
        return pd.DataFrame([
            {
                "\u4ee3\u7801": "000001",
                "\u540d\u79f0": "CacheNearLimit",
                "\u6700\u65b0\u4ef7": 10.99,
                "\u6da8\u8dcc\u5e45": 9.9,
                "\u6628\u6536": 10.0,
                "\u91cf\u6bd4": 1.2,
                "limit_up": 11.0,
            }
        ])

    monkeypatch.setattr(da_ban, "_from_tdx", empty_success)

    results = da_ban.scan(["000001"], lambda *_args, **_kwargs: pd.DataFrame(), get_quotes)

    assert [item["code"] for item in results] == ["000001"]
    assert results[0]["name"] == "CacheNearLimit"
    assert results[0]["price"] == 10.99
