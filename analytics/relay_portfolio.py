# -*- coding: utf-8 -*-
"""Deterministic trade-path and portfolio accounting for relay research."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any, Sequence

import numpy as np
import pandas as pd


def _date_text(value: Any) -> str:
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.strftime("%Y-%m-%d")
    return str(value or "")[:10]


def limit_up_threshold(code: str, trade_date: Any, is_st: bool = False) -> float:
    """Return a conservative daily-return threshold used to identify limit-up bars."""
    if is_st:
        return 0.045
    raw = str(code or "").strip().upper()
    digits = raw.split(".")[0]
    suffix = raw.split(".", 1)[1] if "." in raw else ""
    if suffix == "BJ" or digits.startswith(("4", "8", "92")):
        return 0.295
    if digits.startswith(("688", "689")):
        return 0.195
    if digits.startswith(("300", "301")) and _date_text(trade_date) >= "2020-08-24":
        return 0.195
    return 0.095


def _price(row: pd.Series, field: str, fallback: float | None = None) -> float | None:
    try:
        value = float(row.get(field))
    except (TypeError, ValueError):
        value = np.nan
    if np.isfinite(value) and value > 0:
        return value
    return fallback


def _row_is_st(row: pd.Series) -> bool:
    value = row.get("is_st", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value) if pd.notna(value) else False


def _has_tradable_bar(row: pd.Series) -> bool:
    if any(_price(row, field) is None for field in ("open", "high", "low", "close")):
        return False
    if "volume" in row.index and _price(row, "volume") is None:
        return False
    return True


def _is_locked_limit_down(code: str, bars: pd.DataFrame, index: int) -> bool:
    if index <= 0:
        return False
    row = bars.iloc[index]
    previous_close = _price(bars.iloc[index - 1], "close")
    if previous_close is None:
        return False
    prices = [_price(row, field) for field in ("open", "high", "low", "close")]
    if any(value is None for value in prices):
        return False
    threshold = limit_up_threshold(code, bars.index[index], is_st=_row_is_st(row))
    conservative_limit_price = previous_close * (1.0 - threshold)
    exchange_limit_price = previous_close * (1.0 - min(0.30, threshold + 0.005))
    tolerance = max(0.011, previous_close * 0.001)
    return bool(
        max(prices) - min(prices) <= tolerance
        and exchange_limit_price - tolerance <= max(prices) <= conservative_limit_price + tolerance
    )


def build_trade_path(
    code: str,
    bars: pd.DataFrame,
    signal_index: int,
    hold_days: int = 2,
    stop_loss: float = -0.035,
    take_profit: float = 0.07,
    round_trip_cost_pct: float = 0.16,
    slippage_pct: float = 0.20,
) -> dict[str, Any]:
    """Simulate a next-open long trade using only daily bars.

    Stop/target checks start one session after entry because A shares cannot be
    sold on the entry day. If both levels are touched on one bar, stop loss wins.
    """
    if hold_days < 1:
        raise ValueError("hold_days must be positive")
    if stop_loss >= 0 or take_profit <= 0:
        raise ValueError("stop_loss must be negative and take_profit must be positive")
    required = {"open", "high", "low", "close"}
    if not required.issubset(bars.columns):
        raise ValueError(f"bars missing required columns: {sorted(required - set(bars.columns))}")

    entry_index = int(signal_index) + 1
    max_exit_index = entry_index + int(hold_days)
    if signal_index < 0 or max_exit_index >= len(bars):
        return {}

    entry_row = bars.iloc[entry_index]
    if "volume" in entry_row.index and _price(entry_row, "volume") is None:
        return {}
    entry_price = _price(entry_row, "open", _price(entry_row, "close"))
    if entry_price is None:
        return {}

    stop_price = entry_price * (1.0 + float(stop_loss))
    target_price = entry_price * (1.0 + float(take_profit))
    exit_index: int | None = None
    exit_price: float | None = None
    exit_reason = ""
    missing_bar_seen = False
    locked_limit_down_seen = False

    for index in range(entry_index + 1, len(bars)):
        row = bars.iloc[index]
        if not _has_tradable_bar(row):
            missing_bar_seen = True
            continue
        open_price = _price(row, "open")
        low_price = _price(row, "low")
        high_price = _price(row, "high")
        assert open_price is not None and low_price is not None and high_price is not None
        if _is_locked_limit_down(code, bars, index):
            locked_limit_down_seen = True
            continue
        if index > max_exit_index:
            if locked_limit_down_seen:
                deferred_reason = "deferred_limit_down_exit"
            elif missing_bar_seen:
                deferred_reason = "deferred_missing_bar_exit"
            else:
                deferred_reason = "deferred_exit"
            exit_index, exit_price, exit_reason = index, open_price, deferred_reason
            break
        if open_price <= stop_price:
            exit_index, exit_price, exit_reason = index, open_price, "stop_loss_gap"
            break
        if open_price >= target_price:
            exit_index, exit_price, exit_reason = index, open_price, "take_profit_gap"
            break
        stop_hit = low_price <= stop_price
        target_hit = high_price >= target_price
        if stop_hit:
            exit_index, exit_price, exit_reason = index, stop_price, "stop_loss"
            break
        if target_hit:
            exit_index, exit_price, exit_reason = index, target_price, "take_profit"
            break
        if index == max_exit_index:
            exit_index = index
            exit_price = _price(row, "close", open_price) or open_price
            exit_reason = "max_holding_period"
            break

    if exit_index is None or exit_price is None:
        return {}

    total_cost_pct = float(round_trip_cost_pct) + float(slippage_pct)
    gross_return_pct = (float(exit_price) / entry_price - 1.0) * 100.0
    valuation_path: list[dict[str, Any]] = []
    last_mark_price = float(entry_price)
    for index in range(entry_index, exit_index + 1):
        if index == exit_index:
            mark_price = float(exit_price)
        else:
            close_price = _price(bars.iloc[index], "close")
            if close_price is not None:
                last_mark_price = float(close_price)
            mark_price = last_mark_price
        mark_return_pct = (mark_price / entry_price - 1.0) * 100.0 - total_cost_pct
        valuation_path.append(
            {
                "date": _date_text(bars.index[index]),
                "return_pct": round(float(mark_return_pct), 4),
            }
        )

    return {
        "entry_date": _date_text(bars.index[entry_index]),
        "exit_date": _date_text(bars.index[exit_index]),
        "entry_price": round(float(entry_price), 4),
        "exit_price": round(float(exit_price), 4),
        "exit_reason": exit_reason,
        "plan_return_pct": round(float(gross_return_pct), 4),
        "live_adjusted_return_pct": round(float(gross_return_pct - total_cost_pct), 4),
        "valuation_path": valuation_path,
        "path_policy": "t_plus_1_stop_first_on_same_bar",
    }


def _path_map(attempt: dict[str, Any]) -> dict[str, float]:
    path = attempt.get("valuation_path")
    if not isinstance(path, list):
        return {}
    result: dict[str, float] = {}
    for item in path:
        if not isinstance(item, dict):
            continue
        day = _date_text(item.get("date"))
        try:
            value = float(item.get("return_pct"))
        except (TypeError, ValueError):
            continue
        if day and np.isfinite(value):
            result[day] = value
    return result


def simulate_portfolio(
    attempts: Sequence[dict[str, Any]],
    top_k: int,
    holding_sessions: int,
    initial_equity: float = 1.0,
) -> dict[str, Any]:
    """Simulate cash and overlapping positions without slot redistribution."""
    if top_k <= 0 or holding_sessions <= 0 or initial_equity <= 0:
        raise ValueError("top_k, holding_sessions and initial_equity must be positive")

    clean_attempts = [dict(item) for item in attempts if isinstance(item, dict)]
    by_entry: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_dates: set[str] = set()
    for attempt in clean_attempts:
        entry_date = _date_text(attempt.get("entry_date"))
        exit_date = _date_text(attempt.get("exit_date"))
        if entry_date:
            by_entry[entry_date].append(attempt)
            all_dates.add(entry_date)
        if exit_date:
            all_dates.add(exit_date)
        all_dates.update(_path_map(attempt))

    cash = float(initial_equity)
    min_cash = cash
    positions: list[dict[str, Any]] = []
    executed_trades: list[dict[str, Any]] = []
    realized_returns: list[float] = []
    equity_curve = [{"date": "initial", "equity": round(cash, 8), "cash": round(cash, 8), "gross_exposure": 0.0}]
    max_gross_exposure = 0.0
    utilization_values: list[float] = []
    cash_values: list[float] = []
    blocked_slots = 0
    gate_skipped_slots = 0
    cash_limited_slots = 0

    for current_date in sorted(all_dates):
        current_equity = cash + sum(float(position["current_value"]) for position in positions)
        cohort_budget = current_equity / float(holding_sessions)
        slot_budget = cohort_budget / float(top_k)

        for attempt in by_entry.get(current_date, []):
            if not bool(attempt.get("gate_pass", True)):
                gate_skipped_slots += 1
                continue
            if bool(attempt.get("execution_blocked", False)):
                blocked_slots += 1
                continue
            target_position_pct = attempt.get("target_position_pct")
            try:
                target_position_pct = float(target_position_pct)
            except (TypeError, ValueError):
                target_position_pct = None
            if (
                target_position_pct is None
                or not np.isfinite(target_position_pct)
                or target_position_pct < 0.0
            ):
                target_budget = slot_budget
                target_position_pct = None
            else:
                target_budget = current_equity * target_position_pct
            allocation = min(slot_budget, target_budget, cash)
            if allocation <= 1e-12:
                cash_limited_slots += 1
                continue
            cash -= allocation
            min_cash = min(min_cash, cash)
            trade = {
                "trade_date": _date_text(attempt.get("trade_date")),
                "ts_code": str(attempt.get("ts_code") or ""),
                "entry_date": current_date,
                "exit_date": _date_text(attempt.get("exit_date")),
                "allocation": float(allocation),
                "target_position_pct": target_position_pct,
                "return_pct": float(attempt.get("live_adjusted_return_pct") or 0.0),
            }
            executed_trades.append(trade)
            positions.append(
                {
                    **trade,
                    "current_value": float(allocation),
                    "last_return_pct": 0.0,
                    "valuation_path": _path_map(attempt),
                }
            )

        for position in positions:
            path = position["valuation_path"]
            if current_date in path:
                position["last_return_pct"] = float(path[current_date])
            position["current_value"] = position["allocation"] * (
                1.0 + float(position["last_return_pct"]) / 100.0
            )

        pre_exit_equity = cash + sum(float(position["current_value"]) for position in positions)
        pre_exit_exposure = (
            sum(float(position["current_value"]) for position in positions) / pre_exit_equity
            if pre_exit_equity > 0
            else 0.0
        )
        max_gross_exposure = max(max_gross_exposure, pre_exit_exposure)

        remaining: list[dict[str, Any]] = []
        for position in positions:
            if position["exit_date"] and position["exit_date"] <= current_date:
                cash += float(position["current_value"])
                realized_returns.append(float(position["return_pct"]))
            else:
                remaining.append(position)
        positions = remaining

        equity = cash + sum(float(position["current_value"]) for position in positions)
        gross_exposure = (
            sum(float(position["current_value"]) for position in positions) / equity
            if equity > 0
            else 0.0
        )
        cash_pct = cash / equity if equity > 0 else 0.0
        utilization_values.append(pre_exit_exposure)
        cash_values.append(cash_pct)
        equity_curve.append(
            {
                "date": current_date,
                "equity": round(float(equity), 8),
                "cash": round(float(cash), 8),
                "gross_exposure": round(float(gross_exposure), 8),
            }
        )

    equity_values = np.array([float(item["equity"]) for item in equity_curve], dtype=float)
    peaks = np.maximum.accumulate(equity_values)
    drawdowns = np.divide(equity_values, peaks, out=np.ones_like(equity_values), where=peaks != 0) - 1.0
    final_equity = float(equity_values[-1]) if equity_values.size else float(initial_equity)
    return {
        "initial_equity": round(float(initial_equity), 8),
        "final_equity": round(final_equity, 8),
        "total_return_pct": round((final_equity / float(initial_equity) - 1.0) * 100.0, 4),
        "max_drawdown_pct": round(float(drawdowns.min()) * 100.0, 4),
        "attempted_slots": len(clean_attempts),
        "blocked_slots": int(blocked_slots),
        "gate_skipped_slots": int(gate_skipped_slots),
        "cash_limited_slots": int(cash_limited_slots),
        "executed_trade_count": len(executed_trades),
        "hit_rate": round(sum(1 for value in realized_returns if value > 0) / len(realized_returns), 4)
        if realized_returns
        else 0.0,
        "average_trade_return_pct": round(float(np.mean(realized_returns)), 4) if realized_returns else 0.0,
        "average_capital_utilization": round(float(np.mean(utilization_values)), 4) if utilization_values else 0.0,
        "average_cash_pct": round(float(np.mean(cash_values)), 4) if cash_values else 1.0,
        "max_gross_exposure": round(float(max_gross_exposure), 6),
        "min_cash": round(float(min_cash), 8),
        "executed_trades": executed_trades,
        "equity_curve": equity_curve,
    }
