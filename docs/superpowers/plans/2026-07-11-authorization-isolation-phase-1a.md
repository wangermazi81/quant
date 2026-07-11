# Authorization Isolation Phase 1a Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent non-root users from accessing factor-mining features or another user's in-memory strategy task, bridge signal, trading screenshot, or Windows-order data.

**Architecture:** Reuse the existing `app_state.require_root_admin` decorator for features that are globally sensitive. For user-owned in-memory resources, retain the existing task storage but require its stored `uid` to equal `session["user_id"]` before returning a status or result; unknown and unauthorized task IDs receive the same not-found response.

**Tech Stack:** Python 3, Flask blueprints, pytest, SQLite-backed `core.user_manager`, in-memory task dictionaries.

## Global Constraints

- Modify local code only; do not connect to, deploy to, or migrate the Alibaba Cloud host.
- Do not read, print, alter, or commit `.env`, databases, private keys, passwords, API keys, tokens, or account credentials.
- Factor list, templates, source, save, delete, execution, factor-zoo compute, factor-zoo tasks, progress, results, cancellation, and management are root-admin-only.
- A normal user can retain their own custom strategy workflow but must not see another user's task state or result.
- Do not use a truncated UUID when creating a task ID.
- No handler may authorize an object merely because the caller knows its numeric ID or filename.

---

## File Structure

- Modify: `web/factors.py` ? apply root-only authorization to every factor and factor-zoo endpoint.
- Modify: `web/strategies.py` ? gate strategy scan task status and results by creator `uid`; create full UUID task IDs.
- Modify: `web/bridge.py` ? require root for global Windows-order access and verify signal ownership before a report changes state.
- Modify: `web/pages.py` ? restrict historical global trading screenshots to root until owner metadata exists.
- Modify: `web/misc.py` and `strategy_engine/backtest.py` ? attach and verify owners for all-market backtest tasks.
- Modify: `web/ai.py` ? verify the creator of an asynchronous AI analysis task before progress or result is returned.
- Create: `tests/test_authorization_isolation.py` ? isolated Flask app fixture plus negative authorization tests.

## Task 1: Establish Authorization Regression Tests

**Files:**
- Create: `tests/test_authorization_isolation.py`

**Interfaces:**
- Consumes: `main.app`, `core.user_manager`, Flask `session_transaction()`.
- Produces: `_login_session(client, uid, username, is_admin=False)` and isolated request tests used by later tasks.

- [ ] **Step 1: Write the isolated app fixture and session helper**

```python
import os
from pathlib import Path

import pytest

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
    return main.app, um


def _create_user(um, username):
    with um._conn() as conn:
        um._create_user(conn, username, f"{username}@example.com", "pass123", points=100)
        return conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]


def _login_session(client, uid, username, is_admin=False):
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["username"] = username
        sess["is_admin"] = is_admin
```

- [ ] **Step 2: Add failing root-only factor tests**

```python
@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/factors", None),
        ("get", "/api/factors/template", None),
        ("post", "/api/factors/run", {"filename": "sample.py", "codes": []}),
        ("post", "/api/factors_zoo/compute", {"factor_id": "x", "codes": ["000001"]}),
    ],
)
def test_factor_endpoints_reject_non_root_user(isolated_app, method, path, payload):
    app, um = isolated_app
    uid = _create_user(um, "member")
    client = app.test_client()
    _login_session(client, uid, "member")

    response = getattr(client, method)(path, json=payload)

    assert response.status_code == 403
    assert response.get_json()["code"] == "root_admin_required"
```

- [ ] **Step 3: Run the test and verify failure**

Run: `pytest tests/test_authorization_isolation.py::test_factor_endpoints_reject_non_root_user -v`

Expected: FAIL because one or more factor endpoints currently accept a normal authenticated user or no authenticated user.

- [ ] **Step 4: Commit the red test**

```bash
git add tests/test_authorization_isolation.py
git commit -m "test: cover factor root authorization"
```

## Task 2: Restrict Every Factor Feature to Root

**Files:**
- Modify: `web/factors.py:10-15`
- Modify: `web/factors.py:26-365`
- Test: `tests/test_authorization_isolation.py`

**Interfaces:**
- Consumes: `app_state.require_root_admin`.
- Produces: every `/api/factors*` and `/api/factors_zoo*` route returns the existing `root_admin_required` 403 response for non-root users.

- [ ] **Step 1: Import the existing root decorator**

```python
from app_state import require_auth, require_root_admin, _add_log
```

- [ ] **Step 2: Place `@require_root_admin` directly under every factor and factor-zoo route decorator**

```python
@bp.route("/api/factors/run", methods=["POST"])
@require_root_admin
def api_factor_run():
    ...
```

