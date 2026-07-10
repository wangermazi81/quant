import os
import sys
import json
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


def _create_admin(um, username="ops", permissions=None):
    with um._conn() as c:
        um._create_user(
            c,
            username,
            f"{username}@example.com",
            "pass123",
            is_admin=1,
            points=100,
        )
        uid = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
    if permissions is not None:
        assert um.set_admin_permissions(uid, permissions, granted_by=1)["ok"]
    return uid


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


def _login_session(client, uid, username, is_admin=True):
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["username"] = username
        sess["is_admin"] = is_admin


def test_admin_page_shell_is_public_but_admin_api_still_requires_login(isolated_app):
    app, _um, _main = isolated_app
    client = app.test_client()

    page = client.get("/admin")
    api = client.get("/api/admin/stats")

    assert page.status_code == 200
    assert page.content_type.startswith("text/html")
    assert b"/auth/me" in page.data
    assert api.status_code == 401


def test_auth_me_returns_root_flag_and_admin_permissions(isolated_app):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["dashboard_view", "user_manage"])

    client = app.test_client()
    _login_session(client, ops_uid, "ops")
    resp = client.get("/api/auth/me")
    data = resp.get_json()

    assert data["ok"] is True
    assert data["is_admin"] is True
    assert data["is_root_admin"] is False
    assert data["admin_permissions"] == ["dashboard_view", "user_manage"]

    _login_session(client, 1, "admin")
    root_data = client.get("/api/auth/me").get_json()
    assert root_data["is_root_admin"] is True
    assert "admin_grant" in root_data["admin_permissions"]


def test_permission_catalog_includes_admin_ui_descriptions(isolated_app):
    app, _um, _main = isolated_app

    client = app.test_client()
    _login_session(client, 1, "admin")
    resp = client.get("/api/admin/permissions/catalog")
    data = resp.get_json()
    descriptions = {p["key"]: p.get("description", "") for p in data["permissions"]}

    assert resp.status_code == 200
    assert all(text for text in descriptions.values())
    assert "不能查看核心源码" in descriptions["strategy_manage"]
    assert "查看内置策略" in descriptions["strategy_source_view"]
    assert "仅 admin 拥有" in descriptions["admin_grant"]


def test_admin_endpoint_requires_matching_permission(isolated_app):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["dashboard_view"])

    client = app.test_client()
    _login_session(client, ops_uid, "ops")
    denied = client.get("/api/admin/users")
    assert denied.status_code == 403
    assert denied.get_json()["missing_permission"] == "user_manage"

    assert um.set_admin_permissions(ops_uid, ["dashboard_view", "user_manage"], granted_by=1)["ok"]
    allowed = client.get("/api/admin/users")
    assert allowed.status_code == 200
    assert allowed.get_json()["ok"] is True


def test_promoted_admin_with_stale_session_can_use_granted_permission(isolated_app):
    app, um, _main = isolated_app
    uid = _create_user(um, "fresh_admin", is_admin=0)

    client = app.test_client()
    _login_session(client, uid, "fresh_admin", is_admin=False)

    assert um.set_admin(uid, True)["ok"]
    assert um.set_admin_permissions(uid, ["dashboard_view"], granted_by=1)["ok"]

    allowed = client.get("/api/admin/stats")
    assert allowed.status_code == 200
    assert allowed.get_json()["ok"] is True


def test_root_admin_roles_endpoint_lists_admins_beyond_first_user_page(isolated_app):
    app, um, _main = isolated_app
    target_uid = _create_user(um, "278259", is_admin=0)
    for i in range(25):
        _create_user(um, f"user_{i:02d}", is_admin=0)
    assert um.set_admin(target_uid, True)["ok"]
    assert um.set_admin_permissions(target_uid, ["dashboard_view"], granted_by=1)["ok"]

    client = app.test_client()
    _login_session(client, 1, "admin")
    resp = client.get("/api/admin/users/admins")
    data = resp.get_json()

    assert resp.status_code == 200
    assert data["ok"] is True
    assert any(u["username"] == "278259" for u in data["users"])


