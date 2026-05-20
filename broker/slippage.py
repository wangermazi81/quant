# -*- coding: utf-8 -*-
"""
滑点模型
────────
设计参考：abu/abupy/SlippageBu

A 股次日开盘成交的回测中，"实际成交价" 通常不等于 "open"：
- 流动性差的股票：买入往往滑点向上、卖出向下
- 大单冲击：成交均价偏离开盘价

提供 4 种模型，回测器可注入选用。
"""
from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass


class SlippageModel(ABC):
    """抽象基类"""
    name: str = "SlippageModel"

    @abstractmethod
    def adjust(self, base_price: float, action: str, qty: int = 0) -> float:
        """
        给定基础价 (通常是开盘价) 和方向，返回实际成交价。
        action: "buy" 买入向上滑动；"sell" 卖出向下滑动
        qty: 数量（部分模型用来估冲击成本，默认模型可忽略）
        """
        ...


@dataclass
class NoSlippage(SlippageModel):
    """无滑点 — 默认行为，与重构前完全一致"""
    name: str = "NoSlippage"

    def adjust(self, base_price: float, action: str, qty: int = 0) -> float:
        return base_price


@dataclass
class FixedSlippage(SlippageModel):
    """固定金额滑点（单位：元）"""
    amount: float = 0.01

    def __post_init__(self):
        if self.amount < 0:
            raise ValueError("amount 不能为负")
        self.name = f"FixedSlippage({self.amount})"

    def adjust(self, base_price: float, action: str, qty: int = 0) -> float:
        sign = 1 if action == "buy" else -1
        return max(0.01, base_price + sign * self.amount)


@dataclass
class PercentSlippage(SlippageModel):
    """百分比滑点 — A 股回测最常用"""
    pct: float = 0.001    # 默认 0.1%

    def __post_init__(self):
        if self.pct < 0 or self.pct > 0.2:
            raise ValueError("pct 必须在 [0, 0.2] 之间")
        self.name = f"PercentSlippage({self.pct:.2%})"

    def adjust(self, base_price: float, action: str, qty: int = 0) -> float:
        sign = 1 if action == "buy" else -1
        return max(0.01, base_price * (1 + sign * self.pct))


@dataclass
class RandomSlippage(SlippageModel):
    """
    随机滑点：在 [min_pct, max_pct] 之间均匀采样，模拟成交价的不确定性。
    回测用，做敏感性分析时切此模型。
    """
    min_pct: float = 0.0
    max_pct: float = 0.003

    def __post_init__(self):
        if not (0 <= self.min_pct <= self.max_pct <= 0.05):
            raise ValueError("需满足 0 <= min_pct <= max_pct <= 0.05")
        self.name = f"RandomSlippage[{self.min_pct:.2%},{self.max_pct:.2%}]"
        self._rng = random.Random()

    def adjust(self, base_price: float, action: str, qty: int = 0) -> float:
        sign = 1 if action == "buy" else -1
        pct = self._rng.uniform(self.min_pct, self.max_pct)
        return max(0.01, base_price * (1 + sign * pct))


__all__ = ["SlippageModel", "NoSlippage", "FixedSlippage",
           "PercentSlippage", "RandomSlippage"]
