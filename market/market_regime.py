# -*- coding: utf-8 -*-
"""
市场环境检测器
基于宽基指数技术指标判断: bull / bear / sideways / crash
并给出建议仓位系数 position_multiplier
"""
import logging
from datetime import datetime
import numpy as np
import pandas as pd

from analytics import data_gateway as dg

logger = logging.getLogger(__name__)

# 指数代码 (上证 000001, 深成 399001, 创业 399006)
INDEX_CODES = ["000001", "399001", "399006"]

_cache: dict = {}        # {ts_minute: result}
_CACHE_TTL   = 5 * 60   # 5 分钟缓存


def _get_index_data(code: str, n: int = 60) -> pd.DataFrame:
    """优先 TDX 拉取指数日线，失败则返回空 DataFrame。"""
    try:
        market = 1 if str(code).zfill(6) == "000001" else 0
        df = dg.get_index_kline(code, days=n, market=market)
        if df is not None and not df.empty:
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                return df.sort_values("date").reset_index(drop=True)
            return df.reset_index(drop=True)
    except Exception as e:
        logger.debug(f"[regime] TDX 失败 {code}: {e}")
    # 本地 CSV 回退
    try:
        from pathlib import Path
        csv = Path(__file__).parent.parent / "data" / "daily" / f"{code}.csv"
        if csv.exists():
            df = pd.read_csv(csv, parse_dates=["date"])
            return df.sort_values("date").tail(n).reset_index(drop=True)
    except Exception:
        pass
    return pd.DataFrame()


def _calc_score(df: pd.DataFrame) -> float:
    """对单指数计算 0-100 综合分，越高越牛。"""
    if df.empty or len(df) < 20:
        return 50.0
    close = df["close"]
    score = 50.0

    # MA 斜率
    ma5  = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    if ma5.iloc[-1] > ma20.iloc[-1]:
        score += 10
    else:
        score -= 10

    # 近5日涨幅
    chg5 = (close.iloc[-1] / close.iloc[-5] - 1) * 100 if len(close) >= 5 else 0
    score += max(-20, min(20, chg5 * 2))

    # MA20 方向
    if len(ma20.dropna()) >= 5:
        ma20_slope = ma20.dropna().iloc[-1] - ma20.dropna().iloc[-5]
        score += 5 if ma20_slope > 0 else -5

    # 成交量 vs 20日均量
    if "volume" in df.columns:
        vol_ratio = df["volume"].iloc[-1] / (df["volume"].iloc[-20:].mean() + 1)
        if vol_ratio > 1.5 and chg5 > 0:
            score += 5
        elif vol_ratio > 1.5 and chg5 < 0:
            score -= 5

    return float(np.clip(score, 0, 100))


def _classify(score: float, chg5: float) -> tuple[str, str, float]:
    """(regime, description, position_multiplier)"""
    if chg5 < -5:
        return "crash",    "市场急跌，建议空仓观望", 0.0
    if score >= 65:
        return "bull",     "牛市格局，趋势向上",      1.2
    if score >= 45:
        return "sideways", "震荡市，谨慎操作",         0.8
    return "bear",     "熊市格局，轻仓防守",      0.4


class MarketRegimeDetector:
    """市场环境检测器（带缓存）。"""

    def detect(self, force_refresh: bool = False) -> dict:
        import time
        ts_minute = int(time.time() // _CACHE_TTL)
        if not force_refresh and ts_minute in _cache:
            return _cache[ts_minute]

        scores     = []
        chg5_list  = []
        index_info = {}

        for code in INDEX_CODES:
            df = _get_index_data(code)
            if df.empty:
                continue
            sc    = _calc_score(df)
            close = df["close"]
            chg5  = (close.iloc[-1] / close.iloc[-5] - 1) * 100 if len(close) >= 5 else 0
            chg1  = (close.iloc[-1] / close.iloc[-2] - 1) * 100 if len(close) >= 2 else 0
            scores.append(sc)
            chg5_list.append(chg5)
            index_info[code] = {
                "score": round(sc, 1),
                "price": round(float(close.iloc[-1]), 2),
                "chg1":  round(chg1, 2),
                "chg5":  round(chg5, 2),
            }

        if not scores:
            result = {
                "regime":              "unknown",
                "description":         "无法获取指数数据",
                "score":               50,
                "position_multiplier": 0.8,
                "indices":             {},
                "updated_at":          datetime.now().strftime("%H:%M"),
            }
            return result

        avg_score = float(np.mean(scores))
        avg_chg5  = float(np.mean(chg5_list))
        regime, description, multiplier = _classify(avg_score, avg_chg5)

        result = {
            "regime":              regime,
            "description":         description,
            "score":               round(avg_score, 1),
            "position_multiplier": multiplier,
            "indices":             index_info,
            "updated_at":          datetime.now().strftime("%H:%M"),
        }
        _cache.clear()
        _cache[ts_minute] = result
        return result
