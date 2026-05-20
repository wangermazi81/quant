# -*- coding: utf-8 -*-
"""
止损 / 止盈 / 追踪止损 / 最大持仓天数
"""
from __future__ import annotations

from datetime import datetime
from .manager import RiskRule, RiskAction, RiskContext


class StopLoss(RiskRule):
    """固定百分比止损"""
    def __init__(self, pct: float = 0.05):
        if pct <= 0 or pct >= 1:
            raise ValueError("pct 必须在 (0, 1) 之间")
        self.pct = pct
        self.name = f"StopLoss({pct:.1%})"

    def evaluate(self, ctx: RiskContext) -> RiskAction:
        if ctx.current_price is None or ctx.entry_price is None or ctx.entry_price <= 0:
            return RiskAction.ok()
        loss = (ctx.entry_price - ctx.current_price) / ctx.entry_price
        if loss >= self.pct:
            return RiskAction.sell(
                reason=f"止损：浮亏 {loss:.2%} ≥ {self.pct:.2%}",
                rule_name=self.name,
            )
        return RiskAction.ok()


class StopProfit(RiskRule):
    """固定百分比止盈"""
    def __init__(self, pct: float = 0.10):
        if pct <= 0:
            raise ValueError("pct 必须 > 0")
        self.pct = pct
        self.name = f"StopProfit({pct:.1%})"

    def evaluate(self, ctx: RiskContext) -> RiskAction:
        if ctx.current_price is None or ctx.entry_price is None or ctx.entry_price <= 0:
            return RiskAction.ok()
        gain = (ctx.current_price - ctx.entry_price) / ctx.entry_price
        if gain >= self.pct:
            return RiskAction.sell(
                reason=f"止盈：浮盈 {gain:.2%} ≥ {self.pct:.2%}",
                rule_name=self.name,
            )
        return RiskAction.ok()


class TrailingStop(RiskRule):
    """追踪止损：从持仓期最高价回撤超过 pct 即平仓"""
    def __init__(self, pct: float = 0.05):
        if pct <= 0 or pct >= 1:
            raise ValueError("pct 必须在 (0, 1) 之间")
        self.pct = pct
        self.name = f"TrailingStop({pct:.1%})"

    def evaluate(self, ctx: RiskContext) -> RiskAction:
        peak = ctx.highest_since_entry
        if ctx.current_price is None or peak is None or peak <= 0:
            return RiskAction.ok()
        drawdown = (peak - ctx.current_price) / peak
        if drawdown >= self.pct:
            return RiskAction.sell(
                reason=f"追踪止损：从峰值回撤 {drawdown:.2%} ≥ {self.pct:.2%}",
                rule_name=self.name,
            )
        return RiskAction.ok()


class MaxHoldDays(RiskRule):
    """最大持仓天数：超过即强制平仓"""
    def __init__(self, days: int = 20):
        if days <= 0:
            raise ValueError("days 必须 > 0")
        self.days = days
        self.name = f"MaxHoldDays({days})"

    def evaluate(self, ctx: RiskContext) -> RiskAction:
        if not ctx.entry_date or not ctx.today:
            return RiskAction.ok()
        try:
            ed = datetime.strptime(ctx.entry_date[:10], "%Y-%m-%d")
            td = datetime.strptime(ctx.today[:10], "%Y-%m-%d")
            held = (td - ed).days
        except Exception:
            return RiskAction.ok()
        if held >= self.days:
            return RiskAction.sell(
                reason=f"持仓 {held} 天 ≥ 上限 {self.days}",
                rule_name=self.name,
            )
        return RiskAction.ok()
