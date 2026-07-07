# -*- coding: utf-8 -*-
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

from analytics.factors_zoo.backtest import backtest_factor


class _FakeRegistry:
    def list(self):
        return ["fake_alpha"]

    def compute(self, factor_id, panel):
        close = panel["close"]
        return pd.DataFrame({"AAA": [1.0] * len(close)}, index=close.index)


def test_factor_backtest_executes_after_signal_day_without_lookahead(monkeypatch):
    dates = pd.bdate_range("2026-01-02", periods=14)
    close = pd.DataFrame({"AAA": [10.0, 20.0, 10.0] + [10.0] * 11}, index=dates)
    panel = {"close": close}

    import analytics.factors_zoo as fz

    monkeypatch.setattr(fz, "registry", lambda: _FakeRegistry())
    monkeypatch.setattr(fz, "build_panel", lambda codes, get_kline, days, workers=12: panel)

    result = backtest_factor(
        "fake_alpha",
        ["AAA"],
        get_kline=lambda code, days: pd.DataFrame(),
        days=14,
        top_k=1,
        rebalance_days=1,
        hold_days=1,
        fee_rate=0.0,
    )

    assert result["ok"] is True
    first_trade = result["trades"][0]
    assert first_trade["signal_date"] == dates[0].strftime("%Y-%m-%d")
    assert first_trade["open_date"] == dates[1].strftime("%Y-%m-%d")
    assert first_trade["close_date"] == dates[2].strftime("%Y-%m-%d")
    assert first_trade["pnl_pct"] == -50.0
