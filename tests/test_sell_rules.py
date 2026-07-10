# -*- coding: utf-8 -*-
"""
tests/test_sell_rules.py
账户默认止损/止盈/超期退出决策测试 + 与两处原内联文案的等价核对。

运行：
  cd ths_auto_trader
  python -m pytest tests/test_sell_rules.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sell_rules import account_exit


class TestAccountExit:
    def test_stop_loss(self):
        sell, reason = account_exit(-0.06, 1, -0.05, 0.10, 10)
        assert sell and "止损" in reason

    def test_take_profit(self):
        sell, reason = account_exit(0.12, 1, -0.05, 0.10, 10)
        assert sell and "止盈" in reason

    def test_max_hold(self):
        sell, reason = account_exit(0.02, 10, -0.05, 0.10, 10)
        assert sell and "超期" in reason

    def test_hold_when_nothing(self):
        sell, reason = account_exit(0.02, 3, -0.05, 0.10, 10)
        assert sell is False and reason is None

    def test_priority_stop_over_hold(self):
        # 同时亏损止损 + 超期 → 止损优先（先判）
        sell, reason = account_exit(-0.08, 20, -0.05, 0.10, 10)
        assert sell and "止损" in reason

    def test_threshold_decimals(self):
        # paper 风格 .1f
        _, r1 = account_exit(-0.06, 1, -0.05, 0.10, 10, thr_decimals=1)
        assert "≤-5.0%" in r1
        # 实盘风格 .0f
        _, r0 = account_exit(-0.06, 1, -0.05, 0.10, 10, thr_decimals=0)
        assert "≤-5%" in r0


class TestEquivalence:
    """与 paper_broker / main.py 两处原内联文案逐例比对。"""

    def _paper_original(self, pnl_pct, hold, stop_loss, take_profit, max_hold):
        reason = None
        if pnl_pct <= stop_loss:
            reason = f"止损({pnl_pct*100:.1f}%≤{stop_loss*100:.1f}%)"
        elif pnl_pct >= take_profit:
            reason = f"止盈({pnl_pct*100:.1f}%≥{take_profit*100:.1f}%)"
        elif hold >= max_hold:
            reason = f"超期({hold}天≥{max_hold}天)"
        return reason

    def _live_original(self, pnl, hold, stop, take, max_hold):
        sell, reason = False, None
        if pnl <= stop:
            sell, reason = True, f"止损({pnl*100:.1f}%≤{stop*100:.0f}%)"
        elif pnl >= take:
            sell, reason = True, f"止盈({pnl*100:.1f}%≥{take*100:.0f}%)"
        elif hold >= max_hold:
            sell, reason = True, f"超期({hold}天≥{max_hold}天)"
        return sell, reason

    def test_matches_paper(self):
        cases = [(-0.06, 1, -0.05, 0.10, 10), (0.12, 2, -0.05, 0.10, 10),
                 (0.01, 10, -0.05, 0.10, 10), (0.03, 3, -0.05, 0.10, 10),
                 (-0.082, 1, -0.08, 0.015, 3)]
        for c in cases:
            assert account_exit(*c, thr_decimals=1)[1] == self._paper_original(*c), c

    def test_matches_live(self):
        cases = [(-0.06, 1, -0.05, 0.10, 10), (0.12, 2, -0.05, 0.10, 10),
                 (0.01, 10, -0.05, 0.10, 10), (0.03, 3, -0.05, 0.10, 10)]
        for c in cases:
            assert account_exit(*c, thr_decimals=0) == self._live_original(*c), c
