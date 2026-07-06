import sys
import types
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


def test_breadth_falls_back_to_spot_when_legu_and_strategy_snapshot_unavailable(monkeypatch):
    spot = pd.DataFrame(
        {
            "\u4ee3\u7801": ["000001", "000002", "000003", "000004", "000005"],
            "\u6da8\u8dcc\u5e45": [1.2, -0.5, 0.0, 10.1, -10.0],
        }
    )

    def legu_down():
        raise RuntimeError("legu unavailable")

    fake_ak = types.SimpleNamespace(
        stock_market_activity_legu=legu_down,
        stock_zh_a_spot_em=lambda: spot,
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    monkeypatch.setitem(sys.modules, "strategy_manager", types.SimpleNamespace())

    breadth = market_stats.get_market_breadth(expected_count=5)

    assert breadth["source"] == "eastmoney_spot"
    assert breadth["up"] == 2
    assert breadth["down"] == 2
    assert breadth["flat"] == 1
    assert breadth["limit_up"] == 1
    assert breadth["limit_dn"] == 1
    assert breadth["total"] == 5
    assert breadth["degraded"] is False


def test_verified_eastmoney_snapshot_matches_20260703_three_market_overview():
    snapshot = market_stats.get_verified_market_snapshot("20260703")

    assert snapshot["source"] == "eastmoney_three_market_verified"
    assert snapshot["stat_date"] == "2026-07-03"
    assert snapshot["turnover"]["today"] == 32055
    assert snapshot["turnover"]["amount"] == 32055
    assert snapshot["turnover"]["previous_same_time_delta"] == -2687
    assert snapshot["turnover"]["previous_same_time_pct"] == -7.73
    assert snapshot["turnover"]["avg_60d"] == 29641
    assert snapshot["turnover"]["avg_60d_pct"] == 8.14
    assert snapshot["breadth"]["up"] == 3804
    assert snapshot["breadth"]["flat"] == 84
    assert snapshot["breadth"]["down"] == 1628
    assert snapshot["breadth"]["total"] == 5516
    assert snapshot["breadth"]["distribution"] == {
        "limit_up": 160,
        "up_5_to_limit": 443,
        "up_1_to_5": 2380,
        "up_0_to_1": 821,
        "flat": 84,
        "down_0_to_1": 428,
        "down_1_to_5": 886,
        "down_5_to_limit": 290,
        "limit_down": 24,
    }


def test_market_turnover_prefers_verified_three_market_snapshot_for_20260703():
    turnover = market_stats.get_market_turnover("20260703")

    assert turnover["today"] == 32055
    assert turnover["amount"] == 32055
    assert turnover["source"] == "eastmoney_three_market_verified"
    assert turnover["stat_date"] == "2026-07-03"
    assert turnover["degraded"] is False


def test_market_breadth_prefers_verified_three_market_snapshot_for_20260703():
    breadth = market_stats.get_market_breadth(date_text="20260703")

    assert breadth["up"] == 3804
    assert breadth["flat"] == 84
    assert breadth["down"] == 1628
    assert breadth["total"] == 5516
    assert breadth["source"] == "eastmoney_three_market_verified"
    assert breadth["degraded"] is False


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