def test_security_audit_endpoints_require_security_permission(isolated_app):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["dashboard_view"])

    client = app.test_client()
    _login_session(client, ops_uid, "ops")
    denied = client.get("/api/admin/security/events")
    assert denied.status_code == 403
    assert denied.get_json()["missing_permission"] == "security_audit"

    assert um.set_admin_permissions(ops_uid, ["security_audit"], granted_by=1)["ok"]
    allowed = client.get("/api/admin/security/events")
    assert allowed.status_code == 200
    assert allowed.get_json()["ok"] is True


def test_factor_mining_latest_requires_strategy_permission_and_returns_report(isolated_app, monkeypatch, tmp_path):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["dashboard_view"])

    import web.admin as admin_api

    monkeypatch.setattr(admin_api, "BASE_DIR", tmp_path)
    run_dir = tmp_path / "data" / "research" / "relay_factor" / "20260709-101500"
    run_dir.mkdir(parents=True)
    report = {
        "status": "ok",
        "reason": "ok",
        "generated_at": "2026-07-09 10:15:00",
        "config": {"with_market_signals": True},
        "summary": {
            "feature_rows": 100,
            "trusted_feature_rows": 96,
            "hit_rate": 0.62,
            "total_return_pct": 8.5,
            "max_drawdown_pct": -2.1,
            "latest_market_data_status": "ok",
            "latest_market_emotion_score": 68.2,
        },
        "latest_candidates": [
            {"ts_code": "000001.SZ", "relay_score": 78.1, "confidence": 0.781, "positive_factors": ["volume_confirmation"]}
        ],
    }
    (run_dir / "walkforward_report.json").write_text(json.dumps(report), encoding="utf-8")
    (run_dir / "latest_candidates.json").write_text(json.dumps(report["latest_candidates"]), encoding="utf-8")

    client = app.test_client()
    _login_session(client, ops_uid, "ops")
    denied = client.get("/api/admin/factor-mining/latest")
    assert denied.status_code == 403
    assert denied.get_json()["missing_permission"] == "strategy_manage"

    assert um.set_admin_permissions(ops_uid, ["strategy_manage"], granted_by=1)["ok"]
    allowed = client.get("/api/admin/factor-mining/latest")
    data = allowed.get_json()

    assert allowed.status_code == 200
    assert data["ok"] is True
    assert data["found"] is True
    assert data["run_id"] == "20260709-101500"
    assert data["report"]["summary"]["trusted_feature_rows"] == 96
    assert data["report"]["latest_candidates"][0]["ts_code"] == "000001.SZ"


def test_factor_mining_run_requires_strategy_permission_and_invokes_research(isolated_app, monkeypatch, tmp_path):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["strategy_manage"])

    import web.admin as admin_api

    calls = []

    def fake_run_relay_research(**kwargs):
        calls.append(kwargs)
        return {
            "status": "ok",
            "reason": "ok",
            "generated_at": "2026-07-09 10:30:00",
            "summary": {"feature_rows": 10, "trusted_feature_rows": 10},
            "latest_candidates": [
                {"ts_code": "000002.SZ", "trade_date": "2026-07-09", "candidate_rank": 1, "confidence": 0.7}
            ],
        }

    monkeypatch.setattr(admin_api, "BASE_DIR", tmp_path)
    monkeypatch.setattr(admin_api.relay_factor_mining, "run_relay_research", fake_run_relay_research, raising=False)

    client = app.test_client()
    _login_session(client, ops_uid, "ops")
    resp = client.post(
        "/api/admin/factor-mining/run",
        json={
            "n_stocks": 25,
            "days": 80,
            "train_days": 30,
            "test_days": 10,
            "hold_days": 2,
            "top_k": 4,
            "mining_rules": {"filters": {"min_volume_ratio_20": 1.1}},
        },
    )
    data = resp.get_json()

    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["report"]["status"] == "ok"
    assert data["watchlist"]["imported"] == 1
    assert calls
    assert calls[0]["n_stocks"] == 25
    assert calls[0]["include_market_signals"] is True
    assert calls[0]["mining_rules"]["filters"]["min_volume_ratio_20"] == 1.1
    assert str(calls[0]["out_dir"]).startswith(str(tmp_path / "data" / "research" / "relay_factor"))
    listed = client.get("/api/admin/factor-watchlist").get_json()
    assert listed["items"][0]["ts_code"] == "000002.SZ"


