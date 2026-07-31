# -*- coding: utf-8 -*-
"""Persistent live feedback for relay factor mining.

This module records what happened after a factor candidate left research:
AI decision, realtime/auction quote snapshot, risk blocks, paper execution and
closed paper-trade outcome.  The mining pipeline can then penalize patterns that
look good in historical bars but repeatedly fail in the user's execution path.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FEEDBACK_PATH = ROOT / "data" / "research" / "relay_factor" / "live_feedback.json"
MAX_DECISIONS = 3000
MAX_PAPER_TRADES = 3000
_feedback_guard = threading.RLock()


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today_text() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json_unlocked(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
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


@contextmanager
def _feedback_file_lock():
    lock_path = FEEDBACK_PATH.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _feedback_guard:
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


def _base_store() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": None,
        "decisions": [],
        "paper_trades": [],
        "daily_snapshots": [],
    }


def _load_store_unlocked() -> dict[str, Any]:
    data = _read_json(FEEDBACK_PATH, _base_store())
    if not isinstance(data, dict):
        data = _base_store()
    data.setdefault("version", 1)
    data.setdefault("decisions", [])
    data.setdefault("paper_trades", [])
    data.setdefault("daily_snapshots", [])
    return data


def load_store() -> dict[str, Any]:
    with _feedback_file_lock():
        return _load_store_unlocked()


def _write_json(path: Path, data: dict) -> None:
    if Path(path).resolve() != FEEDBACK_PATH.resolve():
        _write_json_unlocked(path, data)
        return
    with _feedback_file_lock():
        _write_json_unlocked(path, data)


def _clean_code(value: Any) -> str:
    return str(value or "").strip().upper().split(".")[0]


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return round(float(value), 4)
    except Exception:
        return None


def _quote_metrics(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or not snapshot:
        return {}
    price = _to_float(snapshot.get("price"))
    open_price = _to_float(snapshot.get("open"))
    limit_up = _to_float(snapshot.get("limit_up"))
    bid1_price = _to_float(snapshot.get("bid1_price"))
    ask1_price = _to_float(snapshot.get("ask1_price"))
    spread_pct = None
    if bid1_price and ask1_price and price:
        spread_pct = round((ask1_price - bid1_price) / price * 100.0, 4)
    bid1_amount = None
    if bid1_price and _to_float(snapshot.get("bid1_vol")) is not None:
        bid1_amount = round(bid1_price * float(snapshot.get("bid1_vol") or 0) * 100.0, 2)
    near_limit_up = bool(limit_up and price and price >= limit_up * 0.999)
    return {
        "price": price,
        "open": open_price,
        "change_pct": _to_float(snapshot.get("change_pct")),
        "amount": _to_float(snapshot.get("amount")),
        "limit_up": limit_up,
        "limit_down": _to_float(snapshot.get("limit_down")),
        "bid1_price": bid1_price,
        "ask1_price": ask1_price,
        "bid1_vol": _to_float(snapshot.get("bid1_vol")),
        "ask1_vol": _to_float(snapshot.get("ask1_vol")),
        "bid1_amount": bid1_amount,
        "spread_pct": spread_pct,
        "near_limit_up": near_limit_up,
        "server_time": str(snapshot.get("server_time") or ""),
    }


def _decision_record_key(record: dict[str, Any]) -> str:
    identity = {
        key: record.get(key)
        for key in (
            "generated_at",
            "decision_id",
            "decision_date",
            "code",
            "user_id",
            "strategy_key",
            "model_version",
            "experiment_id",
            "model_instance_id",
            "watchlist_id",
            "source_run_id",
            "trade_date",
            "action",
            "risk_ok",
            "blocked",
            "executed",
            "price",
            "confidence",
            "reason",
        )
    }
    return hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def record_ai_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Append AI watchlist decision feedback and quote snapshots."""
    if not isinstance(decision, dict):
        return {"ok": False, "recorded": 0}
    results = decision.get("results") if isinstance(decision.get("results"), list) else []
    if not results:
        return {"ok": False, "recorded": 0}
    generated_at = str(decision.get("generated_at") or _now_text())
    records: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        source = result.get("source") if isinstance(result.get("source"), dict) else {}
        code = _clean_code(result.get("code") or source.get("ts_code"))
        if not code:
            continue
        snapshot = result.get("auction_snapshot") or result.get("quote_snapshot") or {}
        action = str(result.get("action") or "").upper()
        blocked = str(result.get("blocked") or "")

        def identity_value(key: str):
            value = source.get(key)
            if value in (None, ""):
                value = result.get(key)
            if value in (None, ""):
                value = decision.get(key)
            return value

        records.append(
            {
                "recorded_at": _now_text(),
                "generated_at": generated_at,
                "decision_id": str(result.get("id") or ""),
                "decision_date": generated_at[:10] if len(generated_at) >= 10 else _today_text(),
                "code": code,
                "name": str(result.get("name") or ""),
                "user_id": identity_value("user_id"),
                "strategy_key": str(identity_value("strategy_key") or ""),
                "model_version": str(identity_value("model_version") or ""),
                "experiment_id": str(identity_value("experiment_id") or ""),
                "model_instance_id": str(identity_value("model_instance_id") or ""),
                "watchlist_id": str(identity_value("watchlist_id") or ""),
                "source_run_id": str(identity_value("source_run_id") or ""),
                "trade_date": str(source.get("trade_date") or ""),
                "candidate_rank": source.get("candidate_rank"),
                "candidate_tier": str(source.get("candidate_tier") or ""),
                "live_readiness_status": str(source.get("live_readiness_status") or ""),
                "live_readiness_score": source.get("live_readiness_score"),
                "action": action,
                "risk_ok": bool(result.get("risk_ok")),
                "blocked": blocked,
                "executed": bool(result.get("executed")),
                "price": _to_float(result.get("price")),
                "confidence": _to_float(result.get("confidence")),
                "reason": str(result.get("reason") or ""),
                "quote": _quote_metrics(snapshot if isinstance(snapshot, dict) else {}),
            }
        )
    if not records:
        return {"ok": False, "recorded": 0}
    with _feedback_file_lock():
        store = _load_store_unlocked()
        decisions = [x for x in store.get("decisions", []) if isinstance(x, dict)]
        existing_keys = set()
        for existing in decisions:
            decision_key = str(existing.get("decision_key") or "")
            if not decision_key:
                decision_key = _decision_record_key(existing)
                existing["decision_key"] = decision_key
            existing_keys.add(decision_key)
        inserted = []
        for record in records:
            decision_key = _decision_record_key(record)
            record["decision_key"] = decision_key
            if decision_key in existing_keys:
                continue
            existing_keys.add(decision_key)
            inserted.append(record)
        decisions.extend(inserted)
        store["decisions"] = decisions[-MAX_DECISIONS:]
        store["updated_at"] = _now_text()
        _write_json_unlocked(FEEDBACK_PATH, store)
    return {
        "ok": True,
        "recorded": len(inserted),
        "idempotent": not inserted,
        "path": str(FEEDBACK_PATH),
    }


