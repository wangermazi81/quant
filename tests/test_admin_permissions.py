import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import pandas as pd
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


def _eligible_factor_registration(relay):
    source = relay.load_model_preregistration()
    assert source["ok"] is True
    record = json.loads(json.dumps(source["record"]))
    record["research_protocol"] = relay._build_locking_protocol(
        config={
            "n_stocks": 800,
            "days": 180,
            "train_days": 60,
            "test_days": 2,
            "step_days": 2,
            "hold_days": 2,
            "stop_loss": -0.035,
            "take_profit": 0.07,
            "top_k": 5,
            "candidate_pool_size": 100,
            "include_market_signals": False,
            "model_version": relay.MODEL_VERSION,
            "universe_selection": (
                "point_in_time_eligible_stable_sha256_sample"
            ),
            "data_quality_policy": dict(
                relay.FROZEN_DATA_QUALITY_POLICY
            ),
        },
        mining_rules=relay.normalize_mining_rules(None),
        execution_rules=relay.normalize_execution_rules(),
    )
    record["research_validation_rules"].update(
        {
            "evaluation_window_count": 12,
            "window_sessions": 2,
            "oos_session_count": 24,
            "minimum_diagnostic_sample_count_per_session": 20,
        }
    )
    executable_model = record["executable_model"]
    executable_model.update(
        {
            "validation_accepted": True,
            "validation_rejection_reason": None,
            "validation_stable": True,
            "validation_stability_reason": "ok",
            "frozen_model_candidate_eligible": True,
            "frozen_model_candidate_reason": "ok",
        }
    )
    model_sha256 = relay.factor_model_digest(executable_model)
    record["executable_model_sha256"] = model_sha256
    canonical = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    registration = {
        "ok": True,
        "reason": "ok",
        "record": record,
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "model_sha256": model_sha256,
        "current_protocol": True,
    }
    registered_model = relay._registered_model_from_preregistration(
        registration
    )
    registered_model["calibration_data_match"] = True
    return registration, registered_model


