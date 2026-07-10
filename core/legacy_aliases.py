# -*- coding: utf-8 -*-
"""扁平模块名 → 子包路径 的兼容别名层（替代已删除的 _pathsetup sys.path 注入）。

为什么还需要它：
`strategies/*.py`、`factors/*.py` 是用户自建文件（gitignore，云端用户各有自己的），
由 strategy_manager / factor_manager 用 importlib 动态加载，内部写的是扁平导入
（如 `from indicators import pct_change`、`from base import BaseStrategy`）。
这些文件无法批量改写，因此保留一个"扁平名 → 真实包模块"的导入别名。

实现方式（与 _pathsetup 的本质区别）：
- _pathsetup 把子包目录塞进 sys.path → `import user_manager` 与
  `from core import user_manager` 会产生【两个独立模块实例】（锁/缓存/单例翻倍）。
- 本模块注册一个惰性 MetaPathFinder：import 扁平名时直接返回真实包模块
  【同一个实例】，且按需加载、不预先导入任何东西。

平台自身代码一律使用包导入（from core import user_manager），不依赖本层。
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

# 扁平名 → 真实模块路径（与目录结构一一对应，新增子包模块时同步维护）
ALIASES: dict[str, str] = {
    # core/
    "changelog_manager":      "core.changelog_manager",
    "daban_ride":             "core.daban_ride",
    "daban_strategy_runtime": "core.daban_strategy_runtime",
    "db_migration":           "core.db_migration",
    "email_sender":           "core.email_sender",
    "feedback_manager":       "core.feedback_manager",
    "notification_manager":   "core.notification_manager",
    "order_sizing":           "core.order_sizing",
    "run_card":               "core.run_card",
    "sell_rules":             "core.sell_rules",
    "user_manager":           "core.user_manager",
    # market/
    "market_context":         "market.market_context",
    "market_data":            "market.market_data",
    "market_regime":          "market.market_regime",
    "market_stats":           "market.market_stats",
    "news_fetcher":           "market.news_fetcher",
    "ths_quote":              "market.ths_quote",
    "tushare_replay":         "market.tushare_replay",
    # broker/
    "broker_interface":       "broker.broker_interface",
    "paper_broker":           "broker.paper_broker",
    "slippage":               "broker.slippage",
    "ths_operator":           "broker.ths_operator",
    # strategy_engine/
    "backtest":               "strategy_engine.backtest",
    "base":                   "strategy_engine.base",
    "strategy":               "strategy_engine.strategy",
    "strategy_manager":       "strategy_engine.strategy_manager",
    # analytics/
    "ai_analyst":             "analytics.ai_analyst",
    "ai_candidate_ranker":    "analytics.ai_candidate_ranker",
    "ai_position_sizer":      "analytics.ai_position_sizer",
    "ai_research_store":      "analytics.ai_research_store",
    "ai_trader":              "analytics.ai_trader",
    "ai_trade_plan":          "analytics.ai_trade_plan",
    "ai_walkforward":         "analytics.ai_walkforward",
    "auto_factor_scheduler":  "analytics.auto_factor_scheduler",
    "dabang_backtest":        "analytics.dabang_backtest",
    "datasource_config":      "analytics.datasource_config",
    "data_manager":           "analytics.data_manager",
    "factor_manager":         "analytics.factor_manager",
    "indicators":             "analytics.indicators",
    "kronos_predictor":       "analytics.kronos_predictor",
    "lhb_engine":             "analytics.lhb_engine",
    "market_signal_quality":  "analytics.market_signal_quality",
    "metrics":                "analytics.metrics",
    "portfolio_optimizer":    "analytics.portfolio_optimizer",
    "relay_factor_mining":    "analytics.relay_factor_mining",
    "review_engine":          "analytics.review_engine",
    "seal_tracker":           "analytics.seal_tracker",
    "search_manager":         "analytics.search_manager",
    "shadow_account":         "analytics.shadow_account",
    "stock_scorer":           "analytics.stock_scorer",
    "tushare_provider":       "analytics.tushare_provider",
}


class _AliasLoader(importlib.abc.Loader):
    """create_module 直接返回真实包模块 → sys.modules[扁平名] 与包模块是同一实例。"""

    def __init__(self, real_name: str):
        self._real_name = real_name

    def create_module(self, spec):
        return importlib.import_module(self._real_name)

    def exec_module(self, module):
        pass  # 真实模块已执行过，别名无需再执行


class _LegacyAliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        real = ALIASES.get(fullname)
        if real is None:
            return None
        return importlib.util.spec_from_loader(fullname, _AliasLoader(real))


_installed = False


def install() -> None:
    """幂等注册别名 finder（策略/因子动态加载入口处调用）。"""
    global _installed
    if _installed:
        return
    sys.meta_path.append(_LegacyAliasFinder())
    _installed = True
