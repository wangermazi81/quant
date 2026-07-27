"""管理员 API 蓝图：公告管理 / 日志 / 统计 / 用户与积分管理。"""
from __future__ import annotations

import os
import re
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, request, jsonify, session

from core import user_manager as um
from core import factor_watchlist as _fw
import app_security as sec
from analytics import datasource_config
from analytics import tushare_provider
from analytics import relay_factor_mining
from analytics import ai_trader as ai_trader_engine
from analytics import auto_factor_scheduler
from app_state import BASE_DIR, require_admin, require_admin_perm, require_root_admin
from web._params import _bounded_int_arg, _int_value

bp = Blueprint("admin", __name__)


_factor_mining_jobs: dict[str, dict] = {}
_factor_mining_jobs_lock = threading.RLock()
_factor_mining_active_task_id: str | None = None
_factor_mining_last_task_id: str | None = None


def _bounded_int_value(raw, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def _optional_int_arg(name: str) -> int | None:
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _safe_mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _cache_dir_status(relative_dir: str, date_pattern: str, date_format: str) -> dict:
    directory = (BASE_DIR / relative_dir).resolve()
    status = {
        "ok": False,
        "exists": directory.exists() and directory.is_dir(),
        "writable": False,
        "file_count": 0,
        "latest_date": None,
        "latest_updated_at": None,
    }
    if not status["exists"]:
        return status

    status["writable"] = os.access(str(directory), os.W_OK)
    candidates = []
    pattern = re.compile(date_pattern)
    try:
        for item in directory.iterdir():
            if not item.is_file():
                continue
            status["file_count"] += 1
            match = pattern.match(item.name)
            if not match:
                continue
            raw_date = match.group(1)
            try:
                date_text = datetime.strptime(raw_date, date_format).strftime("%Y-%m-%d")
            except Exception:
                continue
            candidates.append((date_text, item))
    except Exception:
        return status

    if candidates:
        latest_date, latest_file = max(candidates, key=lambda pair: pair[0])
        status["latest_date"] = latest_date
        status["latest_updated_at"] = _safe_mtime(latest_file)
    status["ok"] = bool(status["exists"])
    return status


def _database_status() -> dict:
    try:
        with um._conn() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"ok": True, "readable": True}
    except Exception:
        return {"ok": False, "readable": False}


def _data_dir_status() -> dict:
    directory = (BASE_DIR / "data").resolve()
    exists = directory.exists() and directory.is_dir()
    return {
        "ok": bool(exists),
        "exists": bool(exists),
        "writable": bool(exists and os.access(str(directory), os.W_OK)),
    }


def _factor_mining_root() -> Path:
    return (BASE_DIR / "data" / "research" / "relay_factor").resolve()


