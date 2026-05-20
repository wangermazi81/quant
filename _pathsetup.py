# -*- coding: utf-8 -*-
"""
sys.path 注入器 — 让重构后的子包目录被识别为扁平模块路径。

设计原因：
项目原本所有 .py 模块都在 ths_auto_trader/ 根目录扁平存放，
import 形如 `from market_data import ...`、`import strategy_manager as sm`。
为了项目可维护性，模块被分类移入 core/ market/ broker/ strategy_engine/ analytics/。
为避免逐文件改写 import 语句（风险大、易遗漏），把这些子目录加入 sys.path，
让 Python 在导入时仍能用扁平名找到模块。

使用方法：
    # 在 main.py / launcher.py 或任何外部入口的最顶部（其他 import 之前）写：
    import _pathsetup  # noqa: F401
"""
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent
_SUBPKGS = ("core", "market", "broker", "strategy_engine", "analytics")

for _name in _SUBPKGS:
    _p = str(_BASE / _name)
    if _p not in sys.path:
        sys.path.insert(0, _p)
