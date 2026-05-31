"""见微 Alpha 内置因子 academic_smb

经典风格 1993 SMB 小盘因子代理：60日成交额倒数（小流动性=高分）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.factors_zoo.base import ts_mean

__alpha_meta__ = {
    'id': 'academic_smb',
    'nickname': '',
    'theme': ['quality'],
    'formula_latex': r'\mathrm{zscore}_{x}\bigl(-\log(\mathrm{ts\_mean}(\mathrm{volume} \cdot \mathrm{close},\,60) + 1)\bigr)',
    'columns_required': ['close', 'volume'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk'],
    'frequency': ['1d'],
    'decay_horizon': 60,
    'min_warmup_bars': 60,
    'notes': (
        ''
        'The original definition uses market capitalization from book equity data; here '
        'we use the negative log of 60-day average dollar volume (close * volume) as a '
        'liquidity-weighted size proxy, then cross-sectional z-score per date for '
        'long-short ranking. Top z-scores = smaller / less liquid names.'
    ),
    'description_cn': '经典风格 1993 SMB 小盘因子代理：60日成交额倒数（小流动性=高分）',
}


def _cross_sectional_zscore(df: pd.DataFrame) -> pd.DataFrame:
    """Per-row z-score: (x - row_mean) / row_std; zero/NaN std rows -> NaN."""
    mean = df.mean(axis=1, skipna=True)
    std = df.std(axis=1, ddof=1, skipna=True)
    centered = df.sub(mean, axis=0)
    result = centered.div(std.where(std > 0), axis=0)
    return result.replace([np.inf, -np.inf], np.nan)


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return inverse log 60-day dollar-volume z-score per stock."""
    close = panel['close']
    volume = panel['volume']
    dollar_volume = volume * close
    avg = ts_mean(dollar_volume, 60)
    log_size = np.log(avg + 1.0)
    return _cross_sectional_zscore(-log_size)
