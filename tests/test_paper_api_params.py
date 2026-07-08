import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

import main


def _login(client, uid=77):
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["username"] = f"u{uid}"
        sess["is_admin"] = False


class FakeBroker:
    def __init__(self):
        self.calls = []

    def get_trades(self, limit=100):
        self.calls.append(("trades", limit))
        return {"ok": True, "trades": [], "limit": limit}

    def get_orders(self, limit=100):
        self.calls.append(("orders", limit))
        return {"ok": True, "orders": [], "limit": limit}

    def get_equity_curve(self, days=90):
        self.calls.append(("equity", days))
        return {"ok": True, "points": [], "days": days}


def test_paper_list_endpoints_sanitize_numeric_query_params(monkeypatch):
    broker = FakeBroker()
    monkeypatch.setattr(main.pb, "get_broker", lambda uid: broker)

    client = main.app.test_client()
    _login(client)

    trades = client.get("/api/paper/trades?limit=abc")
    orders = client.get("/api/paper/orders?limit=999999")
    equity = client.get("/api/paper/equity?days=-7")

    assert trades.status_code == 200
    assert orders.status_code == 200
    assert equity.status_code == 200
    assert broker.calls == [
        ("trades", 100),
        ("orders", 500),
        ("equity", 1),
    ]