Apply the same decorator to the list, template, source, save, delete, AI generation, factor-zoo list, synchronous compute, asynchronous compute, task progress, task result, cancellation, backtest, and walk-forward routes in this module. Remove a redundant `@require_auth` only when `@require_root_admin` replaces it; do not change route paths or JSON success payloads.

- [ ] **Step 3: Run the root-only regression tests**

Run: `pytest tests/test_authorization_isolation.py::test_factor_endpoints_reject_non_root_user -v`

Expected: PASS.

- [ ] **Step 4: Add root success coverage without calling live factor computation**

```python
def test_factor_list_allows_root_admin(isolated_app):
    app, _um = isolated_app
    client = app.test_client()
    _login_session(client, 1, "admin", is_admin=True)

    response = client.get("/api/factors")

    assert response.status_code == 200
```

- [ ] **Step 5: Run the focused factor suite and commit**

Run: `pytest tests/test_authorization_isolation.py -v`

Expected: PASS.

```bash
git add web/factors.py tests/test_authorization_isolation.py
git commit -m "fix: restrict factor mining to root admin"
```

## Task 3: Enforce Strategy Task Ownership

**Files:**
- Modify: `web/strategies.py:139-219`
- Test: `tests/test_authorization_isolation.py`

**Interfaces:**
- Consumes: `_strat_tasks: dict[str, dict]` with `uid` set at task creation and Flask `session["user_id"]`.
- Produces: `_get_owned_strat_task(task_id: str, uid: int) -> dict | None` and full UUID strategy task IDs.

- [ ] **Step 1: Write failing cross-user progress and result tests**

```python
def test_strategy_task_is_invisible_to_another_user(isolated_app):
    app, um = isolated_app
    owner_uid = _create_user(um, "owner")
    other_uid = _create_user(um, "other")
    from web import strategies

    strategies._strat_tasks.clear()
    strategies._strat_tasks["a" * 32] = {
        "uid": owner_uid,
        "status": "done",
        "result": {"ok": True, "secret": "owner-result"},
    }
    client = app.test_client()
    _login_session(client, other_uid, "other")

    progress = client.get("/api/strategies/progress/" + "a" * 32)
    result = client.get("/api/strategies/result/" + "a" * 32)

    assert progress.status_code == 404
    assert result.status_code == 404
    assert "owner-result" not in result.get_data(as_text=True)
```

- [ ] **Step 2: Run the test and verify failure**

Run: `pytest tests/test_authorization_isolation.py::test_strategy_task_is_invisible_to_another_user -v`

Expected: FAIL because the current handlers return task state and result without comparing `task["uid"]`.

- [ ] **Step 3: Add one locked ownership lookup and use it in both handlers**

```python
def _get_owned_strat_task(task_id: str, uid: int):
    with _strat_tasks_lock:
        task = _strat_tasks.get(task_id)
        if not task or task.get("uid") != uid:
            return None
        return dict(task)
```

Replace direct `_strat_tasks.get(task_id)` calls in `api_strat_progress` and `api_strat_result` with this helper. Return `jsonify({"ok": False, "error": "not found"}), 404` whenever the helper returns `None`.

Change task creation from:

```python
task_id = _uuid.uuid4().hex[:12]
```

to:

```python
task_id = _uuid.uuid4().hex
```

- [ ] **Step 4: Run the task ownership tests and commit**

Run: `pytest tests/test_authorization_isolation.py::test_strategy_task_is_invisible_to_another_user -v`

Expected: PASS.

```bash
git add web/strategies.py tests/test_authorization_isolation.py
git commit -m "fix: isolate strategy task results by user"
```

## Task 4: Secure Bridge Reports, Global Orders, and Screenshots

**Files:**
- Modify: `web/bridge.py:12-15,52-110,142-170`
- Modify: `web/pages.py:33-45`
- Test: `tests/test_authorization_isolation.py`

**Interfaces:**
- Consumes: `um.get_trade_signal(signal_id)` and `signal["user_id"]`.
- Produces: only a signal owner may report a signal; only root may read global Windows orders or historical trading screenshots.

- [ ] **Step 1: Write failing bridge ownership and root-only tests**

```python
def test_non_owner_cannot_report_trade_signal(isolated_app):
    app, um = isolated_app
    owner_uid = _create_user(um, "signal_owner")
    other_uid = _create_user(um, "signal_other")
    signal_id = um.add_trade_signal(owner_uid, "000001", "Ping An", "BUY", 10.0, 100)
    client = app.test_client()
    _login_session(client, other_uid, "signal_other")

    response = client.post(f"/api/bridge/signals/{signal_id}/report", json={"status": "executed"})

    assert response.status_code == 404
    assert um.get_trade_signal(signal_id)["status"] == "pending"


def test_non_root_cannot_read_global_orders_or_screenshots(isolated_app):
    app, um = isolated_app
    uid = _create_user(um, "member_orders")
    client = app.test_client()
    _login_session(client, uid, "member_orders")

    assert client.get("/api/bridge/today_orders").status_code == 403
    assert client.get("/logs/screenshots/example.png").status_code == 403
```

