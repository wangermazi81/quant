from tests._module_stubs import stub_project_module
import os
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

import main
from broker import paper_broker as pb
from strategy_engine import strategy_manager as sm
from analytics import factor_manager as fm
from app_state import get_user_state
import web.misc as misc_web
import web.paper as paper_web
import web.trade as trade_web


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
    monkeypatch.setattr(pb, "get_broker", lambda uid: broker)

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
    monkeypatch.setattr(pb, "get_broker", lambda uid: broker)

    client = main.app.test_client()
    _login(client)

    response = client.post("/api/paper/reset", json={"initial_capital": "bad"})
    data = response.get_json()

    assert response.status_code == 400
    assert data["ok"] is False
    assert "initial_capital" in data["error"]
    assert not any(call[0] == "reset" for call in broker.calls)


def test_paper_reset_defaults_to_one_million(monkeypatch):
    broker = FakeBroker()
    monkeypatch.setattr(pb, "get_broker", lambda uid: broker)

    client = main.app.test_client()
    _login(client)

    response = client.post("/api/paper/reset", json={})

    assert response.status_code == 200
    assert ("reset", 1_000_000) in broker.calls


def test_paper_reset_ui_prompt_defaults_to_one_million():
    """前端「重置模拟盘」的初始资金默认值必须是 100 万。

    与后端保持一致：config.INITIAL_CAPITAL / paper_broker.DEFAULT_INITIAL_CAPITAL == 1_000_000。

    不锁定具体文件名：这段逻辑被拆过两次（index.html → app-core.js → app-paper.js），
    每拆一次这条断言就假红一次。只要 resetPaper() 还在 static/app/js 下的某个模块里就行。
    正则也容忍单双引号与逗号旁空白，格式化不会误伤。
    """
    js_dir = Path(__file__).resolve().parents[1] / "static" / "app" / "js"
    sources = {p: p.read_text(encoding="utf-8") for p in sorted(js_dir.glob("*.js"))}

    owners = [p for p, s in sources.items() if "function resetPaper" in s]
    assert owners, "static/app/js 下找不到 resetPaper() 定义（前端是不是又拆文件了？）"

    pattern = re.compile(
        r"prompt\(\s*['\"]重置模拟盘——输入初始资金（元）：['\"]\s*,\s*['\"](\d+)['\"]\s*\)"
    )
    defaults = [m.group(1) for p in owners for m in pattern.finditer(sources[p])]
    assert defaults, f"{[p.name for p in owners]} 里没找到重置模拟盘的 prompt 默认值"
    assert set(defaults) == {"1000000"}, (
        f"重置模拟盘默认初始资金应为 1000000（与后端 INITIAL_CAPITAL 一致），实际 {defaults}"
    )


def test_paper_buy_rejects_invalid_price_before_deducting(monkeypatch):
    calls = []
    monkeypatch.setattr(paper_web, "_deduct", lambda *args, **kwargs: calls.append(("deduct", args)) or None)
    monkeypatch.setattr(paper_web, "execute_buy", lambda *args, **kwargs: calls.append(("execute", args)) or {"ok": True})

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


def _stub_realtime_quote(monkeypatch, price):
    """把 web.paper 里 `from analytics import data_gateway as dg` 的实时价打桩。"""
    from analytics import data_gateway as dg
    monkeypatch.setattr(dg, "get_realtime_quote", lambda code: {"price": price})


def test_paper_buy_rejects_price_far_from_server_quote(monkeypatch):
    """★安全回归：不得用伪造的低价买入再按市价卖出刷模拟盘利润。

    此前买入完全信任前端传入的 price，用户可用 API 以 ¥0.01「买入」，
    随后按真实市价卖出，凭空刷出收益，污染收益排行与积分体系。
    """
    calls = []
    _stub_realtime_quote(monkeypatch, 11.0)
    monkeypatch.setattr(paper_web, "_deduct", lambda *a, **k: calls.append(("deduct", a)) or None)
    monkeypatch.setattr(paper_web, "execute_buy", lambda *a, **k: calls.append(("execute", a)) or {"ok": True})

    client = main.app.test_client()
    _login(client)
    response = client.post(
        "/api/paper/buy",
        json={"code": "000001", "name": "Ping An", "price": 0.01, "shares": 100},
    )
    data = response.get_json()

    assert data["ok"] is False
    assert "偏离" in data["msg"]
    assert calls == []          # 既没扣积分也没下单


