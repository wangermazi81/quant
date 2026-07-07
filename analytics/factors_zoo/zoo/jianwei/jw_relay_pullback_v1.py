# Jianwei Alpha built-in factor
"""Relay pullback v1: trend continuation with a short-term chase penalty."""
from __future__ import annotations

import pandas as pd

from analytics.factors_zoo.base import rank, safe_div, ts_max, ts_mean, ts_std

__alpha_meta__ = {
    "id": "jw_relay_pullback_v1",
    "theme": ["relay", "momentum", "pullback"],
    "formula_latex": "0.25R(ret_20)+0.20R(close/MA20)+0.15R(vol/volMA20)+0.20R(close/HHV20)-0.45R(ret_3)-0.25R(volatility_10)",
    "columns_required": ["high", "close", "volume"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 5,
    "min_warmup_bars": 25,
    "description_cn": "Jianwei original relay pullback factor: prefers medium-term trend continuation with volume confirmation and near-high structure, while penalizing short-term overextension and volatility.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return a cross-sectional relay-pullback score using only current/past bars."""
    close = panel["close"]
    high = panel["high"]
    volume = panel["volume"]

    ret_3 = safe_div(close, close.shift(3)) - 1.0
    ret_20 = safe_div(close, close.shift(20)) - 1.0
    ma20_position = safe_div(close, ts_mean(close, 20)) - 1.0
    volume_confirm = safe_div(volume, ts_mean(volume, 20))
    near_high = safe_div(close, ts_max(high, 20))
    volatility_10 = ts_std(close.pct_change(fill_method=None), 10)

    score = (
        rank(ret_20) * 0.25
        + rank(ma20_position) * 0.20
        + rank(volume_confirm) * 0.15
        + rank(near_high) * 0.20
        - rank(ret_3) * 0.45
        - rank(volatility_10) * 0.25
    )
    return score.replace([float("inf"), float("-inf")], pd.NA)