def sync_paper_trades(days: int = 180) -> dict[str, Any]:
    """Mirror recent closed paper trades and optional attribution into feedback."""
    try:
        from broker import paper_broker
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}
    db_path = Path(getattr(paper_broker, "DB_PATH", ROOT / "data" / "paper_trading.db"))
    if not db_path.exists():
        return {"ok": True, "synced": 0, "reason": "paper db not found"}
    since = (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
    rows: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(str(db_path), timeout=10)
        con.row_factory = sqlite3.Row
        table_columns = {row[1] for row in con.execute("PRAGMA table_info(paper_trades)")}
        attribution_columns = (
            "strategy_key",
            "model_version",
            "experiment_id",
            "model_instance_id",
            "watchlist_id",
            "source_run_id",
        )
        attribution_select = [
            column if column in table_columns else f"'' AS {column}"
            for column in attribution_columns
        ]
        owner_user_select = (
            "owner_user_id"
            if "owner_user_id" in table_columns
            else "NULL AS owner_user_id"
        )
        select_columns = [
            "id", "user_id", owner_user_select, "code", "name", "buy_time", "sell_time",
            "buy_price", "sell_price", "shares", "pnl", "pnl_pct",
            "hold_days", "sell_reason", *attribution_select,
        ]
        for row in con.execute(
            f"SELECT {','.join(select_columns)} FROM paper_trades "
            "WHERE sell_time IS NOT NULL AND buy_time>=? ORDER BY id DESC LIMIT ?",
            (since, MAX_PAPER_TRADES),
        ).fetchall():
            data = dict(row)
            paper_account_user_id = int(data.get("user_id") or 0)
            owner_user_id = int(data.get("owner_user_id") or 0)
            rows.append({
                "trade_id": int(data.get("id") or 0),
                "user_id": owner_user_id or paper_account_user_id,
                "paper_account_user_id": paper_account_user_id,
                "code": _clean_code(data.get("code")),
                "name": str(data.get("name") or ""),
                "buy_time": str(data.get("buy_time") or ""),
                "sell_time": str(data.get("sell_time") or ""),
                "buy_date": str(data.get("buy_time") or "")[:10],
                "sell_date": str(data.get("sell_time") or "")[:10],
                "buy_price": _to_float(data.get("buy_price")),
                "sell_price": _to_float(data.get("sell_price")),
                "shares": int(data.get("shares") or 0),
                "pnl": _to_float(data.get("pnl")),
                "pnl_pct": _to_float(data.get("pnl_pct")),
                "hold_days": int(data.get("hold_days") or 0),
                "sell_reason": str(data.get("sell_reason") or ""),
                "strategy_key": str(data.get("strategy_key") or ""),
                "model_version": str(data.get("model_version") or ""),
                "experiment_id": str(data.get("experiment_id") or ""),
                "model_instance_id": str(data.get("model_instance_id") or ""),
                "watchlist_id": str(data.get("watchlist_id") or ""),
                "source_run_id": str(data.get("source_run_id") or ""),
            })
        con.close()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}
    with _feedback_file_lock():
        store = _load_store_unlocked()
        store["paper_trades"] = rows[-MAX_PAPER_TRADES:]
        store["updated_at"] = _now_text()
        _write_json_unlocked(FEEDBACK_PATH, store)
    return {"ok": True, "synced": len(rows), "path": str(FEEDBACK_PATH)}


