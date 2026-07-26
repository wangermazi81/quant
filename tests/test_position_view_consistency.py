# -*- coding: utf-8 -*-
"""内存持仓视图与落库事实的一致性（C-9 / C-11）。

三个历史缺陷：
1. execute_buy 把**请求**股数写进内存视图，而 submit_buy 会向下取整到整手 ——
   手动买 150 股实际成交 100 股，面板却按 150 股显示盈亏（虚增 50%）。
2. execute_sell 实盘分支只看内存持仓；服务重启后内存只从模拟盘 DB 恢复，
   实盘持仓不在里面 → 面板点卖出一律「无持仓」，急跌时人工止损通道被堵死。
3. /api/positions/close 只删内存，DB 仓位仍在 → 风控照样自动卖出、重启后复活。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import trade_service
from core import user_manager as um
from broker import paper_broker as pb
from app_state import get_user_state


class _FakeBroker:
    def __init__(self, filled_shares):
        self._filled = filled_shares

    def submit_buy(self, code, name, price, shares, reason="", strategy=""):
        # 真实 PaperBroker 会把 shares 向下取整到整手后回传实际成交数
        return {"ok": True, "exec_price": 10.0, "shares": self._filled,
                "total_cost": 10.0 * self._filled, "commission": 5.0}


def test_memory_position_records_filled_shares_not_requested(monkeypatch):
    """★买 150 股实际成交 100 股时，内存视图必须记 100。"""
    uid = 8801
    monkeypatch.setattr(trade_service, "DRY_RUN", True)
    monkeypatch.setattr(pb, "get_broker", lambda u: _FakeBroker(filled_shares=100))
    monkeypatch.setattr(um, "get_user_broker_config", lambda u: {"broker_type": "paper"})
    monkeypatch.setattr(trade_service, "_persist_state", lambda *a, **k: None)

    r = trade_service.execute_buy(uid, "000001", "平安银行", 10.0, 150, reason="测试")
    assert r["ok"] is True

    pos = get_user_state(uid)["positions"]["000001"]
    assert pos["shares"] == 100, "内存视图记的应是实际成交股数，不是请求股数"


def test_live_sell_falls_back_to_ledger_when_memory_empty(monkeypatch):
    """★重启后内存无实盘持仓时，卖出必须以 live_positions 台账为准。"""
    uid = 8802
    captured = {}
    monkeypatch.setattr(trade_service, "DRY_RUN", False)
    monkeypatch.setattr(um, "get_user_broker_config", lambda u: {"broker_type": "live"})
    monkeypatch.setattr(um, "get_live_positions",
                        lambda u: [{"code": "600519", "name": "贵州茅台", "shares": 300}])

    def _fake_signal(u, code, name, action, price, shares):
        captured.update(dict(uid=u, code=code, name=name, action=action, shares=shares))
        return 4321

    monkeypatch.setattr(um, "add_trade_signal", _fake_signal)

    s = get_user_state(uid)
    s["positions"].pop("600519", None)          # 模拟重启后内存里没有实盘持仓

    r = trade_service.execute_sell(uid, "600519", 1700.0)
    assert r["ok"] is True
    assert captured["shares"] == 300, "应使用台账里的持仓数量"
    assert captured["action"] == "SELL"


def test_live_sell_still_rejects_when_no_position_anywhere(monkeypatch):
    """内存和台账都没有 → 仍然明确拒绝，不能凭空发卖单。"""
    uid = 8803
    monkeypatch.setattr(trade_service, "DRY_RUN", False)
    monkeypatch.setattr(um, "get_user_broker_config", lambda u: {"broker_type": "live"})
    monkeypatch.setattr(um, "get_live_positions", lambda u: [])

    s = get_user_state(uid)
    s["positions"].pop("600519", None)

    r = trade_service.execute_sell(uid, "600519", 1700.0)
    assert r["ok"] is False
    assert "无持仓" in r["msg"]


def test_ledger_query_failure_does_not_fabricate_a_sell(monkeypatch):
    """台账查询失败时不得凭空造出卖单。"""
    uid = 8804
    monkeypatch.setattr(trade_service, "DRY_RUN", False)
    monkeypatch.setattr(um, "get_user_broker_config", lambda u: {"broker_type": "live"})

    def _boom(u):
        raise RuntimeError("db locked")

    monkeypatch.setattr(um, "get_live_positions", _boom)

    s = get_user_state(uid)
    s["positions"].pop("600519", None)

    r = trade_service.execute_sell(uid, "600519", 1700.0)
    assert r["ok"] is False


@pytest.fixture
def logged_client(monkeypatch):
    import main
    client = main.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 8805
        sess["username"] = "u8805"
        sess["is_admin"] = False
    return client


def test_close_position_refuses_when_ledger_still_holds(logged_client, monkeypatch):
    """★账本里还持有时，不许只从视图移除——那会造出幽灵持仓。"""
    import web.trade as trade_web

    uid = 8805
    get_user_state(uid)["positions"]["000001"] = {"name": "平安银行", "shares": 100, "cost": 10.0}

    monkeypatch.setattr(trade_web, "DRY_RUN", True)
    monkeypatch.setattr(trade_web.um, "get_user_broker_config", lambda u: {"broker_type": "paper"})
    monkeypatch.setattr(trade_web.pb, "get_broker",
                        lambda u: type("B", (), {"get_positions": lambda self: [{"code": "000001"}]})())

    resp = logged_client.post("/api/positions/close", json={"code": "000001"})
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False
    assert "000001" in get_user_state(uid)["positions"], "拒绝后不得把内存视图删掉"


def test_close_position_clears_stale_view_entry(logged_client, monkeypatch):
    """账本里已经没有了 → 允许清理这条残留视图。"""
    import web.trade as trade_web

    uid = 8805
    get_user_state(uid)["positions"]["000002"] = {"name": "万科A", "shares": 100, "cost": 10.0}

    monkeypatch.setattr(trade_web, "DRY_RUN", True)
    monkeypatch.setattr(trade_web.um, "get_user_broker_config", lambda u: {"broker_type": "paper"})
    monkeypatch.setattr(trade_web.pb, "get_broker",
                        lambda u: type("B", (), {"get_positions": lambda self: []})())

    resp = logged_client.post("/api/positions/close", json={"code": "000002"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert "000002" not in get_user_state(uid)["positions"]
