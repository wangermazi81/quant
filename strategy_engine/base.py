# -*- coding: utf-8 -*-
"""
策略 / 买入因子 / 卖出因子 抽象基类
─────────────────────────────────────
设计参考：abu/abupy/FactorBuyBu + FactorSellBu + CoreBu

核心思想：
- 把策略拆成"买入决策"和"卖出决策"两个独立可组合的因子
- 现有 strategies/*.py 的 `scan(codes, get_kline, get_quotes) -> list` 入口保持不变
- 新基类是**可选**的：旧函数式策略继续工作，新策略可选择继承 BaseStrategy 获得更多结构
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional
import pandas as pd


# ════════════════════════════════════════════════════
# 候选信号数据结构
# ════════════════════════════════════════════════════
@dataclass
class Signal:
    """单只候选股的信号输出"""
    code: str
    name: str = ""
    price: float = 0.0
    change: float = 0.0           # 当日涨跌幅 %
    score: float = 0.0            # 因子综合分
    reason: str = ""              # 触发理由（人类可读）
    meta: dict = field(default_factory=dict)  # 任意扩展字段（如各分项分）

    def to_dict(self) -> dict:
        d = {"code": self.code, "name": self.name, "price": round(self.price, 2),
             "change": round(self.change, 2), "score": round(self.score, 2),
             "reason": self.reason}
        if self.meta:
            d.update(self.meta)
        return d


# ════════════════════════════════════════════════════
# 因子基类
# ════════════════════════════════════════════════════
class BaseFactor(ABC):
    """共享字段：参数化 + 命名 + 描述"""
    name: str = "BaseFactor"
    desc: str = ""

    def __init__(self, **params):
        self.params = params

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.params}>"


class BaseBuyFactor(BaseFactor):
    """
    买入因子：判断单只股票是否产生买入信号。

    子类必须实现 match_buy()，返回 Signal 或 None。
    框架负责遍历 codes、做 try/except 容错、排序。
    """
    name = "BaseBuyFactor"

    @abstractmethod
    def match_buy(self, code: str, kline: pd.DataFrame, quote: dict) -> Optional[Signal]:
        """
        kline: get_kline(code, N) 返回的 DataFrame，含 open/high/low/close/volume
        quote: 当日实时行情 dict，含 {名称, 最新价, 涨跌幅 ...}（可能为空 dict）
        返回 Signal 表示产生买入信号；None 表示跳过。
        """
        ...


class BaseSellFactor(BaseFactor):
    """
    卖出因子：判断已持仓标的是否触发卖出。
    入参与 BaseBuyFactor 类似，加上 entry_price/entry_date 持仓信息。
    """
    name = "BaseSellFactor"

    @abstractmethod
    def match_sell(self, code: str, kline: pd.DataFrame, quote: dict,
                   entry_price: float, entry_date: str) -> Optional[str]:
        """返回卖出理由字符串则平仓；None 表示继续持有"""
        ...


# ════════════════════════════════════════════════════
# 策略基类
# ════════════════════════════════════════════════════
class BaseStrategy(ABC):
    """
    策略 = 一组买入因子 + 可选卖出因子。

    向后兼容入口：实例可以直接当作 `scan(codes, get_kline, get_quotes)` 调用，
    返回与旧 strategies/*.py 完全一致的 list[dict]。

    用法：
        class MAGoldenCross(BaseStrategy):
            name = "均线金叉"
            buy_factors = [MACrossBuy()]   # 也可以在 __init__ 里动态填

        scan = MAGoldenCross().to_legacy_scan()
        # main.py 的 strategy_manager 调用 scan(codes, get_kline, get_quotes) 即可
    """
    name: str = "BaseStrategy"
    desc: str = ""
    buy_factors: list[BaseBuyFactor]   = []
    sell_factors: list[BaseSellFactor] = []
    top_n: int = 50           # 输出 top 多少只
    min_score: float = 0.0    # 分数下限（小于的过滤掉）

    def __init__(self):
        # 拷贝类属性到实例，避免子类列表共享
        self.buy_factors  = list(self.buy_factors)
        self.sell_factors = list(self.sell_factors)

    # ── 主流程：扫描候选池 ───────────────────────────────
    def scan(self,
             codes: list[str],
             get_kline: Callable[[str, int], pd.DataFrame],
             get_quotes: Callable[[list[str]], pd.DataFrame]
             ) -> list[dict]:
        """对每个 code 跑所有 buy_factor，合并打分，排序输出。"""
        if not self.buy_factors:
            return []

        quotes_df = get_quotes(codes)
        results: list[Signal] = []

        for code in codes:
            try:
                kline = get_kline(code, 60)
                if kline is None or kline.empty:
                    continue
                quote = self._row_to_dict(quotes_df, code)

                # 合并多个买入因子的信号
                merged: Optional[Signal] = None
                for f in self.buy_factors:
                    try:
                        s = f.match_buy(code, kline, quote)
                    except Exception:
                        s = None
                    if s is None:
                        continue
                    if merged is None:
                        merged = s
                    else:
                        merged.score  += s.score
                        merged.reason  = (merged.reason + " | " + s.reason).strip(" |")
                        merged.meta.update(s.meta)

                if merged is not None and merged.score >= self.min_score:
                    results.append(merged)
            except Exception:
                continue

        results.sort(key=lambda x: x.score, reverse=True)
        return [s.to_dict() for s in results[: self.top_n]]

    # ── 持仓评估：是否卖出某个标的 ─────────────────────
    def evaluate_sell(self, code: str, kline: pd.DataFrame, quote: dict,
                      entry_price: float, entry_date: str) -> Optional[str]:
        """返回首个触发卖出的理由；都不触发返回 None"""
        for f in self.sell_factors:
            try:
                r = f.match_sell(code, kline, quote, entry_price, entry_date)
            except Exception:
                r = None
            if r:
                return r
        return None

    # ── 向后兼容包装 ──────────────────────────────────
    def to_legacy_scan(self) -> Callable:
        """返回与旧 scan(codes, get_kline, get_quotes) 签名一致的函数"""
        return self.scan

    # ── 内部工具 ──────────────────────────────────────
    @staticmethod
    def _row_to_dict(quotes_df, code: str) -> dict:
        try:
            if quotes_df is None or quotes_df.empty:
                return {}
            row = quotes_df[quotes_df['代码'] == code]
            if row.empty:
                return {}
            return row.iloc[0].to_dict()
        except Exception:
            return {}


__all__ = ["Signal", "BaseFactor", "BaseBuyFactor", "BaseSellFactor", "BaseStrategy"]
