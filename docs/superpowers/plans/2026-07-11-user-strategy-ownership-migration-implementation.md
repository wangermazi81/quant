# User Strategy Ownership Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every custom Python strategy private to its authenticated owner, quarantine unowned legacy strategy files, and close the remaining factor and enumeration authorization gaps.

**Architecture:** Keep built-in strategies as the only global runnable strategy class. Add owner-aware strategy-manager operations that resolve custom files only below `strategies/users/<user_id>/`; propagate the authenticated user ID through strategy, backtest, and scheduled scan callers. A local-only migration utility moves legacy root-level custom files to quarantine with a manifest and can explicitly assign a quarantined file to an existing user.

**Tech Stack:** Python 3.13 through `py -3.12`, Flask blueprints, `pathlib`, JSON manifests, existing SQLite-backed `core.user_manager`, pytest.

## Global Constraints

- Do not deploy, restart, or inspect Alibaba Cloud services in this implementation.
- Do not read or alter `.env`, credentials, API keys, secrets, production databases, or financial-account data.
- Ordinary users retain save, edit, activate, backtest, and execute access for their own custom Python strategies.
- Built-in strategy source protections remain unchanged.
- A root administrator has no bypass for an already user-owned custom strategy.
- Factor mining, factor watchlists, factor activation, factor tasks, and factor results require the existing `require_root_admin` decorator.
- Missing and foreign user-owned resources return the same `404 {"ok": false, "error": "not found"}` payload.
- Use `py -3.12 -m pytest`; the bare `pytest` executable selects an unsupported Python 3.8 environment.

---

## File Structure

- `strategy_engine/strategy_manager.py`: owner-aware storage resolution, built-in-only global lookup, per-task progress callback support, and legacy quarantine operations.
- `web/strategies.py`: pass session user IDs into all strategy operations; validate activation; store and expose per-task progress.
- `strategy_engine/scan_service.py`: resolve each running user's active custom strategy and position configuration using that user ID.
- `web/misc.py`: resolve strategy source for backtesting using the authenticated user ID and restrict Vibe activation endpoints to root admin.
- `tools/migrate_strategy_ownership.py`: explicit local dry-run, apply, list, assign, and rollback command entry point; never invoked by application startup.
- `web/admin.py`: root-only factor mining and watchlist decorators.
- `web/bridge.py`: indistinguishable missing/foreign signal-status response.
- `web/factors.py`: full UUID hex task identifiers.
- `tests/test_strategy_ownership.py`: ownership, isolation, migration-manifest, and assignment regression tests.
- `tests/test_authorization_isolation.py`, `tests/test_admin_permissions.py`, `tests/test_strategy_progress_api.py`: endpoint, factor role, signal, task-ID, and concurrent-progress regressions.

### Task 1: Establish Owner-Aware Strategy Storage

**Files:**
- Create: `tests/test_strategy_ownership.py`
- Modify: `strategy_engine/strategy_manager.py:26-239`

**Interfaces:**
- Consumes: `STRATEGIES_DIR`, existing `_read_meta(path)`, and `ast_check(code)`.
- Produces: `list_strategies(user_id: int | None = None) -> list[dict]`, `get_meta(filename: str, user_id: int | None = None) -> dict | None`, `get_code(filename: str, user_id: int | None = None) -> str | None`, `get_position_config(filename: str, user_id: int | None = None) -> dict`, `save(filename: str, code: str, user_id: int) -> dict`, `delete(filename: str, user_id: int) -> dict`, `rename(old_filename: str, new_filename: str, user_id: int) -> dict`, `run(filename: str, codes: list, max_codes: int = 0, user_id: int | None = None) -> dict`, and `resolve_strategy_path(filename: str, user_id: int | None = None) -> tuple[Path | None, dict | None]`.

- [ ] **Step 1: Write failing storage-isolation tests**

```python
def test_custom_strategy_is_saved_only_under_its_owner_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "STRATEGIES_DIR", tmp_path / "strategies")
    assert sm.save("alpha", VALID_CODE, user_id=101)["ok"] is True
    assert (tmp_path / "strategies" / "users" / "101" / "alpha.py").exists()
    assert not (tmp_path / "strategies" / "alpha.py").exists()


def test_other_user_cannot_resolve_or_mutate_custom_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "STRATEGIES_DIR", tmp_path / "strategies")
    sm.save("alpha", VALID_CODE, user_id=101)
    assert sm.get_code("alpha", user_id=202) is None
    assert sm.delete("alpha", user_id=202) == {"ok": False, "error": "not found"}
```

