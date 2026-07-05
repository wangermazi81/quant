import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

import market_stats


def test_parse_legu_activity_uses_complete_breadth_counts():
    df = pd.DataFrame(
        {
            "item": [
                "\u4e0a\u6da8",
                "\u6da8\u505c",
                "\u771f\u5b9e\u6da8\u505c",
                "\u4e0b\u8dcc",
                "\u8dcc\u505c",
                "\u771f\u5b9e\u8dcc\u505c",
                "\u5e73\u76d8",
                "\u505c\u724c",
                "\u6d3b\u8dc3\u5ea6",
                "\u7edf\u8ba1\u65e5\u671f",
            ],
            "value": [3527.0, 159.0, 139.0, 1584.0, 28.0, 25.0, 82.0, 11.0, "67.77%", "2026-07-03 15:00:00"],
        }
    )

    breadth = market_stats.parse_legu_activity(df)

    assert breadth["up"] == 3527
    assert breadth["down"] == 1584
    assert breadth["flat"] == 82
    assert breadth["suspended"] == 11
    assert breadth["total"] == 5193
    assert breadth["listed_total"] == 5204
    assert breadth["limit_up"] == 139
    assert breadth["limit_dn"] == 25
    assert breadth["source"] == "legu"
    assert breadth["degraded"] is False


def test_quote_breadth_marks_low_coverage_as_degraded():
    quotes = [{"change_pct": 1.0}] * 3190 + [{"change_pct": -1.0}] * 1441 + [{"change_pct": 0.0}] * 82

    breadth = market_stats.breadth_from_quotes(quotes, expected_count=5193)

    assert breadth["total"] == 4713
    assert breadth["expected_count"] == 5193
    assert breadth["coverage"] == 0.908
    assert breadth["degraded"] is True
    assert "coverage" in breadth["reason"]


def test_official_turnover_combines_sse_yi_and_szse_yuan_units():
    sse = pd.DataFrame(
        [
            ["\u6302\u724c\u6570", 2350.0, 1699.0, 41.0, 610.0],
            ["\u5e02\u4ef7\u603b\u503c", 677302.79, 517964.55, 997.68, 158340.56],
            ["\u6d41\u901a\u5e02\u503c", 628431.36, 497639.85, 699.77, 130091.74],
            ["\u6210\u4ea4\u91d1\u989d", 14667.74, 9529.67, 1.15, 5136.92],
        ],
        columns=["\u5355\u65e5\u60c5\u51b5", "\u80a1\u7968", "\u4e3b\u677fA", "\u4e3b\u677fB", "\u79d1\u521b\u677f"],
    )
    szse = pd.DataFrame(
        [
            ["\u80a1\u7968", 2933, 1.718723e12],
            ["\u4e3b\u677fA\u80a1", 1496, 9.076941e11],
            ["\u4e3b\u677fB\u80a1", 38, 1.000183e8],
            ["\u521b\u4e1a\u677fA\u80a1", 1399, 8.109285e11],
        ],
        columns=["\u8bc1\u5238\u7c7b\u522b", "\u6570\u91cf", "\u6210\u4ea4\u91d1\u989d"],
    )

    turnover = market_stats.official_turnover_from_frames("20260703", sse, szse)

    assert turnover["today"] == 31853.0
    assert turnover["amount"] == 31852.82
    assert turnover["source"] == "official_exchange"
    assert turnover["stat_date"] == "2026-07-03"
    assert turnover["degraded"] is False


def test_index_turnover_uses_tdx_whole_market_indices_not_stock_sum():
    quotes = [
        {"code": "000001", "amount": 1465563086848.0},
        {"code": "399106", "amount": 1716890370048.0},
        {"code": "399001", "amount": 1716890370048.0},
    ]

    turnover = market_stats.turnover_from_index_quotes(quotes)

    assert turnover["today"] == 31825.0
    assert turnover["amount"] == 31824.53
    assert turnover["source"] == "tdx_index"
    assert turnover["degraded"] is False
