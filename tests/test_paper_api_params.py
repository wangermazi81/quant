import os
import sys
import types
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
        self.positions = []

    def get_trades(self, limit=100):
        self.calls.append(("trades", limit))
        return {"ok": True, "trades": [], "limit": limit}

    def get_orders(self, limit=100):
        self.calls.append(("orders", limit))
        return {"ok": True, "orders": [], "limit": limit}

    def get_equity_curve(self, days=90):
        self.calls.append(("equity", days))
        return {"ok": True, "points": [], "days": days}

    def reset(self, initial_capital=None):
        self.calls.append(("reset", initial_capital))
        return {"ok": True, "initial_capital": initial_capital}

    def get_positions(self):
        self.calls.append(("positions",))
        return self.positions


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


def test_paper_reset_rejects_invalid_initial_capital(monkeypatch):
    broker = FakeBroker()
    monkeypatch.setattr(main.pb, "get_broker", lambda uid: broker)

    client = main.app.test_client()
    _login(client)

    response = client.post("/api/paper/reset", json={"initial_capital": "bad"})
    data = response.get_json()

    assert response.status_code == 400
    assert data["ok"] is False
    assert "initial_capital" in data["error"]
    assert not any(call[0] == "reset" for call in broker.calls)


def test_paper_buy_rejects_invalid_price_before_deducting(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_deduct", lambda *args, **kwargs: calls.append(("deduct", args)) or None)
    monkeypatch.setattr(main, "execute_buy", lambda *args, **kwargs: calls.append(("execute", args)) or {"ok": True})

    client = main.app.test_client()
    _login(client)

    response = client.post(
        "/api/paper/buy",
        json={"code": "000001", "name": "Ping An", "price": "bad", "shares": 100},
    )
    data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is False
    assert calls == []


def test_paper_buy_rejects_missing_live_cash_before_deducting(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "DRY_RUN", False)
    monkeypatch.setattr(
        main.um,
        "get_user_broker_config",
        lambda uid: {"broker_type": "live", "config": {"live_cash": 0}},
    )
    monkeypatch.setattr(main, "_deduct", lambda *args, **kwargs: calls.append(("deduct", args)) or None)
    monkeypatch.setattr(main, "execute_buy", lambda *args, **kwargs: calls.append(("execute", args)) or {"ok": True})

    client = main.app.test_client()
    _login(client)

    response = client.post(
        "/api/paper/buy",
        json={"code": "000001", "name": "Ping An", "price": 10.0, "shares": 0},
    )
    data = response.get_json()

    assert response.status_code == 400
    assert data["ok"] is False
    assert data["need_live_cash"] is True
    assert calls == []


def test_paper_sell_rejects_invalid_fallback_price(monkeypatch):
    calls = []
    fake_quote = types.SimpleNamespace(get_client=lambda: None)
    monkeypatch.setitem(sys.modules, "ths_quote", fake_quote)
    monkeypatch.setattr(main, "execute_sell", lambda *args, **kwargs: calls.append(("execute", args)) or {"ok": True})

    client = main.app.test_client()
    _login(client)

    response = client.post("/api/paper/sell", json={"code": "000001", "price": "bad"})
    data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is False
    assert calls == []


def test_queue_add_rejects_invalid_price(monkeypatch):
    client = main.app.test_client()
    _login(client, uid=78)

    response = client.post("/api/queue/add", json={"code": "000001", "price": "bad"})
    data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is False
    assert main.get_user_state(78)["queue"] == []


def test_execute_manual_buy_rejects_invalid_price_before_execution(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "execute_buy", lambda *args, **kwargs: calls.append(("execute", args)) or {"ok": True})

    client = main.app.test_client()
    _login(client, uid=79)

    response = client.post("/api/execute", json={"action": "buy", "code": "000001", "price": "bad"})
    data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is False
    assert calls == []


def test_positions_sell_falls_back_to_broker_price_when_request_price_invalid(monkeypatch):
    broker = FakeBroker()
    broker.positions = [{"code": "000001", "cur_price": 11.2, "cost_price": 10.5}]
    calls = []
    fake_quote = types.SimpleNamespace(get_client=lambda: None)
    monkeypatch.setitem(sys.modules, "ths_quote", fake_quote)
    monkeypatch.setattr(main, "DRY_RUN", True)
    monkeypatch.setattr(main.pb, "get_broker", lambda uid: broker)
    monkeypatch.setattr(
        main,
        "execute_sell",
        lambda uid, code, price, sell_reason="": calls.append((uid, code, price, sell_reason)) or {"ok": True},
    )

    client = main.app.test_client()
    _login(client, uid=80)

    response = client.post("/api/positions/sell", json={"code": "000001", "price": "bad"})
    data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert calls == [(80, "000001", 11.2, "手动卖出")]


def test_backtest_rejects_invalid_numeric_params_before_deducting(monkeypatch):
    calls = []
    monkeypatch.setattr(main.sm, "get_code", lambda filename: "def handle_bar(ctx):\n    pass\n")
    monkeypatch.setattr(main, "_deduct", lambda *args, **kwargs: calls.append(("deduct", args)) or None)

    fake_backtest = types.SimpleNamespace(
        run_backtest=lambda **kwargs: calls.append(("run_backtest", kwargs)) or {"ok": True},
        start_backtest_async=lambda **kwargs: calls.append(("start_backtest_async", kwargs)) or "task-1",
    )
    monkeypatch.setitem(sys.modules, "backtest", fake_backtest)

    client = main.app.test_client()
    _login(client, uid=81)

    response = client.post(
        "/api/backtest",
        json={
            "filename": "strategy.py",
            "codes": ["000001"],
            "capital": "bad",
            "max_positions": 5,
            "stop_loss": -0.05,
            "take_profit": 0.1,
        },
    )
    data = response.get_json()

    assert response.status_code == 400
    assert data["ok"] is False
    assert "capital" in data["error"]
    assert calls == []


def test_portfolio_optimize_rejects_invalid_capital_before_optimizer(monkeypatch):
    calls = []
    fake_optimizer = types.SimpleNamespace(
        optimize_portfolio=lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True}
    )
    monkeypatch.setitem(sys.modules, "portfolio_optimizer", fake_optimizer)

    client = main.app.test_client()
    _login(client, uid=82)

    response = client.post(
        "/api/portfolio/optimize",
        json={"codes": ["000001", "000002"], "mode": "equal_weight", "capital": "bad"},
    )
    data = response.get_json()

    assert response.status_code == 400
    assert data["ok"] is False
    assert "capital" in data["error"]
    assert calls == []