- [ ] **Step 2: Run the focused tests to verify failure**

Run: `py -3.12 -m pytest tests/test_strategy_ownership.py -v`

Expected: FAIL because the current manager writes `strategies/alpha.py` and accepts no `user_id` argument.

- [ ] **Step 3: Add canonical path and ownership helpers**

```python
USERS_DIR_NAME = "users"
LEGACY_UNASSIGNED_DIR_NAME = "legacy_unassigned"


def _safe_filename(filename: str) -> str:
    return "".join(c for c in str(filename) if c.isalnum() or c in "_-")


def _user_strategy_dir(user_id: int) -> Path:
    return STRATEGIES_DIR / USERS_DIR_NAME / str(int(user_id))


def resolve_strategy_path(filename: str, user_id: int | None = None):
    safe = _safe_filename(filename)
    if not safe:
        return None, None
    builtin_path = STRATEGIES_DIR / f"{safe}.py"
    if builtin_path.exists() and _read_meta(builtin_path).get("builtin"):
        return builtin_path, _read_meta(builtin_path)
    if user_id is None:
        return None, None
    custom_path = _user_strategy_dir(user_id) / f"{safe}.py"
    if custom_path.exists():
        return custom_path, _read_meta(custom_path)
    return None, None
```

Implement the listed manager operations with this resolver. New custom saves must create the owner directory with `mkdir(parents=True, exist_ok=True)`, reject invalid names, AST-check source, and always write only the caller's path. Return `{ "ok": False, "error": "not found" }` when a requested custom file is missing or is not owned by the supplied user. Retain built-in delete and rename denial behavior.

- [ ] **Step 4: Run storage tests and existing manager tests**

Run: `py -3.12 -m pytest tests/test_strategy_ownership.py tests/test_strategy_manager_quote_cache.py -v`

Expected: PASS; user directories are the only storage location for new custom files, while root-level `builtin: true` files remain available.

- [ ] **Step 5: Commit the storage boundary**

```bash
git add tests/test_strategy_ownership.py strategy_engine/strategy_manager.py
git commit -m "fix: isolate custom strategy storage by owner"
```

### Task 2: Propagate Strategy Ownership Through Web, Backtest, and Scheduled Scans

**Files:**
- Modify: `web/strategies.py:36-274`
- Modify: `web/misc.py:95-160`
- Modify: `strategy_engine/scan_service.py:42-90`
- Modify: `strategy_engine/strategy_manager.py:242-650`
- Modify: `tests/test_strategy_ownership.py`
- Modify: `tests/test_strategy_progress_api.py`

**Interfaces:**
- Consumes: Task 1 manager methods and `session["user_id"]`.
- Produces: all user-facing strategy reads and mutations call manager methods with `user_id=uid`; `sm.run(filename, codes, max_codes=0, user_id: int | None = None, progress_callback: Callable[[dict], None] | None = None) -> dict`. Calls that omit `user_id` resolve built-ins only, preserving platform-owned AI candidate callers without revealing custom files.

- [ ] **Step 1: Write failing route and progress-isolation tests**

```python
def test_strategy_routes_hide_another_users_custom_file(isolated_app, strategy_roots):
    app, owner_id, other_id = isolated_app
    owner = app.test_client(); _login_session(owner, owner_id, "owner")
    assert owner.post("/api/strategies/save", json={"filename": "alpha", "code": VALID_CODE}).get_json()["ok"]
    other = app.test_client(); _login_session(other, other_id, "other")
    missing = other.get("/api/strategies/alpha")
    unknown = other.get("/api/strategies/unknown")
    assert missing.status_code == unknown.status_code == 404
    assert missing.get_json() == unknown.get_json() == {"ok": False, "error": "not found"}


def test_async_progress_comes_from_its_own_task(isolated_app, monkeypatch):
    # Two task records use different stored progress snapshots; endpoint A must never read B's snapshot.
    app, um, _main = isolated_app
    owner_id = _create_user(um, "progress_owner")
    other_id = _create_user(um, "progress_other")
    from web import strategies
    monkeypatch.setattr(strategies, "_strat_tasks", {
        "owner-task": {"uid": owner_id, "status": "running", "progress": {
            "running": True, "stage": "????", "current": 7, "total": 20, "code": "000001"}},
        "other-task": {"uid": other_id, "status": "running", "progress": {
            "running": True, "stage": "????", "current": 19, "total": 20, "code": "600000"}},
    })
    client = app.test_client()
    _login_session(client, owner_id, "progress_owner")
    response = client.get("/api/strategies/progress/owner-task")
    body = response.get_json()
    assert response.status_code == 200
    assert body["scan_current"] == 7
    assert body["scan_total"] == 20
    assert body["scan_code"] == "000001"
```

