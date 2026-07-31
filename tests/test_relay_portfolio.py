# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics.relay_portfolio import build_trade_path, limit_up_threshold, simulate_portfolio


def _bars(*, evaluation_low: float, evaluation_high: float, evaluation_close: float) -> pd.DataFrame:
    dates = pd.bdate_range("2026-07-01", periods=4)
    return pd.DataFrame(
        {
            "open": [9.8, 10.0, 10.0, 10.0],
            "high": [10.0, 12.0, evaluation_high, 10.2],
            "low": [9.7, 9.0, evaluation_low, 9.8],
            "close": [9.9, 10.1, evaluation_close, 10.0],
        },
        index=dates,
    )


def test_limit_up_threshold_distinguishes_a_share_boards():
    assert limit_up_threshold("000001.SZ", "2026-07-01") == 0.095
    assert limit_up_threshold("300001.SZ", "2026-07-01") == 0.195
    assert limit_up_threshold("688001.SH", "2026-07-01") == 0.195
    assert limit_up_threshold("920001.BJ", "2026-07-01") == 0.295
    assert limit_up_threshold("000001.SZ", "2026-07-01", is_st=True) == 0.045


@pytest.mark.parametrize(
    ("low", "high", "close", "reason", "gross_return"),
    [
        (9.50, 10.20, 10.00, "stop_loss", -3.5),
        (9.80, 10.80, 10.00, "take_profit", 7.0),
        (9.50, 10.80, 10.00, "stop_loss", -3.5),
        (9.80, 10.60, 10.40, "max_holding_period", 4.0),
    ],
)
def test_trade_path_applies_stop_target_and_conservative_same_bar_order(
    low: float,
    high: float,
    close: float,
    reason: str,
    gross_return: float,
):
    result = build_trade_path(
        code="000001.SZ",
        bars=_bars(evaluation_low=low, evaluation_high=high, evaluation_close=close),
        signal_index=0,
        hold_days=1,
        stop_loss=-0.035,
        take_profit=0.07,
        round_trip_cost_pct=0.16,
        slippage_pct=0.20,
    )

    assert result["entry_date"] == "2026-07-02"
    assert result["exit_date"] == "2026-07-03"
    assert result["exit_reason"] == reason
    assert result["plan_return_pct"] == gross_return
    assert result["live_adjusted_return_pct"] == pytest.approx(gross_return - 0.36)


def test_trade_path_does_not_sell_on_entry_day_under_t_plus_one():
    result = build_trade_path(
        code="000001.SZ",
        bars=_bars(evaluation_low=9.8, evaluation_high=10.6, evaluation_close=10.4),
        signal_index=0,
        hold_days=1,
        stop_loss=-0.035,
        take_profit=0.07,
    )

    assert result["exit_reason"] == "max_holding_period"
    assert result["exit_date"] == "2026-07-03"


def test_trade_path_rejects_zero_volume_entry_bar():
    bars = _bars(evaluation_low=9.8, evaluation_high=10.6, evaluation_close=10.4)
    bars["volume"] = [1000.0, 0.0, 1000.0, 1000.0]

    result = build_trade_path(
        code="000001.SZ",
        bars=bars,
        signal_index=0,
        hold_days=1,
        stop_loss=-0.035,
        take_profit=0.07,
    )

    assert result == {}


def test_trade_path_does_not_fallback_missing_entry_open_to_close():
    bars = _bars(evaluation_low=9.8, evaluation_high=10.6, evaluation_close=10.4)
    bars.loc[bars.index[1], "open"] = None

    result = build_trade_path(
        code="000001.SZ",
        bars=bars,
        signal_index=0,
        hold_days=1,
        stop_loss=-0.035,
        take_profit=0.07,
    )

    assert result == {}


def test_trade_path_defers_exit_while_one_price_limit_down_is_locked():
    dates = pd.bdate_range("2026-07-01", periods=5)
    bars = pd.DataFrame(
        {
            "open": [10.0, 10.0, 9.05, 8.80, 8.90],
            "high": [10.1, 10.2, 9.05, 9.00, 9.10],
            "low": [9.9, 9.8, 9.05, 8.70, 8.80],
            "close": [10.0, 10.0, 9.05, 8.90, 9.00],
        },
        index=dates,
    )

    result = build_trade_path(
        code="000001.SZ",
        bars=bars,
        signal_index=0,
        hold_days=1,
        stop_loss=-0.035,
        take_profit=0.07,
        round_trip_cost_pct=0.0,
        slippage_pct=0.0,
    )

    assert result["exit_date"] == dates[3].strftime("%Y-%m-%d")
    assert result["exit_reason"] == "deferred_limit_down_exit"
    assert result["exit_price"] == 8.8
    assert result["plan_return_pct"] == pytest.approx(-12.0)


