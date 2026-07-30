import os
import sys
import json
import threading
import time
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
    # 工单7 拆分后 JS 在 admin-app.js，壳页面须引用它（原断言 b"/auth/me" 随 JS 外迁）
    assert b"/static/app/js/admin-app.js" in page.data
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


def test_factor_mining_latest_is_root_only_and_returns_report(isolated_app, monkeypatch, tmp_path):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["dashboard_view"])

    import web.admin as admin_api
    from analytics import data_manager as dm

    monkeypatch.setattr(admin_api, "BASE_DIR", tmp_path)
    import app_state as _app_state
    monkeypatch.setattr(_app_state, "BASE_DIR", tmp_path)  # 观察池存储已下沉 core/factor_watchlist，按真正的源头打桩
    monkeypatch.setattr(dm, "get_stock_name", lambda code: "平安银行")
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
    assert denied.get_json()["code"] == "root_admin_required"

    _login_session(client, 1, "admin")
    allowed = client.get("/api/admin/factor-mining/latest")
    data = allowed.get_json()

    assert allowed.status_code == 200
    assert data["ok"] is True
    assert data["found"] is True
    assert data["run_id"] == "20260709-101500"
    assert data["report"]["summary"]["trusted_feature_rows"] == 96
    assert data["report"]["latest_candidates"][0]["ts_code"] == "000001.SZ"
    assert data["report"]["latest_candidates"][0]["code"] == "000001"
    assert data["report"]["latest_candidates"][0]["name"] == "平安银行"


def test_factor_mining_run_is_root_only_and_invokes_research(isolated_app, monkeypatch, tmp_path):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["strategy_manage"])

    import web.admin as admin_api

    calls = []
    started = threading.Event()
    release = threading.Event()

    def fake_run_relay_research(**kwargs):
        calls.append(kwargs)
        started.set()
        assert release.wait(timeout=2)
        return {
            "status": "ok",
            "data_status": "ok",
            "reason": "ok",
            "research_status": "exploratory",
            "research_assessment": {
                "research_status": "exploratory",
                "lock_eligible": True,
                "deployment_eligible": False,
            },
            "generated_at": "2026-07-09 10:30:00",
            "summary": {"feature_rows": 10, "trusted_feature_rows": 10},
            "latest_candidates": [
                {"ts_code": "000002.SZ", "trade_date": "2026-07-09", "candidate_rank": 1, "confidence": 0.7}
            ],
        }

    class AcquiredLock:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(admin_api, "BASE_DIR", tmp_path)
    import app_state as _app_state
    monkeypatch.setattr(_app_state, "BASE_DIR", tmp_path)  # 观察池存储已下沉 core/factor_watchlist，按真正的源头打桩
    monkeypatch.setattr(admin_api.relay_factor_mining, "run_relay_research", fake_run_relay_research, raising=False)
    monkeypatch.setattr(admin_api.auto_factor_scheduler, "_file_lock", lambda: AcquiredLock())
    with admin_api._factor_mining_jobs_lock:
        admin_api._factor_mining_jobs.clear()
        admin_api._factor_mining_active_task_id = None
        admin_api._factor_mining_last_task_id = None

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

    assert resp.status_code == 403
    assert data["code"] == "root_admin_required"
    assert not calls

    _login_session(client, 1, "admin")
    try:
        before = time.monotonic()
        resp = client.post(
            "/api/admin/factor-mining/run",
            json={
                "n_stocks": 25,
                "days": 80,
                "train_days": 30,
                "test_days": 10,
                "hold_days": 2,
                "stop_loss": -0.04,
                "take_profit": 0.08,
                "top_k": 4,
                "mining_rules": {"filters": {"min_volume_ratio_20": 1.1}},
            },
        )
        elapsed = time.monotonic() - before
        data = resp.get_json()

        assert resp.status_code == 202
        assert data["ok"] is True
        assert data["task_id"]
        assert data["status"] in {"queued", "running"}
        assert elapsed < 0.5
        assert started.wait(timeout=1)

        duplicate = client.post("/api/admin/factor-mining/run", json={"n_stocks": 30}).get_json()
        assert duplicate["task_id"] == data["task_id"]
        assert duplicate["reused"] is True

        release.set()
        status = None
        for _ in range(40):
            status = client.get(f"/api/admin/factor-mining/status?task_id={data['task_id']}").get_json()
            if status.get("status") == "done":
                break
            time.sleep(0.02)

        assert status["ok"] is True
        assert status["status"] == "done"
        assert status["report"]["status"] == "ok"
        assert status["watchlist"] == {
            "ok": False,
            "skipped": True,
            "reason": "exploratory_research_not_deployable",
        }
        assert calls
        assert calls[0]["n_stocks"] == 25
        assert calls[0]["stop_loss"] == -0.04
        assert calls[0]["take_profit"] == 0.08
        assert calls[0]["include_market_signals"] is True
        assert calls[0]["mining_rules"]["filters"]["min_volume_ratio_20"] == 1.1
        assert str(calls[0]["out_dir"]).startswith(str(tmp_path / "data" / "research" / "relay_factor"))
        listed = client.get("/api/admin/factor-watchlist").get_json()
        assert listed["items"] == []
    finally:
        release.set()


