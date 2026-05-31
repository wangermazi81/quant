"""见微 Alpha 内置因子 alpha101_022

WQ Alpha #22：成交量与最高价相关性 + 收盘价波动率综合
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.factors_zoo.base import (
    decay_linear,
    delta,
    rank,
    safe_div,
    scale,
    signed_power,
    ts_argmax,
    ts_argmin,
    ts_corr,
    ts_cov,
    ts_max,
    ts_mean,
    ts_min,
    ts_rank,
    ts_std,
)

ALPHA_ID = "alpha101_022"

__alpha_meta__ = {
    'id': 'alpha101_022',
    'nickname': '短线因子 #22',
    'theme': ['volume', 'volatility'],
    'formula_latex': '-1 * (delta(correlation(high,volume,5),5) * rank(stddev(close,20)))',
    'columns_required': ['high', 'close', 'volume'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 25,
    'notes': 'WQ Alpha #22：成交量与最高价相关性 + 收盘价波动率综合',
    'description_cn': 'WQ Alpha #22：成交量与最高价相关性 + 收盘价波动率综合',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    close = panel["close"]
    high = panel["high"]
    volume = panel["volume"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = -1.0 * (delta(ts_corr(high, volume, 5), 5) * rank(ts_std(close, 20)))
    return out