def _signed_factor_oos_evidence(
    relay,
    registration,
    registered_model,
):
    locking_protocol = registration["record"]["research_protocol"]
    locking_config = locking_protocol["config"]
    top_k = locking_config["top_k"]
    holding_sessions = locking_config["hold_days"] + 1
    window_sessions = locking_config["test_days"]
    session_dates = [
        value.strftime("%Y-%m-%d")
        for value in pd.bdate_range(
            registration["record"]["oos_not_before"],
            periods=24,
        )
    ]
    latest_feature_date = (
        pd.Timestamp(session_dates[-1])
        + pd.offsets.BDay(holding_sessions)
    ).strftime("%Y-%m-%d")
    attempts = []
    benchmark_attempts = []
    session_diagnostics = []
    trades = []
    for session_index, trade_date in enumerate(session_dates):
        signal_date = pd.Timestamp(trade_date)
        entry_date = (
            signal_date + pd.offsets.BDay(1)
        ).strftime("%Y-%m-%d")
        exit_date = (
            signal_date + pd.offsets.BDay(2)
        ).strftime("%Y-%m-%d")
        codes = [
            f"ADMIN{session_index:02d}{rank:02d}"
            for rank in range(top_k)
        ]
        universe_codes = sorted(
            [
                *codes,
                *[
                    f"CONTROL{session_index:02d}{rank:02d}"
                    for rank in range(20)
                ],
            ]
        )
        benchmark_codes = relay._deterministic_benchmark_codes(
            universe_codes,
            trade_date=trade_date,
            top_k=top_k,
        )
        session_diagnostics.append(
            {
                "trade_date": trade_date,
                "universe_codes": universe_codes,
                "universe_codes_sha256": (
                    relay._canonical_codes_sha256(universe_codes)
                ),
                "sample_count": len(universe_codes),
                "rank_ic": 0.05,
                "bottom_quantile_return_pct": -0.5,
                "top_quantile_return_pct": 1.0,
                "top_bottom_spread_pct": 1.5,
                "benchmark_codes": benchmark_codes,
                "benchmark_evaluable": True,
            }
        )
        for code in benchmark_codes:
            benchmark_attempts.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "execution_blocked": False,
                    "forward_return_pct": 0.0,
                    "live_adjusted_return_pct": 0.0,
                    "valuation_path": [
                        {
                            "date": entry_date,
                            "return_pct": 0.0,
                        },
                        {
                            "date": exit_date,
                            "return_pct": 0.0,
                        },
                    ],
                    "exit_reason": "max_holding_period",
                    "gate_pass": True,
                }
            )
        for code in codes:
            attempts.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "execution_blocked": False,
                    "forward_return_pct": 1.0,
                    "live_adjusted_return_pct": 1.0,
                    "valuation_path": [
                        {
                            "date": entry_date,
                            "return_pct": 0.5,
                        },
                        {
                            "date": exit_date,
                            "return_pct": 1.0,
                        },
                    ],
                    "exit_reason": "take_profit",
                    "gate_pass": True,
                }
            )
        trades.append(
            {
                "trade_date": trade_date,
                "codes": codes,
                "return_pct": 1.0,
                "live_adjusted_return_pct": 1.0,
                "research_evaluable": True,
                "gate_pass": True,
                "gate_reason": "gate_disabled",
                "model_sha256": registration["model_sha256"],
            }
        )
    results = relay._build_preregistered_oos_results(
        session_dates=session_dates,
        trades=trades,
        attempts=attempts,
        benchmark_attempts=benchmark_attempts,
        session_diagnostics=session_diagnostics,
        model=registered_model,
        test_days=window_sessions,
        top_k=top_k,
        holding_sessions=holding_sessions,
        validation_rules=registration["record"][
            "research_validation_rules"
        ],
    )
    data_quality_assessment = {
        "assessment_version": "relay_data_quality_assessment_v1",
        "amount_data_coverage": 1.0,
        "latest_amount_data_coverage": 1.0,
        "st_status_coverage": 1.0,
        "latest_st_status_coverage": 1.0,
        "universe_eligibility_coverage": 1.0,
        "latest_universe_eligibility_coverage": 1.0,
        "panel_max_abs_close_return": 0.10,
        "panel_stock_count": 800,
        "universe_required_count": 800,
        "status": "ok",
        "failure_reasons": [],
    }
    closed_2026 = [
        "2026-01-01",
        "2026-01-02",
        "2026-02-16",
        "2026-02-17",
        "2026-02-18",
        "2026-02-19",
        "2026-02-20",
        "2026-02-23",
        "2026-04-06",
        "2026-05-01",
        "2026-05-04",
        "2026-05-05",
        "2026-06-19",
        "2026-09-25",
        "2026-10-01",
        "2026-10-02",
        "2026-10-05",
        "2026-10-06",
        "2026-10-07",
    ]
    open_days = [
        value.strftime("%Y-%m-%d")
        for value in pd.bdate_range(
            "2026-01-01",
            "2026-12-31",
        )
        if value.strftime("%Y-%m-%d") not in closed_2026
    ]
    calendar_snapshot = relay._build_evidence_calendar_snapshot(
        registration["record"]["oos_not_before"],
        max([
            latest_feature_date,
            *[
                str(attempt["exit_date"])
                for attempt in attempts
            ],
        ]),
        closed_days_by_year={"2026": closed_2026},
        provenance_by_year={
            "2026": {
                "provenance_version": (
                    "tushare_trade_cal_provenance_v1"
                ),
                "source": "tushare_trade_cal",
                "provider_mode": "replay",
                "fetched_at": "2026-07-30T16:10:00+08:00",
                "open_days_count": len(open_days),
                "open_days_sha256": hashlib.sha256(
                    json.dumps(
                        open_days,
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
        },
    )
    assert calendar_snapshot is not None
    fingerprint_dates = pd.to_datetime(session_dates[:2])
    fingerprint_codes = ["RAW0001", "RAW0002"]
    numeric_frame = pd.DataFrame(
        [[10.0, 20.0], [10.1, 20.1]],
        index=fingerprint_dates,
        columns=fingerprint_codes,
    )
    boolean_frame = pd.DataFrame(
        True,
        index=fingerprint_dates,
        columns=fingerprint_codes,
    )
    raw_panel = {
        "open": numeric_frame,
        "high": numeric_frame + 0.2,
        "low": numeric_frame - 0.2,
        "close": numeric_frame + 0.1,
        "volume": numeric_frame * 1000.0,
        "amount": numeric_frame * 10000.0,
        "amount_real": boolean_frame,
        "is_st": ~boolean_frame,
        "st_status_known": boolean_frame,
        "universe_eligible": boolean_frame,
        "security_master_eligible": boolean_frame,
    }
    raw_data_fingerprint = relay.build_raw_data_fingerprint(
        raw_panel,
        security_master=pd.DataFrame(
            {
                "code": fingerprint_codes,
                "list_date": ["20200101", "20200101"],
                "delist_date": ["", ""],
            }
        ),
        market_signal_frame=pd.DataFrame(
            {
                "trade_date": session_dates[:2],
                "market_data_trusted": [True, True],
                "market_emotion_score": [60.0, 60.0],
            }
        ),
        calendar_snapshot=calendar_snapshot,
        source_kind="trusted_cache",
    )
    preregistered_oos = relay._seal_preregistered_oos_evidence(
        {
            "evidence_version": (
                relay.PREREGISTERED_OOS_EVIDENCE_VERSION
            ),
            "protocol_version": relay.MODEL_SPEC_PROTOCOL_VERSION,
            "calibrated_through": (
                relay.MODEL_SPEC_CALIBRATED_THROUGH
            ),
            "oos_not_before": registration["record"]["oos_not_before"],
            "registration_sha256": registration["sha256"],
            "protocol_binding": (
                relay.build_current_frozen_protocol_binding()
            ),
            "model_sha256": registration["model_sha256"],
            "raw_data_fingerprint": raw_data_fingerprint,
            "diagnostic_policy_version": (
                relay.OOS_DIAGNOSTIC_POLICY_VERSION
            ),
            "benchmark_policy_version": (
                relay.BENCHMARK_POLICY_VERSION
            ),
            "window_count": len(results["windows"]),
            "window_sessions": window_sessions,
            "top_k": top_k,
            "holding_sessions": holding_sessions,
            "feature_freshness": {
                "policy_version": (
                    "authoritative_completed_session_v2"
                ),
                "clock_source": "system",
                "as_of": (
                    f"{latest_feature_date}T17:10:00+08:00"
                ),
                "latest_feature_date": latest_feature_date,
                "expected_latest_feature_date": (
                    latest_feature_date
                ),
                "maturity_lag_sessions": holding_sessions,
            },
            "calendar_snapshot": calendar_snapshot,
            "observation_cutoff": session_dates[-1],
            "pending_session_dates": [],
            "data_quality_assessment": data_quality_assessment,
            "locking_protocol": locking_protocol,
            "session_dates": session_dates,
            "windows": results["windows"],
            "trades": trades,
            "attempts": attempts,
            "session_diagnostics": session_diagnostics,
            "benchmark_attempts": benchmark_attempts,
            "portfolio": results["portfolio"],
            "gated_portfolio": results["gated_portfolio"],
            "benchmark_portfolio": results[
                "benchmark_portfolio"
            ],
            "gated_benchmark_portfolio": results[
                "gated_benchmark_portfolio"
            ],
        }
    )
    return {
        "windows": results["windows"],
        "portfolio": results["portfolio"],
        "gated_portfolio": results["gated_portfolio"],
        "benchmark_portfolio": results["benchmark_portfolio"],
        "gated_benchmark_portfolio": results[
            "gated_benchmark_portfolio"
        ],
        "registered_model": registered_model,
        "preregistered_oos": preregistered_oos,
        "data_quality_assessment": data_quality_assessment,
        "raw_data_fingerprint": raw_data_fingerprint,
    }


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


@pytest.mark.parametrize(
    "body",
    [
        {"n_stocks": True},
        {"hold_days": False},
        {"take_profit": True},
        {"n_stocks": 10**400},
        {"take_profit": 10**400},
        {"days": 321},
        ["not", "an", "object"],
    ],
)
def test_factor_mining_run_rejects_invalid_numeric_json(
    isolated_app,
    monkeypatch,
    body,
):
    app, _um, _main = isolated_app
    import web.admin as admin_api

    started = []

    class DeferredThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            started.append(True)

    monkeypatch.setattr(admin_api.threading, "Thread", DeferredThread)
    with admin_api._factor_mining_jobs_lock:
        admin_api._factor_mining_jobs.clear()
        admin_api._factor_mining_active_task_id = None
        admin_api._factor_mining_last_task_id = None

    client = app.test_client()
    _login_session(client, 1, "admin")
    response = client.post(
        "/api/admin/factor-mining/run",
        json=body,
    )

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert started == []
    with admin_api._factor_mining_jobs_lock:
        assert admin_api._factor_mining_jobs == {}


def test_factor_mining_run_rejects_malformed_json_without_starting_job(
    isolated_app,
    monkeypatch,
):
    app, _um, _main = isolated_app
    import web.admin as admin_api

    started = []

    class DeferredThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            started.append(True)

    monkeypatch.setattr(admin_api.threading, "Thread", DeferredThread)
    with admin_api._factor_mining_jobs_lock:
        admin_api._factor_mining_jobs.clear()
        admin_api._factor_mining_active_task_id = None
        admin_api._factor_mining_last_task_id = None

    client = app.test_client()
    _login_session(client, 1, "admin")
    response = client.post(
        "/api/admin/factor-mining/run",
        data='{"n_stocks":',
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert started == []
    with admin_api._factor_mining_jobs_lock:
        assert admin_api._factor_mining_jobs == {}


def test_factor_mining_float_bounds_reject_non_finite_values():
    import web.admin as admin_api

    with pytest.raises(ValueError, match="finite"):
        admin_api._strict_bounded_float_value(
            float("nan"),
            -0.035,
            -0.20,
            -0.005,
        )
    with pytest.raises(ValueError, match="finite"):
        admin_api._strict_bounded_float_value(
            float("inf"),
            0.07,
            0.005,
            0.30,
        )


def test_root_admin_can_lock_query_and_cancel_a_prospective_factor_experiment(
    isolated_app,
    monkeypatch,
    tmp_path,
):
    app, um, _main = isolated_app
    ops_uid = _create_admin(um, permissions=["strategy_manage"])
    import web.admin as admin_api
    from analytics import relay_factor_mining as relay
    from market import trading_calendar

    monkeypatch.setattr(
        trading_calendar,
        "has_verified_calendar",
        lambda _year: True,
    )
    monkeypatch.setattr(
        trading_calendar,
        "is_trading_day",
        lambda value=None: (
            value is not None
            and value.weekday() < 5
        ),
    )

    registration, registered_model = _eligible_factor_registration(relay)
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    monkeypatch.setattr(
        relay,
        "_research_evidence_key",
        lambda: b"test-research-evidence-key",
    )
    monkeypatch.setattr(admin_api, "BASE_DIR", tmp_path)
    run_dir = (
        tmp_path
        / "data"
        / "research"
        / "relay_factor"
        / "run-lockable"
    )
    run_dir.mkdir(parents=True)
    evidence = _signed_factor_oos_evidence(
        relay,
        registration,
        registered_model,
    )
    preregistered_oos = evidence["preregistered_oos"]
    validation_now = pd.Timestamp(
        preregistered_oos["feature_freshness"]["as_of"]
    ).to_pydatetime()
    original_china_datetime = relay._china_datetime
    monkeypatch.setattr(
        relay,
        "_china_datetime",
        lambda value=None: (
            validation_now
            if value is None
            else original_china_datetime(value)
        ),
    )
    assessment = relay.assess_research_result(evidence)
    assert assessment["lock_eligible"] is True
    windows = evidence["windows"]
    portfolio = evidence["portfolio"]
    report = {
        "status": "ok",
        "data_status": "ok",
        "generated_at": pd.Timestamp(
            preregistered_oos["feature_freshness"]["as_of"]
        ).strftime("%Y-%m-%d %H:%M:%S"),
        "research_status": "exploratory",
        "research_assessment": assessment,
        "model": registered_model,
        "config": preregistered_oos["locking_protocol"]["config"],
        "mining_rules": preregistered_oos["locking_protocol"][
            "mining_rules"
        ],
        "execution_rules": preregistered_oos["locking_protocol"][
            "execution_rules"
        ],
        "summary": {
            "latest_trade_date": preregistered_oos[
                "observation_cutoff"
            ],
            "latest_oos_observation_date": preregistered_oos[
                "observation_cutoff"
            ],
            **evidence["data_quality_assessment"],
        },
        "windows": windows,
        "portfolio": portfolio,
        "gated_portfolio": portfolio,
        "raw_data_fingerprint": evidence[
            "raw_data_fingerprint"
        ],
        "preregistered_oos": preregistered_oos,
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
    assert locked["experiment"]["protocol_verified"] is True
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
    assert active["experiments"][0]["progress"]["required_sessions"] == 60

    detail = client.get(
        "/api/admin/factor-mining/experiments/"
        + locked["experiment"]["experiment_id"]
        + "/prospective"
    ).get_json()
    assert detail["progress"]["required_sessions"] == 60
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
            "is_st": False,
            "st_status_known": True,
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
            "is_st": False,
            "st_status_known": True,
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
            "is_st": False,
            "st_status_known": True,
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