def test_factor_mining_float_bounds_reject_non_finite_values():
    import web.admin as admin_api

    assert admin_api._bounded_float_value(float("nan"), -0.035, -0.20, -0.005) == -0.035
    assert admin_api._bounded_float_value(float("inf"), 0.07, 0.005, 0.30) == 0.07


def test_root_admin_can_lock_query_and_cancel_a_prospective_factor_experiment(
    isolated_app,
    monkeypatch,
    tmp_path,
):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["strategy_manage"])
    import web.admin as admin_api
    from analytics import relay_factor_mining as relay

    monkeypatch.setattr(admin_api, "BASE_DIR", tmp_path)
    run_dir = (
        tmp_path
        / "data"
        / "research"
        / "relay_factor"
        / "run-lockable"
    )
    run_dir.mkdir(parents=True)
    report = {
        "status": "ok",
        "data_status": "ok",
        "generated_at": "2026-07-29 17:10:00",
        "research_status": "exploratory",
        "research_assessment": {
            "metric_gate_passed": True,
            "lock_eligible": True,
            "oos_total_return_pct": 8.0,
            "oos_max_drawdown_pct": -5.0,
            "executed_trade_count": 150,
            "bootstrap_total_return_ci_pct": {"lower": 1.0},
        },
        "model": {
            "model_version": "relay_factor_mining_v3_nested_ridge",
            "strategy_key": relay.STRATEGY_KEY,
            "fallback": False,
            "validation_accepted": True,
            "selected_factors": ["ret_5"],
            "coefficients": {"ret_5": 1.0},
            "feature_means": {"ret_5": 0.5},
            "feature_scales": {"ret_5": 0.25},
            "selected_market_gate": {"enabled": False},
        },
        "config": {
            "n_stocks": 800,
            "days": 180,
            "hold_days": 2,
            "stop_loss": -0.035,
            "take_profit": 0.07,
            "top_k": 10,
            "candidate_pool_size": 100,
            "include_market_signals": False,
            "universe_selection": "point_in_time_eligible_stable_sha256_sample",
            "data_quality_policy": {"minimum_real_amount_coverage": 0.95},
        },
        "mining_rules": relay.normalize_mining_rules(None),
        "summary": {"latest_trade_date": "2026-07-29"},
    }
    (run_dir / "walkforward_report.json").write_text(
        json.dumps(report, ensure_ascii=False),
        encoding="utf-8",
    )

    client = app.test_client()
    _login_session(client, ops_uid, "ops")
    denied = client.post(
        "/api/admin/factor-mining/experiments/run-lockable/lock"
    )
    assert denied.status_code == 403

    _login_session(client, 1, "admin")
    locked_response = client.post(
        "/api/admin/factor-mining/experiments/run-lockable/lock"
    )
    locked = locked_response.get_json()
    assert locked_response.status_code == 201
    assert locked["ok"] is True
    assert locked["experiment"]["status"] == "locked_shadow"
    assert locked["experiment"]["manifest"]["source_run_id"] == "run-lockable"
    assert locked["progress"]["observed_sessions"] == 0

    active = client.get(
        "/api/admin/factor-mining/experiments/active"
    ).get_json()
    assert active["found"] is True
    assert (
        active["experiment"]["experiment_id"]
        == locked["experiment"]["experiment_id"]
    )
    assert active["latest_validated"] is None
    assert [item["experiment_id"] for item in active["experiments"]] == [
        locked["experiment"]["experiment_id"]
    ]
    assert active["experiments"][0]["progress"]["required_sessions"] == 20

    detail = client.get(
        "/api/admin/factor-mining/experiments/"
        + locked["experiment"]["experiment_id"]
        + "/prospective"
    ).get_json()
    assert detail["progress"]["required_sessions"] == 20
    assert detail["sessions"] == []

    cancelled = client.post(
        "/api/admin/factor-mining/experiments/"
        + locked["experiment"]["experiment_id"]
        + "/cancel"
    ).get_json()
    assert cancelled["experiment"]["status"] == "cancelled"
    overview = client.get(
        "/api/admin/factor-mining/experiments/active"
    ).get_json()
    assert overview["found"] is False
    assert overview["experiments"][0]["status"] == "cancelled"


