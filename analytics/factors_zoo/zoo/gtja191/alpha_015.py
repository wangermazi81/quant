"""见微 Alpha 内置因子 gtja191_015

隔夜跳空缺口：开盘价相对前日收盘的偏离度，识别跳空动量
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
    "id": "gtja191_015",
    "theme": ['reversal'],
    "formula_latex": '(OPEN/DELAY(CLOSE,1) - 1)',
    "columns_required": ['open', 'close'],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 1,
    "min_warmup_bars": 2,
    "notes": 'Overnight gap return.',
    'description_cn': '隔夜跳空缺口：开盘价相对前日收盘的偏离度，识别跳空动量',
}

def compute(panel: dict) -> pd.DataFrame:
    o = panel["open"]
    c = panel["close"]
    pc = c.shift(1)
    return safe_div(o, pc) - 1.0
