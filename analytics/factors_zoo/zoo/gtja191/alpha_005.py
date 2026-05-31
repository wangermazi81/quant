"""见微 Alpha 内置因子 gtja191_005

量价共振反转：成交量与最高价 5日 rank 相关性，捕捉量价背离
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.factors_zoo.base import (
    decay_linear,
    delta,
    rank,
    safe_div,
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

__alpha_meta__ = {
    "id": "gtja191_005",
    "theme": ['volume'],
    "formula_latex": '(-1 * TSMAX(CORR(TSRANK(VOLUME,5), TSRANK(HIGH,5), 5), 3))',
    "columns_required": ['volume', 'high'],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 5,
    "min_warmup_bars": 13,
    "notes": 'Max over 3 days of 5d corr of TSRANK(volume,5) and TSRANK(high,5).',
    'description_cn': '量价共振反转：成交量与最高价 5日 rank 相关性，捕捉量价背离',
}

def compute(panel: dict) -> pd.DataFrame:
    v = panel["volume"]
    h = panel["high"]
    return -1.0 * ts_max(ts_corr(ts_rank(v, 5), ts_rank(h, 5), 5), 3)