def test_factor_experiment_overview_includes_incumbent_and_recent_history(
    isolated_app,
    monkeypatch,
):
    app, _um, _main = isolated_app
    import web.admin as admin_api

    active = {
        "experiment_id": "exp-challenger",
        "source_run_id": "run-challenger",
        "model_instance_id": "model-challenger",
        "status": "locked_shadow",
    }
    incumbent = {
        "experiment_id": "exp-incumbent",
        "source_run_id": "run-incumbent",
        "model_instance_id": "model-incumbent",
        "status": "validated",
    }
    failed = {
        "experiment_id": "exp-failed",
        "source_run_id": "run-failed",
        "model_instance_id": "model-failed",
        "status": "failed",
    }
    expected_overview = {
        "found": True,
        "experiment": active,
        "progress": {
            "experiment_id": "exp-challenger",
            "status": "locked_shadow",
            "required_sessions": 20,
            "observed_sessions": 7,
            "settled_sessions": 5,
            "ready_to_evaluate": False,
        },
        "latest_validated": incumbent,
        "experiments": [
            {
                **active,
                "progress": {
                    "experiment_id": "exp-challenger",
                    "status": "locked_shadow",
                    "required_sessions": 20,
                    "observed_sessions": 7,
                    "settled_sessions": 5,
                    "ready_to_evaluate": False,
                },
            },
            {
                **incumbent,
                "progress": {
                    "experiment_id": "exp-incumbent",
                    "status": "validated",
                    "required_sessions": 20,
                    "observed_sessions": 7,
                    "settled_sessions": 5,
                    "ready_to_evaluate": False,
                },
            },
            {
                **failed,
                "progress": {
                    "experiment_id": "exp-failed",
                    "status": "failed",
                    "required_sessions": 20,
                    "observed_sessions": 7,
                    "settled_sessions": 5,
                    "ready_to_evaluate": False,
                },
            },
        ],
    }

    class FakeStore:
        def get_overview(self, limit=20):
            assert limit == 10
            return expected_overview

    monkeypatch.setattr(admin_api, "_factor_experiment_store", FakeStore)
    client = app.test_client()
    _login_session(client, 1, "admin")

    response = client.get("/api/admin/factor-mining/experiments/active")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["found"] is True
    assert payload["experiment"] == active
    assert payload["latest_validated"] == incumbent
    assert [item["experiment_id"] for item in payload["experiments"]] == [
        "exp-challenger",
        "exp-incumbent",
        "exp-failed",
    ]
    assert payload["experiments"][1]["progress"]["settled_sessions"] == 5


def test_factor_watchlist_defaults_to_empty_latest_scope_instead_of_older_items(
    isolated_app,
    monkeypatch,
):
    app, _um, _main = isolated_app
    import web.admin as admin_api

    latest_scope = {
        "trade_date": "2026-07-29",
        "source_run_id": "run-empty",
        "strategy_key": "relay_factor_mining_v2",
        "model_version": "relay_factor_mining_v3_nested_ridge",
        "experiment_id": "experiment-validated",
        "model_instance_id": "model-validated",
        "candidate_count": 0,
        "recorded_at": "2026-07-29 17:10:00",
    }
    older_item = {
        "id": "wl-old",
        "ts_code": "000001.SZ",
        "code": "000001",
        "name": "平安银行",
        "trade_date": "2026-07-28",
        "source_run_id": "run-old",
        "strategy_key": "relay_factor_mining_v2",
        "model_version": "relay_factor_mining_v3_nested_ridge",
        "experiment_id": "experiment-validated",
        "model_instance_id": "model-validated",
        "candidate_rank": 1,
        "status": "pending",
        "live_readiness_status": "ready",
    }
    monkeypatch.setattr(
        admin_api,
        "_load_factor_watchlist",
        lambda: {
            "updated_at": "2026-07-29 17:10:00",
            "latest_scope": latest_scope,
            "items": [older_item],
        },
    )
    client = app.test_client()
    _login_session(client, 1, "admin")

    latest = client.get("/api/admin/factor-watchlist").get_json()
    historical = client.get(
        "/api/admin/factor-watchlist?date=2026-07-28"
    ).get_json()

    assert latest["latest_scope"] == latest_scope
    assert latest["selected_date"] == "2026-07-29"
    assert latest["trade_dates"] == ["2026-07-29", "2026-07-28"]
    assert latest["counts_by_date"]["2026-07-29"] == 0
    assert latest["items"] == []
    assert historical["selected_date"] == "2026-07-28"
    assert [item["ts_code"] for item in historical["items"]] == ["000001.SZ"]