- [ ] **Step 2: Run the focused tests to verify failure**

Run: `py -3.12 -m pytest tests/test_strategy_ownership.py tests/test_strategy_progress_api.py -v`

Expected: FAIL because routes resolve global filenames and `/progress/<task_id>` reads `sm.get_scan_progress()` or `ths_quote.get_scan_progress()`.

- [ ] **Step 3: Update every strategy call site to pass the owner**

```python
# web/strategies.py
items = sm.list_strategies(user_id=session["user_id"])
meta = sm.get_meta(filename, user_id=session["user_id"])
code = sm.get_code(filename, user_id=session["user_id"])
r = sm.save(fn, code, user_id=uid)
r = sm.delete(fn, user_id=uid)
r = sm.rename(old_fn, new_fn, user_id=uid)
r = sm.run(fn, cs, max_codes=mc, user_id=uid, progress_callback=_update_task_progress)

# strategy_engine/scan_service.py
_pos_cfg = sm.get_position_config(active, user_id=uid)
ret = sm.run(active, [], max_codes=0, user_id=uid)

# web/misc.py
code_str = sm.get_code(fn, user_id=uid) if fn else ""
```

`api_get` must return the exact not-found payload before it emits a code response. `api_activate` must resolve the requested file for `uid` before writing `active_strategy`; reject missing or foreign names with the same not-found response. `api_run` and `api_run_async` must verify resolvability before deducting points or creating a task.

For async scanning, initialize each `_strat_tasks[task_id]` record with:

```python
{"status": "pending", "uid": uid, "fn": fn,
 "progress": {"running": False, "stage": "???", "current": 0, "total": 0, "code": ""}}
```

Pass a callback that updates only that record under `_strat_tasks_lock`; `api_strat_progress` returns only `task["progress"]` and never consults global `ths_quote` or strategy-manager progress. Preserve existing result and ownership checks.

- [ ] **Step 4: Run focused route, backtest, scan-service, and progress tests**

Run: `py -3.12 -m pytest tests/test_strategy_ownership.py tests/test_strategy_progress_api.py tests/test_daban_auto_execution.py tests/test_paper_api_params.py -v`

Expected: PASS; existing per-user automation still runs an owner's selected custom strategy, and two task progress records remain isolated.

- [ ] **Step 5: Commit call-site and progress isolation**

```bash
git add web/strategies.py web/misc.py strategy_engine/scan_service.py tests/test_strategy_ownership.py tests/test_strategy_progress_api.py
git commit -m "fix: enforce strategy ownership across execution paths"
```

### Task 3: Add Explicit Legacy Quarantine and Assignment Utility

**Files:**
- Create: `tools/migrate_strategy_ownership.py`
- Modify: `strategy_engine/strategy_manager.py`
- Modify: `tests/test_strategy_ownership.py`

**Interfaces:**
- Consumes: `core.user_manager.get_user(user_id)`, Task 1 path helpers, and `hashlib.sha256`.
- Produces: `inventory_legacy_strategies() -> list[dict]`, `quarantine_legacy_strategies(manifest_path: Path, dry_run: bool) -> dict`, `assign_legacy_strategy(filename: str, user_id: int, manifest_path: Path) -> dict`, and `rollback_legacy_strategy_migration(manifest_path: Path) -> dict`.

- [ ] **Step 1: Write failing migration tests in an isolated temporary directory**

