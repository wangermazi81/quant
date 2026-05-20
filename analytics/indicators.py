# -*- coding: utf-8 -*-
"""
统一技术指标库
─────────────
设计参考：abu/abupy/IndicatorBu

所有函数都接受 pandas.Series 或 DataFrame，返回同长度 Series（保留 NaN 头）。
避免 strategies/ 与 factors/ 各自重复实现，提高一致性。

约定：
- close/high/low/volume 等输入参数均为 pandas.Series
- n 周期参数默认值采用业内常见配置
- 一律使用 ewm/rolling 的向量化运算，禁止 Python 循环
"""
from __future__ import annotations

from typing import Tuple
import numpy as np
import pandas as pd


# ════════════════════════════════════════════════════
# 移动平均
# ════════════════════════════════════════════════════
def sma(series: pd.Series, n: int) -> pd.Series:
    """简单移动平均"""
    return series.rolling(n, min_periods=1).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    """指数移动平均"""
    return series.ewm(span=n, adjust=False).mean()


def wma(series: pd.Series, n: int) -> pd.Series:
    """加权移动平均"""
    weights = np.arange(1, n + 1)
    return series.rolling(n).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


# ════════════════════════════════════════════════════
# MACD
# ════════════════════════════════════════════════════
def macd(close: pd.Series,
         fast: int = 12, slow: int = 26, signal: int = 9
         ) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    返回 (DIF, DEA, HIST)
      DIF  = EMA(close, fast) - EMA(close, slow)
      DEA  = EMA(DIF, signal)
      HIST = 2 * (DIF - DEA)  # 国内常见 *2 放大柱状
    """
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    dif      = ema_fast - ema_slow
    dea      = ema(dif, signal)
    hist     = 2 * (dif - dea)
    return dif, dea, hist


# ════════════════════════════════════════════════════
# RSI
# ════════════════════════════════════════════════════
def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """相对强弱指标 (Wilder smoothing 等价 rolling mean)"""
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(n).mean()
    loss  = (-delta.clip(upper=0)).rolling(n).mean()
    rs    = gain / loss.replace(0, np.inf)
    return 100 - 100 / (1 + rs)


# ════════════════════════════════════════════════════
# KDJ
# ════════════════════════════════════════════════════
def kdj(high: pd.Series, low: pd.Series, close: pd.Series,
        n: int = 9, k_smooth: int = 3, d_smooth: int = 3
        ) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    返回 (K, D, J)
    RSV = (C-LLV) / (HHV-LLV) * 100
    K   = EMA(RSV, k_smooth)   ≈ SMA(RSV, k_smooth)
    D   = EMA(K, d_smooth)
    J   = 3K - 2D
    """
    low_n  = low.rolling(n).min()
    high_n = high.rolling(n).max()
    rsv    = (close - low_n) / (high_n - low_n).replace(0, 1) * 100
    k      = rsv.ewm(com=k_smooth - 1, adjust=False).mean()
    d      = k.ewm(com=d_smooth - 1, adjust=False).mean()
    j      = 3 * k - 2 * d
    return k, d, j


# ════════════════════════════════════════════════════
# BOLL 布林带
# ════════════════════════════════════════════════════
def boll(close: pd.Series, n: int = 20, k: float = 2.0
         ) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """返回 (上轨, 中轨, 下轨)"""
    mid = sma(close, n)
    std = close.rolling(n).std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    return upper, mid, lower


# ════════════════════════════════════════════════════
# ATR 平均真实波幅
# ════════════════════════════════════════════════════
def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's ATR"""
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# ════════════════════════════════════════════════════
# ADX 趋势强度
# ════════════════════════════════════════════════════
def adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14
        ) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """返回 (ADX, +DI, -DI)"""
    up   = high.diff()
    down = -low.diff()
    plus_dm  = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    tr_n  = atr(high, low, close, n)
    plus_di  = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean()  / tr_n.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / tr_n.replace(0, np.nan)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    adx_v = dx.ewm(alpha=1 / n, adjust=False).mean()
    return adx_v, plus_di, minus_di


# ════════════════════════════════════════════════════
# 交叉信号工具
# ════════════════════════════════════════════════════
def cross_up(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """返回布尔 Series：fast 上穿 slow 的位置为 True"""
    return (fast > slow) & (fast.shift() <= slow.shift())


def cross_down(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """fast 下穿 slow"""
    return (fast < slow) & (fast.shift() >= slow.shift())


# ════════════════════════════════════════════════════
# 量能 / 价格强度
# ════════════════════════════════════════════════════
def volume_ratio(volume: pd.Series, n: int = 20) -> pd.Series:
    """量比 = 当日成交量 / 前 n 日均量"""
    avg = volume.shift(1).rolling(n).mean()
    return volume / avg.replace(0, np.nan)


def pct_change(series: pd.Series, n: int = 1) -> pd.Series:
    """n 日涨跌幅（百分比）"""
    return series.pct_change(n) * 100


__all__ = [
    "sma", "ema", "wma",
    "macd", "rsi", "kdj", "boll", "atr", "adx",
    "cross_up", "cross_down",
    "volume_ratio", "pct_change",
]
