# -*- coding: utf-8 -*-
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

from analytics.factors_zoo import research


def test_rank_strategy_uses_next_day_execution_price():
    dates = pd.bdate_range("2026-01-02", periods=4)
    close = pd.DataFrame({"AAA": [10.0, 20.0, 10.0, 10.0]}, index=dates)
    factor = pd.DataFrame({"AAA": [1.0, 1.0, 1.0, 1.0]}, index=dates)

    trades = research.backtest_rank_strategy(
        factor,
        close,
        signal_dates=[dates[0]],
        direction=1,
        hold_days=1,
        top_k=1,
        fee_rate=0.0,
    )

    assert trades[0]["signal_date"] == "2026-01-02"
    assert trades[0]["open_date"] == "2026-01-05"
    assert trades[0]["close_date"] == "2026-01-06"
    assert trades[0]["return_pct"] == -50.0


def test_rank_strategy_respects_rebalance_spacing():
    dates = pd.bdate_range("2026-01-02", periods=8)
    close = pd.DataFrame({"AAA": [10, 11, 12, 13, 14, 15, 16, 17]}, index=dates, dtype=float)
    factor = pd.DataFrame({"AAA": [1.0] * 8}, index=dates)

    trades = research.backtest_rank_strategy(
        factor,
        close,
        signal_dates=list(dates[:5]),
        direction=1,
        hold_days=1,
        rebalance_days=2,
        top_k=1,
        fee_rate=0.0,
    )

    assert [trade["signal_date"] for trade in trades] == ["2026-01-02", "2026-01-06", "2026-01-08"]


def test_trade_summary_annualizes_by_elapsed_days_when_available():
    summary = research.summarize_trades(
        [
            {"return_pct": 10.0, "open_date": "2026-01-02", "close_date": "2026-03-31"},
        ]
    )

    assert summary["total_return_pct"] == 10.0
    assert 10.0 < summary["annual_return_pct"] < 60.0


def test_walk_forward_chooses_direction_from_train_only_and_tests_out_of_sample():
    dates = pd.bdate_range("2026-01-02", periods=10)
    close = pd.DataFrame(
        {
            "LOW": [10, 11, 12, 13, 14, 16, 18, 20, 22, 24],
            "HIGH": [10, 9, 8, 7, 6, 5, 4, 3, 2.5, 2.0],
        },
        index=dates,
        dtype=float,
    )
    factor = pd.DataFrame({"LOW": [1.0] * 10, "HIGH": [10.0] * 10}, index=dates)

    result = research.walk_forward_factor(
        "synthetic_reversal",
        factor,
        close,
        horizon=1,
        train_days=4,
        test_days=3,
        step_days=3,
        top_k=1,
        fee_rate=0.0,
    )

    assert result["windows"][0]["direction"] == -1
    assert result["summary"]["trade_count"] > 0
    assert result["summary"]["avg_return_pct"] > 0
    assert result["summary"]["test_start"] > result["summary"]["train_end_first"]


def test_write_report_uses_readable_utf8_chinese(tmp_path):
    payload = {
        "results": [
            {
                "alpha_id": "demo_factor",
                "summary": {
                    "window_count": 1,
                    "positive_window_rate": 1.0,
                    "trade_count": 2,
                    "hit_rate": 0.5,
                    "total_return_pct": 3.2,
                    "avg_return_pct": 1.6,
                    "max_drawdown_pct": -1.1,
                },
            }
        ]
    }

    out_dir = research.write_report(payload, tmp_path)
    text = (out_dir / "research.md").read_text(encoding="utf-8")

    assert text.startswith("# 因子 Walk-Forward 研究报告")
    assert "生成时间" in text
    assert "| 因子 | 窗口 | 正收益窗口 | 交易数 | 胜率 | 总收益% | 平均单次% | 最大回撤% |" in text