```python
def test_dry_run_classifies_only_root_builtin_files_as_builtins(tmp_path, monkeypatch):
    # root builtin stays listed as builtin; root non-builtin is reported as unassigned and is not moved.
    root = tmp_path / "strategies"
    root.mkdir()
    (root / "builtin.py").write_text('# META: {"builtin": true}\ndef scan(*args): return []\n', encoding="utf-8")
    (root / "legacy_alpha.py").write_text('# META: {"builtin": false}\ndef scan(*args): return []\n', encoding="utf-8")
    monkeypatch.setattr(sm, "STRATEGIES_DIR", root)
    result = sm.quarantine_legacy_strategies(tmp_path / "manifest.json", dry_run=True)
    assert result["builtins"] == ["builtin"]
    assert result["unassigned"] == ["legacy_alpha"]
    assert result["moved"] == []
    assert (root / "legacy_alpha.py").exists()


def test_apply_quarantines_unassigned_file_and_records_hash(tmp_path, monkeypatch):
    root = tmp_path / "strategies"
    root.mkdir()
    (root / "legacy_alpha.py").write_text('# META: {"builtin": false}\ndef scan(*args): return []\n', encoding="utf-8")
    monkeypatch.setattr(sm, "STRATEGIES_DIR", root)
    result = sm.quarantine_legacy_strategies(tmp_path / "manifest.json", dry_run=False)
    assert result["moved"] == ["legacy_alpha"]
    assert (tmp_path / "strategies" / "legacy_unassigned" / "legacy_alpha.py").exists()
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["events"][0]["sha256"]


def test_assignment_requires_existing_user_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(sm.um, "get_user", lambda uid: {"id": uid} if uid == 101 else None)
    assert sm.assign_legacy_strategy("legacy_alpha", 101, manifest_path)["ok"] is True
    assert sm.assign_legacy_strategy("legacy_alpha", 202, manifest_path)["error"] == "not found"
```

- [ ] **Step 2: Run migration tests to verify failure**

Run: `py -3.12 -m pytest tests/test_strategy_ownership.py -v`

Expected: FAIL because no inventory, manifest, quarantine, assignment, or rollback API exists.

- [ ] **Step 3: Implement an explicit command and manifest safeguards**

```python
# tools/migrate_strategy_ownership.py
parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="command", required=True)
sub.add_parser("dry-run")
sub.add_parser("apply")
assign = sub.add_parser("assign")
assign.add_argument("filename")
assign.add_argument("--user-id", type=int, required=True)
rollback = sub.add_parser("rollback")
rollback.add_argument("--manifest", required=True)
```

The default manifest location is `data/security_migrations/strategy_ownership_manifest.json`. `dry-run` performs no writes. `apply` only moves root-level files whose META is not `builtin: true`; it rechecks the SHA-256 hash before moving, uses `Path.replace()` within the same strategy tree when possible, and appends a JSON event only after a successful move. `assign` requires an existing database user, rejects occupied destination names, moves the quarantined file to that exact user's directory, and appends an assignment event without reading or printing source code. `rollback` moves back only files whose current hash matches the recorded quarantine event and reports mismatches without changing them. No application import or startup path may call this utility.

- [ ] **Step 4: Run migration tests and static compilation**

Run: `py -3.12 -m pytest tests/test_strategy_ownership.py -v; py -3.12 -m compileall -q strategy_engine tools`

Expected: PASS; quarantine is explicit, idempotent, manifest-backed, and cannot silently assign or overwrite a user's file.

- [ ] **Step 5: Commit migration utility**

```bash
git add tools/migrate_strategy_ownership.py strategy_engine/strategy_manager.py tests/test_strategy_ownership.py
git commit -m "feat: add explicit legacy strategy quarantine migration"
```

### Task 4: Close Root-Only Factor and Enumeration Gaps

**Files:**
- Modify: `web/admin.py:480-597`
- Modify: `web/misc.py:34-85`
- Modify: `web/bridge.py:113-139`
- Modify: `web/factors.py:520-538`
- Modify: `tests/test_admin_permissions.py:190-300`
- Modify: `tests/test_authorization_isolation.py`

**Interfaces:**
- Consumes: existing `require_root_admin` and the not-found payload convention.
- Produces: root-only factor routes, indistinguishable bridge signal status responses, and 32-character factor-zoo task IDs.

- [ ] **Step 1: Write failing authorization and identifier tests**

