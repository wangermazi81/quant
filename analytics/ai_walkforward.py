# -*- coding: utf-8 -*-
from __future__ import annotations


def walk_forward_splits(rows, train_size: int, test_size: int, step: int | None = None) -> list[dict]:
    ordered = sorted(list(rows or []), key=lambda r: str(r.get("as_of_date") or ""))
    train_size = int(train_size)
    test_size = int(test_size)
    step = int(step or test_size)
    if train_size <= 0 or test_size <= 0 or step <= 0:
        raise ValueError("train_size, test_size and step must be positive")

    windows = []
    start = 0
    while start + train_size + test_size <= len(ordered):
        train = ordered[start : start + train_size]
        test = ordered[start + train_size : start + train_size + test_size]
        train_end = str(train[-1].get("as_of_date"))
        test_start = str(test[0].get("as_of_date"))
        if train_end >= test_start:
            raise ValueError("walk-forward split would leak future samples")
        windows.append({"train": train, "test": test, "train_end": train_end, "test_start": test_start})
        start += step
    return windows


def calculate_metrics(trades) -> dict:
    values = [_to_float(t.get("pnl_pct"), 0.0) for t in trades or []]
    if not values:
        return {"trades": 0, "hit_rate": 0.0, "total_return_pct": 0.0, "avg_return_pct": 0.0, "max_drawdown_pct": 0.0}
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    wins = sum(1 for v in values if v > 0)
    return {
        "trades": len(values),
        "hit_rate": wins / len(values),
        "total_return_pct": round(sum(values), 4),
        "avg_return_pct": round(sum(values) / len(values), 4),
        "max_drawdown_pct": round(max_dd, 4),
    }


def _to_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
