"""见微 Alpha 内置因子 alpha101_006

WQ Alpha #6：开盘成交量 10日相关性反转
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

ALPHA_ID = "alpha101_006"

__alpha_meta__ = {
    'id': 'alpha101_006',
    'nickname': '短线因子 #6',
    'theme': ['volume', 'reversal'],
    'formula_latex': '-1 * correlation(open, volume, 10)',
    'columns_required': ['open', 'volume', 'close'],
    'extras_required': [],
    'requires_sector': False,
    'universe': ['equity_us'],
    'frequency': ['1D'],
    'decay_horizon': 5,
    'min_warmup_bars': 10,
    'notes': 'WQ Alpha #6：开盘成交量 10日相关性反转',
    'description_cn': 'WQ Alpha #6：开盘成交量 10日相关性反转',
}


def compute(panel: dict) -> pd.DataFrame:
    """Compute the alpha on the OHLCV+ panel and return a wide DataFrame."""
    open_ = panel["open"]
    volume = panel["volume"]


    # Helper aliases (local closures keep the file standalone & purity-safe).
    out = -1.0 * ts_corr(open_, volume, 10)
    return out
