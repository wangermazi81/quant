# -*- coding: utf-8 -*-
"""AI 自主交易的风控闸与执行边界（A-5/A-6 的前置安全网）。

为什么先写这个：审查里 A-5 建议把 1130 行的 `_Engine` 拆成
DecisionPipeline / RiskGate / ExecutionService / EngineRuntime。
但那条链路**直接下真单**，而此前只有外围测试（cockpit/config），
`_risk_gate` 与 `_execute` 这两个最要命的边界一条断言都没有。
没有安全网就重构 = 拿「以后好改」赌一次真实资金事故。

这些用例把两个边界的**行为契约**钉死；拆完之后它们必须原样通过。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics import ai_trader


def _engine(monkeypatch, *, positions=None, cash=1_000_000.0, capital=1_000_000.0,
            today_trades=0, degraded=None, mode="paper"):
    eng = ai_trader._Engine(uid=7701)
    acct = {
        "positions": positions if positions is not None else {},
        "cash": cash,
        "capital": capital,
    }
    monkeypatch.setattr(eng, "_account", lambda: dict(acct))
    monkeypatch.setattr(eng, "_today_trades", lambda: today_trades)
    monkeypatch.setattr(eng, "_broker_mode", lambda: mode)
    eng.market_data_status = degraded or {}
    return eng, acct


def _settings(**risk):
    r = {
        "min_confidence": 60,
        "daily_trade_limit": 10,
        "max_positions": 5,
        "per_trade_ratio": 0.2,
    }
    r.update(risk)
    return {"risk": r, "block_buy_on_degraded_data": True}


def _buy(**over):
    d = {"id": "d1", "code": "000001", "name": "平安银行", "action": "BUY",
         "confidence": 80, "price": 10.0, "reason": "test"}
    d.update(over)
    return d


class TestRiskGateBuy:
    def test_allows_normal_buy(self, monkeypatch):
        eng, _ = _engine(monkeypatch)
        ok, why = eng._risk_gate(_buy(), _settings())
        assert ok is True and why == ""

    def test_blocks_low_confidence(self, monkeypatch):
        eng, _ = _engine(monkeypatch)
        ok, why = eng._risk_gate(_buy(confidence=30), _settings())
        assert ok is False and "信心" in why

    def test_blocks_when_daily_limit_reached(self, monkeypatch):
        eng, _ = _engine(monkeypatch, today_trades=10)
        ok, why = eng._risk_gate(_buy(), _settings(daily_trade_limit=10))
        assert ok is False and "上限" in why

    def test_blocks_buy_on_degraded_market_data(self, monkeypatch):
        """★行情降级时必须停止新开仓——否则会拿脏数据下真单。"""
        eng, _ = _engine(monkeypatch, degraded={"degraded": True, "reason": "empty snapshot"})
        ok, why = eng._risk_gate(_buy(), _settings())
        assert ok is False and "degraded" in why

    def test_degraded_block_can_be_disabled(self, monkeypatch):
        eng, _ = _engine(monkeypatch, degraded={"degraded": True, "reason": "x"})
        st = _settings()
        st["block_buy_on_degraded_data"] = False
        assert eng._risk_gate(_buy(), st)[0] is True

    def test_blocks_when_already_holding(self, monkeypatch):
        eng, _ = _engine(monkeypatch, positions={"000001": {"shares": 100}})
        ok, why = eng._risk_gate(_buy(), _settings())
        assert ok is False and "已持有" in why

    def test_blocks_when_max_positions_reached(self, monkeypatch):
        eng, _ = _engine(monkeypatch,
                         positions={f"60000{i}": {"shares": 100} for i in range(5)})
        ok, why = eng._risk_gate(_buy(), _settings(max_positions=5))
        assert ok is False and "持仓已达上限" in why

    @pytest.mark.parametrize("price", [0, -1, None])
    def test_blocks_invalid_price(self, monkeypatch, price):
        """★无有效价必须拒绝：放过去会按 0 价或兜底价下单。"""
        eng, _ = _engine(monkeypatch)
        ok, why = eng._risk_gate(_buy(price=price), _settings())
        assert ok is False and "买入价" in why

    def test_blocks_when_budget_below_one_lot(self, monkeypatch):
        eng, _ = _engine(monkeypatch, capital=1000.0)
        ok, why = eng._risk_gate(_buy(price=50.0), _settings(per_trade_ratio=0.2))
        assert ok is False and "预算不足" in why

    def test_blocks_when_cash_below_one_lot(self, monkeypatch):
        eng, _ = _engine(monkeypatch, cash=500.0)
        ok, why = eng._risk_gate(_buy(price=50.0), _settings())
        assert ok is False and "现金不足" in why

    def test_blocks_without_team_consensus(self, monkeypatch):
        eng, _ = _engine(monkeypatch)
        d = _buy(consensus={"buy_ok": False, "long": 1, "short": 4, "market_cn": "看空"})
        ok, why = eng._risk_gate(d, _settings())
        assert ok is False and "共识" in why


class TestRiskGateSell:
    def test_blocks_sell_without_position(self, monkeypatch):
        eng, _ = _engine(monkeypatch)
        ok, why = eng._risk_gate(_buy(action="SELL"), _settings())
        assert ok is False and "未持有" in why

    def test_allows_sell_with_position(self, monkeypatch):
        eng, _ = _engine(monkeypatch, positions={"000001": {"shares": 100}})
        assert eng._risk_gate(_buy(action="SELL"), _settings())[0] is True

    def test_sell_not_blocked_by_degraded_data(self, monkeypatch):
        """降级只挡新开仓，不能挡卖出——否则行情异常时反而跑不掉。"""
        eng, _ = _engine(monkeypatch,
                         positions={"000001": {"shares": 100}},
                         degraded={"degraded": True, "reason": "x"})
        assert eng._risk_gate(_buy(action="SELL"), _settings())[0] is True


class TestExecuteBoundary:
    def test_paper_buy_goes_to_ai_broker_not_user_account(self, monkeypatch):
        """★模拟盘必须下到 AI 独立账户，绝不能碰用户主账户。"""
        eng, acct = _engine(monkeypatch, mode="paper")
        calls = []

        class _Broker:
            def submit_buy(self, code, name, price, shares, reason=""):
                calls.append(("ai_broker", code, shares))
                return {"ok": True, "exec_price": price}

        monkeypatch.setattr(eng, "_ai_broker", lambda: _Broker())
        monkeypatch.setattr(eng, "_bump_trades", lambda: None)
        monkeypatch.setattr(eng, "_push", lambda *a, **k: None)
        monkeypatch.setattr(eng, "_log", lambda *a, **k: None)
        monkeypatch.setattr(eng, "_audit", lambda *a, **k: None)
        monkeypatch.setattr(eng, "ai_account", lambda: {})

        eng._execute(_buy(), _settings(), acct=acct)
        assert calls and calls[0][0] == "ai_broker"

    def test_live_buy_goes_through_trade_service(self, monkeypatch):
        """实盘必须走 core.trade_service，不得 import main（会拉起整个 Flask）。"""
        eng, acct = _engine(monkeypatch, mode="live")
        calls = []

        import core.trade_service as ts
        monkeypatch.setattr(ts, "execute_buy",
                            lambda uid, code, name, price, shares, reason="":
                            calls.append((uid, code, shares)) or {"ok": True})
        monkeypatch.setattr(eng, "_bump_trades", lambda: None)
        monkeypatch.setattr(eng, "_push", lambda *a, **k: None)
        monkeypatch.setattr(eng, "_log", lambda *a, **k: None)
        monkeypatch.setattr(eng, "_audit", lambda *a, **k: None)
        monkeypatch.setattr(eng, "ai_account", lambda: {})

        eng._execute(_buy(), _settings(), acct=acct)
        assert calls and calls[0][0] == 7701

    def test_buy_shares_respect_per_trade_ratio(self, monkeypatch):
        """未显式给股数时按 capital × per_trade_ratio 计算，且整手。"""
        eng, acct = _engine(monkeypatch, capital=1_000_000.0, mode="paper")
        got = {}

        class _Broker:
            def submit_buy(self, code, name, price, shares, reason=""):
                got["shares"] = shares
                return {"ok": True, "exec_price": price}

        monkeypatch.setattr(eng, "_ai_broker", lambda: _Broker())
        monkeypatch.setattr(eng, "_bump_trades", lambda: None)
        monkeypatch.setattr(eng, "_push", lambda *a, **k: None)
        monkeypatch.setattr(eng, "_log", lambda *a, **k: None)
        monkeypatch.setattr(eng, "_audit", lambda *a, **k: None)
        monkeypatch.setattr(eng, "ai_account", lambda: {})

        eng._execute(_buy(price=10.0), _settings(per_trade_ratio=0.2), acct=acct)
        # 20 万预算 / 10 元 = 2 万股
        assert got["shares"] == 20000
        assert got["shares"] % 100 == 0


class TestConfirmRechecksRisk:
    """★实盘确认路径必须重新过风控。

    从生成待确认到用户点确认之间可能过了很久：现金被别处花掉、这只票已经买过、
    当日交易已达上限……拿一份过期的放行结论去下真单是不可接受的。
    """

    def _prep(self, monkeypatch, eng):
        monkeypatch.setattr(ai_trader, "load_settings", lambda uid: _settings())
        monkeypatch.setattr(eng, "_push", lambda *a, **k: None)
        monkeypatch.setattr(eng, "_log", lambda *a, **k: None)

    def test_confirm_blocked_when_conditions_changed(self, monkeypatch):
        # 待确认生成时没持仓；确认时已经持有了 → 必须拦下
        eng, _ = _engine(monkeypatch, positions={"000001": {"shares": 100}}, mode="live")
        self._prep(monkeypatch, eng)
        executed = []
        monkeypatch.setattr(eng, "_execute", lambda *a, **k: executed.append(a))

        eng.pending["p1"] = _buy()
        r = eng.confirm("p1", approve=True)

        assert r["ok"] is False and "风控" in r["msg"]
        assert executed == [], "风控已拦截，不得下单"

    def test_confirm_executes_when_still_valid(self, monkeypatch):
        eng, _ = _engine(monkeypatch, mode="live")
        self._prep(monkeypatch, eng)
        executed = []
        monkeypatch.setattr(eng, "_execute", lambda *a, **k: executed.append(a))

        eng.pending["p1"] = _buy()
        r = eng.confirm("p1", approve=True)

        assert r["ok"] is True
        assert len(executed) == 1

    def test_reject_never_executes(self, monkeypatch):
        eng, _ = _engine(monkeypatch, mode="live")
        self._prep(monkeypatch, eng)
        executed = []
        monkeypatch.setattr(eng, "_execute", lambda *a, **k: executed.append(a))

        eng.pending["p1"] = _buy()
        r = eng.confirm("p1", approve=False)

        assert r["ok"] is True
        assert executed == []
