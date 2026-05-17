import pandas as pd
import numpy as np
from market_data import get_kline, get_realtime_quotes
from config import STRATEGY
import logging

logger = logging.getLogger(__name__)

def calc_ma(series, period):
    return series.rolling(window=period).mean()

def calc_macd(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast).mean()
    ema_slow = close.ewm(span=slow).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal).mean()
    return dif, dea

def check_conditions(code: str, realtime_row: pd.Series) -> dict:
    """检查单只股票的所有选股条件"""
    result = {
        "code": code,
        "name": realtime_row.get('名称', ''),
        "price": realtime_row.get('最新价', 0),
        "change": realtime_row.get('涨跌幅', 0),
        "conditions": {},
        "pass_count": 0,
        "signal": False
    }

    df = get_kline(code)
    if df.empty or len(df) < 30:
        return result

    close = df['close']
    volume = df['volume']

    # 条件1: MA5上穿MA20
    ma5 = calc_ma(close, STRATEGY['ma_short'])
    ma20 = calc_ma(close, STRATEGY['ma_long'])
    ma_cross = (ma5.iloc[-1] > ma20.iloc[-1]) and (ma5.iloc[-2] <= ma20.iloc[-2])
    result['conditions']['MA均线金叉'] = {
        'pass': ma_cross,
        'value': f"MA5:{ma5.iloc[-1]:.2f} MA20:{ma20.iloc[-1]:.2f}"
    }

    # 条件2: 成交量放大
    avg_vol = volume.iloc[-20:-1].mean()
    today_vol = volume.iloc[-1]
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 0
    vol_ok = vol_ratio >= STRATEGY['vol_multiplier']
    result['conditions']['成交量放大'] = {
        'pass': vol_ok,
        'value': f"{vol_ratio:.1f}倍"
    }

    # 条件3: MACD金叉
    dif, dea = calc_macd(close)
    macd_cross = (dif.iloc[-1] > dea.iloc[-1]) and (dif.iloc[-2] <= dea.iloc[-2])
    result['conditions']['MACD金叉'] = {
        'pass': macd_cross,
        'value': f"DIF:{dif.iloc[-1]:.3f}"
    }

    # 条件4: 突破前高
    recent_high = close.iloc[-20:-1].max()
    break_high = close.iloc[-1] > recent_high
    result['conditions']['突破前高'] = {
        'pass': break_high,
        'value': f"前高:{recent_high:.2f}"
    }

    # 条件5: 涨幅在合理区间
    chg = result['change']
    chg_ok = STRATEGY['min_change'] <= chg <= STRATEGY['max_change']
    result['conditions']['涨幅区间'] = {
        'pass': chg_ok,
        'value': f"{chg:+.2f}%"
    }

    result['pass_count'] = sum(1 for c in result['conditions'].values() if c['pass'])
    result['signal'] = result['pass_count'] >= STRATEGY['min_conditions']
    return result

def scan_all(codes: list) -> list:
    """扫描所有股票"""
    quotes = get_realtime_quotes(codes)
    results = []
    for code in codes:
        row = quotes[quotes['代码'] == code]
        if row.empty:
            continue
        r = check_conditions(code, row.iloc[0])
        if r['signal']:
            results.append(r)
            logger.info(f"✓ {code} {r['name']} 满足{r['pass_count']}/5条件")
    return results
