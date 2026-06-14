# -*- coding: utf-8 -*-
"""打板 Ride 卖出决策（纯函数，无副作用，模拟盘/实盘共用，可独立单测）。

从 main.py 抽出（main.py 拆分第二刀）。核心是打板正确的非对称盈亏比卖法
（回测盈亏比≈2.5、期望 +3%/笔）：
  - 当日仍封板(连板) → 持有，让利润奔跑（不卖）；
  - 跌破成本 stop_loss → 止损（砍得快）；
  - 从持仓期最高价回落 ≥ trail → 卖（吃完连板后保护利润）；
  - 持有满 max_hold 天 → 卖（backstop）。
config 的 DABAN_* 作为默认参数，测试可覆盖。
"""
from __future__ import annotations

try:
    from config import (DABAN_STOP_LOSS as _SL, DABAN_TRAIL as _TRAIL,
                        DABAN_MAX_HOLD as _HOLD, DABAN_HOLD_LIMITUP as _HOLD_LU)
except Exception:
    _SL, _TRAIL, _HOLD, _HOLD_LU = -0.05, 0.07, 10, True


def board_rate(code: str) -> float:
    """涨停幅度：创业板/科创板(300/301/688/689)=20%，北交所(4/8 开头)=30%，其余主板=10%。"""
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("4", "8")):
        return 0.30
    return 0.10


def ride_decision(code: str, cost: float, cur: float, last_close: float,
                  hold_days: int, peak: float, *,
                  stop_loss: float = _SL, trail: float = _TRAIL,
                  max_hold: int = _HOLD, hold_limitup: bool = _HOLD_LU):
    """打板 Ride 卖出决策。返回 (should_sell: bool, reason: str|None, new_peak: float)。

    与 main.py 原 _daban_ride_decision 严格等价（逻辑逐行搬迁，默认参数取同一 config 常量）。
    """
    new_peak = max(peak or cost, cur)
    if cost <= 0 or cur <= 0:
        return False, None, new_peak
    limit_up = round(last_close * (1 + board_rate(code)), 2) if last_close > 0 else 0
    if hold_limitup and limit_up > 0 and cur >= limit_up - 0.001:
        return False, None, new_peak          # 连板，持有
    pnl = (cur - cost) / cost
    ddp = (new_peak - cur) / new_peak if new_peak > 0 else 0.0
    if pnl <= stop_loss:
        return True, f"打板止损({pnl*100:.1f}%≤{stop_loss*100:.0f}%)", new_peak
    if ddp >= trail:
        return True, f"高位回落({ddp*100:.1f}%≥{trail*100:.0f}%,峰¥{new_peak:.2f})", new_peak
    if hold_days >= max_hold:
        return True, f"超期({hold_days}天≥{max_hold}天)", new_peak
    return False, None, new_peak
