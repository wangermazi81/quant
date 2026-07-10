"""管理员 API 蓝图：公告管理 / 日志 / 统计 / 用户与积分管理。"""
from __future__ import annotations

import os
import re
import json
from datetime import datetime
from pathlib import Path

from flask import Blueprint, request, jsonify, session

import user_manager as um
import app_security as sec
import datasource_config
import tushare_provider
from analytics import relay_factor_mining
from analytics import ai_trader as ai_trader_engine
from app_state import BASE_DIR, require_admin, require_admin_perm, require_root_admin

bp = Blueprint("admin", __name__)


def _bounded_int_arg(name: str, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def _bounded_int_value(raw, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def _int_value(raw, default: int | None = None) -> int | None:
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


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
    if isinstance(report, dict):
        report["latest_candidates"] = candidates if isinstance(candidates, list) else []
    return {
        "ok": True,
        "found": True,
        "run_id": run_dir.name,
        "updated_at": _safe_mtime(report_path),
        "report": report,
    }


def _factor_watchlist_path() -> Path:
    return (_factor_mining_root() / "watchlist.json").resolve()


def _load_factor_watchlist() -> dict:
    data = _read_json_file(_factor_watchlist_path(), {})
    if not isinstance(data, dict):
        data = {}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return {
        "updated_at": data.get("updated_at"),
        "items": [item for item in items if isinstance(item, dict)],
    }


def _save_factor_watchlist(data: dict) -> None:
    path = _factor_watchlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _clean_watchlist_text(value, limit: int = 200) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    return text[:limit]


def _watchlist_item_id(item: dict) -> str:
    raw = f"{item.get('trade_date') or 'unknown'}-{item.get('ts_code') or item.get('code') or ''}"
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "-", raw).strip("-")
    return safe or datetime.now().strftime("manual-%Y%m%d%H%M%S")


def _sanitize_watchlist_item(raw: dict, run_id: str | None = None, strategy_name: str | None = None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    ts_code = _clean_watchlist_text(raw.get("ts_code") or raw.get("code"), 32).upper()
    if not ts_code:
        return None
    code = ts_code.split(".")[0]
    item = {
        "ts_code": ts_code,
        "code": code,
        "name": _clean_watchlist_text(raw.get("name"), 40),
        "trade_date": _clean_watchlist_text(raw.get("trade_date"), 20),
        "source_run_id": _clean_watchlist_text(run_id or raw.get("source_run_id") or raw.get("run_id"), 40),
        "strategy_name": _clean_watchlist_text(strategy_name or raw.get("strategy_name") or "AI因子挖掘候选", 60),
        "candidate_rank": _int_value(raw.get("candidate_rank"), None),
        "candidate_tier": _clean_watchlist_text(raw.get("candidate_tier"), 20),
        "relay_score": raw.get("relay_score"),
        "confidence": raw.get("confidence"),
        "score_percentile": raw.get("score_percentile"),
        "position_pct": raw.get("position_pct"),
        "stop_loss": raw.get("stop_loss"),
        "take_profit": raw.get("take_profit"),
        "hold_days": _int_value(raw.get("hold_days"), None),
        "positive_factors": raw.get("positive_factors") if isinstance(raw.get("positive_factors"), list) else [],
        "risk_flags": raw.get("risk_flags") if isinstance(raw.get("risk_flags"), list) else [],
        "market_regime": _clean_watchlist_text(raw.get("market_regime"), 40),
        "market_data_status": _clean_watchlist_text(raw.get("market_data_status"), 40),
        "status": _clean_watchlist_text(raw.get("status") or "pending", 20),
        "note": _clean_watchlist_text(raw.get("note"), 240),
    }
    item["id"] = _watchlist_item_id(item)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    item["created_at"] = _clean_watchlist_text(raw.get("created_at") or now, 30)
    item["updated_at"] = now
    return item


def _import_factor_candidates_to_watchlist(
    candidates: list,
    run_id: str | None = None,
    strategy_name: str | None = None,
) -> dict:
    imported = []
    for candidate in candidates if isinstance(candidates, list) else []:
        item = _sanitize_watchlist_item(candidate, run_id=run_id, strategy_name=strategy_name)
        if item is not None:
            imported.append(item)
    if not imported:
        return {"ok": False, "imported": 0, "total": len(_load_factor_watchlist()["items"])}

    data = _load_factor_watchlist()
    by_id = {str(item.get("id")): item for item in data["items"] if item.get("id")}
    for item in imported:
        previous = by_id.get(item["id"], {})
        if previous.get("note") and not item.get("note"):
            item["note"] = previous["note"]
        if previous.get("status") and previous.get("status") != "pending":
            item["status"] = previous["status"]
        item["created_at"] = previous.get("created_at") or item["created_at"]
        by_id[item["id"]] = item
    data["items"] = list(by_id.values())
    _save_factor_watchlist(data)
    return {"ok": True, "imported": len(imported), "total": len(data["items"]), "items": data["items"]}


def _ai_trade_decision_path() -> Path:
    return (_factor_mining_root() / "ai_trade_decision.json").resolve()


def _load_ai_trade_decision() -> dict:
    data = _read_json_file(_ai_trade_decision_path(), {})
    return data if isinstance(data, dict) else {}


def _save_ai_trade_decision(data: dict) -> None:
    path = _ai_trade_decision_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _factor_watchlist_candidates(max_candidates: int = 6, latest_only: bool = True) -> list[dict]:
    data = _load_factor_watchlist()
    allowed_status = {"pending", "ready", "watch"}
    items = [item for item in data["items"] if str(item.get("status") or "pending") in allowed_status]
    if latest_only and items:
        latest_date = max(str(item.get("trade_date") or "") for item in items)
        if latest_date:
            items = [item for item in items if str(item.get("trade_date") or "") == latest_date]

    def _rank_value(item: dict) -> int:
        try:
            return int(item.get("candidate_rank") or 999999)
        except (TypeError, ValueError):
            return 999999

    items.sort(key=lambda item: (str(item.get("trade_date") or ""), -_rank_value(item)), reverse=True)
    return items[:max_candidates]


def _apply_ai_trade_decision_to_watchlist(decision: dict) -> None:
    results = decision.get("results") if isinstance(decision, dict) else None
    if not isinstance(results, list):
        return
    data = _load_factor_watchlist()
    by_id = {str(item.get("id")): item for item in data["items"] if item.get("id")}
    changed = False
    for result in results:
        if not isinstance(result, dict):
            continue
        source = result.get("source") if isinstance(result.get("source"), dict) else {}
        wid = str(source.get("watchlist_id") or "")
        item = by_id.get(wid)
        if not item:
            continue
        action = str(result.get("action") or "")
        if result.get("executed"):
            item["status"] = "bought"
        elif action == "BUY" and result.get("risk_ok"):
            item["status"] = "ready"
        elif action in {"HOLD", "SKIP"}:
            item["status"] = "watch"
        elif result.get("blocked"):
            item["status"] = "skipped"
        reason = result.get("reason") or result.get("blocked") or action
        item["note"] = _clean_watchlist_text(f"AI {action}: {reason}", 240)
        item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        changed = True
    if changed:
        _save_factor_watchlist(data)


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
@require_admin_perm("strategy_manage")
def admin_factor_mining_latest():
    return jsonify(_factor_mining_payload(_latest_factor_mining_dir()))


@bp.route("/api/admin/factor-mining/rules", methods=["GET"])
@require_admin_perm("strategy_manage")
def admin_factor_mining_rules():
    rules = relay_factor_mining.normalize_mining_rules(None)
    return jsonify({"ok": True, "mining_rules": rules, "condition_explain": relay_factor_mining.condition_explain(rules)})


@bp.route("/api/admin/factor-mining/run", methods=["POST"])
@require_admin_perm("strategy_manage")
def admin_factor_mining_run():
    body = request.json or {}
    n_stocks = _bounded_int_value(body.get("n_stocks"), 800, 5, 2000)
    days = _bounded_int_value(body.get("days"), 180, 60, 320)
    train_days = _bounded_int_value(body.get("train_days"), 60, 10, 160)
    test_days = _bounded_int_value(body.get("test_days"), 10, 5, 60)
    hold_days = _bounded_int_value(body.get("hold_days"), 2, 1, 10)
    top_k = _bounded_int_value(body.get("top_k"), 10, 1, 50)
    candidate_pool_size = _bounded_int_value(body.get("candidate_pool_size"), 100, top_k, 300)
    mining_rules = body.get("mining_rules") if isinstance(body.get("mining_rules"), dict) else None

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = _factor_mining_root() / run_id
    try:
        report = relay_factor_mining.run_relay_research(
            out_dir=out_dir,
            n_stocks=n_stocks,
            days=days,
            train_days=train_days,
            test_days=test_days,
            hold_days=hold_days,
            top_k=top_k,
            candidate_pool_size=candidate_pool_size,
            include_market_signals=True,
            mining_rules=mining_rules,
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "run_id": run_id}), 500
    watchlist = _import_factor_candidates_to_watchlist(
        report.get("latest_candidates", []) if isinstance(report, dict) else [],
        run_id=run_id,
        strategy_name="AI因子挖掘候选",
    )
    return jsonify({"ok": True, "run_id": run_id, "report": report, "watchlist": watchlist})


@bp.route("/api/admin/factor-watchlist", methods=["GET"])
@require_admin_perm("strategy_manage")
def admin_factor_watchlist_get():
    data = _load_factor_watchlist()
    items = sorted(
        data["items"],
        key=lambda item: (str(item.get("trade_date") or ""), -int(item.get("candidate_rank") or 999999), str(item.get("code") or "")),
        reverse=True,
    )
    return jsonify({"ok": True, "updated_at": data.get("updated_at"), "items": items})


@bp.route("/api/admin/factor-watchlist/import", methods=["POST"])
@require_admin_perm("strategy_manage")
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
@require_admin_perm("strategy_manage")
def admin_factor_watchlist_update(item_id):
    body = request.json or {}
    allowed_status = {"pending", "ready", "bought", "skipped", "watch"}
    data = _load_factor_watchlist()
    found = False
    for item in data["items"]:
        if str(item.get("id")) != item_id:
            continue
        found = True
        if "status" in body:
            status = _clean_watchlist_text(body.get("status"), 20)
            if status not in allowed_status:
                return jsonify({"ok": False, "error": "状态不合法"}), 400
            item["status"] = status
        if "note" in body:
            item["note"] = _clean_watchlist_text(body.get("note"), 240)
        item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        break
    if not found:
        return jsonify({"ok": False, "error": "候选标的不存在"}), 404
    _save_factor_watchlist(data)
    return jsonify({"ok": True})


@bp.route("/api/admin/factor-watchlist/<item_id>", methods=["DELETE"])
@require_admin_perm("strategy_manage")
def admin_factor_watchlist_delete(item_id):
    data = _load_factor_watchlist()
    before = len(data["items"])
    data["items"] = [item for item in data["items"] if str(item.get("id")) != item_id]
    if len(data["items"]) == before:
        return jsonify({"ok": False, "error": "候选标的不存在"}), 404
    _save_factor_watchlist(data)
    return jsonify({"ok": True, "total": len(data["items"])})


@bp.route("/api/admin/ai-trade-decision/latest", methods=["GET"])
@require_root_admin
def admin_ai_trade_decision_latest():
    max_candidates = _bounded_int_arg("max_candidates", 6, 1, 20)
    candidates = _factor_watchlist_candidates(max_candidates=max_candidates, latest_only=True)
    return jsonify({
        "ok": True,
        "candidates": candidates,
        "decision": _load_ai_trade_decision(),
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
import changelog_manager as clm

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
