# 见微 Alpha 内置因子.py
#  
"""rh158 MA5: formula = \\mathrm{ts\\_mean}(\\mathrm{close}, 5) / \\mathrm{close}."""
from __future__ import annotations

import pandas as pd
from analytics.factors_zoo.base import safe_div, ts_mean

__alpha_meta__ = {
    'id': 'rh158_ma5',
    'theme': ['momentum'],
    'formula_latex': '\\\\mathrm{ts\\\\_mean}(\\\\mathrm{close}, 5) / \\\\mathrm{close}',
    'columns_required': ['close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk'],
    'frequency': ['1d'],
    'decay_horizon': 5,
    'min_warmup_bars': 5,
    'description_cn': '5日均线相对价格：当前价相对 5日均价的偏离度',
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return rh158 MA5 on the supplied OHLCV panel."""
    c = panel['close']
    return safe_div(ts_mean(c, 5), c)