def test_factor_watchlist_import_update_delete_requires_strategy_permission(isolated_app, monkeypatch, tmp_path):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["dashboard_view"])

    import web.admin as admin_api

    monkeypatch.setattr(admin_api, "BASE_DIR", tmp_path)
    client = app.test_client()
    _login_session(client, ops_uid, "ops")

    denied = client.get("/api/admin/factor-watchlist")
    assert denied.status_code == 403
    assert denied.get_json()["missing_permission"] == "strategy_manage"

    assert um.set_admin_permissions(ops_uid, ["strategy_manage"], granted_by=1)["ok"]
    imported = client.post(
        "/api/admin/factor-watchlist/import",
        json={
            "run_id": "20260709-160000",
            "strategy_name": "AI因子挖掘候选",
            "candidates": [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "2026-07-09",
                    "candidate_rank": 1,
                    "candidate_tier": "strong",
                    "relay_score": 88.5,
                    "confidence": 0.72,
                    "stop_loss": -0.055,
                    "take_profit": 0.12,
                    "positive_factors": ["volume_confirmation"],
                    "factor_explain": {
                        "factor_version": "relay_factor_mining_v1",
                        "summary": "二三板接力情绪强势延续",
                        "matched_factors": [{"key": "volume_ratio_20", "label": "20日量比", "text": "量能确认"}],
                        "risk_controls": ["不追一字板排队"],
                        "trading_plan": {"hold_days": 2, "stop_loss": -0.055, "take_profit": 0.12},
                    },
                }
            ],
        },
    )
    data = imported.get_json()
    assert imported.status_code == 200
    assert data["ok"] is True
    assert data["imported"] == 1

    listed = client.get("/api/admin/factor-watchlist").get_json()
    item = listed["items"][0]
    assert item["id"] == "2026-07-09-000001.SZ"
    assert item["strategy_name"] == "AI因子挖掘候选"
    assert item["factor_explain"]["factor_version"] == "relay_factor_mining_v1"
    assert item["factor_explain"]["matched_factors"][0]["label"] == "20日量比"

    updated = client.patch(
        f"/api/admin/factor-watchlist/{item['id']}",
        json={"status": "ready", "note": "竞价不过热再买"},
    )
    assert updated.status_code == 200
    assert updated.get_json()["ok"] is True
    listed = client.get("/api/admin/factor-watchlist").get_json()
    assert listed["items"][0]["status"] == "ready"
    assert listed["items"][0]["note"] == "竞价不过热再买"

    deleted = client.delete(f"/api/admin/factor-watchlist/{item['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/admin/factor-watchlist").get_json()["items"] == []


