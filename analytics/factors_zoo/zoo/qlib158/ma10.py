# 见微 Alpha 内置因子.py
#  
"""rh158 MA10: formula = \\mathrm{ts\\_mean}(\\mathrm{close}, 10) / \\mathrm{close}."""
from __future__ import annotations

import pandas as pd
from analytics.factors_zoo.base import safe_div, ts_mean

__alpha_meta__ = {
    'id': 'rh158_ma10',
    'theme': ['momentum'],
    'formula_latex': '\\\\mathrm{ts\\\\_mean}(\\\\mathrm{close}, 10) / \\\\mathrm{close}',
    'columns_required': ['close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk'],
    'frequency': ['1d'],
    'decay_horizon': 10,
    'min_warmup_bars': 10,
    'description_cn': '10日均线相对价格：当前价相对 10日均价的偏离度',
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return rh158 MA10 on the supplied OHLCV panel."""
    c = panel['close']
    return safe_div(ts_mean(c, 10), c)
