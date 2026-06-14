# -*- coding: utf-8 -*-
"""账户默认止损/止盈/超期 退出决策（纯函数，无副作用，可独立单测）。

原本在两处重复：broker/paper_broker.py 的 check_risk_rules 与 main.py 的
_check_live_daban_ride（非打板实盘持仓）。两处逻辑相同，仅 reason 文案里阈值的
小数位不同（paper 用 .1f、实盘用 .0f）→ 用 thr_decimals 参数保住各自精确文案。
"""
from __future__ import annotations


def account_exit(pnl: float, hold_days: int, stop_loss: float, take_profit: float,
                 max_hold: int, *, thr_decimals: int = 1):
    """账户默认退出决策。返回 (should_sell: bool, reason: str|None)。

    优先级：止损 > 止盈 > 超期（与原内联 if/elif 一致）。
    pnl 为浮动盈亏比例（如 -0.06 表示亏 6%）；stop_loss 为负数阈值（如 -0.05）。
    thr_decimals 控制阈值在文案里的小数位：paper_broker 用 1，main 实盘用 0。
    """
    if pnl <= stop_loss:
        return True, f"止损({pnl*100:.1f}%≤{stop_loss*100:.{thr_decimals}f}%)"
    if pnl >= take_profit:
        return True, f"止盈({pnl*100:.1f}%≥{take_profit*100:.{thr_decimals}f}%)"
    if hold_days >= max_hold:
        return True, f"超期({hold_days}天≥{max_hold}天)"
    return False, None
