import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"
os.environ.setdefault("ADMIN_PASSWORD", "adminpass123")


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    from core import db_migration as dbm
    from core import user_manager as um
    import app_security as sec

    db_path = str(tmp_path / "users_test.db")
    monkeypatch.setattr(um, "DB_PATH", db_path)
    monkeypatch.setattr(dbm, "USERS_DB", db_path)
    monkeypatch.setattr(sec, "SECURITY_DB", db_path)
    monkeypatch.setattr(sec, "_security_tables_ready", False)
    sec._ip_blocks.clear()
    sec._suspicious_hits.clear()
    um.init_db()
    dbm.migrate_users_db()

    import main

    main.app.config.update(TESTING=True)
    return main.app, um, main


def _create_user(um, username, is_admin=0):
    with um._conn() as c:
        um._create_user(
            c,
            username,
            f"{username}@example.com",
            "pass123",
            is_admin=is_admin,
            points=100,
        )
        return c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]


def _login_session(client, uid, username, is_admin=False):
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["username"] = username
        sess["is_admin"] = is_admin


def _assert_root_admin_required(response):
    assert response.status_code == 403
    assert response.is_json
    assert response.get_json()["code"] == "root_admin_required"


def _assert_indistinguishable_not_found(foreign_response, unknown_response):
    """越权访问他人资源，必须与访问「根本不存在的资源」完全无法区分。

    这里断言的是**性质**而不是某个固定状态码：只要两个响应逐字节一致、且都是
    拒绝，就没有泄漏「该 id 是否存在」。不同路由的拒绝层次不同——例如
    /api/bridge/signals/<id>/report 挂的是 @require_bridge_token，会话用户在
    走到属主校验之前就被 403 挡下，两种情况同样返回 403，性质依然满足。
    （此前写死 404，装饰器换成 bridge token 后测试就一直是红的。）
    """
    assert foreign_response.is_json
    assert unknown_response.is_json
    assert foreign_response.status_code in (403, 404)
    # 状态码与响应体都必须一致，否则可据此判断资源是否存在
    assert foreign_response.status_code == unknown_response.status_code
    assert foreign_response.get_json() == unknown_response.get_json()
    assert foreign_response.get_json()["ok"] is False


def _assert_response_hides_owner_data(response):
    body = response.get_data(as_text=True)
    assert "owner-only" not in body
    assert "completed-owner-result" not in body


_FACTOR_ROUTE_REQUESTS = [
    pytest.param("GET", "/api/factors", id="factors-list"),
    pytest.param("GET", "/api/factors/template", id="factors-template"),
    pytest.param("POST", "/api/factors/run", id="factors-run"),
    pytest.param("POST", "/api/factors/save", id="factors-save"),
    pytest.param("POST", "/api/factors/delete", id="factors-delete"),
    pytest.param("POST", "/api/factors/ai_generate", id="factors-ai-generate"),
    pytest.param("GET", "/api/factors/private_factor.py", id="factors-get"),
    pytest.param("POST", "/api/factors/evaluate", id="factors-evaluate"),
    pytest.param("GET", "/api/factors_zoo/list", id="factors-zoo-list"),
    pytest.param("POST", "/api/factors_zoo/compute", id="factors-zoo-compute"),
    pytest.param("POST", "/api/factors_zoo/compute_async", id="factors-zoo-compute-async"),
    pytest.param("GET", "/api/factors_zoo/progress/not-a-real-task", id="factors-zoo-progress"),
    pytest.param("POST", "/api/factors_zoo/backtest", id="factors-zoo-backtest"),
    pytest.param("GET", "/api/factors_zoo/result/not-a-real-task", id="factors-zoo-result"),
]


@pytest.mark.parametrize(("method", "path"), _FACTOR_ROUTE_REQUESTS)
def test_every_factor_route_requires_root_admin_before_compute(
    isolated_app, monkeypatch, method, path
):
    app, um, _main = isolated_app
    user_id = _create_user(um, "factor_user")
    client = app.test_client()
    _login_session(client, user_id, "factor_user")

    from analytics.factors_zoo import backtest as zoo_backtest
    from web import factors

    compute_calls = []
    monkeypatch.setattr(
        factors.fm,
        "run",
        lambda *args, **kwargs: compute_calls.append(("factor_run", args, kwargs)),
    )
    monkeypatch.setattr(
        zoo_backtest,
        "backtest_factor",
        lambda *args, **kwargs: compute_calls.append(("zoo_backtest", args, kwargs)),
    )

    response = client.open(path, method=method, json={})

    _assert_root_admin_required(response)
    assert compute_calls == []


