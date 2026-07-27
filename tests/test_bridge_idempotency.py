import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

from tools.ths_bridge import CloudLocalBridge


class _Operator:
    def __init__(self):
        self.buy_calls = 0

    def buy(self, code, price, shares):
        self.buy_calls += 1
        return {"status": "executed", "msg": "broker accepted", "exec_price": price}


def _bridge(state_path, operator, reports):
    bridge = CloudLocalBridge.__new__(CloudLocalBridge)
    bridge.config = {
        "confirm_mode": "auto",
        "use_mobile_available_cash": False,
        "execution_state_path": str(state_path),
    }
    bridge.execution_state_path = state_path
    bridge.operator = operator
    bridge.is_connected_ths = True
    bridge.report_result = lambda signal_id, status, msg, exec_price=None: reports.append(
        (signal_id, status, msg, exec_price)
    )
    return bridge


def test_bridge_restart_replays_report_without_reexecuting_order(tmp_path):
    state_path = tmp_path / "bridge_execution_state.db"
    operator = _Operator()
    signal = {
        "id": 41,
        "code": "600000",
        "name": "Test",
        "action": "BUY",
        "price": 10.0,
        "shares": 100,
    }

    first_reports = []
    first = _bridge(state_path, operator, first_reports)
    first.execute_signal(signal)

    second_reports = []
    restarted = _bridge(state_path, operator, second_reports)
    result = restarted.execute_signal(signal)

    assert operator.buy_calls == 1
    assert result["status"] == "executed"
    assert second_reports == [(41, "executed", "broker accepted", 10.0)]
