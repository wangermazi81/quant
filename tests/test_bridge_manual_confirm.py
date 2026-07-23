import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

from tools.ths_bridge import CloudLocalBridge


def test_bridge_exits_after_manual_confirm_submission():
    bridge = CloudLocalBridge.__new__(CloudLocalBridge)
    bridge.poll_interval = 0
    bridge.manual_confirm_pending = False
    fetch_calls = []

    bridge.init_ths = lambda: None
    bridge.login = lambda: True

    def fake_fetch_signals():
        fetch_calls.append(1)
        return [{"id": 1, "code": "600000", "action": "BUY", "price": 10.0, "shares": 100}]

    bridge.fetch_signals = fake_fetch_signals
    bridge.execute_signal = lambda sig: {"status": "submitted", "manual_confirm": True}

    bridge.run()

    assert bridge.manual_confirm_pending is True
    assert len(fetch_calls) == 1