def test_factor_experiment_lock_rejects_degraded_report(
    isolated_app,
    monkeypatch,
    tmp_path,
):
    app, _um, _main = isolated_app
    import web.admin as admin_api

    monkeypatch.setattr(admin_api, "BASE_DIR", tmp_path)
    run_dir = (
        tmp_path
        / "data"
        / "research"
        / "relay_factor"
        / "run-degraded"
    )
    run_dir.mkdir(parents=True)
    (run_dir / "walkforward_report.json").write_text(
        json.dumps(
            {
                "status": "degraded",
                "data_status": "degraded",
                "research_assessment": {
                    "metric_gate_passed": True,
                    "lock_eligible": True,
                },
                "model": {
                    "fallback": False,
                    "validation_accepted": True,
                    "selected_factors": ["ret_5"],
                },
            }
        ),
        encoding="utf-8",
    )
    client = app.test_client()
    _login_session(client, 1, "admin")

    response = client.post(
        "/api/admin/factor-mining/experiments/run-degraded/lock"
    )

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_factor_watchlist_import_update_delete_is_root_only(isolated_app, monkeypatch, tmp_path):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["dashboard_view"])

    import web.admin as admin_api
    from analytics import data_manager as dm

    monkeypatch.setattr(admin_api, "BASE_DIR", tmp_path)
    import app_state as _app_state
    monkeypatch.setattr(_app_state, "BASE_DIR", tmp_path)  # 观察池存储已下沉 core/factor_watchlist，按真正的源头打桩
    monkeypatch.setattr(dm, "get_stock_name", lambda code: "平安银行")
    monkeypatch.setattr(
        admin_api._fw,
        "_expected_scope_trade_date",
        lambda: "2026-07-09",
    )
    trusted_candidates = [
        {
            "ts_code": "000001.SZ",
            "trade_date": "2026-07-09",
            "candidate_rank": 1,
            "candidate_tier": "strong",
            "relay_score": 88.5,
            "confidence": 0.72,
            "strategy_key": "relay_factor_mining_v2",
            "model_version": "relay_factor_mining_v3_nested_ridge",
            "experiment_id": "experiment-admin-import",
            "model_instance_id": "model-instance-admin-import",
            "validation_accepted": True,
            "research_status": "validated",
            "deployment_eligible": True,
            "research_only": False,
            "live_readiness_status": "ready",
            "position_pct": 0.1,
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
    ]
    monkeypatch.setattr(
        admin_api,
        "_factor_mining_payload",
        lambda run_dir: {
            "ok": True,
            "found": True,
            "run_id": "20260709-160000",
            "report": {"latest_candidates": trusted_candidates},
        },
    )
    client = app.test_client()
    _login_session(client, ops_uid, "ops")

    denied = client.get("/api/admin/factor-watchlist")
    assert denied.status_code == 403
    assert denied.get_json()["code"] == "root_admin_required"

    _login_session(client, 1, "admin")
    imported = client.post(
        "/api/admin/factor-watchlist/import",
        json={
            "run_id": "20260709-160000",
            "strategy_name": "AI因子挖掘候选",
            "candidates": [{"ts_code": "999999.SZ", "trade_date": "2026-07-09"}],
        },
    )
    data = imported.get_json()
    assert imported.status_code == 200
    assert data["ok"] is True
    assert data["imported"] == 1

    listed = client.get("/api/admin/factor-watchlist").get_json()
    item = listed["items"][0]
    assert item["id"].startswith("wl-")
    assert item["ts_code"] == "000001.SZ"
    assert item["name"] == "平安银行"
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

    trusted_candidates[0].update(
        {
            "research_status": "exploratory",
            "deployment_eligible": False,
            "research_only": True,
            "position_pct": 0.0,
        }
    )
    rejected = client.post("/api/admin/factor-watchlist/import", json={})
    assert rejected.status_code == 400
    assert rejected.get_json()["ok"] is False


def test_ai_trade_decision_is_root_only_and_updates_watchlist(isolated_app, monkeypatch, tmp_path):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["strategy_manage", "dashboard_view"])

    import web.admin as admin_api

    monkeypatch.setattr(admin_api, "BASE_DIR", tmp_path)
    import app_state as _app_state
    monkeypatch.setattr(_app_state, "BASE_DIR", tmp_path)  # 观察池存储已下沉 core/factor_watchlist，按真正的源头打桩
    monkeypatch.setattr(
        admin_api._fw,
        "_expected_scope_trade_date",
        lambda: "2026-07-09",
    )
    trusted_candidates = [
        {
            "ts_code": "000001.SZ",
            "trade_date": "2026-07-09",
            "candidate_rank": 1,
            "confidence": 0.8,
            "strategy_key": "relay_factor_mining_v2",
            "model_version": "relay_factor_mining_v3_nested_ridge",
            "experiment_id": "experiment-ai-decision",
            "model_instance_id": "model-instance-ai-decision",
            "validation_accepted": True,
            "research_status": "validated",
            "deployment_eligible": True,
            "research_only": False,
            "live_readiness_status": "ready",
            "position_pct": 0.1,
        },
        {
            "ts_code": "000002.SZ",
            "trade_date": "2026-07-09",
            "candidate_rank": 2,
            "confidence": 0.7,
            "strategy_key": "relay_factor_mining_v2",
            "model_version": "relay_factor_mining_v3_nested_ridge",
            "validation_accepted": True,
            "research_status": "validated",
            "deployment_eligible": True,
            "research_only": False,
            "live_readiness_status": "ready",
            "position_pct": 0.1,
        },
    ]
    monkeypatch.setattr(
        admin_api,
        "_factor_mining_payload",
        lambda run_dir: {
            "ok": True,
            "found": True,
            "run_id": "20260709-160000",
            "report": {"latest_candidates": trusted_candidates},
        },
    )
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
            "candidates": [{"ts_code": "999999.SZ", "trade_date": "2026-07-09"}],
        },
    )
    assert imported.status_code == 200

    class FakeEngine:
        def decide_watchlist_candidates(self, candidates, max_candidates=6, execute_paper=False):
            assert max_candidates == 1
            assert execute_paper is True
            assert candidates[0]["code"] == "000001"
            watchlist_id = candidates[0]["id"]
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
                        "source": {"watchlist_id": watchlist_id},
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

    # 工单4 后策略/因子路由在 web/strategies.py、web/factors.py，直接打模块桩
    from strategy_engine import strategy_manager as sm
    from analytics import factor_manager as fm
    monkeypatch.setattr(sm, "get_meta", lambda filename, **_kwargs: {"name": "Secret Strategy", "builtin": True})
    monkeypatch.setattr(sm, "get_code", lambda filename, **_kwargs: "SECRET_STRATEGY_CODE")
    monkeypatch.setattr(fm, "get_meta", lambda filename: {"name": "Secret Factor", "builtin": True}, raising=False)
    monkeypatch.setattr(fm, "get_code", lambda filename: "SECRET_FACTOR_CODE")

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
    factor_response = client.get("/api/factors/core_factor")

    assert strategy["locked"] is True
    assert "SECRET_STRATEGY_CODE" not in strategy["code"]
    assert factor_response.status_code == 403
    assert factor_response.get_json()["code"] == "root_admin_required"

    assert um.set_admin_permissions(
        ops_uid,
        ["strategy_manage", "strategy_source_view"],
        granted_by=1,
    )["ok"]

    strategy = client.get("/api/strategies/core_alpha").get_json()
    factor_response = client.get("/api/factors/core_factor")

    assert strategy["locked"] is False
    assert strategy["code"] == "SECRET_STRATEGY_CODE"
    assert factor_response.status_code == 403
    assert factor_response.get_json()["code"] == "root_admin_required"


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
