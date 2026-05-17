import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, date
import logging

logger = logging.getLogger(__name__)

def get_realtime_quotes(codes: list) -> pd.DataFrame:
    """
    获取实时行情。
    codes 为空列表时返回全 A 股行情（供打板等全市场策略使用）。
    返回列：代码, 名称, 最新价, 涨跌幅, 涨跌额, 昨收, 今开, 最高, 最低, 成交量, 换手率, 量比
    """
    try:
        df = ak.stock_zh_a_spot_em()
        if codes:
            df = df[df['代码'].isin(codes)].copy()
        keep = [c for c in
                ['代码','名称','最新价','涨跌幅','涨跌额','昨收','今开','最高','最低','成交量','换手率','量比']
                if c in df.columns]
        return df[keep].reset_index(drop=True)
    except Exception as e:
        logger.error(f"获取行情失败: {e}")
        return pd.DataFrame()

def get_kline(code: str, period: int = 60) -> pd.DataFrame:
    """获取日K线数据"""
    try:
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=(pd.Timestamp.now() - pd.Timedelta(days=period)).strftime("%Y%m%d"),
            end_date=pd.Timestamp.now().strftime("%Y%m%d"),
            adjust="qfq"
        )
        df.columns = ['date','open','close','high','low','volume','turnover','change_pct','change_amt','amplitude','turnover_rate']
        return df.sort_values('date').reset_index(drop=True)
    except Exception as e:
        logger.error(f"获取K线失败 {code}: {e}")
        return pd.DataFrame()

def is_trading_time() -> bool:
    """判断是否在交易时间"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return (930 <= t <= 1130) or (1300 <= t <= 1500)
