# -*- coding: utf-8 -*-
"""因子观察池 + AI 决策落盘（领域服务）。

从 web/admin.py 搬出来的原因（A-2 分层倒置）：
这些是领域逻辑，不是 HTTP 关注点，却一直住在 Flask 蓝图里，于是
analytics/auto_factor_scheduler.py 必须在运行时 `from web import admin` 反向拉起
web 层才能复用；而且它调的是**下划线私有函数**，等于把私有名当成了跨层 API——
admin.py 任何一次重构都会悄悄砸坏后台调度器。

现在 web/admin.py 与 auto_factor_scheduler 都从本模块导入。
admin.py 保留同名薄别名，既不改路由行为，也不破坏既有的 monkeypatch 测试。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from web._params import _int_value


def factor_mining_root() -> Path:
    """研究产物根目录。

    **运行时**读取 app_state.BASE_DIR 而不是 import 时绑定：
    测试要把它指到 tmp_path，import 时绑定就没法改了。
    """
    import app_state

    return (app_state.BASE_DIR / "data" / "research" / "relay_factor").resolve()


def _read_json_file(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def factor_watchlist_path() -> Path:
    return (factor_mining_root() / "watchlist.json").resolve()


def clean_watchlist_text(value, limit: int = 200) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    return text[:limit]


def resolve_watchlist_name(ts_code: str, raw_name=None) -> str:
    code = str(ts_code or "").split(".")[0]
    cleaned = clean_watchlist_text(raw_name, 40)
    if cleaned and cleaned not in {ts_code, code}:
        return cleaned
    try:
        from analytics import data_manager as dm

        resolved = clean_watchlist_text(dm.get_stock_name(code), 40)
    except Exception:
        resolved = ""
    return resolved if resolved and resolved not in {ts_code, code} else (cleaned or code)


def enrich_watchlist_identity(raw: dict) -> dict:
    item = dict(raw)
    ts_code = clean_watchlist_text(item.get("ts_code") or item.get("code"), 32).upper()
    code = ts_code.split(".")[0]
    item["ts_code"] = ts_code
    item["code"] = code
    item["name"] = resolve_watchlist_name(ts_code, item.get("name"))
    return item


def load_factor_watchlist() -> dict:
    data = _read_json_file(factor_watchlist_path(), {})
    if not isinstance(data, dict):
        data = {}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return {
        "updated_at": data.get("updated_at"),
        "items": [enrich_watchlist_identity(item) for item in items if isinstance(item, dict)],
    }


def save_factor_watchlist(data: dict) -> None:
    path = factor_watchlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_watchlist_json(value, limit: int = 12000):
    if not isinstance(value, (dict, list)):
        return {} if isinstance(value, dict) else None
    try:
        text = json.dumps(value, ensure_ascii=False)
        if len(text) > limit:
            return {} if isinstance(value, dict) else []
        return json.loads(text)
    except Exception:
        return {} if isinstance(value, dict) else []


def watchlist_item_id(item: dict) -> str:
    raw = f"{item.get('trade_date') or 'unknown'}-{item.get('ts_code') or item.get('code') or ''}"
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "-", raw).strip("-")
    return safe or datetime.now().strftime("manual-%Y%m%d%H%M%S")


def sanitize_watchlist_item(raw: dict, run_id: str | None = None,
                            strategy_name: str | None = None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    ts_code = clean_watchlist_text(raw.get("ts_code") or raw.get("code"), 32).upper()
    if not ts_code:
        return None
    code = ts_code.split(".")[0]
    item = {
        "ts_code": ts_code,
        "code": code,
        "name": resolve_watchlist_name(ts_code, raw.get("name")),
        "trade_date": clean_watchlist_text(raw.get("trade_date"), 20),
        "source_run_id": clean_watchlist_text(run_id or raw.get("source_run_id") or raw.get("run_id"), 40),
        "strategy_name": clean_watchlist_text(strategy_name or raw.get("strategy_name") or "AI因子挖掘候选", 60),
        "candidate_rank": _int_value(raw.get("candidate_rank"), None),
        "candidate_tier": clean_watchlist_text(raw.get("candidate_tier"), 20),
        "relay_score": raw.get("relay_score"),
        "confidence": raw.get("confidence"),
        "live_confidence": raw.get("live_confidence"),
        "live_readiness_score": raw.get("live_readiness_score"),
        "live_readiness_status": clean_watchlist_text(raw.get("live_readiness_status"), 20),
        "live_readiness_label": clean_watchlist_text(raw.get("live_readiness_label"), 40),
        "live_ready_score": raw.get("live_ready_score"),
        "score_percentile": raw.get("score_percentile"),
        "position_pct": raw.get("position_pct"),
        "stop_loss": raw.get("stop_loss"),
        "take_profit": raw.get("take_profit"),
        "hold_days": _int_value(raw.get("hold_days"), None),
        "positive_factors": raw.get("positive_factors") if isinstance(raw.get("positive_factors"), list) else [],
        "risk_flags": raw.get("risk_flags") if isinstance(raw.get("risk_flags"), list) else [],
        "live_warnings": raw.get("live_warnings") if isinstance(raw.get("live_warnings"), list) else [],
        "live_warning_keys": raw.get("live_warning_keys") if isinstance(raw.get("live_warning_keys"), list) else [],
        "factor_explain": clean_watchlist_json(raw.get("factor_explain")) or {},
        "market_regime": clean_watchlist_text(raw.get("market_regime"), 40),
        "market_data_status": clean_watchlist_text(raw.get("market_data_status"), 40),
        "status": clean_watchlist_text(raw.get("status") or "pending", 20),
        "note": clean_watchlist_text(raw.get("note"), 240),
    }
    item["id"] = watchlist_item_id(item)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    item["created_at"] = clean_watchlist_text(raw.get("created_at") or now, 30)
    item["updated_at"] = now
    return item


def import_factor_candidates_to_watchlist(candidates: list, run_id: str | None = None,
                                          strategy_name: str | None = None) -> dict:
    imported = []
    for candidate in candidates if isinstance(candidates, list) else []:
        item = sanitize_watchlist_item(candidate, run_id=run_id, strategy_name=strategy_name)
        if item is not None:
            imported.append(item)
    if not imported:
        return {"ok": False, "imported": 0, "total": len(load_factor_watchlist()["items"])}

    data = load_factor_watchlist()
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
    save_factor_watchlist(data)
    return {"ok": True, "imported": len(imported), "total": len(data["items"]), "items": data["items"]}


def ai_trade_decision_path() -> Path:
    return (factor_mining_root() / "ai_trade_decision.json").resolve()


def load_ai_trade_decision() -> dict:
    data = _read_json_file(ai_trade_decision_path(), {})
    return data if isinstance(data, dict) else {}


def with_ai_trade_quote_snapshots(decision: dict) -> dict:
    if not isinstance(decision, dict):
        return {}
    results = decision.get("results")
    if not isinstance(results, list) or not results:
        return decision
    missing = []
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("auction_snapshot") or item.get("quote_snapshot"):
            continue
        code = str(item.get("code") or "").strip()
        if code:
            missing.append(code)
    if not missing:
        return decision
    try:
        from analytics import data_gateway as dg

        quotes = dg.get_realtime_quotes(sorted(set(missing))) or []
    except Exception:
        quotes = []
    qmap = {str(q.get("code") or ""): q for q in quotes if isinstance(q, dict)}
    if not qmap:
        return decision
    payload = dict(decision)
    patched_results = []
    for item in results:
        if not isinstance(item, dict):
            patched_results.append(item)
            continue
        row = dict(item)
        if not row.get("auction_snapshot") and not row.get("quote_snapshot"):
            try:
                from analytics import ai_trader as ai_trader_engine

                snap = ai_trader_engine._quote_snapshot(qmap.get(str(row.get("code") or "")))
            except Exception:
                snap = None
            if snap:
                row["auction_snapshot"] = snap
        patched_results.append(row)
    payload["results"] = patched_results
    return payload


def save_ai_trade_decision(data: dict) -> None:
    payload = with_ai_trade_quote_snapshots(data)
    try:
        from analytics import live_factor_feedback

        feedback_result = live_factor_feedback.record_ai_decision(payload)
        if isinstance(payload, dict):
            payload = {**payload, "live_feedback_record": feedback_result}
    except Exception as exc:
        if isinstance(payload, dict):
            payload = {**payload, "live_feedback_record": {"ok": False, "error": str(exc)[:160]}}
    path = ai_trade_decision_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def factor_watchlist_candidates(max_candidates: int = 6, latest_only: bool = True) -> list[dict]:
    data = load_factor_watchlist()
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


def apply_ai_trade_decision_to_watchlist(decision: dict) -> None:
    results = decision.get("results") if isinstance(decision, dict) else None
    if not isinstance(results, list):
        return
    data = load_factor_watchlist()
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
        item["note"] = clean_watchlist_text(f"AI {action}: {reason}", 240)
        item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        changed = True
    if changed:
        save_factor_watchlist(data)
