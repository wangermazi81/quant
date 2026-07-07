# Jianwei Alpha built-in factor
"""Overheat avoid v1: avoid crowded, overextended short-term heat."""
from __future__ import annotations

import pandas as pd

from analytics.factors_zoo.base import rank, safe_div, ts_max, ts_mean, ts_std

__alpha_meta__ = {
    "id": "jw_overheat_avoid_v1",
    "theme": ["overheat", "reversal", "risk-control"],
    "formula_latex": "-(0.20R(limit20)+0.20R(ret20)+0.20R(close/HHV20)+0.15R(vol/volMA20)+0.15R(-ret3)+0.10R(-volatility10))",
    "columns_required": ["high", "close", "volume"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 5,
    "min_warmup_bars": 25,
    "description_cn": "见微原创过热回避因子：回避20日内涨停代理次数多、阶段涨幅大、贴近高位、明显放量、短线急拉且波动偏高的拥挤标的。",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return a cross-sectional overheat-avoid score using only current/past bars."""
    close = panel["close"]
    high = panel["high"]
    volume = panel["volume"]

    ret_1 = safe_div(close, close.shift(1)) - 1.0
    ret_3 = safe_div(close, close.shift(3)) - 1.0
    ret_20 = safe_div(close, close.shift(20)) - 1.0
    limit_count_20 = (ret_1 >= 0.095).rolling(20).sum()
    near_high_20 = safe_div(close, ts_max(high, 20))
    volume_ratio_20 = safe_div(volume, ts_mean(volume, 20))
    volatility_10 = ts_std(close.pct_change(fill_method=None), 10)

    overheat = (
        rank(limit_count_20) * 0.20
        + rank(ret_20) * 0.20
        + rank(near_high_20) * 0.20
        + rank(volume_ratio_20) * 0.15
        + rank(-ret_3) * 0.15
        + rank(-volatility_10) * 0.10
    )
    return (-overheat).replace([float("inf"), float("-inf")], pd.NA)
