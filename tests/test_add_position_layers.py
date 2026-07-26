# -*- coding: utf-8 -*-
"""加仓追板（_check_add_position）的层数持久性与护栏测试。

这条链路此前完全没有测试，而它是**自动追加真金白银仓位**的逻辑：
层数只存在进程内存的 _ADD_POSITION_LAYERS 里，服务一重启就归零，
若持仓涨幅仍 ≥9%，会对同一只票再生成一笔约 17% 仓位的追板买入。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import trade_service
from core import user_manager as um


def _quote(code="000001", price=10.9, last_close=10.0, limit_up=11.0):
    return {"code": code, "price": price, "last_close": last_close, "limit_up": limit_up}


def _stub_quotes(monkeypatch, rows):
    """把 _check_add_position 内部的行情客户端打桩。"""
    import types

    class _Client:
        _ok = True

        def get_quotes(self, codes):
            return rows

    fake_tq = types.SimpleNamespace(get_client=lambda: _Client())
    monkeypatch.setitem(sys.modules, "market.ths_quote", fake_tq)
    import market
    monkeypatch.setattr(market, "ths_quote", fake_tq, raising=False)


def test_paper_mode_skips_add_position(monkeypatch):
    """模拟盘 submit_buy 对已有持仓一律拒绝，加仓必须在入口就跳过而不是每轮去撞拒绝。"""
    calls = []
    monkeypatch.setattr(trade_service, "DRY_RUN", False)
    monkeypatch.setattr(trade_service, "execute_buy",
                        lambda *a, **k: calls.append(a) or {"ok": True})

    state = {"positions": {"000001": {"name": "平安银行", "shares": 100}}}
    trade_service._check_add_position(9001, state, "paper", 1_000_000)
    assert calls == []


def test_layers_restored_from_ledger_after_restart(monkeypatch):
    """★核心回归：重启导致内存层数丢失时，必须从当日买入信号台账还原。

    台账里已有 2 条当日买入信号（建仓 + 已加过一次）→ 不得再加仓。
    """
    trade_service._ADD_POSITION_LAYERS.clear()      # 模拟进程重启后内存归零
    calls = []
    monkeypatch.setattr(trade_service, "DRY_RUN", False)
    monkeypatch.setattr(trade_service, "execute_buy",
                        lambda *a, **k: calls.append(a) or {"ok": True})
    monkeypatch.setattr(um, "count_today_buy_signals", lambda uid, code: 2)
    _stub_quotes(monkeypatch, [_quote()])

    state = {"positions": {"000001": {"name": "平安银行", "shares": 100}}}
    trade_service._check_add_position(9002, state, "live", 1_000_000)

    assert calls == [], "台账显示今天已加过仓，重启后不得重复加仓"
    assert trade_service._ADD_POSITION_LAYERS[(9002, "000001")] == 3.0


def test_first_add_allowed_when_ledger_shows_only_open(monkeypatch):
    """台账只有 1 条买入信号（仅建仓）→ 允许加一次，且层数被记为 3.0"""
    trade_service._ADD_POSITION_LAYERS.clear()
    calls = []
    monkeypatch.setattr(trade_service, "DRY_RUN", False)
    monkeypatch.setattr(trade_service, "execute_buy",
                        lambda *a, **k: calls.append(a) or {"ok": True})
    monkeypatch.setattr(um, "count_today_buy_signals", lambda uid, code: 1)
    _stub_quotes(monkeypatch, [_quote()])

    state = {"positions": {"000001": {"name": "平安银行", "shares": 100}}}
    trade_service._check_add_position(9003, state, "live", 1_000_000)

    assert len(calls) == 1, "仅建仓过的持仓涨到 9% 应触发一次加仓"
    uid, code, _name, price, shares = calls[0][:5]
    assert (uid, code, price) == (9003, "000001", 11.0)   # 以涨停价挂单
    assert shares % 100 == 0 and shares > 0
    assert trade_service._ADD_POSITION_LAYERS[(9003, "000001")] == 3.0


def test_ledger_query_failure_is_conservative(monkeypatch):
    """查台账失败时必须保守视为「已加过」——宁可不加，也不能重复下单。"""
    trade_service._ADD_POSITION_LAYERS.clear()
    calls = []
    monkeypatch.setattr(trade_service, "DRY_RUN", False)
    monkeypatch.setattr(trade_service, "execute_buy",
                        lambda *a, **k: calls.append(a) or {"ok": True})

    def _boom(uid, code):
        raise RuntimeError("db locked")

    monkeypatch.setattr(um, "count_today_buy_signals", _boom)
    _stub_quotes(monkeypatch, [_quote()])

    state = {"positions": {"000001": {"name": "平安银行", "shares": 100}}}
    trade_service._check_add_position(9004, state, "live", 1_000_000)
    assert calls == []


def test_budget_below_one_lot_does_not_force_a_lot(monkeypatch):
    """预算不足 1 手时跳过，不得用 max(...,100) 强行凑一手绕过仓位预算。"""
    trade_service._ADD_POSITION_LAYERS.clear()
    calls = []
    monkeypatch.setattr(trade_service, "DRY_RUN", False)
    monkeypatch.setattr(trade_service, "execute_buy",
                        lambda *a, **k: calls.append(a) or {"ok": True})
    monkeypatch.setattr(um, "count_today_buy_signals", lambda uid, code: 1)
    _stub_quotes(monkeypatch, [_quote()])

    state = {"positions": {"000001": {"name": "平安银行", "shares": 100}}}
    # base_cash 极小 → 加仓预算 = 1000 * 0.17 = 170 元 < 1 手（11.0 × 100 = 1100）
    trade_service._check_add_position(9005, state, "live", 1_000)
    assert calls == []