def test_ai_trade_decision_is_root_only_and_updates_watchlist(isolated_app, monkeypatch, tmp_path):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["strategy_manage", "dashboard_view"])

    import web.admin as admin_api

    monkeypatch.setattr(admin_api, "BASE_DIR", tmp_path)
    client = app.test_client()

    _login_session(client, ops_uid, "ops")
    denied = client.get("/api/admin/ai-trade-decision/latest")
    assert denied.status_code == 403
    assert denied.get_json()["code"] == "root_admin_required"

    _login_session(client, 1, "admin")
    imported = client.post(
        "/api/admin/factor-watchlist/import",
        json={
            "run_id": "20260709-160000",
            "strategy_name": "ai-factor",
            "candidates": [
                {"ts_code": "000001.SZ", "trade_date": "2026-07-09", "candidate_rank": 1, "confidence": 0.8},
                {"ts_code": "000002.SZ", "trade_date": "2026-07-09", "candidate_rank": 2, "confidence": 0.7},
            ],
        },
    )
    assert imported.status_code == 200

    class FakeEngine:
        def decide_watchlist_candidates(self, candidates, max_candidates=6, execute_paper=False):
            assert max_candidates == 1
            assert execute_paper is True
            assert candidates[0]["code"] == "000001"
            return {
                "ok": True,
                "generated_at": "2026-07-10 09:26:00",
                "broker_mode": "paper",
                "execute_paper": True,
                "selected_count": 1,
                "token_usage": {"total_tokens": 123, "estimated": False},
                "results": [
                    {
                        "code": "000001",
                        "name": "000001",
                        "action": "BUY",
                        "confidence": 82,
                        "risk_ok": True,
                        "executed": False,
                        "reason": "test buy",
                        "source": {"watchlist_id": "2026-07-09-000001.SZ"},
                    }
                ],
            }

    monkeypatch.setattr(admin_api.ai_trader_engine, "get_engine", lambda uid: FakeEngine())
    run = client.post(
        "/api/admin/ai-trade-decision/run",
        json={"max_candidates": 1, "execute_paper": True},
    )
    data = run.get_json()

    assert run.status_code == 200
    assert data["ok"] is True
    assert data["results"][0]["action"] == "BUY"
    assert data["token_usage"]["total_tokens"] == 123

    latest = client.get("/api/admin/ai-trade-decision/latest").get_json()
    assert latest["decision"]["results"][0]["code"] == "000001"
    watchlist = client.get("/api/admin/factor-watchlist").get_json()["items"]
    assert next(item for item in watchlist if item["code"] == "000001")["status"] == "ready"


def test_ai_trade_auto_scheduler_endpoints_are_root_only(isolated_app, monkeypatch):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["strategy_manage"])

    import web.admin as admin_api

    monkeypatch.setattr(
        admin_api.auto_factor_scheduler,
        "get_status",
        lambda: {"ok": True, "enabled": True, "running": False, "history": [], "next_runs": {}},
    )
    monkeypatch.setattr(
        admin_api.auto_factor_scheduler,
        "run_job",
        lambda job, force=False: {"ok": True, "job": job, "force": force},
    )

    client = app.test_client()
    _login_session(client, ops_uid, "ops")
    denied = client.get("/api/admin/ai-trade-decision/auto/status")
    assert denied.status_code == 403
    assert denied.get_json()["code"] == "root_admin_required"

    _login_session(client, 1, "admin")
    status = client.get("/api/admin/ai-trade-decision/auto/status")
    assert status.status_code == 200
    assert status.get_json()["enabled"] is True

    run = client.post("/api/admin/ai-trade-decision/auto/run", json={"job": "ai_decision"})
    assert run.status_code == 200
    assert run.get_json() == {"ok": True, "job": "ai_decision", "force": True}


def test_admin_list_endpoints_sanitize_numeric_query_params(isolated_app, monkeypatch):
    app, um, _main = isolated_app
    ops_uid = _create_admin(
        um,
        permissions=["user_manage", "points_manage", "security_audit"],
    )

    import web.admin as admin_api

    calls = []
    monkeypatch.setattr(
        admin_api.um,
        "list_users",
        lambda page, page_size, search: calls.append(("users", page, page_size, search)) or {"users": [], "total": 0},
    )
    monkeypatch.setattr(
        admin_api.um,
        "get_all_logs",
        lambda uid=None, limit=500: calls.append(("logs", uid, limit)) or [],
    )
    monkeypatch.setattr(
        admin_api.sec,
        "list_security_events",
        lambda limit=200, ip="", event_type="": calls.append(("events", limit, ip, event_type)) or [],
    )
    monkeypatch.setattr(
        admin_api.um,
        "all_point_transactions",
        lambda page, page_size: calls.append(("transactions", page, page_size)) or {"transactions": [], "total": 0},
    )

    client = app.test_client()
    _login_session(client, ops_uid, "ops")

    users = client.get("/api/admin/users?page=abc&search=ops")
    logs = client.get("/api/admin/logs?uid=bad&limit=abc")
    events = client.get("/api/admin/security/events?limit=999999&ip=1.2.3.4&event_type=login")
    transactions = client.get("/api/admin/transactions?page=-10")

    assert users.status_code == 200
    assert logs.status_code == 200
    assert events.status_code == 200
    assert transactions.status_code == 200
    assert calls == [
        ("users", 1, 20, "ops"),
        ("logs", None, 500),
        ("events", 1000, "1.2.3.4", "login"),
        ("transactions", 1, 30),
    ]


