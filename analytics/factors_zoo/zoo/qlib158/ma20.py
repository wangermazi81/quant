# 见微 Alpha 内置因子.py
#  
"""rh158 MA20: formula = \\mathrm{ts\\_mean}(\\mathrm{close}, 20) / \\mathrm{close}."""
from __future__ import annotations

import pandas as pd
from analytics.factors_zoo.base import safe_div, ts_mean

__alpha_meta__ = {
    'id': 'rh158_ma20',
    'theme': ['momentum'],
    'formula_latex': '\\\\mathrm{ts\\\\_mean}(\\\\mathrm{close}, 20) / \\\\mathrm{close}',
    'columns_required': ['close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk'],
    'frequency': ['1d'],
    'decay_horizon': 20,
    'min_warmup_bars': 20,
    'description_cn': '20日均线相对价格：当前价相对 20日均价的偏离度（中线趋势）',
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return rh158 MA20 on the supplied OHLCV panel."""
    c = panel['close']
    return safe_div(ts_mean(c, 20), c)
