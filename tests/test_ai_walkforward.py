# -*- coding: utf-8 -*-
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

import ai_walkforward as wf


def test_walk_forward_split_never_leaks_future_samples():
    rows = [{"as_of_date": f"2026-07-{day:02d}", "value": day} for day in range(1, 11)]

    windows = wf.walk_forward_splits(rows, train_size=4, test_size=2, step=2)

    assert len(windows) == 3
    for window in windows:
        assert max(r["as_of_date"] for r in window["train"]) < min(r["as_of_date"] for r in window["test"])
        assert window["train_end"] < window["test_start"]


def test_backtest_metrics_include_drawdown_and_hit_rate():
    metrics = wf.calculate_metrics([
        {"pnl_pct": 5.0},
        {"pnl_pct": -2.0},
        {"pnl_pct": 3.0},
    ])

    assert metrics["trades"] == 3
    assert metrics["hit_rate"] == 2 / 3
    assert metrics["total_return_pct"] == 6.0
    assert metrics["max_drawdown_pct"] <= 0
