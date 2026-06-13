# -*- coding: utf-8 -*-
"""
tests/test_order_sizing.py
下单仓位/手数纯函数测试 + 与 main.py 原内联公式的等价性核对。

运行：
  cd ths_auto_trader
  python -m pytest tests/test_order_sizing.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

from order_sizing import lot_shares, pick_ratio, clamp_to_max_order, LOT


# ── lot_shares ────────────────────────────────────────────
class TestLotShares:
    def test_round_down_to_lot(self):
        # 预算 10000 / 价 12.5 = 800 股 → 整 800
        assert lot_shares(10000, 12.5) == 800

    def test_floor_partial_lot(self):
        # 预算 10000 / 价 13 = 769.2 → 向下取整到 700
        assert lot_shares(10000, 13) == 700

    def test_min_one_lot(self):
        # 预算不足一手也至少 1 手
        assert lot_shares(50, 12.5) == LOT

    def test_high_price(self):
        # 贵州茅台式高价：预算 100000 / 1700 = 58.8 → 0 手 → 兜底 1 手
        assert lot_shares(100000, 1700) == LOT

    def test_zero_price_safe(self):
        assert lot_shares(10000, 0) == LOT

    @pytest.mark.parametrize("budget,price", [
        (100000, 12.5), (45000, 8.88), (20000, 3.0), (99999, 199.9),
        (1, 1), (500000, 50), (33333, 17.7), (10000, 13),
    ])
    def test_equivalent_to_original_inline(self, budget, price):
        # 与 main.py 原式 max(int(budget/price/100)*100, 100) 逐一比对
        original = max(int(budget / price / 100) * 100, 100)
        assert lot_shares(budget, price) == original


# ── pick_ratio ────────────────────────────────────────────
class TestPickRatio:
    def test_panel_capped_by_single_max(self):
        assert pick_ratio(0.50, 0.20) == 0.20      # 面板 50% 被单笔 20% 护栏夹

    def test_panel_below_cap(self):
        assert pick_ratio(0.15, 0.20) == 0.15

    def test_signal_ratio_capped(self):
        assert pick_ratio(0.20, 0.20, strat_ratio=0.50) == 0.20   # 信号分层仓也夹护栏

    def test_signal_ratio_below_cap(self):
        assert pick_ratio(0.20, 0.20, strat_ratio=0.10) == 0.10

    def test_strat_equal_not_capped(self):
        # 策略声明的平均仓位是策略口径，不被护栏二次夹
        assert pick_ratio(0.20, 0.20, strat_equal=0.33) == 0.33

    def test_signal_ratio_beats_equal(self):
        # 信号分层仓优先于策略平均仓
        assert pick_ratio(0.20, 0.20, strat_ratio=0.10, strat_equal=0.33) == 0.10

    @pytest.mark.parametrize("panel,sp,sr,eq", [
        (0.20, 0.20, 0.0, 0.0), (0.50, 0.20, 0.0, 0.0), (0.10, 0.20, 0.0, 0.0),
        (0.20, 0.20, 0.50, 0.0), (0.20, 0.20, 0.10, 0.0), (0.20, 0.20, 0.0, 0.33),
        (0.20, 0.20, 0.15, 0.33),
    ])
    def test_equivalent_to_original_inline(self, panel, sp, sr, eq):
        # 复刻 main.py 原内联决策（_eff_ratio 在 eq>0 时等于 eq，否则 min(panel,sp)）
        eff = eq if eq > 0 else min(panel, sp)
        if sr > 0:
            expected = min(sr, sp)
        elif eq > 0:
            expected = eff           # 原码此处取 _eff_ratio（==eq）
        else:
            expected = min(eff, sp)  # ==min(panel,sp)
        assert pick_ratio(panel, sp, strat_ratio=sr, strat_equal=eq) == expected


# ── clamp_to_max_order（修正版护栏）────────────────────────
class TestClampToMaxOrder:
    def test_within_limit_unchanged(self):
        assert clamp_to_max_order(400, 50.0, 50000) == (400, False)   # 20000 ≤ 50000

    def test_shrink_when_over(self):
        # 1000 股 × 60 = 60000 > 50000 → 缩到 800 股(48000)
        shares, clamped = clamp_to_max_order(1000, 60.0, 50000)
        assert clamped and shares == 800 and 60.0 * shares <= 50000

    def test_reject_when_one_lot_over(self):
        # 单价 600 × 100 股 = 60000 > 50000 护栏 → 返回 0（原内联 bug 会返回 100 照下单）
        assert clamp_to_max_order(100, 600.0, 50000) == (0, True)

    def test_disabled_when_limit_zero(self):
        assert clamp_to_max_order(10000, 50.0, 0) == (10000, False)
