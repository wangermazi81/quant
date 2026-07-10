# -*- coding: utf-8 -*-
"""测试用：替换项目内部模块的统一打桩助手（工单3 包导入改造配套）。

背景：改造前产线代码用扁平导入（import ths_quote），测试只需
monkeypatch.setitem(sys.modules, "ths_quote", 假模块) 即可打桩。
改造后产线代码走包导入（from market import ths_quote），解析路径变成
「sys.modules['market.ths_quote'] + market 包属性」——扁平名打桩不再生效。

本助手把三个解析路径一次打全：
  1. sys.modules[扁平名]      —— 用户自建策略/因子（经 legacy_aliases）仍走扁平名
  2. sys.modules[包.模块名]   —— `from market.ths_quote import X` 形式
  3. setattr(包, 模块名)      —— `from market import ths_quote` 形式
monkeypatch 负责用例结束后自动还原。
"""
import importlib
import sys

from core.legacy_aliases import ALIASES


def stub_project_module(monkeypatch, flat_name: str, stub) -> None:
    """替换一个项目内部模块（扁平名 + 包路径 + 包属性 三处一起打桩）。"""
    monkeypatch.setitem(sys.modules, flat_name, stub)
    real = ALIASES.get(flat_name)
    if real:
        monkeypatch.setitem(sys.modules, real, stub)
        pkg_name, attr = real.rsplit(".", 1)
        pkg = importlib.import_module(pkg_name)
        monkeypatch.setattr(pkg, attr, stub, raising=False)
