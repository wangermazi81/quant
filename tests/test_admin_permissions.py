import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"
os.environ.setdefault("ADMIN_PASSWORD", "adminpass123")


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    import db_migration as dbm
    import user_manager as um
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


def test_builtin_strategy_and_factor_source_are_not_returned_to_admin(isolated_app, monkeypatch):
    app, _um, main = isolated_app

    monkeypatch.setattr(main.sm, "get_meta", lambda filename: {"name": "Secret Strategy", "builtin": True})
    monkeypatch.setattr(main.sm, "get_code", lambda filename: "SECRET_STRATEGY_CODE")
    monkeypatch.setattr(main.fm, "get_meta", lambda filename: {"name": "Secret Factor", "builtin": True}, raising=False)
    monkeypatch.setattr(main.fm, "get_code", lambda filename: "SECRET_FACTOR_CODE")

    client = app.test_client()
    _login_session(client, 1, "admin")

    strategy = client.get("/api/strategies/core_alpha").get_json()
    factor = client.get("/api/factors/core_factor").get_json()

    assert strategy["locked"] is True
    assert "SECRET_STRATEGY_CODE" not in strategy["code"]
    assert factor["locked"] is True
    assert "SECRET_FACTOR_CODE" not in factor["code"]


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