def test_trade_path_defers_exit_when_holding_deadline_has_no_bar():
    dates = pd.bdate_range("2026-07-01", periods=5)
    bars = pd.DataFrame(
        {
            "open": [10.0, 10.0, None, 8.80, 8.90],
            "high": [10.1, 10.2, None, 9.00, 9.10],
            "low": [9.9, 9.8, None, 8.70, 8.80],
            "close": [10.0, 10.0, None, 8.90, 9.00],
        },
        index=dates,
    )

    result = build_trade_path(
        code="000001.SZ",
        bars=bars,
        signal_index=0,
        hold_days=1,
        stop_loss=-0.035,
        take_profit=0.07,
        round_trip_cost_pct=0.0,
        slippage_pct=0.0,
    )

    assert result["exit_date"] == dates[3].strftime("%Y-%m-%d")
    assert result["exit_reason"] == "deferred_missing_bar_exit"
    assert result["exit_price"] == 8.8
    assert result["plan_return_pct"] == pytest.approx(-12.0)
    assert result["valuation_path"][1] == {
        "date": dates[2].strftime("%Y-%m-%d"),
        "return_pct": 0.0,
    }


def test_trade_path_defers_exit_after_suspension_then_locked_limit_down():
    dates = pd.bdate_range("2026-07-01", periods=5)
    bars = pd.DataFrame(
        {
            "open": [10.0, 10.0, None, 9.05, 8.80],
            "high": [10.1, 10.2, None, 9.05, 9.00],
            "low": [9.9, 9.8, None, 9.05, 8.70],
            "close": [10.0, 10.0, None, 9.05, 8.90],
            "volume": [1000.0, 1000.0, None, 1000.0, 1000.0],
        },
        index=dates,
    )

    result = build_trade_path(
        code="000001.SZ",
        bars=bars,
        signal_index=0,
        hold_days=1,
        stop_loss=-0.035,
        take_profit=0.07,
        round_trip_cost_pct=0.0,
        slippage_pct=0.0,
    )

    assert result["exit_date"] == dates[4].strftime("%Y-%m-%d")
    assert result["exit_reason"] == "deferred_limit_down_exit"
    assert result["exit_price"] == 8.8
    assert result["plan_return_pct"] == pytest.approx(-12.0)


def test_trade_path_fails_closed_after_deferred_exit_limit():
    dates = pd.bdate_range("2026-07-01", periods=8)
    bars = pd.DataFrame(
        {
            "open": [10.0, 10.0, None, None, None, None, None, 8.8],
            "high": [10.1, 10.2, None, None, None, None, None, 9.0],
            "low": [9.9, 9.8, None, None, None, None, None, 8.7],
            "close": [10.0, 10.0, None, None, None, None, None, 8.9],
            "volume": [
                1000.0,
                1000.0,
                None,
                None,
                None,
                None,
                None,
                1000.0,
            ],
        },
        index=dates,
    )

    result = build_trade_path(
        code="000001.SZ",
        bars=bars,
        signal_index=0,
        hold_days=1,
        stop_loss=-0.035,
        take_profit=0.07,
        max_deferred_exit_sessions=3,
    )

    assert result == {}


def _attempt(
    code: str,
    entry_date: str,
    exit_date: str,
    return_pct: float,
    *,
    blocked: bool = False,
) -> dict:
    return {
        "trade_date": entry_date,
        "ts_code": code,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "execution_blocked": blocked,
        "live_adjusted_return_pct": return_pct,
        "valuation_path": [
            {"date": entry_date, "return_pct": 0.0},
            {"date": exit_date, "return_pct": return_pct},
        ],
        "gate_pass": True,
    }


def test_portfolio_keeps_blocked_slots_as_cash_and_prevents_capital_reuse():
    attempts = [
        _attempt("AAA", "2026-07-02", "2026-07-03", 10.0),
        _attempt("BLOCKED", "2026-07-02", "2026-07-03", 50.0, blocked=True),
        _attempt("BBB", "2026-07-03", "2026-07-06", 0.0),
        _attempt("CCC", "2026-07-03", "2026-07-06", 0.0),
    ]

    result = simulate_portfolio(attempts, top_k=2, holding_sessions=2, initial_equity=1.0)

    assert result["attempted_slots"] == 4
    assert result["blocked_slots"] == 1
    assert result["executed_trade_count"] == 3
    assert result["executed_trades"][0]["allocation"] == pytest.approx(0.25)
    assert result["total_return_pct"] == pytest.approx(2.5)
    assert result["max_gross_exposure"] <= 1.0
    assert result["min_cash"] >= 0.0


def test_portfolio_caps_allocation_at_frozen_target_position_pct():
    attempt = {
        **_attempt("AAA", "2026-07-02", "2026-07-03", 10.0),
        "target_position_pct": 0.1,
    }

    result = simulate_portfolio(
        [attempt],
        top_k=1,
        holding_sessions=1,
        initial_equity=1.0,
    )

    assert result["executed_trades"][0]["allocation"] == pytest.approx(0.1)
    assert result["executed_trades"][0]["target_position_pct"] == 0.1
    assert result["total_return_pct"] == pytest.approx(1.0)


def test_portfolio_preserves_explicit_empty_session_dates_as_zero_return_days():
    sessions = [
        "2026-07-02",
        "2026-07-03",
        "2026-07-06",
    ]

    result = simulate_portfolio(
        [],
        top_k=1,
        holding_sessions=1,
        initial_equity=1.0,
        session_dates=sessions,
    )

    assert [item["date"] for item in result["equity_curve"]] == [
        "initial",
        *sessions,
    ]
    assert [item["equity"] for item in result["equity_curve"]] == [
        1.0,
        1.0,
        1.0,
        1.0,
    ]