def _avg(values: list[float]) -> float | None:
    clean = [float(x) for x in values if x is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 4)


def build_code_feedback(
    days: int = 90,
    user_id: int | None = None,
    strategy_key: str | None = None,
    model_version: str | None = None,
    experiment_id: str | None = None,
    model_instance_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate recent live feedback keyed by six-digit code."""
    store = load_store()
    cutoff = (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
    out: dict[str, dict[str, Any]] = {}

    def user_matches(row: dict[str, Any]) -> bool:
        if user_id is None:
            return True
        try:
            return int(row.get("user_id")) == int(user_id)
        except (TypeError, ValueError):
            return False

    def bucket(code: str) -> dict[str, Any]:
        code = _clean_code(code)
        if code not in out:
            out[code] = {
                "decision_count": 0,
                "buy_vote_count": 0,
                "risk_ok_count": 0,
                "blocked_count": 0,
                "executed_count": 0,
                "near_limit_count": 0,
                "paper_trade_count": 0,
                "paper_win_count": 0,
                "paper_pnl_values": [],
                "last_feedback_at": "",
                "last_blocked": "",
            }
        return out[code]

    for row in store.get("decisions", []):
        if not isinstance(row, dict):
            continue
        if not user_matches(row):
            continue
        if strategy_key is not None and row.get("strategy_key") != strategy_key:
            continue
        if model_version is not None and row.get("model_version") != model_version:
            continue
        if experiment_id is not None and row.get("experiment_id") != experiment_id:
            continue
        if model_instance_id is not None and row.get("model_instance_id") != model_instance_id:
            continue
        day = str(row.get("decision_date") or row.get("generated_at") or "")[:10]
        if day and day < cutoff:
            continue
        code = _clean_code(row.get("code"))
        if not code:
            continue
        b = bucket(code)
        b["decision_count"] += 1
        if str(row.get("action") or "").upper() == "BUY":
            b["buy_vote_count"] += 1
        if row.get("risk_ok"):
            b["risk_ok_count"] += 1
        if row.get("blocked"):
            b["blocked_count"] += 1
            b["last_blocked"] = str(row.get("blocked") or "")[:120]
        if row.get("executed"):
            b["executed_count"] += 1
        quote = row.get("quote") if isinstance(row.get("quote"), dict) else {}
        if quote.get("near_limit_up"):
            b["near_limit_count"] += 1
        b["last_feedback_at"] = max(str(b.get("last_feedback_at") or ""), str(row.get("recorded_at") or ""))

    for row in store.get("paper_trades", []):
        if not isinstance(row, dict):
            continue
        if not user_matches(row):
            continue
        if strategy_key is not None and row.get("strategy_key") != strategy_key:
            continue
        if model_version is not None and row.get("model_version") != model_version:
            continue
        if experiment_id is not None and row.get("experiment_id") != experiment_id:
            continue
        if model_instance_id is not None and row.get("model_instance_id") != model_instance_id:
            continue
        day = str(row.get("buy_date") or row.get("sell_date") or "")[:10]
        if day and day < cutoff:
            continue
        code = _clean_code(row.get("code"))
        if not code:
            continue
        b = bucket(code)
        b["paper_trade_count"] += 1
        pnl_pct = _to_float(row.get("pnl_pct"))
        if pnl_pct is not None:
            b["paper_pnl_values"].append(pnl_pct)
            if pnl_pct > 0:
                b["paper_win_count"] += 1
        b["last_feedback_at"] = max(str(b.get("last_feedback_at") or ""), str(row.get("sell_time") or ""))

    for code, b in out.items():
        decisions = max(1, int(b["decision_count"]))
        buys = max(1, int(b["buy_vote_count"]))
        paper_count = int(b["paper_trade_count"])
        b["blocked_rate"] = round(int(b["blocked_count"]) / decisions, 4)
        b["execution_rate"] = round(int(b["executed_count"]) / buys, 4)
        b["risk_ok_rate"] = round(int(b["risk_ok_count"]) / decisions, 4)
        b["near_limit_rate"] = round(int(b["near_limit_count"]) / decisions, 4)
        b["paper_avg_pnl_pct"] = _avg(b.pop("paper_pnl_values", []))
        b["paper_win_rate"] = round(int(b["paper_win_count"]) / paper_count, 4) if paper_count else None
        b["sample_count"] = int(b["decision_count"]) + paper_count
    return out


def feedback_health() -> dict[str, Any]:
    store = load_store()
    return {
        "ok": True,
        "updated_at": store.get("updated_at"),
        "decision_count": len(store.get("decisions", [])),
        "paper_trade_count": len(store.get("paper_trades", [])),
        "path": str(FEEDBACK_PATH),
    }
