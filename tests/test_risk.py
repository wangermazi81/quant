# -*- coding: utf-8 -*-
"""
tests/test_risk.py
风控规则与 RiskManager 单元测试（财务安全关键路径，纯逻辑）。

运行：
  cd ths_auto_trader
  python -m pytest tests/test_risk.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

from risk.manager import RiskManager, RiskContext, RiskAction
from risk.limits import MaxPositions, MaxDrawdown, BlacklistFilter, SinglePositionLimit
from risk.stops import StopLoss, StopProfit, TrailingStop, MaxHoldDays


# ── RiskAction 工厂 ───────────────────────────────────────
class TestRiskAction:
    def test_ok(self):
        a = RiskAction.ok()
        assert not a.should_sell and not a.should_block_buy

    def test_sell(self):
        a = RiskAction.sell("止损", "SL")
        assert a.should_sell and a.reason == "止损" and a.rule_name == "SL"

    def test_block(self):
        a = RiskAction.block("满仓")
        assert a.should_block_buy and not a.should_sell


# ── 拒买类规则 ────────────────────────────────────────────
class TestMaxPositions:
    def test_block_at_limit(self):
        assert MaxPositions(5).evaluate(RiskContext(positions_count=5)).should_block_buy
        assert MaxPositions(5).evaluate(RiskContext(positions_count=6)).should_block_buy

    def test_ok_below(self):
        assert not MaxPositions(5).evaluate(RiskContext(positions_count=4)).should_block_buy

    def test_ok_when_unknown(self):
        assert not MaxPositions(5).evaluate(RiskContext()).should_block_buy

    def test_invalid_n(self):
        with pytest.raises(ValueError):
            MaxPositions(0)


class TestMaxDrawdown:
    def test_block_at_limit(self):
        # 初始 100k，现 79k → 回撤 21% ≥ 20%
        a = MaxDrawdown(0.20).evaluate(RiskContext(portfolio_value=79000, initial_capital=100000))
        assert a.should_block_buy

    def test_ok_below(self):
        a = MaxDrawdown(0.20).evaluate(RiskContext(portfolio_value=85000, initial_capital=100000))
        assert not a.should_block_buy

    def test_ok_when_missing(self):
        assert not MaxDrawdown(0.20).evaluate(RiskContext()).should_block_buy

    def test_invalid_pct(self):
        with pytest.raises(ValueError):
            MaxDrawdown(0)
        with pytest.raises(ValueError):
            MaxDrawdown(1.0)


class TestBlacklist:
    def test_block(self):
        ctx = RiskContext(code="000001", blacklist=["000001", "600519"])
        assert BlacklistFilter().evaluate(ctx).should_block_buy

    def test_ok(self):
        ctx = RiskContext(code="000002", blacklist=["000001"])
        assert not BlacklistFilter().evaluate(ctx).should_block_buy


class TestSinglePositionLimit:
    def test_block_over(self):
        # 1000 股 * 50 = 50000，占 100000 的 50% > 30%
        ctx = RiskContext(qty=1000, current_price=50.0, portfolio_value=100000)
        assert SinglePositionLimit(0.30).evaluate(ctx).should_block_buy

    def test_ok_at_or_below(self):
        ctx = RiskContext(qty=500, current_price=50.0, portfolio_value=100000)  # 25%
        assert not SinglePositionLimit(0.30).evaluate(ctx).should_block_buy

    def test_ok_when_missing(self):
        assert not SinglePositionLimit(0.30).evaluate(RiskContext(qty=100)).should_block_buy

    def test_invalid(self):
        with pytest.raises(ValueError):
            SinglePositionLimit(0)
        with pytest.raises(ValueError):
            SinglePositionLimit(1.5)


# ── 卖出类规则 ────────────────────────────────────────────
class TestStopLoss:
    def test_sell_on_loss(self):
        # 成本 10，现 9.4 → 亏 6% ≥ 5%
        a = StopLoss(0.05).evaluate(RiskContext(entry_price=10.0, current_price=9.4))
        assert a.should_sell

    def test_hold_small_loss(self):
        a = StopLoss(0.05).evaluate(RiskContext(entry_price=10.0, current_price=9.7))  # -3%
        assert not a.should_sell

    def test_ok_when_missing(self):
        assert not StopLoss(0.05).evaluate(RiskContext(current_price=9.0)).should_sell


class TestStopProfit:
    def test_sell_on_gain(self):
        a = StopProfit(0.10).evaluate(RiskContext(entry_price=10.0, current_price=11.5))  # +15%
        assert a.should_sell

    def test_hold_small_gain(self):
        a = StopProfit(0.10).evaluate(RiskContext(entry_price=10.0, current_price=10.5))  # +5%
        assert not a.should_sell


class TestTrailingStop:
    def test_sell_on_drawdown(self):
        # 峰值 12，现 11.0 → 回撤 8.3% ≥ 7%
        a = TrailingStop(0.07).evaluate(RiskContext(highest_since_entry=12.0, current_price=11.0))
        assert a.should_sell

    def test_hold_near_peak(self):
        a = TrailingStop(0.07).evaluate(RiskContext(highest_since_entry=12.0, current_price=11.6))  # -3.3%
        assert not a.should_sell


class TestMaxHoldDays:
    def test_sell_when_expired(self):
        a = MaxHoldDays(10).evaluate(RiskContext(entry_date="2026-06-01", today="2026-06-13"))  # 12 天
        assert a.should_sell

    def test_hold_within(self):
        a = MaxHoldDays(10).evaluate(RiskContext(entry_date="2026-06-10", today="2026-06-13"))  # 3 天
        assert not a.should_sell

    def test_ok_when_missing(self):
        assert not MaxHoldDays(10).evaluate(RiskContext(today="2026-06-13")).should_sell


# ── RiskManager 串联与优先级 ───────────────────────────────
class TestRiskManager:
    def test_empty_ok(self):
        assert not RiskManager().evaluate(RiskContext()).should_sell

    def test_first_block_returned(self):
        mgr = RiskManager([MaxPositions(3), MaxDrawdown(0.20)])
        a = mgr.evaluate(RiskContext(positions_count=5, portfolio_value=100000, initial_capital=100000))
        assert a.should_block_buy and "MaxPositions" in a.rule_name

    def test_sell_prioritized_over_block(self):
        # 同时满足"满仓拒买"和"止损卖出" → 应优先返回卖出
        mgr = RiskManager([MaxPositions(1), StopLoss(0.05)])
        ctx = RiskContext(positions_count=5, entry_price=10.0, current_price=9.0)
        a = mgr.evaluate(ctx)
        assert a.should_sell and not a.should_block_buy

    def test_none_triggered(self):
        mgr = RiskManager([MaxPositions(5), StopLoss(0.05)])
        ctx = RiskContext(positions_count=2, entry_price=10.0, current_price=9.9)
        a = mgr.evaluate(ctx)
        assert not a.should_sell and not a.should_block_buy

    def test_add_chaining_and_len(self):
        mgr = RiskManager().add(MaxPositions(5)).add(StopLoss(0.05))
        assert len(mgr) == 2
