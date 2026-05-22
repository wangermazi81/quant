# -*- coding: utf-8 -*-
"""
SealTracker — 封板强弱时间序列追踪器
─────────────────────────────────────────────
贯穿一次 Flask 进程内的"封板股状态时间线"，让 broker 在判断
"能不能排板买进"时不再依赖单点封单快照，而是看：
  1. 当前封单金额（30 分）
  2. 封单趋势（20 分，最近 3 个采样是否递增）
  3. 开板次数（−20 分扣减）
  4. 连板天数（15 分）
  5. 首次封板时间（15 分，早盘封最强）

总分 0–100，**分越高越强势（越买不到）**。买入判定方走
`score > threshold` 则拒。

冷启动（无历史样本）时回退到"当前封单单点"逻辑，避免重启后
所有股票都被打高分误拒。

数据结构：
    _data[code] = {
        "first_sealed_ts": float,   # 首次封板 unix 时间戳
        "max_seal_amount": float,   # 历史最大封单金额（元）
        "samples":         deque,   # 最近 N 个采样
        "open_count":      int,     # 开板次数（从已封→未封的转换数）
        "last_was_sealed": bool,
        "consec_limit":    int,
    }
"""

from __future__ import annotations

import time
import threading
import logging
from collections import deque
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

# ── 配置常量 ─────────────────────────────────────────────
MAX_SAMPLES = 120           # 每只股票最多保留 120 个采样（30s × 120 = 1 小时）
DEFAULT_SCORE_THRESHOLD = 60  # 综合分 > 60 视为强封拒买


class SealTracker:
    """全局封板状态追踪器（单例）"""

    def __init__(self):
        self._lock = threading.RLock()
        self._data: Dict[str, dict] = {}

    # ── 写入接口 ─────────────────────────────────────────
    def update(self, code: str, seal_amount: float, price: float,
               limit_up: float, is_sealed: bool,
               consec_limit: int = 0, ts: Optional[float] = None) -> None:
        """
        喂入一个采样点。
        seal_amount: 当前封单金额（元，= bid1_vol × 100 × limit_up）
        is_sealed:   当前是否封板（price >= limit_up - 0.001）
        consec_limit: 连板天数（不传则保留旧值）
        """
        if ts is None:
            ts = time.time()
        with self._lock:
            d = self._data.get(code)
            if d is None:
                d = {
                    "first_sealed_ts": None,
                    "max_seal_amount": 0.0,
                    "samples":         deque(maxlen=MAX_SAMPLES),
                    "open_count":      0,
                    "last_was_sealed": False,
                    "consec_limit":    consec_limit,
                }
                self._data[code] = d
            # 首次封板时间（只记最早一次）
            if is_sealed and d["first_sealed_ts"] is None:
                d["first_sealed_ts"] = ts
            # 历史最大封单
            if is_sealed and seal_amount > d["max_seal_amount"]:
                d["max_seal_amount"] = seal_amount
            # 开板次数：上次封了 → 现在没封
            if d["last_was_sealed"] and not is_sealed:
                d["open_count"] += 1
            d["last_was_sealed"] = is_sealed
            d["samples"].append({
                "ts":     ts,
                "seal":   seal_amount,
                "price":  price,
                "lu":     limit_up,
                "sealed": is_sealed,
            })
            if consec_limit > 0:
                d["consec_limit"] = consec_limit

    # ── 评分接口 ─────────────────────────────────────────
    def score(self, code: str) -> int:
        """
        返回 0-100 综合强度评分。分越高越强势（越买不到）。
        无历史样本时回退到"当前数据保守判定"。
        """
        with self._lock:
            d = self._data.get(code)
            if not d:
                return 100   # 完全没数据，保守拒
            samples = list(d["samples"])
            if not samples:
                return 100
            current = samples[-1]

            # 当前未封板（开板状态）：直接低分允许买
            if not current["sealed"]:
                # 加权 open_count 让多次开板的也评出"强度差"
                if d["open_count"] >= 2:
                    return 20    # 多次炸板，已经很弱
                if d["open_count"] == 1:
                    return 30
                return 40        # 首次开板瞬间

            sc = 0

            # 1) 当前封单金额（30 分）
            seal = current["seal"]
            if   seal >= 100_000_000: sc += 30
            elif seal >=  50_000_000: sc += 20
            elif seal >=  10_000_000: sc += 10
            else:                     sc +=  0

            # 2) 封单趋势（20 分）：最近 3 个已封板采样
            recent = [s["seal"] for s in samples[-5:] if s["sealed"]]
            if len(recent) >= 3:
                if   recent[-1] > recent[0] * 1.1: sc += 20  # 持续递增
                elif recent[-1] < recent[0] * 0.9: sc +=  0  # 持续递减（分歧）
                else:                              sc += 10  # 震荡
            else:
                # 历史样本不足，回退到"当前封单单点"判断
                # 当前 ≥ 5000 万 给 15 分（接近强封），< 5000 万 给 0 分（弱封）
                sc += 15 if seal >= 50_000_000 else 0

            # 3) 开板次数（扣分）
            opens = d["open_count"]
            if   opens == 0: sc -= 0
            elif opens == 1: sc -= 10
            else:            sc -= 20

            # 4) 连板天数（15 分）
            cs = d["consec_limit"]
            if   cs >= 5: sc += 15
            elif cs >= 3: sc += 12
            elif cs >= 2: sc +=  8
            else:         sc +=  5

            # 5) 首次封板时间（15 分，早盘封最强）
            ft = d.get("first_sealed_ts")
            if ft:
                lt = time.localtime(ft)
                hm = lt.tm_hour * 100 + lt.tm_min
                if   hm < 1000: sc += 15
                elif hm < 1100: sc += 10
                elif hm < 1300: sc +=  5
                else:           sc +=  0

            return max(0, min(100, sc))

    # ── 查询接口 ─────────────────────────────────────────
    def status(self, code: str) -> dict:
        """详细状态（给前端/调试用）"""
        with self._lock:
            d = self._data.get(code)
            if not d:
                return {}
            samples = list(d["samples"])
            cur = samples[-1] if samples else {}
            return {
                "first_sealed_ts": d["first_sealed_ts"],
                "max_seal_amount": d["max_seal_amount"],
                "current_seal":    cur.get("seal", 0),
                "current_sealed":  cur.get("sealed", False),
                "open_count":      d["open_count"],
                "consec_limit":    d["consec_limit"],
                "sample_count":    len(samples),
                "score":           self.score(code),
            }

    def all_tracked(self) -> List[str]:
        with self._lock:
            return list(self._data.keys())


# ── 单例 ─────────────────────────────────────────────────
_INSTANCE: Optional[SealTracker] = None

def get_tracker() -> SealTracker:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = SealTracker()
    return _INSTANCE
