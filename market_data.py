import os
import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, date
import logging

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DAILY_CACHE_DIR  = os.path.join(BASE_DIR, "data", "daily")
MINUTE_CACHE_DIR = os.path.join(BASE_DIR, "data", "minute")


def get_realtime_quotes(codes: list) -> pd.DataFrame:
    """
    获取实时行情。
    codes 为空列表时返回全 A 股行情（供全市场扫描使用）。
    返回列：代码, 名称, 最新价, 涨跌幅, 涨跌额, 昨收, 今开, 最高, 最低, 成交量, 换手率, 量比
    """
    try:
        df = ak.stock_zh_a_spot_em()
        if codes:
            df = df[df["代码"].isin(codes)].copy()
        keep = [c for c in
                ["代码", "名称", "最新价", "涨跌幅", "涨跌额", "昨收", "今开",
                 "最高", "最低", "成交量", "换手率", "量比"]
                if c in df.columns]
        return df[keep].reset_index(drop=True)
    except Exception as e:
        logger.error(f"获取行情失败: {e}")
        return pd.DataFrame()


def get_kline(code: str, period: int = 60) -> pd.DataFrame:
    """
    获取日K线数据。
    优先读取本地缓存 data/daily/<code>.csv，若无则从 akshare 拉取。
    返回 DataFrame，列: date/open/close/high/low/volume
    """
    # 1. 本地 CSV 缓存
    cache_path = os.path.join(DAILY_CACHE_DIR, f"{code}.csv")
    if os.path.exists(cache_path):
        try:
            df = pd.read_csv(cache_path)
            if len(df) >= 10:
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").tail(period).reset_index(drop=True)
                return df
        except Exception as e:
            logger.debug(f"本地K线读取失败 {code}: {e}")

    # 2. akshare 远程拉取
    return _fetch_kline_remote(code, period)


def _fetch_kline_remote(code: str, period: int = 60) -> pd.DataFrame:
    try:
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=(pd.Timestamp.now() - pd.Timedelta(days=period * 2)).strftime("%Y%m%d"),
            end_date=pd.Timestamp.now().strftime("%Y%m%d"),
            adjust="qfq",
        )
        rename_map = {
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
        }
        df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
        # 兼容英文列名（老版本 akshare）
        if "date" not in df.columns and len(df.columns) >= 6:
            df.columns = ["date", "open", "close", "high", "low", "volume",
                          "turnover", "change_pct", "change_amt", "amplitude", "turnover_rate"][:len(df.columns)]
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").tail(period).reset_index(drop=True)
        return df
    except Exception as e:
        logger.error(f"akshare K线失败 {code}: {e}")
        return pd.DataFrame()


def get_minute_kline(code: str, date: str) -> dict:
    """
    获取 1 分钟 K 线（用于回测精度提升）。
    date 格式: YYYYMMDD
    本地缓存路径: data/minute/<code>_<date>.csv
    返回 dict: {datetime, open, high, low, close, volume}
    """
    os.makedirs(MINUTE_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(MINUTE_CACHE_DIR, f"{code}_{date}.csv")

    if os.path.exists(cache_path):
        try:
            df = pd.read_csv(cache_path)
        except Exception:
            df = pd.DataFrame()
    else:
        df = pd.DataFrame()
        try:
            raw = ak.stock_zh_a_hist_min_em(
                symbol=code,
                start_date=date,
                end_date=date,
                period="1",
                adjust="qfq",
            )
            col_map = {
                "时间": "datetime", "开盘": "open", "最高": "high",
                "最低": "low",  "收盘": "close", "成交量": "volume",
            }
            raw = raw.rename(columns={k: v for k, v in col_map.items() if k in raw.columns})
            raw.to_csv(cache_path, index=False)
            df = raw
        except Exception as e:
            logger.warning(f"分钟线获取失败 {code} {date}: {e}")

    result = {}
    for col in ("datetime", "open", "high", "low", "close", "volume"):
        result[col] = df[col].tolist() if col in df.columns else []
    return result


def is_trading_time() -> bool:
    """判断是否在 A 股交易时间"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return (930 <= t <= 1130) or (1300 <= t <= 1500)
