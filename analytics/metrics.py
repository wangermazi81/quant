# -*- coding: utf-8 -*-
"""
回测/实盘性能指标
─────────────────
设计参考：abu/abupy/MetricsBu

把分散在 backtest.py 内的 SharpeAnalyzer/DrawDownAnalyzer/TradeAnalyzer 评分逻辑
抽成可独立使用的纯函数，回测器之外（如模拟盘日报、AI 分析）也可调用。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence, Optional
import numpy as np


# ════════════════════════════════════════════════════
# 收益类
# ════════════════════════════════════════════════════
def total_return(equity: Sequence[float]) -> float:
    """累计收益率"""
    if len(equity) < 2 or equity[0] <= 0:
        return 0.0
    return equity[-1] / equity[0] - 1


def annual_return(equity: Sequence[float], periods_per_year: int = 252) -> float:
    """年化收益"""
    n = len(equity) - 1
    if n < 1 or equity[0] <= 0:
        return 0.0
    years = n / periods_per_year
    if years <= 0:
        return 0.0
    return (equity[-1] / equity[0]) ** (1 / years) - 1


# ════════════════════════════════════════════════════
# 风险调整收益
# ════════════════════════════════════════════════════
def sharpe_ratio(returns: Sequence[float], rf_annual: float = 0.025,
                 periods_per_year: int = 252) -> float:
    """夏普比率（年化），ddof=1"""
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return 0.0
    rf = rf_annual / periods_per_year
    excess = r - rf
    std = float(np.std(excess, ddof=1))
    if std < 1e-9:
        return 0.0
    return float(np.mean(excess)) / std * math.sqrt(periods_per_year)


def sortino_ratio(returns: Sequence[float], rf_annual: float = 0.025,
                  periods_per_year: int = 252) -> float:
    """索提诺比率（只惩罚下行波动）"""
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return 0.0
    rf = rf_annual / periods_per_year
    excess = r - rf
    downside = excess[excess < 0]
    if downside.size == 0:
        return 0.0
    down_std = float(np.std(downside, ddof=1)) * math.sqrt(periods_per_year)
    if down_std < 1e-9:
        return 0.0
    mean_excess_annual = (1 + float(np.mean(r))) ** periods_per_year - 1 - rf_annual
    return mean_excess_annual / down_std


def calmar_ratio(equity: Sequence[float], periods_per_year: int = 252) -> float:
    """Calmar = 年化收益 / |最大回撤|"""
    ann = annual_return(equity, periods_per_year)
    mdd = max_drawdown(equity)
    if mdd == 0:
        return 0.0
    return ann / abs(mdd)


# ════════════════════════════════════════════════════
# 回撤
# ════════════════════════════════════════════════════
def max_drawdown(equity: Sequence[float]) -> float:
    """最大回撤（负数）"""
    if len(equity) < 2:
        return 0.0
    eq = np.asarray(equity, dtype=float)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / np.where(peak > 0, peak, 1)
    return float(np.min(dd))


def drawdown_duration(equity: Sequence[float]) -> int:
    """最长回撤持续天数"""
    if len(equity) < 2:
        return 0
    eq = np.asarray(equity, dtype=float)
    peak = np.maximum.accumulate(eq)
    in_dd = eq < peak
    max_len = cur = 0
    for x in in_dd:
        if x:
            cur += 1
            max_len = max(max_len, cur)
        else:
            cur = 0
    return int(max_len)


# ════════════════════════════════════════════════════
# 交易统计
# ════════════════════════════════════════════════════
def win_rate(trade_pnls: Iterable[float]) -> float:
    """胜率：盈利交易数 / 总交易数"""
    arr = [p for p in trade_pnls]
    if not arr:
        return 0.0
    wins = sum(1 for p in arr if p > 0)
    return wins / len(arr)


def profit_factor(trade_pnls: Iterable[float]) -> float:
    """盈亏比 = 总盈利 / |总亏损|"""
    arr = list(trade_pnls)
    gross_profit = sum(p for p in arr if p > 0)
    gross_loss   = -sum(p for p in arr if p < 0)
    if gross_loss < 1e-9:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def avg_win_loss(trade_pnls: Iterable[float]) -> tuple[float, float]:
    """平均盈利 / 平均亏损"""
    arr = list(trade_pnls)
    wins   = [p for p in arr if p > 0]
    losses = [p for p in arr if p < 0]
    avg_w = float(np.mean(wins))   if wins   else 0.0
    avg_l = float(np.mean(losses)) if losses else 0.0
    return avg_w, avg_l


def expectancy(trade_pnls: Iterable[float]) -> float:
    """期望值 = 胜率*平均盈利 + 败率*平均亏损"""
    arr = list(trade_pnls)
    if not arr:
        return 0.0
    wr = win_rate(arr)
    aw, al = avg_win_loss(arr)
    return wr * aw + (1 - wr) * al


# ════════════════════════════════════════════════════
# 一站式汇总
# ════════════════════════════════════════════════════
def summary(equity: Sequence[float],
            trade_pnls: Optional[Iterable[float]] = None,
            rf_annual: float = 0.025,
            periods_per_year: int = 252) -> dict:
    """
    一次返回核心指标字典，方便回测/日报直接渲染。
    """
    eq = list(equity)
    if len(eq) >= 2 and eq[0] > 0:
        rets = [(eq[i] - eq[i-1]) / eq[i-1] for i in range(1, len(eq)) if eq[i-1] > 0]
    else:
        rets = []

    out = {
        "total_return":     round(total_return(eq), 4),
        "annual_return":    round(annual_return(eq, periods_per_year), 4),
        "sharpe":           round(sharpe_ratio(rets, rf_annual, periods_per_year), 4),
        "sortino":          round(sortino_ratio(rets, rf_annual, periods_per_year), 4),
        "calmar":           round(calmar_ratio(eq, periods_per_year), 4),
        "max_drawdown":     round(max_drawdown(eq), 4),
        "drawdown_days":    drawdown_duration(eq),
    }
    if trade_pnls is not None:
        pnls = list(trade_pnls)
        out.update({
            "trade_count":   len(pnls),
            "win_rate":      round(win_rate(pnls), 4),
            "profit_factor": round(profit_factor(pnls), 4)
                              if profit_factor(pnls) != float("inf") else None,
            "expectancy":    round(expectancy(pnls), 4),
        })
    return out


__all__ = [
    "total_return", "annual_return",
    "sharpe_ratio", "sortino_ratio", "calmar_ratio",
    "max_drawdown", "drawdown_duration",
    "win_rate", "profit_factor", "avg_win_loss", "expectancy",
    "summary",
]