def _read_json_file(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _latest_factor_mining_dir() -> Path | None:
    root = _factor_mining_root()
    if not root.exists() or not root.is_dir():
        return None
    candidates = [item for item in root.iterdir() if item.is_dir() and (item / "walkforward_report.json").exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def _factor_mining_payload(run_dir: Path | None) -> dict:
    if run_dir is None:
        return {"ok": True, "found": False, "report": None, "run_id": None}
    report_path = run_dir / "walkforward_report.json"
    report = _read_json_file(report_path, {})
    candidates_path = run_dir / "latest_candidates.json"
    candidates = _read_json_file(candidates_path, report.get("latest_candidates", []))
    if isinstance(candidates, list):
        candidates = [_enrich_watchlist_identity(item) for item in candidates if isinstance(item, dict)]
    if isinstance(report, dict):
        report["latest_candidates"] = candidates if isinstance(candidates, list) else []
    return {
        "ok": True,
        "found": True,
        "run_id": run_dir.name,
        "updated_at": _safe_mtime(report_path),
        "report": report,
    }


def _factor_mining_job_snapshot(task: dict) -> dict:
    payload = {
        "task_id": task.get("task_id"),
        "run_id": task.get("run_id"),
        "status": task.get("status", "queued"),
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "error": task.get("error"),
    }
    if task.get("status") == "done":
        payload["report"] = task.get("report")
        payload["watchlist"] = task.get("watchlist")
    return payload


def _set_factor_mining_job(task_id: str, **changes) -> None:
    with _factor_mining_jobs_lock:
        task = _factor_mining_jobs.get(task_id)
        if task is not None:
            task.update(changes)


def _run_factor_mining_job(task_id: str, params: dict) -> None:
    global _factor_mining_active_task_id

    _set_factor_mining_job(task_id, status="running", started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        with auto_factor_scheduler._file_lock() as acquired:
            if not acquired:
                raise RuntimeError("已有自动或手动因子任务正在运行，请稍后重试")
            out_dir = _factor_mining_root() / params["run_id"]
            report = relay_factor_mining.run_relay_research(
                out_dir=out_dir,
                n_stocks=params["n_stocks"],
                days=params["days"],
                train_days=params["train_days"],
                test_days=params["test_days"],
                hold_days=params["hold_days"],
                top_k=params["top_k"],
                candidate_pool_size=params["candidate_pool_size"],
                include_market_signals=True,
                mining_rules=params["mining_rules"],
            )
            watchlist = _import_factor_candidates_to_watchlist(
                report.get("latest_candidates", []) if isinstance(report, dict) else [],
                run_id=params["run_id"],
                strategy_name="AI因子挖掘候选",
            )
        _set_factor_mining_job(
            task_id,
            status="done",
            report=report,
            watchlist=watchlist,
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception as exc:
        _set_factor_mining_job(
            task_id,
            status="error",
            error=str(exc),
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    finally:
        with _factor_mining_jobs_lock:
            if _factor_mining_active_task_id == task_id:
                _factor_mining_active_task_id = None


# ── 因子观察池 / AI 决策落盘：领域逻辑已搬到 core/factor_watchlist.py ──────
# 搬迁原因（A-2 分层倒置）：analytics/auto_factor_scheduler.py 原来必须在运行时
# `from web import admin` 反向拉起 web 层，还调的是这里的**下划线私有函数** ——
# 等于把私有名当跨层 API 用，本文件任何重构都会悄悄砸坏后台调度器。
# 这里保留同名薄别名：路由行为一字不变，既有 monkeypatch 这些名字的测试也照常有效。
_factor_watchlist_path = _fw.factor_watchlist_path
_load_factor_watchlist = _fw.load_factor_watchlist
_save_factor_watchlist = _fw.save_factor_watchlist
_factor_watchlist_transaction = _fw.factor_watchlist_transaction
_clean_watchlist_text = _fw.clean_watchlist_text
_resolve_watchlist_name = _fw.resolve_watchlist_name
_enrich_watchlist_identity = _fw.enrich_watchlist_identity
_clean_watchlist_json = _fw.clean_watchlist_json
_watchlist_item_id = _fw.watchlist_item_id
_sanitize_watchlist_item = _fw.sanitize_watchlist_item
_import_factor_candidates_to_watchlist = _fw.import_factor_candidates_to_watchlist
_ai_trade_decision_path = _fw.ai_trade_decision_path
_load_ai_trade_decision = _fw.load_ai_trade_decision
_with_ai_trade_quote_snapshots = _fw.with_ai_trade_quote_snapshots
_save_ai_trade_decision = _fw.save_ai_trade_decision
_factor_watchlist_candidates = _fw.factor_watchlist_candidates
_apply_ai_trade_decision_to_watchlist = _fw.apply_ai_trade_decision_to_watchlist


# ── 公告管理 ──────────────────────────────────────────────────
@bp.route("/api/admin/announcements", methods=["GET"])
@require_admin_perm("content_manage")
def admin_list_announcements():
    items = um.list_all_announcements()
    return jsonify({"ok": True, "announcements": items})


@bp.route("/api/admin/announcements", methods=["POST"])
@require_admin_perm("content_manage")
def admin_create_announcement():
    d = request.json or {}
    r = um.create_announcement(
        title      = d.get("title", ""),
        content    = d.get("content", ""),
        ann_type   = d.get("type", "info"),
        created_by = session.get("user_id", 0),
        expires_at = d.get("expires_at"),
    )
    return jsonify(r)


@bp.route("/api/admin/announcements/<int:ann_id>", methods=["PUT"])
@require_admin_perm("content_manage")
def admin_update_announcement(ann_id):
    d = request.json or {}
    r = um.update_announcement(ann_id, **{k: v for k, v in d.items()
                                          if k in ("title", "content", "type", "active", "expires_at")})
    return jsonify(r)


@bp.route("/api/admin/announcements/<int:ann_id>", methods=["DELETE"])
@require_admin_perm("content_manage")
def admin_delete_announcement(ann_id):
    return jsonify(um.delete_announcement(ann_id))


@bp.route("/api/admin/logs")
@require_admin_perm("security_audit")
def admin_get_logs():
    uid_filter = _optional_int_arg("uid")
    limit      = _bounded_int_arg("limit", 500, 1, 1000)
    logs = um.get_all_logs(uid=uid_filter, limit=limit)
    return jsonify({"ok": True, "logs": logs})


@bp.route("/api/admin/security/events")
@require_admin_perm("security_audit")
def admin_security_events():
    limit = _bounded_int_arg("limit", 200, 1, 1000)
    ip = (request.args.get("ip") or "").strip()
    event_type = (request.args.get("event_type") or "").strip()
    return jsonify({
        "ok": True,
        "events": sec.list_security_events(limit=limit, ip=ip, event_type=event_type),
    })


@bp.route("/api/admin/security/ip_blocks")
@require_admin_perm("security_audit")
def admin_security_ip_blocks():
    return jsonify({"ok": True, "blocks": sec.list_ip_blocks()})


@bp.route("/api/admin/security/ip_blocks", methods=["POST"])
@require_admin_perm("security_audit")
def admin_security_block_ip():
    d = request.json or {}
    ip = (d.get("ip") or "").strip()
    if not ip:
        return jsonify({"ok": False, "error": "ip required"}), 400
    seconds = _bounded_int_value(d.get("seconds"), 900, 1, 86400)
    reason = (d.get("reason") or "manual admin block").strip()
    sec.block_ip(ip, seconds=seconds, reason=reason)
    sec.record_security_event(
        "manual_ip_block",
        severity="medium",
        ip=ip,
        user_id=session.get("user_id"),
        detail=reason,
    )
    return jsonify({"ok": True, "blocks": sec.list_ip_blocks()})


@bp.route("/api/admin/security/ip_blocks/<path:ip>", methods=["DELETE"])
@require_admin_perm("security_audit")
def admin_security_release_ip(ip):
    ip = (ip or "").strip()
    sec.release_ip_block(ip)
    sec.record_security_event(
        "manual_ip_release",
        severity="medium",
        ip=ip,
        user_id=session.get("user_id"),
    )
    return jsonify({"ok": True, "blocks": sec.list_ip_blocks()})


# ── 用户与积分管理 ────────────────────────────────────────────
@bp.route("/api/admin/stats")
@require_admin_perm("dashboard_view")
def admin_stats():
    return jsonify({"ok": True, **um.stats()})


@bp.route("/api/admin/system/status")
@require_admin_perm("dashboard_view")
def admin_system_status():
    checks = {
        "database": _database_status(),
        "data_dir": _data_dir_status(),
        "review_archive": _cache_dir_status("data/review", r"^(\d{4}-\d{2}-\d{2})\.json$", "%Y-%m-%d"),
        "lhb_cache": _cache_dir_status("data/lhb", r"^lhb_(\d{8})\.json$", "%Y%m%d"),
        "market_snapshot": _cache_dir_status("data/market_snapshots", r"^(\d{8})\.json$", "%Y%m%d"),
    }
    return jsonify({
        "ok": True,
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "checks": checks,
    })


@bp.route("/api/admin/datasource/config", methods=["GET"])
@require_admin_perm("data_config")
def admin_datasource_config_get():
    cfg = datasource_config.get_config()
    return jsonify({"ok": True, **cfg})


@bp.route("/api/admin/datasource/config", methods=["POST"])
@require_admin_perm("data_config")
def admin_datasource_config_save():
    try:
        cfg = datasource_config.save_config(request.json or {})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "config": cfg})


@bp.route("/api/admin/datasource/test", methods=["POST"])
@require_admin_perm("data_config")
def admin_datasource_test():
    body = request.json or {}
    saved = datasource_config.get_internal_config()
    mode = (body.get("tushare_mode") or saved.get("tushare_mode") or "replay").strip().lower()
    token = (body.get("tushare_token") or "").strip()
    if not token:
        token = datasource_config.resolve_tushare_token(saved, mode=mode)
    result = tushare_provider.test_connection(token, mode=mode)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@bp.route("/api/admin/factor-mining/latest", methods=["GET"])
@require_root_admin
def admin_factor_mining_latest():
    return jsonify(_factor_mining_payload(_latest_factor_mining_dir()))


@bp.route("/api/admin/factor-mining/rules", methods=["GET"])
@require_root_admin
def admin_factor_mining_rules():
    rules = relay_factor_mining.normalize_mining_rules(None)
    return jsonify({"ok": True, "mining_rules": rules, "condition_explain": relay_factor_mining.condition_explain(rules)})


@bp.route("/api/admin/factor-mining/status", methods=["GET"])
@require_root_admin
def admin_factor_mining_status():
    task_id = str(request.args.get("task_id") or "").strip()
    with _factor_mining_jobs_lock:
        if not task_id:
            task_id = _factor_mining_active_task_id or _factor_mining_last_task_id or ""
        task = dict(_factor_mining_jobs.get(task_id) or {})
    if not task:
        return jsonify({"ok": True, "found": False, "status": "idle", "task_id": task_id or None})
    return jsonify({"ok": True, "found": True, **_factor_mining_job_snapshot(task)})


@bp.route("/api/admin/factor-mining/run", methods=["POST"])
@require_root_admin
def admin_factor_mining_run():
    global _factor_mining_active_task_id, _factor_mining_last_task_id

    body = request.json or {}
    n_stocks = _bounded_int_value(body.get("n_stocks"), 800, 5, 2000)
    days = _bounded_int_value(body.get("days"), 180, 60, 320)
    train_days = _bounded_int_value(body.get("train_days"), 60, 10, 160)
    test_days = _bounded_int_value(body.get("test_days"), 10, 5, 60)
    hold_days = _bounded_int_value(body.get("hold_days"), 2, 1, 10)
    top_k = _bounded_int_value(body.get("top_k"), 10, 1, 50)
    candidate_pool_size = _bounded_int_value(body.get("candidate_pool_size"), 100, top_k, 300)
    mining_rules = body.get("mining_rules") if isinstance(body.get("mining_rules"), dict) else None

    with _factor_mining_jobs_lock:
        active = _factor_mining_jobs.get(_factor_mining_active_task_id or "")
        if active and active.get("status") in {"queued", "running"}:
            return jsonify({"ok": True, "found": True, "reused": True, **_factor_mining_job_snapshot(active)}), 202

        task_id = uuid.uuid4().hex
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{task_id[:6]}"
        params = {
            "run_id": run_id,
            "n_stocks": n_stocks,
            "days": days,
            "train_days": train_days,
            "test_days": test_days,
            "hold_days": hold_days,
            "top_k": top_k,
            "candidate_pool_size": candidate_pool_size,
            "mining_rules": mining_rules,
        }
        task = {
            "task_id": task_id,
            "run_id": run_id,
            "status": "queued",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _factor_mining_jobs[task_id] = task
        _factor_mining_active_task_id = task_id
        _factor_mining_last_task_id = task_id
        while len(_factor_mining_jobs) > 20:
            oldest = next(iter(_factor_mining_jobs))
            if oldest == task_id:
                break
            _factor_mining_jobs.pop(oldest, None)

    threading.Thread(
        target=_run_factor_mining_job,
        args=(task_id, params),
        daemon=True,
        name=f"factor-mining-{task_id[:8]}",
    ).start()
    return jsonify({"ok": True, "found": True, "reused": False, **_factor_mining_job_snapshot(task)}), 202


# 可执行性排序权重：ready > watch > avoid。
# 市场弱势时（如 2026-07-24，情绪<50、状态门未通过）100 只候选里可能只有 5 只
# 是 ready，而它们按 relay_score 排会散落在 #1/#10/#12/#26/#32 —— 用户得人肉翻找。
# 后台真正要回答的问题是「明天能买哪几只」，所以先按可执行性分组，再按原排名。
_READY_ORDER = {"ready": 2, "watch": 1, "avoid": 0}


@bp.route("/api/admin/factor-watchlist", methods=["GET"])
@require_root_admin
def admin_factor_watchlist_get():
    """次日自选池。

    **默认只返回最新交易日**的候选：每次挖掘导入约 100 只，多跑几天就是几百条
    新旧混排，后台打开根本分不清今天该看哪些。真正要盯的永远是最近一个交易日。
    需要看别的日子传 ?date=YYYY-MM-DD，要全量传 ?all=1。
    """
    data = _load_factor_watchlist()
    all_items = data["items"]

    def _sort_key(item):
        return (str(item.get("trade_date") or ""),
                _READY_ORDER.get(str(item.get("live_readiness_status") or ""), 0),
                -int(item.get("candidate_rank") or 999999),
                str(item.get("code") or ""))

    dates = sorted({str(i.get("trade_date") or "") for i in all_items if i.get("trade_date")},
                   reverse=True)
    counts_by_date = {}
    counts_by_status = {}
    for i in all_items:
        counts_by_date[str(i.get("trade_date") or "")] = counts_by_date.get(str(i.get("trade_date") or ""), 0) + 1
        counts_by_status[str(i.get("status") or "pending")] = counts_by_status.get(str(i.get("status") or "pending"), 0) + 1

    want_all = str(request.args.get("all") or "").strip() in ("1", "true", "yes")
    want_date = _clean_watchlist_text(request.args.get("date"), 20)
    selected = ""
    if want_all:
        items = all_items
    else:
        selected = want_date if want_date in dates else (dates[0] if dates else "")
        items = [i for i in all_items if str(i.get("trade_date") or "") == selected] if selected else all_items

    # 当日可执行性分布（前端用来显示「今日 N 只可执行」并支持一键只看可执行）
    counts_by_readiness = {}
    for i in items:
        key = str(i.get("live_readiness_status") or "unknown")
        counts_by_readiness[key] = counts_by_readiness.get(key, 0) + 1

    want_readiness = _clean_watchlist_text(request.args.get("readiness"), 12)
    if want_readiness:
        items = [i for i in items if str(i.get("live_readiness_status") or "") == want_readiness]

    items = sorted(items, key=_sort_key, reverse=True)
    return jsonify({
        "ok": True,
        "updated_at": data.get("updated_at"),
        "items": items,
        "trade_dates": dates,
        "selected_date": selected,
        "total_all": len(all_items),
        "counts_by_date": counts_by_date,
        "counts_by_status": counts_by_status,
        "counts_by_readiness": counts_by_readiness,
        "readiness_filter": want_readiness,
    })


@bp.route("/api/admin/factor-watchlist/import", methods=["POST"])
@require_root_admin
def admin_factor_watchlist_import():
    body = request.json or {}
    candidates = body.get("candidates")
    run_id = _clean_watchlist_text(body.get("run_id"), 40)
    strategy_name = _clean_watchlist_text(body.get("strategy_name") or "AI因子挖掘候选", 60)
    if not isinstance(candidates, list):
        payload = _factor_mining_payload(_latest_factor_mining_dir())
        report = payload.get("report") if isinstance(payload, dict) else None
        candidates = report.get("latest_candidates", []) if isinstance(report, dict) else []
        run_id = run_id or _clean_watchlist_text(payload.get("run_id"), 40)

    result = _import_factor_candidates_to_watchlist(candidates, run_id=run_id, strategy_name=strategy_name)
    if not result.get("ok"):
        return jsonify({"ok": False, "error": "没有可导入的候选标的"}), 400
    return jsonify(result)


@bp.route("/api/admin/factor-watchlist/<item_id>", methods=["PATCH"])
@require_root_admin
def admin_factor_watchlist_update(item_id):
    body = request.json or {}
    allowed_status = {"pending", "ready", "bought", "skipped", "watch"}
    status = _clean_watchlist_text(body.get("status"), 20) if "status" in body else None
    if status is not None and status not in allowed_status:
        return jsonify({"ok": False, "error": "状态不合法"}), 400
    with _factor_watchlist_transaction() as data:
        found = False
        for item in data["items"]:
            if str(item.get("id")) != item_id:
                continue
            found = True
            if status is not None:
                item["status"] = status
            if "note" in body:
                item["note"] = _clean_watchlist_text(body.get("note"), 240)
            item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            break
        if not found:
            return jsonify({"ok": False, "error": "候选标的不存在"}), 404
    return jsonify({"ok": True})


@bp.route("/api/admin/factor-watchlist/<item_id>", methods=["DELETE"])
@require_root_admin
def admin_factor_watchlist_delete(item_id):
    with _factor_watchlist_transaction() as data:
        before = len(data["items"])
        data["items"] = [item for item in data["items"] if str(item.get("id")) != item_id]
        if len(data["items"]) == before:
            return jsonify({"ok": False, "error": "候选标的不存在"}), 404
        total = len(data["items"])
    return jsonify({"ok": True, "total": total})


@bp.route("/api/admin/ai-trade-decision/latest", methods=["GET"])
@require_root_admin
def admin_ai_trade_decision_latest():
    max_candidates = _bounded_int_arg("max_candidates", 6, 1, 20)
    candidates = _factor_watchlist_candidates(max_candidates=max_candidates, latest_only=True)
    return jsonify({
        "ok": True,
        "candidates": candidates,
        "decision": _with_ai_trade_quote_snapshots(_load_ai_trade_decision()),
    })


@bp.route("/api/admin/ai-trade-decision/run", methods=["POST"])
@require_root_admin
def admin_ai_trade_decision_run():
    body = request.json or {}
    max_candidates = _bounded_int_value(body.get("max_candidates"), 6, 1, 20)
    execute_paper = bool(body.get("execute_paper"))
    latest_only = body.get("latest_only") is not False
    candidates = body.get("candidates") if isinstance(body.get("candidates"), list) else None
    if candidates is None:
        candidates = _factor_watchlist_candidates(max_candidates=max_candidates, latest_only=latest_only)
    if not candidates:
        return jsonify({"ok": False, "error": "no factor watchlist candidates"}), 400
    result = ai_trader_engine.get_engine(session["user_id"]).decide_watchlist_candidates(
        candidates,
        max_candidates=max_candidates,
        execute_paper=execute_paper,
    )
    if not result.get("ok"):
        return jsonify(result), 400
    payload = {
        **result,
        "candidate_source": "factor_watchlist",
        "latest_only": latest_only,
    }
    _save_ai_trade_decision(payload)
    _apply_ai_trade_decision_to_watchlist(payload)
    return jsonify(payload)


@bp.route("/api/admin/ai-trade-decision/auto/status", methods=["GET"])
@require_root_admin
def admin_ai_trade_auto_status():
    return jsonify(auto_factor_scheduler.get_status())


@bp.route("/api/admin/ai-trade-decision/auto/run", methods=["POST"])
@require_root_admin
def admin_ai_trade_auto_run():
    body = request.json or {}
    job = str(body.get("job") or "full_after_close")
    result = auto_factor_scheduler.run_job(job, force=True)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@bp.route("/api/admin/permissions/catalog")
@require_admin
def admin_permissions_catalog():
    return jsonify({"ok": True, "permissions": um.admin_permission_catalog()})


@bp.route("/api/admin/users")
@require_admin_perm("user_manage")
def admin_users():
    page   = _bounded_int_arg("page", 1, 1, 100000)
    search = request.args.get("search", "")
    return jsonify({"ok": True, **um.list_users(page, 20, search)})


@bp.route("/api/admin/users/admins")
@require_root_admin
def admin_list_admin_users():
    return jsonify({"ok": True, **um.list_admin_users()})


@bp.route("/api/admin/users/<int:uid>/points", methods=["POST"])
@require_admin_perm("points_manage")
def admin_add_points(uid):
    d      = request.json or {}
    amount = _int_value(d.get("amount"))
    if amount is None:
        return jsonify({"ok": False, "error": "invalid amount"}), 400
    desc   = d.get("description", "管理员充值")
    r = um.add_points(uid, amount, desc)
    return jsonify(r)


@bp.route("/api/admin/analytics")
@require_admin_perm("dashboard_view")
def admin_analytics():
    """运营统计聚合（真实数据）。前端工作台/使用统计页的图表数据源。"""
    try:
        days = max(1, min(31, int(request.args.get("days", 7))))
    except (TypeError, ValueError):
        days = 7
    try:
        months = max(1, min(24, int(request.args.get("months", 6))))
    except (TypeError, ValueError):
        months = 6
    return jsonify(um.admin_analytics(days=days, months=months))


@bp.route("/api/admin/users/<int:uid>", methods=["PUT"])
@require_admin_perm("user_manage")
def admin_update_user(uid):
    """编辑用户资料（用户名/邮箱）。root 管理员的资料只允许其本人改。"""
    d = request.json or {}
    target = um.get_user(uid)
    if not target:
        return jsonify({"ok": False, "error": "用户不存在"}), 404
    if um.is_root_admin_user(target) and uid != session.get("user_id"):
        return jsonify({"ok": False, "error": "不能编辑最高管理员资料"}), 403
    return jsonify(um.update_user_profile(
        uid, username=d.get("username"), email=d.get("email")))


@bp.route("/api/admin/users/<int:uid>/active", methods=["POST"])
@require_admin_perm("user_manage")
def admin_set_active(uid):
    active = request.json.get("active", True)
    if uid == session.get("user_id") and not active:
        return jsonify({"ok": False, "error": "不能禁用自己的账号"})
    return jsonify(um.set_active(uid, active))


@bp.route("/api/admin/users/<int:uid>/admin", methods=["POST"])
@require_root_admin
def admin_set_admin(uid):
    d = request.json or {}
    is_admin = d.get("is_admin", False)
    r = um.set_admin(uid, is_admin)
    if r.get("ok") and is_admin:
        perms = d.get("permissions")
        if perms is not None:
            pr = um.set_admin_permissions(uid, perms, granted_by=session.get("user_id"))
            if not pr.get("ok"):
                return jsonify(pr), 400
            r["permissions"] = pr.get("permissions", [])
    return jsonify(r)


@bp.route("/api/admin/users/<int:uid>/permissions", methods=["GET"])
@require_root_admin
def admin_get_user_permissions(uid):
    user = um.get_user(uid)
    if not user:
        return jsonify({"ok": False, "error": "user not found"}), 404
    return jsonify({
        "ok": True,
        "user_id": uid,
        "is_root_admin": um.is_root_admin_user(user),
        "permissions": um.get_admin_permissions(uid),
    })


@bp.route("/api/admin/users/<int:uid>/permissions", methods=["POST"])
@require_root_admin
def admin_save_user_permissions(uid):
    d = request.json or {}
    r = um.set_admin_permissions(uid, d.get("permissions", []), granted_by=session.get("user_id"))
    return (jsonify(r), 200 if r.get("ok") else 400)


@bp.route("/api/admin/transactions")
@require_admin_perm("points_manage")
def admin_transactions():
    page = _bounded_int_arg("page", 1, 1, 100000)
    return jsonify({"ok": True, **um.all_point_transactions(page, 30)})


@bp.route("/api/admin/users/<int:uid>/reset_password", methods=["POST"])
@require_admin_perm("user_manage")
def admin_reset_password(uid):
    new_pw = (request.json or {}).get("new_password", "")
    return jsonify(um.admin_reset_password(uid, new_pw))


# ── 找回密码申请（后台审批重置）────────────────────────────────
@bp.route("/api/admin/reset_requests", methods=["GET"])
@require_admin_perm("user_manage")
def admin_list_reset_requests():
    """列出找回密码申请（默认仅 pending）。"""
    status = request.args.get("status", "pending")
    return jsonify({"ok": True, "requests": um.list_reset_requests(status),
                    "pending": um.count_pending_reset_requests()})


@bp.route("/api/admin/reset_requests/<int:req_id>/resolve", methods=["POST"])
@require_admin_perm("user_manage")
def admin_resolve_reset_request(req_id):
    """审批通过：生成临时密码并重置，返回临时密码供管理员转告用户。"""
    return jsonify(um.resolve_reset_request(req_id, session.get("user_id", 0)))


@bp.route("/api/admin/reset_requests/<int:req_id>/reject", methods=["POST"])
@require_admin_perm("user_manage")
def admin_reject_reset_request(req_id):
    """驳回申请（不重置密码）。"""
    return jsonify(um.reject_reset_request(req_id, session.get("user_id", 0)))


@bp.route("/api/admin/users/<int:uid>/set_points", methods=["POST"])
@require_admin_perm("points_manage")
def admin_set_points(uid):
    """把用户积分设为指定值（可增可减），区别于 /points 的增量充值。"""
    d     = request.json or {}
    value = d.get("value")
    desc  = d.get("description", "管理员修改积分")
    return jsonify(um.set_points(uid, value, desc))


@bp.route("/api/admin/users/<int:uid>", methods=["DELETE"])
@require_admin_perm("user_manage")
def admin_delete_user(uid):
    """删除用户（不可删自己）。"""
    if uid == session.get("user_id"):
        return jsonify({"ok": False, "error": "不能删除自己的账号"})
    return jsonify(um.delete_user(uid))


# ── AI/功能调用积分设置 ───────────────────────────────────────
@bp.route("/api/admin/point_costs", methods=["GET"])
@require_admin_perm("points_manage")
def admin_get_point_costs():
    """各功能调用积分消耗（0=免费）。"""
    return jsonify({"ok": True, "costs": um.get_point_costs(),
                    "labels": um.POINT_COST_LABELS})


@bp.route("/api/admin/point_costs", methods=["POST"])
@require_admin_perm("points_manage")
def admin_set_point_costs():
    """保存积分消耗配置：内存即时生效 + data/point_costs.json 持久化。"""
    d = request.json or {}
    return jsonify(um.set_point_costs(d.get("costs", d)))


# ── 邀请码管理（邀请制注册）──────────────────────────────────
@bp.route("/api/admin/invite_codes", methods=["GET"])
@require_admin_perm("invite_manage")
def admin_list_invites():
    """列出全部邀请码（含生成者、名额、已用、奖励、状态）。"""
    return jsonify({"ok": True, "codes": um.list_invite_codes()})


@bp.route("/api/admin/invite_codes", methods=["POST"])
@require_admin_perm("invite_manage")
def admin_create_invite():
    """生成邀请码：max_uses 名额 / reward 每邀请奖励积分 / note 备注 / expires_at 可选过期。"""
    d = request.json or {}
    r = um.create_invite_code(
        creator_uid   = session.get("user_id", 0),
        max_uses      = d.get("max_uses", 1),
        reward        = d.get("reward"),
        note          = d.get("note", ""),
        is_admin_code = True,
        expires_at    = d.get("expires_at"),
    )
    return jsonify(r)


@bp.route("/api/admin/invite_codes/<int:cid>", methods=["DELETE"])
@require_admin_perm("invite_manage")
def admin_revoke_invite(cid):
    """作废邀请码（停止可用，不删历史）。"""
    return jsonify(um.revoke_invite_code(cid))


# ── 更新日志管理 ──────────────────────────────────────────────
from core import changelog_manager as clm

@bp.route("/api/admin/changelog", methods=["GET"])
@require_admin_perm("content_manage")
def admin_list_changelog():
    """管理员列出全部更新日志（含未发布）。"""
    items = clm.list_changelog(published_only=False)
    return jsonify({"ok": True, "items": items})


@bp.route("/api/admin/changelog", methods=["POST"])
@require_admin_perm("content_manage")
def admin_create_changelog():
    """管理员创建更新日志。"""
    d = request.json or {}
    title = (d.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "标题不能为空"}), 400
    cid = clm.add_changelog(
        title=title,
        content=d.get("content", ""),
        version=d.get("version", ""),
        change_type=d.get("change_type", "notice"),
        created_by=session.get("user_id", 0),
        is_published=d.get("is_published", 1),
    )
    return jsonify({"ok": True, "id": cid})


@bp.route("/api/admin/changelog/<int:cid>", methods=["PUT"])
@require_admin_perm("content_manage")
def admin_update_changelog(cid):
    """管理员更新日志。"""
    d = request.json or {}
    ok = clm.update_changelog(cid, **{k: v for k, v in d.items()
                                       if k in ("version", "title", "content", "change_type", "is_published")})
    return jsonify({"ok": ok})


@bp.route("/api/admin/changelog/<int:cid>", methods=["DELETE"])
@require_admin_perm("content_manage")
def admin_delete_changelog(cid):
    """管理员删除更新日志。"""
    return jsonify({"ok": clm.delete_changelog(cid)})
