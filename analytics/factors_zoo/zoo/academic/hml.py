"""见微 Alpha 内置因子 academic_hml

经典风格 1993 HML 价值因子代理：基于历史价格反转的价值估计
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.factors_zoo.base import delta, safe_div

__alpha_meta__ = {
    'id': 'academic_hml',
    'nickname': '',
    'theme': ['value'],
    'formula_latex': r'\mathrm{zscore}_{x}\bigl(-(\mathrm{close}_t - \mathrm{close}_{t-252}) / \mathrm{close}_{t-252}\bigr)',
    'columns_required': ['close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk'],
    'frequency': ['1d'],
    'decay_horizon': 60,
    'min_warmup_bars': 252,
    'notes': (
        ''
        'The original definition uses book-to-market ratio from fundamental data; here '
        'we use the negative 252-day total return as a long-term reversal proxy, then '
        'cross-sectional z-score per date for long-short ranking. Top z-scores = '
        'long-term underperformers (deeper value). Canonical 252d window; declared '
        'decay_horizon=60 due to registry schema cap (le=60); real signal horizon=252.'
    ),
    'description_cn': '经典风格 1993 HML 价值因子代理：基于历史价格反转的价值估计',
}


def _cross_sectional_zscore(df: pd.DataFrame) -> pd.DataFrame:
    """Per-row z-score: (x - row_mean) / row_std; zero/NaN std rows -> NaN."""
    mean = df.mean(axis=1, skipna=True)
    std = df.std(axis=1, ddof=1, skipna=True)
    centered = df.sub(mean, axis=0)
    result = centered.div(std.where(std > 0), axis=0)
    return result.replace([np.inf, -np.inf], np.nan)


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return inverse 252-day return cross-sectional z-score per stock.

    Uses the canonical 252-day window without silent shrink on short panels.
    Short panels produce an all-NaN result, which the registry surfaces as a
    >95% NaN error (RegistryError) so the user sees "insufficient history"
    instead of a misleading shrunk-window value.
    """
    close = panel['close']
    ret = safe_div(delta(close, 252), close.shift(252))
    return _cross_sectional_zscore(-ret)
