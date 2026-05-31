"""见微 Alpha 内置因子 academic_carhart_mom

动量风格 动量因子：跳过最近 1 月的 12-2 动量
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.factors_zoo.base import delta, safe_div

__alpha_meta__ = {
    'id': 'academic_carhart_mom',
    'nickname': '动量风格 momentum — 12m-1m return',
    'theme': ['momentum'],
    'formula_latex': r'\mathrm{zscore}_{x}\bigl((\mathrm{close}_t - \mathrm{close}_{t-252})/\mathrm{close}_{t-252} - (\mathrm{close}_t - \mathrm{close}_{t-21})/\mathrm{close}_{t-21}\bigr)',
    'columns_required': ['close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk'],
    'frequency': ['1d'],
    'decay_horizon': 60,
    'min_warmup_bars': 252,
    'notes': (
        '动量风格. 12-month return minus 1-month return, '
        'cross-sectional z-score per date for long-short ranking. Top z-scores = '
        'winners. Constructed directly from prices, so this matches the original '
        'definition modulo the z-score wrapper. Canonical 252d window; declared '
        'decay_horizon=60 due to registry schema cap (le=60); real signal horizon=252.'
    ),
    'description_cn': '动量风格 动量因子：跳过最近 1 月的 12-2 动量',
}


def _cross_sectional_zscore(df: pd.DataFrame) -> pd.DataFrame:
    """Per-row z-score: (x - row_mean) / row_std; zero/NaN std rows -> NaN."""
    mean = df.mean(axis=1, skipna=True)
    std = df.std(axis=1, ddof=1, skipna=True)
    centered = df.sub(mean, axis=0)
    result = centered.div(std.where(std > 0), axis=0)
    return result.replace([np.inf, -np.inf], np.nan)


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return 252-day minus 21-day return z-score (动量 UMD).

    Uses canonical (252, 21) windows without silent shrink on short panels.
    Short panels produce all-NaN; the registry surfaces this as >95% NaN
    (RegistryError) rather than returning a misleading shrunk-window value.
    """
    close = panel['close']
    ret_long = safe_div(delta(close, 252), close.shift(252))
    ret_short = safe_div(delta(close, 21), close.shift(21))
    return _cross_sectional_zscore(ret_long - ret_short)
