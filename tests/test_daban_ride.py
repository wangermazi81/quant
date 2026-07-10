# -*- coding: utf-8 -*-
"""
tests/test_daban_ride.py
打板 Ride 卖出决策纯函数测试（主力实盘策略的卖出逻辑，财务关键）。

运行：
  cd ths_auto_trader
  python -m pytest tests/test_daban_ride.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

from daban_ride import board_rate, ride_decision

# 固定一套阈值，使测试不依赖 config 当前值
TH = dict(stop_loss=-0.05, trail=0.07, max_hold=10, hold_limitup=True)


# ── 涨停幅度 ──────────────────────────────────────────────
class TestBoardRate:
    def test_main_board(self):
        assert board_rate("600519") == 0.10
        assert board_rate("000001") == 0.10

    def test_chinext_star(self):
        for c in ("300750", "301029", "688981", "689009"):
            assert board_rate(c) == 0.20

    def test_bse(self):
        assert board_rate("830799") == 0.30
        assert board_rate("430139") == 0.30


# ── Ride 决策：各触发分支 ──────────────────────────────────
class TestRideDecision:
    def test_hold_when_limit_up(self):
        # 主板 last_close=10 → 涨停 11.0；cur=11.0 仍封板 → 持有（即使是浮盈也不卖）
        sell, reason, peak = ride_decision("600000", 9.0, 11.0, 10.0, 1, 9.0, **TH)
        assert sell is False and reason is None

    def test_stop_loss(self):
        # 成本 10，现 9.4 → -6% ≤ -5% → 止损
        sell, reason, _ = ride_decision("600000", 10.0, 9.4, 9.5, 1, 10.0, **TH)
        assert sell is True and "止损" in reason

    def test_trailing_stop(self):
        # 峰 12，现 11.0 → 回落 8.3% ≥ 7% → 卖
        sell, reason, _ = ride_decision("600000", 10.0, 11.0, 11.2, 2, 12.0, **TH)
        assert sell is True and "高位回落" in reason

    def test_max_hold(self):
        # 持有 10 天、无止损/回落 → 超期卖
        sell, reason, _ = ride_decision("600000", 10.0, 10.3, 10.4, 10, 10.4, **TH)
        assert sell is True and "超期" in reason

    def test_hold_when_nothing_triggered(self):
        # 小幅波动、未到任何阈值 → 持有
        sell, reason, _ = ride_decision("600000", 10.0, 10.2, 10.3, 2, 10.3, **TH)
        assert sell is False and reason is None

    def test_new_peak_tracks_current(self):
        # 现价创新高 → new_peak 跟随
        _, _, peak = ride_decision("600000", 10.0, 13.0, 12.0, 1, 11.0, **TH)
        assert peak == 13.0

    def test_peak_holds_when_current_lower(self):
        _, _, peak = ride_decision("600000", 10.0, 11.0, 11.5, 2, 12.0, **TH)
        assert peak == 12.0

    def test_limit_up_priority_over_stop(self):
        # 即使浮亏，只要当日封板就持有（让利润奔跑优先于止损）
        # 主板 last_close=20 → 涨停 22.0；成本 25(亏)，现 22.0 封板 → 持有
        sell, _, _ = ride_decision("600000", 25.0, 22.0, 20.0, 1, 25.0, **TH)
        assert sell is False

    def test_hold_limitup_false_allows_sell(self):
        # 关掉"封板持有"后，封板价位也按止损/回落正常判定
        th = dict(TH); th["hold_limitup"] = False
        sell, reason, _ = ride_decision("600000", 25.0, 22.0, 20.0, 1, 25.0, **th)
        assert sell is True and "止损" in reason   # -12% 触发止损

    def test_guard_invalid_prices(self):
        assert ride_decision("600000", 0, 10, 10, 1, 10, **TH)[0] is False
        assert ride_decision("600000", 10, 0, 10, 1, 10, **TH)[0] is False


# ── 与 main.py 原内联逻辑等价性 ────────────────────────────
class TestEquivalence:
    def _original(self, code, cost, cur, last_close, hold_days, peak,
                  SL=-0.05, TRAIL=0.07, HOLD=10, HOLD_LU=True):
        """复刻 main.py 原 _daban_ride_decision（含 _daban_board_rate）。"""
        def _rate(c):
            if c.startswith(("300", "301", "688", "689")):
                return 0.20
            if c.startswith(("4", "8")):
                return 0.30
            return 0.10
        new_peak = max(peak or cost, cur)
        if cost <= 0 or cur <= 0:
            return False, None, new_peak
        limit_up = round(last_close * (1 + _rate(code)), 2) if last_close > 0 else 0
        if HOLD_LU and limit_up > 0 and cur >= limit_up - 0.001:
            return False, None, new_peak
        pnl = (cur - cost) / cost
        ddp = (new_peak - cur) / new_peak if new_peak > 0 else 0.0
        if pnl <= SL:
            return True, f"打板止损({pnl*100:.1f}%≤{SL*100:.0f}%)", new_peak
        if ddp >= TRAIL:
            return True, f"高位回落({ddp*100:.1f}%≥{TRAIL*100:.0f}%,峰¥{new_peak:.2f})", new_peak
        if hold_days >= HOLD:
            return True, f"超期({hold_days}天≥{HOLD}天)", new_peak
        return False, None, new_peak

    def test_matches_original(self):
        cases = [
            ("600000", 10.0, 9.4, 9.5, 1, 10.0),
            ("600000", 10.0, 11.0, 11.2, 2, 12.0),
            ("600000", 10.0, 10.3, 10.4, 10, 10.4),
            ("300750", 9.0, 10.8, 9.0, 1, 9.0),     # 创业板封板
            ("830799", 8.0, 10.4, 8.0, 1, 8.0),     # 北交所封板
            ("600000", 10.0, 10.2, 10.3, 2, 10.3),
            ("600000", 25.0, 22.0, 20.0, 1, 25.0),  # 封板但浮亏
            ("600000", 0, 10, 10, 1, 10),           # 边界
        ]
        for c in cases:
            assert ride_decision(*c, **TH) == self._original(*c), f"不一致: {c}"
