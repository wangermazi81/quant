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
import logging
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

from web._params import _int_value


_watchlist_guard = threading.RLock()
WATCHLIST_BUSINESS_KEY_VERSION = 2


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


def _atomic_write_json_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def factor_watchlist_path() -> Path:
    return (factor_mining_root() / "watchlist.json").resolve()


def clean_watchlist_text(value, limit: int = 200) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    return text[:limit]


def _strict_optional_bool(value) -> bool | None:
    return value if isinstance(value, bool) else None


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


def _load_factor_watchlist_unlocked() -> dict:
    data = _read_json_file(factor_watchlist_path(), {})
    if not isinstance(data, dict):
        data = {}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    latest_scope = (
        dict(data["latest_scope"])
        if isinstance(data.get("latest_scope"), dict)
        else None
    )
    normalized_items = []
    migration_required = False
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = enrich_watchlist_identity(raw)
        current_business_key = watchlist_business_key(item)
        if (
            item.get("business_key") != current_business_key
            or item.get("business_key_version") != WATCHLIST_BUSINESS_KEY_VERSION
        ):
            migration_required = True
        item["business_key"] = current_business_key
        item["business_key_version"] = WATCHLIST_BUSINESS_KEY_VERSION
        if not item.get("id"):
            item["id"] = watchlist_item_id(item)
            migration_required = True
        normalized_items.append(item)
    normalized = {
        "updated_at": data.get("updated_at"),
        "latest_scope": latest_scope,
        "items": normalized_items,
    }
    if migration_required:
        _save_factor_watchlist_unlocked(normalized)
    return normalized


def _save_factor_watchlist_unlocked(data: dict) -> None:
    path = factor_watchlist_path()
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _atomic_write_json_file(path, data)


@contextmanager
def _watchlist_file_lock():
    lock_path = factor_watchlist_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _watchlist_guard:
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_factor_watchlist() -> dict:
    with _watchlist_file_lock():
        return _load_factor_watchlist_unlocked()


def save_factor_watchlist(data: dict) -> None:
    with _watchlist_file_lock():
        _save_factor_watchlist_unlocked(data)


@contextmanager
def factor_watchlist_transaction():
    with _watchlist_file_lock():
        data = _load_factor_watchlist_unlocked()
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        yield data
        after = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if after != before:
            _save_factor_watchlist_unlocked(data)


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
    return f"wl-{uuid.uuid4().hex}"


def watchlist_business_key(item: dict) -> str:
    fields = (
        item.get("strategy_key"),
        item.get("model_version"),
        item.get("experiment_id"),
        item.get("model_instance_id"),
        item.get("source_run_id"),
        item.get("trade_date"),
        item.get("ts_code") or item.get("code"),
    )
    return "|".join(clean_watchlist_text(value, 120) for value in fields)