def test_root_admin_can_list_factors(isolated_app, monkeypatch):
    app, _um, _main = isolated_app
    client = app.test_client()
    _login_session(client, 1, "admin", is_admin=True)

    from web import factors

    expected = {"ok": True, "factors": ["root-visible-factor"]}
    monkeypatch.setattr(factors.fm, "list_factors", lambda: expected)

    response = client.get("/api/factors")

    assert response.status_code == 200
    assert response.get_json() == expected


def test_strategy_async_creation_uses_full_uuid4_hex_task_id(isolated_app, monkeypatch):
    app, um, _main = isolated_app
    user_id = _create_user(um, "strategy_creator")
    client = app.test_client()
    _login_session(client, user_id, "strategy_creator")

    from web import strategies

    class NonStartingThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            self.daemon = daemon

        def start(self):
            return None

    runner_calls = []
    monkeypatch.setattr(strategies, "_deduct", lambda *args, **kwargs: None)
    monkeypatch.setattr(strategies, "_add_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(strategies.threading, "Thread", NonStartingThread)
    monkeypatch.setattr(
        strategies.sm,
        "run",
        lambda *args, **kwargs: runner_calls.append((args, kwargs)),
    )

    response = client.post(
        "/api/strategies/run_async",
        json={"filename": "controlled_strategy.py", "codes": ["000001"]},
    )

    assert response.status_code == 200
    task_id = response.get_json()["task_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", task_id)
    task = strategies._strat_tasks[task_id]
    assert {key: task[key] for key in ("status", "uid", "fn")} == {
        "status": "pending",
        "uid": user_id,
        "fn": "controlled_strategy.py",
    }
    assert task["progress"] == {
        "running": False,
        "stage": "pending",
        "current": 0,
        "total": 0,
        "code": "",
    }
    assert runner_calls == []


def test_all_market_backtest_route_forwards_requester_uid(isolated_app, monkeypatch):
    app, um, _main = isolated_app
    user_id = _create_user(um, "backtest_creator")
    client = app.test_client()
    _login_session(client, user_id, "backtest_creator")

    from strategy_engine import backtest
    from web import misc

    captured_kwargs = {}

    def capture_and_start(**kwargs):
        captured_kwargs.update(kwargs)
        return "a" * 32

    monkeypatch.setattr(
        misc.sm,
        "get_code",
        lambda _filename, **_kwargs: "def strategy(df):\n    return {}",
    )
    monkeypatch.setattr(misc, "_deduct", lambda *args, **kwargs: None)
    monkeypatch.setattr(misc, "_add_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(backtest, "start_backtest_async", capture_and_start)

    response = client.post(
        "/api/backtest",
        json={
            "filename": "owned_strategy.py",
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
        },
    )

    assert response.status_code == 200
    assert captured_kwargs.get("uid", captured_kwargs.get("user_id")) == user_id


def test_start_backtest_async_creates_full_uuid4_task_record_owned_by_requester(
    isolated_app, monkeypatch
):
    _app, um, _main = isolated_app
    user_id = _create_user(um, "backtest_engine_creator")

    from strategy_engine import backtest

    class NonStartingThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            self.daemon = daemon

        def start(self):
            return None

    monkeypatch.setattr(backtest, "_bt_tasks", {})
    monkeypatch.setattr(backtest._bt_threading, "Thread", NonStartingThread)

    task_id = backtest.start_backtest_async(
        strategy_code="def strategy(df):\n    return {}",
        codes=[],
        start_date="2024-01-01",
        end_date="2024-01-31",
        uid=user_id,
    )

    assert re.fullmatch(r"[0-9a-f]{32}", task_id)
    assert backtest._bt_tasks[task_id]["uid"] == user_id


def test_strategy_async_task_is_indistinguishable_from_unknown_to_another_user(
    isolated_app, monkeypatch
):
    app, um, _main = isolated_app
    owner_id = _create_user(um, "strategy_owner")
    other_id = _create_user(um, "strategy_other")
    task_id = "owner-strategy-task"

    from web import strategies

    monkeypatch.setitem(
        strategies._strat_tasks,
        task_id,
        {
            "status": "done",
            "uid": owner_id,
            "result": {
                "ok": True,
                "secret": "owner-only",
                "summary": "completed-owner-result",
            },
        },
    )

    client = app.test_client()
    _login_session(client, other_id, "strategy_other")

    progress = client.get(f"/api/strategies/progress/{task_id}")
    unknown_progress = client.get("/api/strategies/progress/unknown-strategy-task")
    result = client.get(f"/api/strategies/result/{task_id}")
    unknown_result = client.get("/api/strategies/result/unknown-strategy-task")

    _assert_indistinguishable_not_found(progress, unknown_progress)
    _assert_indistinguishable_not_found(result, unknown_result)
    _assert_response_hides_owner_data(progress)
    _assert_response_hides_owner_data(result)


def test_bridge_token_can_fetch_and_report_configured_user_signal(isolated_app, tmp_path, monkeypatch):
    app, um, _main = isolated_app
    owner_id = _create_user(um, "bridge_token_owner")
    signal_id = um.add_trade_signal(
        owner_id,
        "000001",
        "Ping An",
        "BUY",
        10.0,
        100,
    )
    assert signal_id > 0

    from web import bridge

    cfg_path = tmp_path / "bridge_config.json"
    cfg_path.write_text('{"bridge_token":"token-123","bridge_user_id":%d}' % owner_id, encoding="utf-8")
    monkeypatch.setattr(bridge, "THS_BRIDGE_CONFIG", cfg_path)

    # 实盘下单总闸缺省为「关」（安全默认，见 core/bridge_state._live_enabled）：
    # 桥接器照常轮询但拿不到单。要取到信号必须先显式开闸。
    from core import bridge_state
    monkeypatch.setitem(bridge_state._live_trade_enabled, owner_id, True)

    client = app.test_client()
    headers = {"X-Bridge-Token": "token-123"}
    fetched = client.get("/api/bridge/signals", headers=headers)

    assert fetched.status_code == 200
    body = fetched.get_json()
    assert body["ok"] is True
    assert [sig["id"] for sig in body["signals"]] == [signal_id]
    assert um.get_trade_signal(signal_id)["status"] == "sent"

    reported = client.post(
        f"/api/bridge/signals/{signal_id}/report",
        json={"status": "submitted", "msg": "手机委托已提交"},
        headers=headers,
    )
    assert reported.status_code == 200
    assert reported.get_json()["ok"] is True
    signal = um.get_trade_signal(signal_id)
    assert signal["status"] == "submitted"
    assert "手机委托已提交" in signal["msg"]


def test_bridge_token_is_scoped_to_configured_user(isolated_app, tmp_path, monkeypatch):
    app, um, _main = isolated_app
    owner_id = _create_user(um, "bridge_token_scope_owner")
    other_id = _create_user(um, "bridge_token_scope_other")
    signal_id = um.add_trade_signal(other_id, "000002", "Other", "BUY", 11.0, 100, msg="owner-only")
    assert signal_id > 0

    from web import bridge

    cfg_path = tmp_path / "bridge_config.json"
    cfg_path.write_text('{"bridge_token":"token-456","bridge_user_id":%d}' % owner_id, encoding="utf-8")
    monkeypatch.setattr(bridge, "THS_BRIDGE_CONFIG", cfg_path)

    client = app.test_client()
    headers = {"X-Bridge-Token": "token-456"}
    response = client.post(
        f"/api/bridge/signals/{signal_id}/report",
        json={"status": "submitted", "msg": "wrong-user-report"},
        headers=headers,
    )
    unknown_response = client.post(
        "/api/bridge/signals/999999/report",
        json={"status": "submitted", "msg": "wrong-user-report"},
        headers=headers,
    )

    _assert_indistinguishable_not_found(response, unknown_response)
    signal = um.get_trade_signal(signal_id)
    assert signal["status"] == "pending"
    assert signal["msg"] == "owner-only"

def test_bridge_report_is_indistinguishable_from_unknown_and_preserves_owner_signal(isolated_app):
    app, um, _main = isolated_app
    owner_id = _create_user(um, "signal_owner")
    other_id = _create_user(um, "signal_other")
    signal_id = um.add_trade_signal(
        owner_id,
        "000001",
        "Ping An",
        "BUY",
        10.0,
        100,
        msg="owner-only",
    )
    assert signal_id > 0

    client = app.test_client()
    _login_session(client, other_id, "signal_other")
    report = {"status": "submitted", "msg": "other-user-report"}
    response = client.post(f"/api/bridge/signals/{signal_id}/report", json=report)
    unknown_response = client.post("/api/bridge/signals/999999/report", json=report)

    _assert_indistinguishable_not_found(response, unknown_response)
    _assert_response_hides_owner_data(response)
    signal = um.get_trade_signal(signal_id)
    assert signal["status"] == "pending"
    assert signal["msg"] == "owner-only"


def test_root_admin_cannot_report_another_users_bridge_signal(isolated_app):
    app, um, _main = isolated_app
    owner_id = _create_user(um, "root_signal_owner")
    signal_id = um.add_trade_signal(
        owner_id,
        "000001",
        "Ping An",
        "BUY",
        10.0,
        100,
        msg="owner-only",
    )
    assert signal_id > 0

    client = app.test_client()
    _login_session(client, 1, "admin", is_admin=True)
    report = {"status": "submitted", "msg": "root-admin-report"}
    response = client.post(f"/api/bridge/signals/{signal_id}/report", json=report)
    unknown_response = client.post("/api/bridge/signals/999999/report", json=report)

    _assert_indistinguishable_not_found(response, unknown_response)
    _assert_response_hides_owner_data(response)
    signal = um.get_trade_signal(signal_id)
    assert signal["status"] == "pending"
    assert signal["msg"] == "owner-only"


def test_bridge_today_orders_and_screenshots_require_root_admin(isolated_app, monkeypatch, tmp_path):
    app, um, _main = isolated_app
    user_id = _create_user(um, "bridge_user")
    client = app.test_client()
    _login_session(client, user_id, "bridge_user")

    import app_security as sec
    from web import bridge, pages

    monkeypatch.setattr(sec, "_ANTI_CRAWL_ENABLED", False)
    operator_lookups = []
    monkeypatch.setattr(bridge, "get_operator", lambda: operator_lookups.append(True))
    monkeypatch.setattr(pages, "BASE_DIR", tmp_path)
    screenshot_dir = tmp_path / "logs" / "screenshots"
    screenshot_dir.mkdir(parents=True)
    (screenshot_dir / "today_orders.png").write_bytes(b"png")

    today_orders = client.get("/api/bridge/today_orders")
    screenshot = client.get("/logs/screenshots/today_orders.png")

    _assert_root_admin_required(today_orders)
    _assert_root_admin_required(screenshot)
    assert operator_lookups == []


def test_root_admin_can_access_bridge_today_orders_and_screenshot(isolated_app, monkeypatch, tmp_path):
    app, _um, _main = isolated_app
    client = app.test_client()
    _login_session(client, 1, "admin", is_admin=True)

    import app_security as sec
    from web import bridge, pages

    monkeypatch.setattr(sec, "_ANTI_CRAWL_ENABLED", False)
    expected_orders = {"ok": True, "orders": [], "positions": []}

    class FakeOperator:
        def query_today_orders(self, **kwargs):
            return expected_orders

    monkeypatch.setattr(bridge.platform, "system", lambda: "Windows")
    monkeypatch.setattr(bridge, "get_operator", lambda: FakeOperator())
    monkeypatch.setattr(pages, "BASE_DIR", tmp_path)
    screenshot_dir = tmp_path / "logs" / "screenshots"
    screenshot_dir.mkdir(parents=True)
    (screenshot_dir / "today_orders.png").write_bytes(b"png")

    from flask import Response

    monkeypatch.setattr(
        pages,
        "send_from_directory",
        lambda *_args, **_kwargs: Response(b"png", mimetype="image/png"),
    )

    today_orders = client.get("/api/bridge/today_orders")
    screenshot = client.get("/logs/screenshots/today_orders.png")

    assert today_orders.status_code == 200
    assert today_orders.get_json() == expected_orders
    assert screenshot.status_code == 200
    assert screenshot.data == b"png"


def test_anonymous_backtest_task_requires_authentication_before_task_lookup(isolated_app, monkeypatch):
    app, _um, _main = isolated_app

    from strategy_engine import backtest

    task_lookups = []
    monkeypatch.setattr(
        backtest,
        "get_backtest_task",
        lambda task_id: task_lookups.append(task_id) or {"ok": True},
    )

    response = app.test_client().get("/api/backtest/task/owned-backtest-task")

    assert response.status_code == 401
    assert response.is_json
    assert response.get_json()["code"] == 401
    assert task_lookups == []


def test_all_market_backtest_tasks_are_indistinguishable_from_unknown_to_another_user(
    isolated_app, monkeypatch
):
    app, um, _main = isolated_app
    owner_id = _create_user(um, "backtest_owner")
    other_id = _create_user(um, "backtest_other")
    result_task_id = "owner-backtest-result-task"
    cancel_task_id = "owner-backtest-cancel-task"

    from strategy_engine import backtest

    monkeypatch.setitem(
        backtest._bt_tasks,
        result_task_id,
        {
            "status": "done",
            "uid": owner_id,
            "user_id": owner_id,
            "progress": {"current": 100, "total": 100, "stage": "done"},
            "result": {
                "ok": True,
                "secret": "owner-only",
                "summary": "completed-owner-result",
            },
            "cancel": False,
        },
    )
    monkeypatch.setitem(
        backtest._bt_tasks,
        cancel_task_id,
        {
            "status": "running",
            "uid": owner_id,
            "user_id": owner_id,
            "progress": {"current": 3, "total": 100, "stage": "running"},
            "result": None,
            "cancel": False,
        },
    )

    client = app.test_client()
    _login_session(client, other_id, "backtest_other")
    task_response = client.get(f"/api/backtest/task/{result_task_id}")
    unknown_task_response = client.get("/api/backtest/task/unknown-backtest-task")
    cancel_response = client.post(f"/api/backtest/cancel/{cancel_task_id}")
    unknown_cancel_response = client.post("/api/backtest/cancel/unknown-backtest-task")

    _assert_indistinguishable_not_found(task_response, unknown_task_response)
    _assert_indistinguishable_not_found(cancel_response, unknown_cancel_response)
    _assert_response_hides_owner_data(task_response)
    _assert_response_hides_owner_data(cancel_response)
    assert backtest._bt_tasks[cancel_task_id]["status"] == "running"
    assert backtest._bt_tasks[cancel_task_id]["cancel"] is False


def test_ai_async_creation_uses_full_uuid4_hex_task_id_and_records_owner(isolated_app, monkeypatch):
    app, um, _main = isolated_app
    user_id = _create_user(um, "ai_creator")
    client = app.test_client()
    _login_session(client, user_id, "ai_creator")

    from web import ai

    class NonStartingThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            self.daemon = daemon

        def start(self):
            return None

    monkeypatch.setattr(ai, "_ai_tasks", {})
    monkeypatch.setattr(ai, "_deduct", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai, "_add_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai._threading, "Thread", NonStartingThread)

    response = client.post("/api/ai/analyze_async", json={"code": "000001"})

    assert response.status_code == 200
    task_id = response.get_json()["task_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", task_id)
    assert ai._ai_tasks[task_id] == {
        "status": "pending",
        "uid": user_id,
        "code": "000001",
    }


def test_ai_async_task_is_indistinguishable_from_unknown_to_another_user(isolated_app, monkeypatch):
    app, um, _main = isolated_app
    owner_id = _create_user(um, "ai_owner")
    other_id = _create_user(um, "ai_other")
    task_id = "owner-ai-task"

    from web import ai

    monkeypatch.setitem(
        ai._ai_tasks,
        task_id,
        {
            "status": "done",
            "uid": owner_id,
            "code": "000001",
            "result": {
                "ok": True,
                "secret": "owner-only",
                "summary": "completed-owner-result",
            },
        },
    )

    client = app.test_client()
    _login_session(client, other_id, "ai_other")
    progress = client.get(f"/api/ai/analyze_progress/{task_id}")
    unknown_progress = client.get("/api/ai/analyze_progress/unknown-ai-task")
    result = client.get(f"/api/ai/analyze_result/{task_id}")
    unknown_result = client.get("/api/ai/analyze_result/unknown-ai-task")

    _assert_indistinguishable_not_found(progress, unknown_progress)
    _assert_indistinguishable_not_found(result, unknown_result)
    _assert_response_hides_owner_data(progress)
    _assert_response_hides_owner_data(result)
