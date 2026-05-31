"""见微 Alpha 内置因子 gtja191_018

5日动量：当前价 / 5日前价，简单的中短期动量
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
    "id": "gtja191_018",
    "theme": ['momentum'],
    "formula_latex": 'CLOSE/DELAY(CLOSE,5)',
    "columns_required": ['close'],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 5,
    "min_warmup_bars": 6,
    "notes": '5d price ratio.',
    'description_cn': '5日动量：当前价 / 5日前价，简单的中短期动量',
}

def compute(panel: dict) -> pd.DataFrame:
    c = panel["close"]
    return safe_div(c, c.shift(5))
