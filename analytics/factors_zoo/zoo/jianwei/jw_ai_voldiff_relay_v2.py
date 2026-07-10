# Jianwei Alpha AI-mined factor
"""AI volume-divergence relay v2.

The factor was selected from local walk-forward research on the current A-share
cache. It uses only current and historical daily bars: volume/price divergence
is the core signal, then relay pullback structure, MA20 pullback position, and
overheat avoidance are blended as risk controls.
"""
from __future__ import annotations

import pandas as pd

from analytics.factors_zoo.base import rank, safe_div, ts_corr, ts_max, ts_mean, ts_rank, ts_std

__alpha_meta__ = {
    "id": "jw_ai_voldiff_relay_v2",
    "theme": ["ai-mined", "volume-divergence", "relay", "risk-control"],
    "formula_latex": "0.55R(voldiff)+0.20R(relay_pullback)+0.15R(MA20/close)+0.10R(overheat_avoid)",
    "columns_required": ["high", "close", "volume"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 8,
    "min_warmup_bars": 25,
    "description_cn": "AI挖掘量价背离接力因子：以量价背离修复为核心，叠加接力回踩、20日均线回归与过热规避。",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    close = panel["close"]
    high = panel["high"]
    volume = panel["volume"]

    ret_1 = safe_div(close, close.shift(1)) - 1.0
    ret_3 = safe_div(close, close.shift(3)) - 1.0
    ret_20 = safe_div(close, close.shift(20)) - 1.0
    ma20_position = safe_div(ts_mean(close, 20), close)
    close_to_ma20 = safe_div(close, ts_mean(close, 20)) - 1.0
    volume_ratio_20 = safe_div(volume, ts_mean(volume, 20))
    near_high_20 = safe_div(close, ts_max(high, 20))
    volatility_10 = ts_std(close.pct_change(fill_method=None), 10)
    limit_count_20 = (ret_1 >= 0.095).rolling(20).sum()

    volume_divergence = -1.0 * ts_max(ts_corr(ts_rank(volume, 5), ts_rank(high, 5), 5), 3)
    relay_pullback = (
        rank(ret_20) * 0.25
        + rank(close_to_ma20) * 0.20
        + rank(volume_ratio_20) * 0.15
        + rank(near_high_20) * 0.20
        - rank(ret_3) * 0.45
        - rank(volatility_10) * 0.25
    )
    overheat_avoid = -(
        rank(limit_count_20) * 0.20
        + rank(ret_20) * 0.20
        + rank(near_high_20) * 0.20
        + rank(volume_ratio_20) * 0.15
        + rank(-ret_3) * 0.15
        + rank(-volatility_10) * 0.10
    )

    # Rank each component first, then fill unavailable component ranks with a
    # neutral percentile so tiny universes/constant windows do not disappear.
    score = (
        rank(volume_divergence).fillna(0.5) * 0.55
        + rank(relay_pullback).fillna(0.5) * 0.20
        + rank(ma20_position).fillna(0.5) * 0.15
        + rank(overheat_avoid).fillna(0.5) * 0.10
    )
    return score.where(close.notna()).replace([float("inf"), float("-inf")], pd.NA)
