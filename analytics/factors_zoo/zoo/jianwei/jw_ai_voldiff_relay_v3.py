# Jianwei Alpha AI-mined factor
"""AI volume-divergence relay v3.

This factor was mined from local walk-forward tests on the current A-share daily
cache. It keeps the empirically strongest signal simple: short-window
volume/price divergence, then adds relay structure and overheat avoidance as
risk controls. All inputs use only current and historical daily bars.
"""
from __future__ import annotations

import pandas as pd

from analytics.factors_zoo.base import rank, safe_div, ts_corr, ts_max, ts_mean, ts_rank, ts_std

__alpha_meta__ = {
    "id": "jw_ai_voldiff_relay_v3",
    "theme": ["ai-mined", "volume-divergence", "relay", "risk-control"],
    "formula_latex": "0.65R(-Corr(TsRank(volume,3),TsRank(high,3),3))+0.20R(relay)+0.15R(overheat_avoid)",
    "columns_required": ["high", "close", "volume"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 8,
    "min_warmup_bars": 25,
    "description_cn": "AI挖掘量价背离接力V3：短窗口成交量与高价背离为核心，叠加接力结构和过热规避；建议8个交易日持有、Top20等权观察。",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    close = panel["close"]
    high = panel["high"]
    volume = panel["volume"]

    ret_1 = safe_div(close, close.shift(1)) - 1.0
    ret_3 = safe_div(close, close.shift(3)) - 1.0
    ret_20 = safe_div(close, close.shift(20)) - 1.0
    close_to_ma20 = safe_div(close, ts_mean(close, 20)) - 1.0
    volume_ratio_20 = safe_div(volume, ts_mean(volume, 20))
    near_high_20 = safe_div(close, ts_max(high, 20))
    volatility_10 = ts_std(close.pct_change(fill_method=None), 10)
    limit_count_20 = (ret_1 >= 0.095).rolling(20).sum()

    volume_divergence = -1.0 * ts_corr(ts_rank(volume, 3), ts_rank(high, 3), 3)
    relay = (
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

    score = (
        rank(volume_divergence).fillna(0.5) * 0.65
        + rank(relay).fillna(0.5) * 0.20
        + rank(overheat_avoid).fillna(0.5) * 0.15
    )
    return score.where(close.notna()).replace([float("inf"), float("-inf")], pd.NA)
