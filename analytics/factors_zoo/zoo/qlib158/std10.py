# 见微 Alpha 内置因子.py
#  
"""rh158 STD10: formula = \\mathrm{ts\\_std}(\\mathrm{close}, 10) / \\mathrm{close}."""
from __future__ import annotations

import pandas as pd
from analytics.factors_zoo.base import safe_div, ts_std

__alpha_meta__ = {
    'id': 'rh158_std10',
    'theme': ['momentum'],
    'formula_latex': '\\\\mathrm{ts\\\\_std}(\\\\mathrm{close}, 10) / \\\\mathrm{close}',
    'columns_required': ['close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk'],
    'frequency': ['1d'],
    'decay_horizon': 10,
    'min_warmup_bars': 10,
    'description_cn': '10日收益率标准差：中短期波动率',
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return rh158 STD10 on the supplied OHLCV panel."""
    c = panel['close']
    return safe_div(ts_std(c, 10), c)
