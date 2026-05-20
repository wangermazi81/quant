# -*- coding: utf-8 -*-
"""
持仓数 / 回撤上限 / 黑名单 / 单笔限额
"""
from __future__ import annotations

from .manager import RiskRule, RiskAction, RiskContext


class MaxPositions(RiskRule):
    """同时最多持有 N 个标的；超过拒买"""
    def __init__(self, n: int = 5):
        if n <= 0:
            raise ValueError("n 必须 > 0")
        self.n = n
        self.name = f"MaxPositions({n})"

    def evaluate(self, ctx: RiskContext) -> RiskAction:
        if ctx.positions_count is None:
            return RiskAction.ok()
        if ctx.positions_count >= self.n:
            return RiskAction.block(
                reason=f"持仓数 {ctx.positions_count} ≥ 上限 {self.n}",
                rule_name=self.name,
            )
        return RiskAction.ok()


class MaxDrawdown(RiskRule):
    """
    账户最大回撤保护：从初始资金亏损超过 pct 时拒绝任何买入（"保命模式"）
    不强平已有持仓 — 那是 StopLoss 的责任。
    """
    def __init__(self, pct: float = 0.20):
        if pct <= 0 or pct >= 1:
            raise ValueError("pct 必须在 (0, 1) 之间")
        self.pct = pct
        self.name = f"MaxDrawdown({pct:.1%})"

    def evaluate(self, ctx: RiskContext) -> RiskAction:
        pv, ic = ctx.portfolio_value, ctx.initial_capital
        if pv is None or ic is None or ic <= 0:
            return RiskAction.ok()
        dd = (ic - pv) / ic
        if dd >= self.pct:
            return RiskAction.block(
                reason=f"账户回撤 {dd:.2%} ≥ 上限 {self.pct:.2%}，停止开仓",
                rule_name=self.name,
            )
        return RiskAction.ok()


class BlacklistFilter(RiskRule):
    """黑名单过滤：上下文 blacklist 含 code 即拒买"""
    name = "BlacklistFilter"

    def evaluate(self, ctx: RiskContext) -> RiskAction:
        if ctx.code and ctx.code in ctx.blacklist:
            return RiskAction.block(
                reason=f"{ctx.code} 在黑名单中",
                rule_name=self.name,
            )
        return RiskAction.ok()


class SinglePositionLimit(RiskRule):
    """
    单标的仓位上限：单股市值不能超过总资金 max_pct。
    本规则只能在交易前用 portfolio_value 校验，由调用方保证 ctx.portfolio_value 反映拟买入后的市值。
    """
    def __init__(self, max_pct: float = 0.30):
        if max_pct <= 0 or max_pct > 1:
            raise ValueError("max_pct 必须在 (0, 1] 之间")
        self.max_pct = max_pct
        self.name = f"SinglePositionLimit({max_pct:.1%})"

    def evaluate(self, ctx: RiskContext) -> RiskAction:
        # 当前实现仅作为拒买开关，由调用方传入 single_position_ratio 字段时启用。
        # 暂以 ctx.qty * ctx.current_price / ctx.portfolio_value 估算。
        if (ctx.qty is None or ctx.current_price is None
                or ctx.portfolio_value is None or ctx.portfolio_value <= 0):
            return RiskAction.ok()
        ratio = (ctx.qty * ctx.current_price) / ctx.portfolio_value
        if ratio > self.max_pct:
            return RiskAction.block(
                reason=f"单股占比 {ratio:.2%} > {self.max_pct:.2%}",
                rule_name=self.name,
            )
        return RiskAction.ok()
