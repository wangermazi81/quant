# Jianwei Alpha built-in factor
"""Relay heat v1: trend, volume confirmation, near-high strength and risk control."""
from __future__ import annotations

import pandas as pd

from analytics.factors_zoo.base import rank, safe_div, ts_max, ts_mean, ts_std

__alpha_meta__ = {
    "id": "jw_relay_heat_v1",
    "theme": ["relay", "sentiment", "momentum"],
    "formula_latex": "0.30R(ret_5)+0.25R(close/MA20)+0.20R(vol/volMA20)+0.15R(close/HHV20)-0.10R(volatility_10)-0.10R(drawdown_20)",
    "columns_required": ["high", "close", "volume"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 5,
    "min_warmup_bars": 25,
    "description_cn": "见微原创接力热度因子：偏好短期动量、站上MA20、量能确认且接近阶段高位的股票，同时惩罚高波动和阶段回撤。",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return a cross-sectional relay heat score using only current/past bars."""
    close = panel["close"]
    high = panel["high"]
    volume = panel["volume"]

    ret_5 = safe_div(close, close.shift(5)) - 1.0
    ma20_position = safe_div(close, ts_mean(close, 20)) - 1.0
    volume_confirm = safe_div(volume, ts_mean(volume, 20))
    near_high = safe_div(close, ts_max(high, 20))
    volatility_10 = ts_std(close.pct_change(fill_method=None), 10)
    drawdown_20 = 1.0 - safe_div(close, ts_max(high, 20))

    score = (
        rank(ret_5) * 0.30
        + rank(ma20_position) * 0.25
        + rank(volume_confirm) * 0.20
        + rank(near_high) * 0.15
        - rank(volatility_10) * 0.10
        - rank(drawdown_20) * 0.10
    )
    return score.replace([float("inf"), float("-inf")], pd.NA)