def test_admin_add_points_rejects_invalid_amount(isolated_app, monkeypatch):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["points_manage"])
    target_uid = _create_user(um, "points_target")

    import web.admin as admin_api

    calls = []
    monkeypatch.setattr(
        admin_api.um,
        "add_points",
        lambda uid, amount, desc: calls.append((uid, amount, desc)) or {"ok": True},
    )

    client = app.test_client()
    _login_session(client, ops_uid, "ops")

    resp = client.post(f"/api/admin/users/{target_uid}/points", json={"amount": "bad"})
    data = resp.get_json()

    assert resp.status_code == 400
    assert data["ok"] is False
    assert "amount" in data["error"]
    assert calls == []


def test_admin_manual_ip_block_sanitizes_seconds(isolated_app, monkeypatch):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["security_audit"])

    import web.admin as admin_api

    calls = []
    monkeypatch.setattr(
        admin_api.sec,
        "block_ip",
        lambda ip, seconds=None, reason="": calls.append((ip, seconds, reason)),
    )
    monkeypatch.setattr(admin_api.sec, "list_ip_blocks", lambda: [])
    monkeypatch.setattr(admin_api.sec, "record_security_event", lambda *args, **kwargs: None)

    client = app.test_client()
    _login_session(client, ops_uid, "ops")

    bad = client.post(
        "/api/admin/security/ip_blocks",
        json={"ip": "203.0.113.10", "seconds": "bad", "reason": "manual"},
    )
    huge = client.post(
        "/api/admin/security/ip_blocks",
        json={"ip": "203.0.113.11", "seconds": 999999999, "reason": "manual"},
    )

    assert bad.status_code == 200
    assert huge.status_code == 200
    assert calls == [
        ("203.0.113.10", 900, "manual"),
        ("203.0.113.11", 86400, "manual"),
    ]


def test_admin_system_status_requires_dashboard_permission_and_hides_sensitive_details(isolated_app, tmp_path, monkeypatch):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=[])
    base_dir = tmp_path / "platform"
    for subdir in ("data/review", "data/lhb", "data/market_snapshots"):
        (base_dir / subdir).mkdir(parents=True, exist_ok=True)
    (base_dir / "data" / "review" / "2026-07-07.json").write_text("{}", encoding="utf-8")
    (base_dir / "data" / "lhb" / "lhb_20260708.json").write_text("{}", encoding="utf-8")
    (base_dir / "data" / "market_snapshots" / "20260708.json").write_text("{}", encoding="utf-8")

    import web.admin as admin_api
    monkeypatch.setattr(admin_api, "BASE_DIR", base_dir, raising=False)

    client = app.test_client()

    login_required = client.get("/api/admin/system/status")
    assert login_required.status_code == 401

    _login_session(client, ops_uid, "ops")
    denied = client.get("/api/admin/system/status")
    assert denied.status_code == 403
    assert denied.get_json()["missing_permission"] == "dashboard_view"

    assert um.set_admin_permissions(ops_uid, ["dashboard_view"], granted_by=1)["ok"]
    allowed = client.get("/api/admin/system/status")
    data = allowed.get_json()

    assert allowed.status_code == 200
    assert data["ok"] is True
    assert data["checks"]["database"]["ok"] is True
    assert data["checks"]["data_dir"]["ok"] is True
    assert data["checks"]["review_archive"]["latest_date"] == "2026-07-07"
    assert data["checks"]["lhb_cache"]["latest_date"] == "2026-07-08"
    assert data["checks"]["market_snapshot"]["latest_date"] == "2026-07-08"

    serialized = str(data).lower()
    assert str(base_dir).lower() not in serialized
    assert "users.db" not in serialized
    assert "secret" not in serialized
    assert "api_key" not in serialized
    assert "token" not in serialized