```python
def test_non_root_admin_cannot_read_or_run_factor_mining(isolated_app):
    client = _logged_in_non_root_strategy_admin(isolated_app)
    response = client.get("/api/admin/factor-mining/latest")
    assert response.status_code == 403
    assert response.get_json()["error"] == "root_admin_required"


def test_foreign_signal_status_matches_unknown_signal(isolated_app, monkeypatch):
    foreign = client.get("/api/bridge/signals/91/status")
    unknown = client.get("/api/bridge/signals/92/status")
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.get_json() == unknown.get_json() == {"ok": False, "error": "not found"}


def test_factor_zoo_async_task_id_is_full_uuid_hex(isolated_app, monkeypatch):
    app, um, _main = isolated_app
    root_id = _create_root_admin(um, "factor_root")
    from web import factors

    class NonStartingThread:
        def __init__(self, target=None, args=(), daemon=None, name=None):
            self.target = target
            self.args = args
        def start(self):
            return None

    monkeypatch.setattr(factors.threading, "Thread", NonStartingThread)
    monkeypatch.setattr(factors.sm, "_full_market_universe_with_quotes", lambda max_codes: (["000001"], {}))
    client = app.test_client()
    _login_session(client, root_id, "factor_root", is_admin=True)
    response = client.post("/api/factors_zoo/compute_async", json={
        "factor_id": "volume_confirmation", "mode": "market_top", "max_codes": 1, "days": 20,
    })
    assert response.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{32}", response.get_json()["task_id"])
```

- [ ] **Step 2: Run the focused tests to verify failure**

Run: `py -3.12 -m pytest tests/test_admin_permissions.py tests/test_authorization_isolation.py -v`

Expected: FAIL because non-root `strategy_manage` administrators are currently permitted, bridge signal status distinguishes 200/403 cases, and factor-zoo IDs are truncated to 12 characters.

- [ ] **Step 3: Make minimal authorization and identifier changes**

```python
# web/admin.py: apply this decorator to every factor-mining and factor-watchlist route
@require_root_admin

# web/misc.py: import the existing decorator and apply it to each /api/vibe_activations/* route
from app_state import require_auth, require_root_admin, get_user_state, _add_log, _deduct

@require_root_admin

# web/bridge.py
if not sig or sig.get("user_id") != uid:
    return jsonify({"ok": False, "error": "not found"}), 404

# web/factors.py
task_id = _uuid.uuid4().hex
```

Remove no factor logic and retain the existing factor task owner checks. Update tests that previously expected `strategy_manage` to grant factor access.

- [ ] **Step 4: Run focused authorization tests**

Run: `py -3.12 -m pytest tests/test_admin_permissions.py tests/test_authorization_isolation.py -v`

Expected: PASS; only the configured root administrator receives factor operations, and missing/foreign signals are indistinguishable.

- [ ] **Step 5: Commit the authorization closure**

```bash
git add web/admin.py web/misc.py web/bridge.py web/factors.py tests/test_admin_permissions.py tests/test_authorization_isolation.py
git commit -m "fix: restrict factor operations to root admin"
```

### Task 5: End-to-End Regression Verification and Security Review

**Files:**
- Modify only if failures require a scoped correction: files from Tasks 1-4.
- Verify: `tests/test_strategy_ownership.py`, `tests/test_authorization_isolation.py`, `tests/test_admin_permissions.py`, `tests/test_strategy_progress_api.py`, and directly affected strategy tests.

**Interfaces:**
- Consumes: completed Tasks 1-4.
- Produces: evidence that source compiles and the affected authorization surface remains regression-tested.

- [ ] **Step 1: Run the complete focused regression suite**

Run: `py -3.12 -m pytest tests/test_strategy_ownership.py tests/test_authorization_isolation.py tests/test_admin_permissions.py tests/test_strategy_progress_api.py tests/test_strategy_manager_quote_cache.py tests/test_daban_auto_execution.py tests/test_paper_api_params.py -v`

Expected: PASS with no test relying on global custom-strategy visibility.

- [ ] **Step 2: Compile the changed application modules**

Run: `py -3.12 -m compileall -q web strategy_engine tools app_state.py`

Expected: exit code 0.

- [ ] **Step 3: Review the staged diff for accidental sensitive changes**

Run: `git diff HEAD -- web/strategies.py web/misc.py web/admin.py web/bridge.py web/factors.py strategy_engine/strategy_manager.py strategy_engine/scan_service.py tools/migrate_strategy_ownership.py tests`

Expected: no `.env`, credential, cloud, database schema, user-data, or unrelated trade-logic changes.

- [ ] **Step 4: Commit any final narrowly scoped correction**

```bash
git add web/strategies.py web/misc.py web/admin.py web/bridge.py web/factors.py strategy_engine/strategy_manager.py strategy_engine/scan_service.py tools/migrate_strategy_ownership.py tests/test_strategy_ownership.py tests/test_authorization_isolation.py tests/test_admin_permissions.py tests/test_strategy_progress_api.py
git commit -m "test: verify strategy ownership isolation"
```

Only create this commit when a verification failure required an actual correction; otherwise leave Tasks 1-4 commits unchanged.
