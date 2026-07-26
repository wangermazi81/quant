# -*- coding: utf-8 -*-
"""实盘下单总闸状态（从 main.py 下沉，工单4）。

前端可一键开关；关闭时不给桥接器任何信号 = 彻底停下单。
状态持久化到 data/bridge_flags.json。
`_live_trade_enabled` 会在加载时整体重建（重新赋值），外部一律通过
_live_enabled() / set_live_enabled() 访问，不要直接 import 这个字典。
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
_BRIDGE_FLAGS_FILE = _BASE_DIR / "data" / "bridge_flags.json"
_live_trade_enabled = {}   # uid -> bool（缺省 False=关，必须显式开闸）


def _load_bridge_flags():
    global _live_trade_enabled
    try:
        if _BRIDGE_FLAGS_FILE.exists():
            _live_trade_enabled = {int(k): bool(v) for k, v in
                                   json.loads(_BRIDGE_FLAGS_FILE.read_text(encoding="utf-8")).items()}
    except Exception as e:
        # 加载失败必须整表清空：残留半截状态会让部分 uid 沿用上一次的开闸值
        _live_trade_enabled = {}
        logger.warning(f"[bridge闸门] 加载失败，已全部按「关」处理: {e}")


def _save_bridge_flags():
    try:
        _BRIDGE_FLAGS_FILE.write_text(
            json.dumps({str(k): v for k, v in _live_trade_enabled.items()}), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[bridge闸门] 保存失败: {e}")


def _live_enabled(uid) -> bool:
    """实盘下单总闸。缺省 **False**：与 config 的「实盘必须显式打开」同向。

    此前缺省为 True，而 data/bridge_flags.json 是运行态文件（gitignore，全新部署
    必然不存在）→ 新用户/重装后无需任何操作即具备下单能力，方向与安全默认相反。
    """
    return _live_trade_enabled.get(uid, False)


def set_live_enabled(uid: int, enabled: bool) -> None:
    """设置某用户实盘下单总闸并持久化。"""
    _live_trade_enabled[uid] = bool(enabled)
    _save_bridge_flags()


_load_bridge_flags()
