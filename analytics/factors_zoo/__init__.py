# -*- coding: utf-8 -*-
"""见微 Alpha 因子动物园 (factors_zoo)

见微 Alpha 自研多因子量化模块，内置量价/反转/动量/波动等多个主题因子库。

特性：
  - AST 静态扫描注册：无侵入加载因子，单个因子异常不影响整体
  - 多连接 K 线池：本地 CSV + TDX 并行池，全市场覆盖率 95%+
  - 输出校验：禁止 inf / NaN 占比 > 95%，杜绝脏数据进入策略链
  - 完全独立：和 analytics/factor_manager.py 解耦，不破坏现有功能

用法：
    from analytics.factors_zoo import registry, build_panel, latest_score

    reg = registry()
    panel = build_panel(codes, get_kline, days=60)
    factor_df = reg.compute("gtja191_001", panel)
    scores = latest_score(factor_df)  # {code: float|None}
"""
from .registry import (
    Registry,
    AlphaHandle,
    SkipAlpha,
    RegistryError,
    get_default_registry,
    reset_default_registry,
)
from .panel_adapter import build_panel, latest_score
from . import activator  # noqa: F401


def registry() -> Registry:
    """返回进程级单例（首次调用懒扫描 zoo/）"""
    return get_default_registry()


__all__ = [
    "Registry",
    "AlphaHandle",
    "SkipAlpha",
    "RegistryError",
    "registry",
    "get_default_registry",
    "reset_default_registry",
    "build_panel",
    "latest_score",
]
