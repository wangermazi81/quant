# -*- coding: utf-8 -*-
"""请求参数解析助手。

原先 main.py 与 web/user、admin、search、pages、review 各复制一份
_bounded_int_arg（共 6 份），工单4 收敛到此处唯一实现。
保留下划线命名以兼容既有调用点。
"""
from flask import request


def _bounded_int_arg(name: str, default: int, min_value: int, max_value: int) -> int:
    """从 query string 取整数并夹在 [min_value, max_value]，解析失败用默认值。"""
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def _positive_float_value(raw, default: float | None = None) -> float | None:
    """解析正浮点：空值给默认，非法/非正返回 None。"""
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _float_value(raw, default: float | None = None) -> float | None:
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _int_value(raw, default: int | None = None) -> int | None:
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
