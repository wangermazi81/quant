# -*- coding: utf-8 -*-
"""下单仓位 / 手数计算（纯函数，无副作用、无外部依赖，可独立单测）。

从 main.py 的 do_scan 抽出的财务关键逻辑，是 main.py 拆分的第一刀：
  - pick_ratio          单笔仓位比例的选择（信号分层 > 策略平均 > 面板比例，夹护栏）
  - lot_shares          按预算/价格算 A 股整手手数（100 股整数倍，最少 1 手）
  - clamp_to_max_order  实盘单笔金额护栏（超 MAX_ORDER_AMOUNT 则缩仓，连 1 手都超则拒）

pick_ratio / lot_shares 与原内联逻辑**严格等价**。
clamp_to_max_order 是**修正版**：原 do_scan 内联实现有 bug——当单价×100 股已超
上限时仍会返回 100 股照常下单（`if safe_shares<100` 因 max(...,100) 恒不成立而成死代码）。
本函数在该情形返回 0，让调用方跳过该票。接入 main.py 时即修复该护栏。
"""
from __future__ import annotations

LOT = 100  # A 股一手 = 100 股


def lot_shares(budget: float, price: float) -> int:
    """按预算和价格算可买手数：向下取整到 100 股整数倍，最少 1 手。

    等价于原 do_scan：max(int(budget/price/100)*100, 100)。
    """
    if price <= 0:
        return LOT
    return max(int(budget / price / LOT) * LOT, LOT)


def pick_ratio(panel_ratio: float, single_max: float,
               strat_ratio: float = 0.0, strat_equal: float = 0.0) -> float:
    """选择单笔仓位比例（占总资金）。优先级：
      1. 信号自带分层仓位 strat_ratio>0 → min(strat_ratio, single_max)  夹护栏
      2. 策略声明的平均仓位 strat_equal>0 → strat_equal（策略口径，不二次夹护栏）
      3. 否则面板比例 panel_ratio → min(panel_ratio, single_max)  夹护栏
    """
    if strat_ratio and strat_ratio > 0:
        return min(strat_ratio, single_max)
    if strat_equal and strat_equal > 0:
        return strat_equal
    return min(panel_ratio, single_max)


def clamp_to_max_order(shares: int, price: float, max_order_amount: float):
    """实盘单笔金额护栏：price*shares 超过上限时按 100 股向下取整缩仓。

    返回 (shares, clamped)：
      - max_order_amount<=0 或未超限 → (原 shares, False)
      - 超限、缩仓后仍 ≥1 手        → (缩后 shares, True)
      - 连 1 手金额都超上限          → (0, True)，调用方据 shares==0 跳过该票
    """
    if price <= 0 or max_order_amount <= 0:
        return shares, False
    if price * shares <= max_order_amount:
        return shares, False
    safe = int(max_order_amount / price / LOT) * LOT
    if safe < LOT:                    # 连一手都买不起（单价×100 已超上限）→ 拒
        return 0, True
    return safe, True
