"""见微 Alpha 内置因子 gtja191_006

价格惯性反向：开盘价 + 最高价加权的 4 日变化做反向信号
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
    "id": "gtja191_006",
    "theme": ['reversal'],
    "formula_latex": '(RANK(SIGN(DELTA((OPEN*0.85+HIGH*0.15), 4))) * -1)',
    "columns_required": ['open', 'high'],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 4,
    "min_warmup_bars": 5,
    "notes": 'Sign of 4d change of weighted price; cross-sectionally ranked, negated.',
    'description_cn': '价格惯性反向：开盘价 + 最高价加权的 4 日变化做反向信号',
}

def compute(panel: dict) -> pd.DataFrame:
    o = panel["open"]
    h = panel["high"]
    x = o * 0.85 + h * 0.15
    return -1.0 * rank(np.sign(delta(x, 4)))