def test_only_root_admin_can_grant_admin_and_permissions(isolated_app):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["user_manage"])
    target_uid = _create_admin(um, username="target", permissions=[])
    assert um.set_admin(target_uid, False)["ok"]
    rejected = um.set_admin_permissions(ops_uid, ["admin_grant"], granted_by=1)
    assert rejected["ok"] is False

    client = app.test_client()
    _login_session(client, ops_uid, "ops")
    denied = client.post(
        f"/api/admin/users/{target_uid}/admin",
        json={"is_admin": True, "permissions": ["dashboard_view"]},
    )
    assert denied.status_code == 403
    assert denied.get_json()["code"] == "root_admin_required"

    _login_session(client, 1, "admin")
    allowed = client.post(
        f"/api/admin/users/{target_uid}/admin",
        json={"is_admin": True, "permissions": ["dashboard_view"]},
    )
    assert allowed.status_code == 200
    assert allowed.get_json()["ok"] is True
    assert um.get_admin_permissions(target_uid) == ["dashboard_view"]


def test_builtin_strategy_and_factor_source_visibility_respects_root_admin(isolated_app, monkeypatch):
    app, um, main = isolated_app

    monkeypatch.setattr(main.sm, "get_meta", lambda filename: {"name": "Secret Strategy", "builtin": True})
    monkeypatch.setattr(main.sm, "get_code", lambda filename: "SECRET_STRATEGY_CODE")
    monkeypatch.setattr(main.fm, "get_meta", lambda filename: {"name": "Secret Factor", "builtin": True}, raising=False)
    monkeypatch.setattr(main.fm, "get_code", lambda filename: "SECRET_FACTOR_CODE")

    client = app.test_client()
    _login_session(client, 1, "admin")

    strategy = client.get("/api/strategies/core_alpha").get_json()
    factor = client.get("/api/factors/core_factor").get_json()

    assert strategy["locked"] is False
    assert strategy["code"] == "SECRET_STRATEGY_CODE"
    assert factor["locked"] is False
    assert factor["code"] == "SECRET_FACTOR_CODE"

    ops_uid = _create_admin(um, username="limited_ops", permissions=["strategy_manage"])
    _login_session(client, ops_uid, "limited_ops")

    strategy = client.get("/api/strategies/core_alpha").get_json()
    factor = client.get("/api/factors/core_factor").get_json()

    assert strategy["locked"] is True
    assert "SECRET_STRATEGY_CODE" not in strategy["code"]
    assert factor["locked"] is True
    assert "SECRET_FACTOR_CODE" not in factor["code"]

    assert um.set_admin_permissions(
        ops_uid,
        ["strategy_manage", "strategy_source_view"],
        granted_by=1,
    )["ok"]

    strategy = client.get("/api/strategies/core_alpha").get_json()
    factor = client.get("/api/factors/core_factor").get_json()

    assert strategy["locked"] is False
    assert strategy["code"] == "SECRET_STRATEGY_CODE"
    assert factor["locked"] is False
    assert factor["code"] == "SECRET_FACTOR_CODE"


def test_root_admin_login_has_stricter_lockout(isolated_app):
    app, um, _main = isolated_app
    _create_admin(um, permissions=["dashboard_view"])
    client = app.test_client()

    for _ in range(3):
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is False

    locked = client.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    assert locked.status_code == 429

    for _ in range(3):
        resp = client.post("/api/auth/login", json={"username": "ops", "password": "wrong"})
        assert resp.status_code == 200

    not_locked = client.post("/api/auth/login", json={"username": "ops", "password": "pass123"})
    assert not_locked.status_code == 200
    assert not_locked.get_json()["ok"] is True