def test_paper_buy_uses_server_price_not_client_price(monkeypatch):
    """价格在合理区间内时，成交价仍以服务端行情为准（前端价只作参考）"""
    captured = []
    _stub_realtime_quote(monkeypatch, 11.0)
    monkeypatch.setattr(paper_web, "_deduct", lambda *a, **k: None)
    monkeypatch.setattr(
        paper_web, "execute_buy",
        lambda uid, code, name, price, shares, reason="": captured.append(price) or {"ok": True},
    )

    client = main.app.test_client()
    _login(client)
    response = client.post(
        "/api/paper/buy",
        json={"code": "000001", "name": "Ping An", "price": 10.5, "shares": 100},
    )

    assert response.get_json()["ok"] is True
    assert captured == [11.0]   # 用的是服务端价，不是前端传的 10.5


def test_paper_buy_rejects_missing_live_cash_before_deducting(monkeypatch):
    calls = []
    monkeypatch.setattr(paper_web, "DRY_RUN", False)
    monkeypatch.setattr(
        paper_web.um,
        "get_user_broker_config",
        lambda uid: {"broker_type": "live", "config": {"live_cash": 0}},
    )
    monkeypatch.setattr(paper_web, "_deduct", lambda *args, **kwargs: calls.append(("deduct", args)) or None)
    monkeypatch.setattr(paper_web, "execute_buy", lambda *args, **kwargs: calls.append(("execute", args)) or {"ok": True})

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
    stub_project_module(monkeypatch, "ths_quote", fake_quote)
    monkeypatch.setattr(paper_web, "execute_sell", lambda *args, **kwargs: calls.append(("execute", args)) or {"ok": True})

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
    assert get_user_state(78)["queue"] == []


def test_execute_manual_buy_rejects_invalid_price_before_execution(monkeypatch):
    calls = []
    monkeypatch.setattr(trade_web, "execute_buy", lambda *args, **kwargs: calls.append(("execute", args)) or {"ok": True})

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
    _stub_realtime_quote(monkeypatch, None)
    monkeypatch.setattr(trade_web, "DRY_RUN", True)
    monkeypatch.setattr(pb, "get_broker", lambda uid: broker)
    monkeypatch.setattr(
        trade_web,
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
    monkeypatch.setattr(sm, "get_code", lambda filename, **_kwargs: "def handle_bar(ctx):\n    pass\n")
    monkeypatch.setattr(misc_web, "_deduct", lambda *args, **kwargs: calls.append(("deduct", args)) or None)

    fake_backtest = types.SimpleNamespace(
        run_backtest=lambda **kwargs: calls.append(("run_backtest", kwargs)) or {"ok": True},
        start_backtest_async=lambda **kwargs: calls.append(("start_backtest_async", kwargs)) or "task-1",
    )
    stub_project_module(monkeypatch, "backtest", fake_backtest)

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
    stub_project_module(monkeypatch, "portfolio_optimizer", fake_optimizer)

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


def test_factor_evaluate_rejects_invalid_period_before_evaluator(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fm,
        "evaluate",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
        raising=False,
    )

    monkeypatch.setattr("app_state.is_root_admin_session", lambda: True)
    client = main.app.test_client()
    _login(client, uid=83)

    response = client.post(
        "/api/factors/evaluate",
        json={"filename": "factor.py", "codes": ["000001"], "period": "bad"},
    )
    data = response.get_json()

    assert response.status_code == 400
    assert data["ok"] is False
    assert "period" in data["error"]
    assert calls == []
