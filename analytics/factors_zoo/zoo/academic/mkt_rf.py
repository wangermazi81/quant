"""见微 Alpha 内置因子 academic_mkt_rf

经典风格 1993 MKT-RF 市场风险溢价代理
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.factors_zoo.base import delta, safe_div

__alpha_meta__ = {
    'id': 'academic_mkt_rf',
    'nickname': '',
    'theme': ['momentum'],
    'formula_latex': r'\mathrm{zscore}_{x}\bigl((\mathrm{close}_t - \mathrm{close}_{t-21}) / \mathrm{close}_{t-21}\bigr)',
    'columns_required': ['close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk'],
    'frequency': ['1d'],
    'decay_horizon': 21,
    'min_warmup_bars': 21,
    'notes': (
        ''
        'The original definition uses value-weighted market excess returns; here we '
        'use a 21-day per-stock total return and cross-sectional z-score per date '
        'for long-short ranking. Top z-scores = strong recent winners; bottom = losers.'
    ),
    'description_cn': '经典风格 1993 MKT-RF 市场风险溢价代理',
}


def _cross_sectional_zscore(df: pd.DataFrame) -> pd.DataFrame:
    """Per-row z-score: (x - row_mean) / row_std; zero/NaN std rows -> NaN."""
    mean = df.mean(axis=1, skipna=True)
    std = df.std(axis=1, ddof=1, skipna=True)
    centered = df.sub(mean, axis=0)
    # std.where(std > 0) turns 0 / NaN std rows into NaN, which propagates
    # through div to NaN — no silent inf.
    result = centered.div(std.where(std > 0), axis=0)
    return result.replace([np.inf, -np.inf], np.nan)


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return 21-day return cross-sectional z-score per stock."""
    close = panel['close']
    ret = safe_div(delta(close, 21), close.shift(21))
    return _cross_sectional_zscore(ret)
