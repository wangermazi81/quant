# -*- coding: utf-8 -*-
"""打板 Ride 的运行态（从 main.py 下沉，工单4）。

- _DABAN_PEAKS：模拟盘持仓期最高价（Ride 追踪止盈用），持久化到 data/daban_peaks.json
- _LIVE_SELL_PENDING：实盘已发卖出信号待成交的 (uid, code) 去重集合
- _daban_ride_decision：打板 Ride 卖出决策薄包装（逻辑在 core/daban_ride.py）

注意：_DABAN_PEAKS / _LIVE_SELL_PENDING 只做原地增删、绝不重新赋值，
main.py 等处通过 `from core.daban_state import _DABAN_PEAKS` 持有的引用
才能始终指向同一份状态。
"""
import json
import logging
from pathlib import Path

from core import daban_ride
from core import daban_strategy_runtime as daban_runtime

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent

_DABAN_PEAKS: dict = {}   # (uid, code) -> 持仓期最高价（Ride 追踪止盈用）
_DABAN_PEAKS_FILE = _BASE_DIR / "data" / "daban_peaks.json"

_LIVE_SELL_PENDING: set = set()   # (uid, code) 已生成卖出信号、等成交确认，避免每轮重复发


def _load_daban_peaks():
    """启动时加载模拟盘持仓期峰值。
    ★ 跟实盘 um.update_live_peak 一致：peak 持久化，重启不丢失，
    否则回落止盈会因 peak 被重置成成本价而失真（高位回落判断算不准、漏卖）。"""
    try:
        if _DABAN_PEAKS_FILE.exists():
            raw = json.loads(_DABAN_PEAKS_FILE.read_text(encoding="utf-8"))
            for k, v in raw.items():
                uid_s, code = k.split("|", 1)
                _DABAN_PEAKS[(int(uid_s), code)] = float(v)
            logger.info(f"[daban] 已加载持仓峰值缓存 {len(_DABAN_PEAKS)} 条")
    except Exception as e:
        logger.warning(f"[daban] 加载峰值缓存失败: {e}")


def _save_daban_peaks():
    try:
        raw = {f"{uid}|{code}": v for (uid, code), v in _DABAN_PEAKS.items()}
        _DABAN_PEAKS_FILE.write_text(json.dumps(raw), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[daban] 保存峰值缓存失败: {e}")


_load_daban_peaks()


def _daban_ride_decision(code: str, cost: float, cur: float, last_close: float,
                         hold_days: int, peak: float, strategy: str = "da_ban"):
    """打板 Ride 卖出决策（薄包装；逻辑与默认阈值见 core/daban_ride.ride_decision）。"""
    return daban_ride.ride_decision(
        code, cost, cur, last_close, hold_days, peak,
        **daban_runtime.ride_params_for_strategy(strategy)
    )
