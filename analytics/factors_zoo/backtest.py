# -*- coding: utf-8 -*-
"""见微 Alpha 因子回测（最小可行版本）

设计：
- 拉历史 N 天 wide panel
- 每隔 rebalance_days 算因子 → 取 top K（按分数降序）
- T+1 等权买入，持有 hold_days 天后卖出
- 累计净值 + 胜率 + 最大回撤 + IC

简化（明确：这不是生产级回测）：
- 不考虑停牌、涨跌停限制
- 不扣交易成本（默认 0；可传 fee_rate）
- 等权配置
- 收盘价成交（无滑点）
"""
from __future__ import annotations

import logging
from typing import Callable, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def backtest_factor(
    factor_id: str,
    codes: Sequence[str],
    get_kline: Callable[[str, int], pd.DataFrame],
    days: int = 200,
    top_k: int = 10,
    rebalance_days: int = 5,
    hold_days: int = 5,
    fee_rate: float = 0.0,
) -> dict:
    """对一个因子做历史回测

    Args:
        factor_id:        因子 id，例 "gtja191_001"
        codes:            股票池
        get_kline:        get_kline(code, days) -> DataFrame
        days:             历史回看天数（应 >> hold_days + warmup）
        top_k:            每次调仓选前 K 只
        rebalance_days:   调仓周期（每 N 天选一次股）
        hold_days:        持有天数（必须 <= rebalance_days，否则卖在调仓日）
        fee_rate:         单边交易费率（0.0005 = 万 5），双边 = 2 * fee_rate

    Returns:
        {
          "ok": bool,
          "factor_id": str,
          "config": {...},
          "stats": {
            "total_return":    累计收益率
            "annual_return":   年化收益率
            "max_drawdown":    最大回撤
            "sharpe":          夏普比率（年化）
            "win_rate":        交易胜率
            "trade_count":     交易笔数
            "avg_holding":     平均持仓天数
          },
          "equity_curve": [{"date": "...", "nav": 1.05}, ...]
          "trades":       [{"open_date","close_date","codes","pnl_pct"}, ...]
          "ic_series":    [{"date":"...","ic":0.12}, ...] (信息系数 — 因子分数与未来收益的相关性)
        }
    """
    from analytics.factors_zoo import registry, build_panel
    reg = registry()
    if factor_id not in reg.list():
        return {"ok": False, "error": f"因子 {factor_id} 不存在"}

    config = {
        "factor_id": factor_id,
        "n_codes": len(codes),
        "days": days,
        "top_k": top_k,
        "rebalance_days": rebalance_days,
        "hold_days": hold_days,
        "fee_rate": fee_rate,
    }

    # 拉面板
    panel = build_panel(list(codes), get_kline, days=days, workers=12)
    if not panel or "close" not in panel:
        return {"ok": False, "error": "无 K 线数据", "config": config}
    close = panel["close"]
    dates = close.index
    if len(dates) < rebalance_days + hold_days + 10:
        return {"ok": False, "error": f"历史数据太少（{len(dates)} 天，需要 ≥ {rebalance_days+hold_days+10}）", "config": config}

    # 计算因子值面板（整个历史一次性算好）
    try:
        factor_df = reg.compute(factor_id, panel)
    except Exception as e:
        return {"ok": False, "error": f"因子计算失败: {e}", "config": config}

    # 用未来 hold_days 收益评估 IC（信息系数）
    future_return = close.shift(-hold_days) / close - 1.0
    # IC 序列：每日 cross-sectional Spearman 相关
    ic_series = []
    for i, dt in enumerate(dates):
        if i + hold_days >= len(dates):
            break
        fac = factor_df.loc[dt].dropna()
        ret = future_return.loc[dt].dropna()
        common = fac.index.intersection(ret.index)
        if len(common) < 5:
            continue
        try:
            ic = fac.loc[common].corr(ret.loc[common], method="spearman")
            if not pd.isna(ic):
                ic_series.append({"date": dt.strftime("%Y-%m-%d"), "ic": float(ic)})
        except Exception:
            pass

    # 模拟交易：每隔 rebalance_days 天选 top_k → 持有 hold_days
    nav = 1.0
    equity = []
    trades = []
    wins = 0
    total_holding_days = 0

    i = 0
    n = len(dates)
    while i + hold_days + 1 < n:
        dt = dates[i]
        fac_row = factor_df.loc[dt].dropna()
        if len(fac_row) < top_k:
            i += 1
            equity.append({"date": dt.strftime("%Y-%m-%d"), "nav": round(nav, 6)})
            continue
        top_codes = fac_row.nlargest(top_k).index.tolist()
        open_idx = i + 1
        close_idx = open_idx + hold_days

        # 计算这 K 只股票从 i 到 i+hold_days 的等权收益
        period_returns = []
        for code in top_codes:
            try:
                p0 = close.loc[dates[open_idx], code]
                p1 = close.loc[dates[close_idx], code]
                if pd.notna(p0) and pd.notna(p1) and p0 > 0:
                    period_returns.append(float(p1 / p0 - 1.0))
            except (KeyError, IndexError):
                continue
        if not period_returns:
            i += rebalance_days
            equity.append({"date": dt.strftime("%Y-%m-%d"), "nav": round(nav, 6)})
            continue
        avg_ret = float(np.mean(period_returns))
        # 扣双边费率
        avg_ret -= 2 * fee_rate
        nav *= (1 + avg_ret)
        trades.append({
            "signal_date": dt.strftime("%Y-%m-%d"),
            "open_date":  dates[open_idx].strftime("%Y-%m-%d"),
            "close_date": dates[close_idx].strftime("%Y-%m-%d"),
            "codes":      top_codes,
            "pnl_pct":    round(avg_ret * 100, 4),
        })
        if avg_ret > 0:
            wins += 1
        total_holding_days += hold_days
        # 标记每一天的 NAV（线性插值简化为持有期间不变）
        for j in range(i, min(i + rebalance_days, n)):
            equity.append({"date": dates[j].strftime("%Y-%m-%d"), "nav": round(nav, 6)})
        i += rebalance_days

    # 统计指标
    if not equity:
        return {"ok": False, "error": "回测中未产生任何持仓", "config": config}

    nav_series = pd.Series([e["nav"] for e in equity])
    total_return = float(nav_series.iloc[-1] - 1.0)
    # 年化（约 250 交易日）
    n_days = len(equity)
    if n_days > 0:
        annual_return = float((nav_series.iloc[-1]) ** (250 / max(n_days, 1)) - 1.0)
    else:
        annual_return = 0.0
    # 最大回撤
    cummax = nav_series.cummax()
    drawdown = (nav_series - cummax) / cummax
    max_drawdown = float(drawdown.min())
    # 夏普（日收益）
    daily_ret = nav_series.pct_change().dropna()
    sharpe = 0.0
    if daily_ret.std() > 0:
        sharpe = float(daily_ret.mean() / daily_ret.std() * np.sqrt(250))

    win_rate = wins / len(trades) if trades else 0.0
    avg_holding = total_holding_days / len(trades) if trades else 0.0
    ic_mean = float(np.mean([x["ic"] for x in ic_series])) if ic_series else 0.0
    ic_ir = 0.0
    if ic_series and len(ic_series) > 1:
        ic_arr = np.array([x["ic"] for x in ic_series])
        if ic_arr.std() > 0:
            ic_ir = float(ic_arr.mean() / ic_arr.std())

    return {
        "ok": True,
        "factor_id": factor_id,
        "config": config,
        "stats": {
            "total_return":  round(total_return, 4),
            "annual_return": round(annual_return, 4),
            "max_drawdown":  round(max_drawdown, 4),
            "sharpe":        round(sharpe, 3),
            "win_rate":      round(win_rate, 3),
            "trade_count":   len(trades),
            "avg_holding":   round(avg_holding, 1),
            "ic_mean":       round(ic_mean, 4),
            "ic_ir":         round(ic_ir, 3),
            "panel_days":    len(dates),
            "scanned":       close.shape[1],
        },
        "equity_curve": equity,
        "trades":       trades[-50:],   # 最近 50 笔（避免 payload 过大）
        "ic_series":    ic_series[-100:],
    }
