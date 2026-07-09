# Tushare Datasource Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Tushare Pro as a selectable market-data provider without removing the current AkShare/Eastmoney/THS fallback stack.

**Architecture:** Introduce a small datasource configuration module, a Tushare adapter for daily qfq bars, admin APIs for read/save/test, and a compact admin UI section. Existing data functions keep their current contract and only consult the datasource config before running the existing fallback chain.

**Tech Stack:** Flask blueprints, plain HTML/JS admin page, pandas, optional `tushare` Python package, pytest.

## Global Constraints

- Default provider remains the current datasource stack.
- Tushare token is masked when returned to the browser and preserved when the admin leaves the field blank.
- Tushare failures fall back to the current datasource stack when fallback is enabled.
- Daily K-line output keeps columns `date, open, high, low, close, volume` and qfq adjustment semantics.

---

### Task 1: Datasource Config Module

**Files:**
- Create: `analytics/datasource_config.py`
- Test: `tests/test_datasource_config.py`

**Interfaces:**
- Produces: `get_config() -> dict`, `save_config(patch: dict) -> dict`, `mask_token(token: str) -> str`, `resolve_tushare_token(cfg: dict | None = None) -> str`

- [ ] Write tests for default config, masked token, blank token preservation, and provider validation.
- [ ] Implement JSON-backed config at `data/datasource_config.json`.
- [ ] Run `py -3.12 -m pytest tests/test_datasource_config.py -q`.

### Task 2: Tushare K-Line Adapter

**Files:**
- Create: `analytics/tushare_provider.py`
- Modify: `analytics/data_manager.py`
- Test: `tests/test_tushare_provider.py`, `tests/test_data_manager_tushare.py`

**Interfaces:**
- Produces: `fetch_kline_qfq(code: str, start: str, end: str, token: str | None = None) -> pandas.DataFrame | None`, `test_connection(token: str | None = None) -> dict`
- Consumes: `datasource_config.get_config()`

- [ ] Write tests for ts_code conversion and dataframe normalization.
- [ ] Write data_manager tests proving Tushare is used first when selected and fallback is preserved.
- [ ] Implement optional import of `tushare` so the app still starts before dependency install.
- [ ] Wire `_download_one` to try Tushare first when configured.
- [ ] Run targeted pytest commands.

### Task 3: Admin API and UI

**Files:**
- Modify: `web/admin.py`
- Modify: `admin.html`
- Test: `tests/test_admin_datasource_api.py`

**Interfaces:**
- Produces: `GET /api/admin/datasource/config`, `POST /api/admin/datasource/config`, `POST /api/admin/datasource/test`

- [ ] Write API tests for admin permissions, masked config, save, and test endpoint with monkeypatched provider.
- [ ] Add admin routes under `data_config` permission.
- [ ] Add `datasource-config` menu item and compact settings card.
- [ ] Add JS load/save/test functions.
- [ ] Run API/UI parse checks.

### Task 4: Dependency and Verification

**Files:**
- Modify: `requirements.txt`

- [ ] Add optional Tushare dependency line.
- [ ] Run targeted tests and Python compile checks.
- [ ] Parse `admin.html` inline script with Node.
- [ ] Commit, push to Gitee and GitHub, deploy with `deploy/server_update.sh`.