- [ ] **Step 2: Run the test and verify failure**

Run: `pytest tests/test_authorization_isolation.py -k "trade_signal or global_orders" -v`

Expected: FAIL because reporting does not compare the signal owner and global routes accept any login.

- [ ] **Step 3: Reuse the existing status-route owner check for reports and root decorator for global data**

```python
sig = um.get_trade_signal(signal_id)
if not sig or sig.get("user_id") != uid:
    return jsonify({"ok": False, "error": "not found"}), 404
```

Import `require_root_admin` from `app_state`; place it on `bridge_today_orders` and the screenshot serving route. Keep the existing path traversal validation in the screenshot route.

- [ ] **Step 4: Run tests and commit**

Run: `pytest tests/test_authorization_isolation.py -k "trade_signal or global_orders" -v`

Expected: PASS.

```bash
git add web/bridge.py web/pages.py tests/test_authorization_isolation.py
git commit -m "fix: isolate bridge signals and trade diagnostics"
```

## Task 5: Gate Existing All-Market Backtest and AI Tasks by Owner

**Files:**
- Modify: `strategy_engine/backtest.py`
- Modify: `web/misc.py`
- Modify: `web/ai.py:312-425`
- Test: `tests/test_authorization_isolation.py`

**Interfaces:**
- Consumes: the task creator's `uid` from each route.
- Produces: `_bt_tasks[task_id]["uid"]` and existing AI task `uid` validation on progress/result reads.

- [ ] **Step 1: Write negative tests that seed one task for user A and query it as user B**

```python
@pytest.mark.parametrize(
    "path",
    [
        "/api/backtest/task/owner-task",
        "/api/ai/analyze_progress/owner-task",
        "/api/ai/analyze_result/owner-task",
    ],
)
def test_async_task_is_invisible_to_another_user(isolated_app, path):
    app, um = isolated_app
    owner_uid = _create_user(um, "async_owner")
    other_uid = _create_user(um, "async_other")
    client = app.test_client()
    _login_session(client, other_uid, "async_other")

    # Seed the target module's task mapping with {"uid": owner_uid, ...}
    response = client.get(path)

    assert response.status_code == 404
```

Seed each module's real in-memory mapping in separate tests so the production handler, rather than a mock route, performs the authorization decision.

- [ ] **Step 2: Run each test and verify failure**

Run: `pytest tests/test_authorization_isolation.py -k async_task -v`

Expected: FAIL because current task reads do not consistently compare `uid`.

- [ ] **Step 3: Add owner data at creation and perform a locked equality check before every read or cancel**

```python
if not task or task.get("uid") != session["user_id"]:
    return jsonify({"ok": False, "error": "not found"}), 404
```

Add `uid` when creating all-market backtest tasks. Preserve the existing `uid` stored by AI analysis tasks; add the equality check to both progress and result routes. Require authentication for all-market backtest status reads before the ownership check. Use full `uuid.uuid4().hex` task IDs for newly created task records.

- [ ] **Step 4: Run focused tests and commit**

Run: `pytest tests/test_authorization_isolation.py -k async_task -v`

Expected: PASS.

```bash
git add web/misc.py strategy_engine/backtest.py web/ai.py tests/test_authorization_isolation.py
git commit -m "fix: enforce ownership for async analysis tasks"
```

## Task 6: Run the Authorization Regression Set and Record Remaining Scope

**Files:**
- Modify: `docs/superpowers/specs/2026-07-11-local-security-isolation-design.md` only if a verified implementation decision differs from the approved design.
- Test: `tests/test_authorization_isolation.py`, `tests/test_admin_permissions.py`

- [ ] **Step 1: Run focused and adjacent permission suites**

Run: `pytest tests/test_authorization_isolation.py tests/test_admin_permissions.py -v`

Expected: PASS.

- [ ] **Step 2: Run syntax compilation without reading sensitive data**

Run: `python -m compileall -q web app_state.py strategy_engine`

Expected: exit code `0`.

- [ ] **Step 3: Check the working tree and commit only changed source, tests, and approved documentation**

```bash
git status --short
git diff --check
git add web/factors.py web/strategies.py web/bridge.py web/pages.py web/misc.py web/ai.py strategy_engine/backtest.py tests/test_authorization_isolation.py
git commit -m "fix: enforce user ownership across sensitive workflows"
```

- [ ] **Step 4: Record the next independent plans before enabling production strategy Workers**

Create separate plans for:

1. Server-side unified trading hard risk controls.
2. Secret encryption, session revocation, CSRF, SSRF, and ACL hardening.
3. Containerized strategy Worker enablement after a new or upgraded cloud host passes memory review.
