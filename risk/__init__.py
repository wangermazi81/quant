# -*- coding: utf-8 -*-
"""
risk/ — 可组合风控规则
─────────────────────
设计参考：abu/abupy/UmpBu（"主裁系统"）

核心思想：
- 每条规则实现 RiskRule 接口：判断是否触发并返回 RiskAction
- RiskManager 把规则组合起来，依次评估
- 同一份规则集既可被 BackBroker 用（回测期），也可被 PaperBroker 用（模拟盘）和实盘逻辑用

用法示例：
    from risk import RiskManager, StopLoss, StopProfit, MaxPositions
    rm = RiskManager([
        StopLoss(pct=0.05),
        StopProfit(pct=0.10),
        MaxPositions(5),
    ])
    action = rm.evaluate(position=pos, current_price=px, portfolio=pf)
    if action.should_sell:
        ...
"""
from .stops import StopLoss, StopProfit, TrailingStop, MaxHoldDays
from .limits import MaxPositions, MaxDrawdown, BlacklistFilter, SinglePositionLimit
from .manager import RiskManager, RiskAction, RiskRule

__all__ = [
    "RiskManager", "RiskAction", "RiskRule",
    "StopLoss", "StopProfit", "TrailingStop", "MaxHoldDays",
    "MaxPositions", "MaxDrawdown", "BlacklistFilter", "SinglePositionLimit",
]
