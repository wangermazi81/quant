import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

from app_state import get_user_state
from core import trade_service
from tools.ths_bridge import CloudLocalBridge


def test_live_execute_buy_clamps_signal_shares_before_bridge_queue(monkeypatch):
    uid = 91001
    state = get_user_state(uid)
    state["position_ratio"] = 0.50
    state["single_position_max_ratio"] = 0.10
    captured = {}

    monkeypatch.setattr(trade_service, "DRY_RUN", False)
    monkeypatch.setattr(
        trade_service.um,
        "get_user_broker_config",
        lambda user_id: {"broker_type": "live", "config": {"live_cash": 20000}},
    )
    monkeypatch.setattr(trade_service, "MAX_ORDER_AMOUNT", 50000, raising=False)
    monkeypatch.setattr(
        trade_service.um,
        "add_trade_signal",
        lambda user_id, code, name, action, price, shares: (captured.setdefault("args", (user_id, code, name, action, price, shares)), 123)[1],
    )

    result = trade_service.execute_buy(uid, "600519", "贵州茅台", 10.0, 15600, reason="测试")

    assert result["ok"] is True
    assert captured["args"][5] == 200


def test_live_execute_buy_rejects_signal_when_budget_below_one_lot(monkeypatch):
    uid = 91002
    state = get_user_state(uid)
    state["position_ratio"] = 0.01
    state["single_position_max_ratio"] = 0.01
    calls = []

    monkeypatch.setattr(trade_service, "DRY_RUN", False)
    monkeypatch.setattr(
        trade_service.um,
        "get_user_broker_config",
        lambda user_id: {"broker_type": "live", "config": {"live_cash": 5000}},
    )
    monkeypatch.setattr(
        trade_service.um,
        "add_trade_signal",
        lambda *args: calls.append(args) or 123,
    )

    result = trade_service.execute_buy(uid, "600519", "贵州茅台", 10.0, 100, reason="测试")

    assert result["ok"] is False
    assert calls == []


def test_live_execute_buy_reports_duplicate_signal(monkeypatch):
    uid = 91003
    state = get_user_state(uid)
    state["position_ratio"] = 0.50
    state["single_position_max_ratio"] = 0.10

    monkeypatch.setattr(trade_service, "DRY_RUN", False)
    monkeypatch.setattr(
        trade_service.um,
        "get_user_broker_config",
        lambda user_id: {"broker_type": "live", "config": {"live_cash": 20000}},
    )
    monkeypatch.setattr(trade_service.um, "add_trade_signal", lambda *args: -1)

    result = trade_service.execute_buy(uid, "600519", "Test", 10.0, 100, reason="test")

    assert result == {"ok": False, "msg": "\u4eca\u65e5\u5df2\u6709\u540c\u65b9\u5411\u672a\u5b8c\u6210\u4fe1\u53f7"}


class FakeMobileAccountOperator:
    def get_account(self):
        return {"ok": True, "available": 3200}


def test_bridge_clamps_buy_signal_by_mobile_available_cash():
    bridge = CloudLocalBridge.__new__(CloudLocalBridge)
    bridge.config = {"use_mobile_available_cash": True, "max_order_amount": 50000}
    bridge.operator = FakeMobileAccountOperator()

    shares, msg = bridge._clamp_buy_shares_by_mobile_cash(10.0, 1000)

    assert shares == 300
    assert "手机资金夹仓" in msg


class FakeMobileMissingAvailableOperator:
    def get_account(self):
        return {"ok": True, "available": None, "available_missing": True}


def test_bridge_uses_amount_guard_when_mobile_available_cash_missing():
    bridge = CloudLocalBridge.__new__(CloudLocalBridge)
    bridge.config = {"use_mobile_available_cash": True, "max_order_amount": 5000}
    bridge.operator = FakeMobileMissingAvailableOperator()

    shares, msg = bridge._clamp_buy_shares_by_mobile_cash(10.0, 1000)

    assert shares == 500
    assert "单笔金额护栏" in msg
