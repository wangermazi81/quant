# -*- coding: utf-8 -*-
"""
RiskManager + RiskAction + RiskRule 基类
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RiskAction:
    """风控决策结果"""
    should_sell: bool = False            # 平仓建议
    should_block_buy: bool = False       # 拒绝开仓
    reason: str = ""                     # 触发说明
    rule_name: str = ""                  # 触发规则名

    @classmethod
    def ok(cls) -> "RiskAction":
        return cls()

    @classmethod
    def sell(cls, reason: str, rule_name: str = "") -> "RiskAction":
        return cls(should_sell=True, reason=reason, rule_name=rule_name)

    @classmethod
    def block(cls, reason: str, rule_name: str = "") -> "RiskAction":
        return cls(should_block_buy=True, reason=reason, rule_name=rule_name)


@dataclass
class RiskContext:
    """
    评估上下文，调用方按需填字段，规则各自取所需。
    不强制全部填，未填的字段规则应跳过自身判断。
    """
    code: Optional[str] = None
    current_price: Optional[float] = None      # 当前价
    entry_price: Optional[float] = None        # 持仓成本
    entry_date: Optional[str] = None           # 入场日期
    today: Optional[str] = None                # 当前日期
    qty: Optional[int] = None                  # 持仓量
    highest_since_entry: Optional[float] = None  # 持仓期最高价（追踪止损用）
    portfolio_value: Optional[float] = None    # 当前账户总市值
    initial_capital: Optional[float] = None    # 初始资金
    positions_count: Optional[int] = None      # 当前持仓数
    blacklist: list[str] = field(default_factory=list)


class RiskRule(ABC):
    """每条规则的统一接口"""
    name: str = "RiskRule"

    @abstractmethod
    def evaluate(self, ctx: RiskContext) -> RiskAction:
        ...


class RiskManager:
    """
    串联多条规则。
    evaluate() 返回**第一条**触发的 RiskAction；都未触发返回 RiskAction.ok()。
    设计上"卖出"规则优先于"拒买"规则。
    """
    def __init__(self, rules: list[RiskRule] | None = None):
        self.rules: list[RiskRule] = rules or []

    def add(self, rule: RiskRule) -> "RiskManager":
        self.rules.append(rule)
        return self

    def evaluate(self, ctx: RiskContext) -> RiskAction:
        # 先评估卖出类规则
        for r in self.rules:
            a = r.evaluate(ctx)
            if a.should_sell:
                a.rule_name = a.rule_name or r.name
                return a
        # 再评估拒买类
        for r in self.rules:
            a = r.evaluate(ctx)
            if a.should_block_buy:
                a.rule_name = a.rule_name or r.name
                return a
        return RiskAction.ok()

    def __len__(self) -> int:
        return len(self.rules)
