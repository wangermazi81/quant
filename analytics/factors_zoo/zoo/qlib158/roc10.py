# 见微 Alpha 内置因子.py
#  
"""rh158 ROC10: formula = \\mathrm{close}_t / \\mathrm{close}_{{t-10}} - 1."""
from __future__ import annotations

import pandas as pd
from analytics.factors_zoo.base import safe_div

__alpha_meta__ = {
    'id': 'rh158_roc10',
    'theme': ['momentum'],
    'formula_latex': '\\\\mathrm{close}_t / \\\\mathrm{close}_{{t-10}} - 1',
    'columns_required': ['close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk'],
    'frequency': ['1d'],
    'decay_horizon': 10,
    'min_warmup_bars': 10,
    'description_cn': '10日变化率（ROC）：(P_t - P_{t-10}) / P_{t-10}',
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return rh158 ROC10 on the supplied OHLCV panel."""
    c = panel['close']
    return safe_div(c, c.shift(10)) - 1.0