def _watchlist_scope(
    *,
    trade_date,
    source_run_id,
    strategy_key,
    model_version,
    experiment_id,
    model_instance_id,
    candidate_count,
) -> dict:
    date_text = clean_watchlist_text(trade_date, 20)
    if not date_text:
        raise ValueError("watchlist scope trade_date is required")
    try:
        count = int(candidate_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("watchlist scope candidate_count must be an integer") from exc
    if count < 0:
        raise ValueError("watchlist scope candidate_count must be non-negative")
    return {
        "trade_date": date_text,
        "source_run_id": clean_watchlist_text(source_run_id, 40),
        "strategy_key": clean_watchlist_text(strategy_key, 120),
        "model_version": clean_watchlist_text(model_version, 120),
        "experiment_id": clean_watchlist_text(experiment_id, 80),
        "model_instance_id": clean_watchlist_text(model_instance_id, 80),
        "candidate_count": count,
        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _latest_import_scope(items: list[dict]) -> dict | None:
    dated_items = [item for item in items if item.get("trade_date")]
    if not dated_items:
        return None
    latest_date = max(str(item.get("trade_date") or "") for item in dated_items)
    scoped = [
        item
        for item in dated_items
        if str(item.get("trade_date") or "") == latest_date
    ]
    latest_run_id = next(
        (
            str(item.get("source_run_id") or "")
            for item in reversed(scoped)
            if item.get("source_run_id")
        ),
        "",
    )
    if latest_run_id:
        scoped = [
            item
            for item in scoped
            if str(item.get("source_run_id") or "") == latest_run_id
        ]
    identity = scoped[-1]
    return _watchlist_scope(
        trade_date=latest_date,
        source_run_id=latest_run_id,
        strategy_key=identity.get("strategy_key"),
        model_version=identity.get("model_version"),
        experiment_id=identity.get("experiment_id"),
        model_instance_id=identity.get("model_instance_id"),
        candidate_count=len(scoped),
    )


def record_factor_watchlist_scope(
    *,
    trade_date,
    source_run_id,
    strategy_key,
    model_version,
    experiment_id,
    model_instance_id,
    candidate_count,
) -> dict:
    scope = _watchlist_scope(
        trade_date=trade_date,
        source_run_id=source_run_id,
        strategy_key=strategy_key,
        model_version=model_version,
        experiment_id=experiment_id,
        model_instance_id=model_instance_id,
        candidate_count=candidate_count,
    )
    with factor_watchlist_transaction() as data:
        data["latest_scope"] = scope
    return {"ok": True, "latest_scope": scope}


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
        "strategy_key": clean_watchlist_text(raw.get("strategy_key"), 120),
        "model_version": clean_watchlist_text(raw.get("model_version"), 120),
        "experiment_id": clean_watchlist_text(raw.get("experiment_id"), 80),
        "model_instance_id": clean_watchlist_text(raw.get("model_instance_id"), 80),
        "validation_accepted": _strict_optional_bool(raw.get("validation_accepted")),
        "research_status": clean_watchlist_text(raw.get("research_status"), 20),
        "deployment_eligible": _strict_optional_bool(raw.get("deployment_eligible")),
        "research_only": _strict_optional_bool(raw.get("research_only")),
        "is_st": _strict_optional_bool(raw.get("is_st")),
        "st_status_known": _strict_optional_bool(raw.get("st_status_known")),
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
    item["business_key"] = watchlist_business_key(item)
    item["business_key_version"] = WATCHLIST_BUSINESS_KEY_VERSION
    item["id"] = watchlist_item_id(item)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    item["created_at"] = clean_watchlist_text(raw.get("created_at") or now, 30)
    item["updated_at"] = now
    return item


# 观察池只保留最近 N 个**交易日**的候选。
# 此前从不淘汰：每次挖掘导入 100 条，线上已堆到 700 条、7 个日期混排，
# 后台打开就是一片新旧混杂的列表，根本分不清哪些是今天该看的。
WATCHLIST_KEEP_DAYS = int(os.environ.get("FACTOR_WATCHLIST_KEEP_DAYS", "5") or "5")
# 已经产生过动作的条目（人工标记/AI 决策过）永不自动删除 —— 那是决策留痕，
# 删了就无法复盘「当初为什么买/为什么跳过」。
_STICKY_STATUS = {"bought", "skipped", "ready", "watch"}


def prune_factor_watchlist(data: dict, keep_days: int | None = None) -> dict:
    """按交易日裁剪观察池，返回裁剪后的 data（原地修改并返回）。

    规则：
      · 按 trade_date 倒序取最近 keep_days 个交易日，其余日期的条目丢弃；
      · 但 status 属于「已产生动作」的一律保留（决策留痕，不能删）；
      · 没有 trade_date 的脏条目视为最旧，优先丢。
    """
    keep_days = WATCHLIST_KEEP_DAYS if keep_days is None else keep_days
    items = data.get("items") or []
    dates = sorted({str(i.get("trade_date") or "") for i in items if i.get("trade_date")},
                   reverse=True)
    keep_dates = set(dates[:max(1, keep_days)])
    kept = [
        i for i in items
        if str(i.get("trade_date") or "") in keep_dates
        or str(i.get("status") or "") in _STICKY_STATUS
    ]
    dropped = len(items) - len(kept)
    if dropped:
        logger.info("[watchlist] 裁剪 %s 条过期候选（保留最近 %s 个交易日 + 已决策条目），剩 %s 条",
                    dropped, keep_days, len(kept))
    data["items"] = kept
    return data


def import_factor_candidates_to_watchlist(candidates: list, run_id: str | None = None,
                                          strategy_name: str | None = None) -> dict:
    imported = []
    for candidate in candidates if isinstance(candidates, list) else []:
        item = sanitize_watchlist_item(candidate, run_id=run_id, strategy_name=strategy_name)
        if item is not None:
            imported.append(item)
    if not imported:
        return {"ok": False, "imported": 0, "total": len(load_factor_watchlist()["items"])}

    with factor_watchlist_transaction() as data:
        by_id = {str(item.get("id")): item for item in data["items"] if item.get("id")}
        by_business_key = {
            str(item.get("business_key") or watchlist_business_key(item)): item
            for item in data["items"]
        }
        for item in imported:
            previous = by_business_key.get(item["business_key"], {})
            if previous.get("id"):
                item["id"] = previous["id"]
            if previous.get("note") and not item.get("note"):
                item["note"] = previous["note"]
            if previous.get("status") and previous.get("status") != "pending":
                item["status"] = previous["status"]
            if "market_gate_blocked" in (previous.get("live_warning_keys") or []):
                item["live_warning_keys"] = list(previous.get("live_warning_keys") or [])
                item["live_warnings"] = list(previous.get("live_warnings") or [])
                item["candidate_tier"] = "observe"
                item["position_pct"] = 0.0
                item["live_readiness_status"] = "avoid"
            item["created_at"] = previous.get("created_at") or item["created_at"]
            by_id[item["id"]] = item
            by_business_key[item["business_key"]] = item
        data["items"] = list(by_id.values())
        latest_scope = _latest_import_scope(imported)
        if latest_scope is not None:
            data["latest_scope"] = latest_scope
        prune_factor_watchlist(data)          # ★ 写盘前裁剪，防止无限堆积
        result = {
            "ok": True,
            "imported": len(imported),
            "total": len(data["items"]),
            "items": data["items"],
            "latest_scope": data.get("latest_scope"),
        }
    return result


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
    _atomic_write_json_file(path, payload)


def is_deployable_factor_candidate(item: dict) -> bool:
    try:
        position_pct = float(item.get("position_pct"))
    except (TypeError, ValueError):
        position_pct = 0.0
    return bool(
        clean_watchlist_text(item.get("strategy_key"), 120)
        and clean_watchlist_text(item.get("model_version"), 120)
        and clean_watchlist_text(item.get("experiment_id"), 80)
        and clean_watchlist_text(item.get("model_instance_id"), 80)
        and item.get("validation_accepted") is True
        and str(item.get("research_status") or "") == "validated"
        and item.get("deployment_eligible") is True
        and item.get("research_only") is False
        and item.get("st_status_known") is True
        and item.get("is_st") is False
        and position_pct > 0.0
    )


def _is_usable_ai_candidate(item: dict) -> bool:
    allowed_status = {"pending", "ready", "watch"}
    return bool(
        is_deployable_factor_candidate(item)
        and str(item.get("status") or "pending") in allowed_status
        and str(item.get("live_readiness_status") or "") in {"ready", "watch"}
        and "market_gate_blocked" not in (item.get("live_warning_keys") or [])
    )


def _expected_scope_trade_date() -> str:
    try:
        from analytics import data_manager as dm

        return clean_watchlist_text(dm.expected_completed_trade_date(), 20)
    except Exception:
        return ""


def _latest_watchlist_scope(
    items: list[dict],
    latest_scope: dict | None = None,
) -> list[dict]:
    if isinstance(latest_scope, dict) and latest_scope.get("trade_date"):
        expected_trade_date = _expected_scope_trade_date()
        if (
            not expected_trade_date
            or str(latest_scope.get("trade_date") or "") != expected_trade_date
        ):
            return []
        if int(latest_scope.get("candidate_count") or 0) == 0:
            return []
        scope_fields = (
            "trade_date",
            "source_run_id",
            "strategy_key",
            "model_version",
            "experiment_id",
            "model_instance_id",
        )
        return [
            item
            for item in items
            if all(
                not latest_scope.get(field)
                or str(item.get(field) or "") == str(latest_scope[field])
                for field in scope_fields
            )
        ]
    scoped = list(items)
    dates = [str(item.get("trade_date") or "") for item in scoped if item.get("trade_date")]
    if dates:
        latest_date = max(dates)
        scoped = [item for item in scoped if str(item.get("trade_date") or "") == latest_date]
    latest_run_id = next(
        (
            str(item.get("source_run_id") or "")
            for item in reversed(scoped)
            if item.get("source_run_id")
        ),
        "",
    )
    if latest_run_id:
        scoped = [
            item
            for item in scoped
            if str(item.get("source_run_id") or "") == latest_run_id
        ]
    return scoped


def factor_watchlist_candidates(max_candidates: int = 6, latest_only: bool = True) -> list[dict]:
    data = load_factor_watchlist()
    source_items = data["items"]
    if latest_only:
        source_items = _latest_watchlist_scope(
            source_items,
            data.get("latest_scope"),
        )
    items = [
        item
        for item in source_items
        if _is_usable_ai_candidate(item)
    ]

    def _rank_value(item: dict) -> int:
        try:
            return int(item.get("candidate_rank") or 999999)
        except (TypeError, ValueError):
            return 999999

    items.sort(key=lambda item: (str(item.get("trade_date") or ""), -_rank_value(item)), reverse=True)
    return items[:max_candidates]


def authoritative_watchlist_candidates(requested: list, max_candidates: int = 6) -> list[dict]:
    requested_ids = []
    for raw in requested if isinstance(requested, list) else []:
        if not isinstance(raw, dict):
            continue
        source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
        item_id = clean_watchlist_text(
            raw.get("id") or raw.get("watchlist_id") or source.get("watchlist_id"),
            80,
        )
        if item_id and item_id not in requested_ids:
            requested_ids.append(item_id)
    if not requested_ids:
        return []

    data = load_factor_watchlist()
    by_id = {
        str(item.get("id")): item
        for item in _latest_watchlist_scope(
            data["items"],
            data.get("latest_scope"),
        )
        if item.get("id")
    }
    resolved = []
    for item_id in requested_ids:
        item = by_id.get(item_id)
        if item and _is_usable_ai_candidate(item):
            resolved.append(dict(item))
        if len(resolved) >= max(1, int(max_candidates or 1)):
            break
    return resolved


def apply_ai_trade_decision_to_watchlist(decision: dict) -> None:
    results = decision.get("results") if isinstance(decision, dict) else None
    if not isinstance(results, list):
        return
    with factor_watchlist_transaction() as data:
        by_id = {str(item.get("id")): item for item in data["items"] if item.get("id")}
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
