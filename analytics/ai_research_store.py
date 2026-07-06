# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "ai_research.db"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _require_as_of(data: dict) -> str:
    as_of = str(data.get("as_of_date") or "").strip()
    if not as_of:
        raise ValueError("as_of_date is required to prevent future leakage")
    return as_of


def _json(data: dict) -> str:
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True)


def _loads(text: str | None) -> dict:
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(DB_PATH))


def init_research_db() -> None:
    with _conn() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_feature_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER NOT NULL,
                as_of_date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                features_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER NOT NULL,
                as_of_date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                action TEXT,
                confidence REAL,
                decision_json TEXT NOT NULL,
                features_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_trade_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER NOT NULL,
                as_of_date TEXT NOT NULL,
                code TEXT NOT NULL,
                outcome_json TEXT NOT NULL,
                pnl_pct REAL,
                created_at TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_ai_decisions_uid_code ON ai_decisions(uid, code, as_of_date)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ai_outcomes_uid_code ON ai_trade_outcomes(uid, code, as_of_date)")
        con.commit()


def record_feature_snapshot(uid: int, as_of_date: str, code: str, name: str, features: dict) -> int:
    _require_as_of({"as_of_date": as_of_date})
    init_research_db()
    with _conn() as con:
        cur = con.execute(
            """
            INSERT INTO ai_feature_snapshots(uid, as_of_date, code, name, features_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (int(uid), str(as_of_date), str(code), str(name or code), _json(features), _now()),
        )
        con.commit()
        return int(cur.lastrowid)


def record_decision(uid: int, decision: dict) -> int:
    as_of = _require_as_of(decision)
    init_research_db()
    features = decision.get("features") if isinstance(decision.get("features"), dict) else {}
    with _conn() as con:
        cur = con.execute(
            """
            INSERT INTO ai_decisions(uid, as_of_date, code, name, action, confidence, decision_json, features_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(uid),
                as_of,
                str(decision.get("code") or ""),
                str(decision.get("name") or decision.get("code") or ""),
                str(decision.get("action") or ""),
                _to_float(decision.get("confidence"), 0.0),
                _json(decision),
                _json(features),
                _now(),
            ),
        )
        con.commit()
        return int(cur.lastrowid)


def record_trade_outcome(uid: int, code: str, outcome: dict) -> int:
    as_of = _require_as_of(outcome)
    init_research_db()
    with _conn() as con:
        cur = con.execute(
            """
            INSERT INTO ai_trade_outcomes(uid, as_of_date, code, outcome_json, pnl_pct, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (int(uid), as_of, str(code), _json(outcome), _to_float(outcome.get("pnl_pct"), 0.0), _now()),
        )
        con.commit()
        return int(cur.lastrowid)


def find_similar_memories(uid: int, code: str, features: dict, limit: int = 3) -> list[dict]:
    init_research_db()
    with _conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT * FROM ai_decisions
            WHERE uid=? AND code=?
            ORDER BY as_of_date DESC, id DESC
            LIMIT 50
            """,
            (int(uid), str(code)),
        ).fetchall()
        outcomes = con.execute(
            """
            SELECT * FROM ai_trade_outcomes
            WHERE uid=? AND code=?
            ORDER BY as_of_date DESC, id DESC
            LIMIT 50
            """,
            (int(uid), str(code)),
        ).fetchall()

    scored = []
    for row in rows:
        row_features = _loads(row["features_json"])
        score = _similarity(features or {}, row_features)
        outcome = _nearest_outcome(row["as_of_date"], outcomes)
        scored.append(
            {
                "uid": int(row["uid"]),
                "as_of_date": row["as_of_date"],
                "code": row["code"],
                "name": row["name"],
                "similarity": score,
                "decision": _loads(row["decision_json"]),
                "features": row_features,
                "outcome": _loads(outcome["outcome_json"]) if outcome else {},
            }
        )
    scored.sort(key=lambda x: (x["similarity"], x["as_of_date"]), reverse=True)
    return scored[: max(0, int(limit or 0))]


def backtest_summary(uid: int, start: str = "", end: str = "", top: int = 100) -> dict:
    init_research_db()
    top = max(1, min(1000, int(top or 100)))
    where = ["uid=?"]
    params = [int(uid)]
    if start:
        where.append("as_of_date>=?")
        params.append(str(start))
    if end:
        where.append("as_of_date<=?")
        params.append(str(end))
    with _conn() as con:
        con.row_factory = sqlite3.Row
        decisions = con.execute(
            f"""
            SELECT * FROM ai_decisions
            WHERE {' AND '.join(where)}
            ORDER BY as_of_date ASC, id ASC
            LIMIT ?
            """,
            (*params, top),
        ).fetchall()
        outcomes = con.execute(
            """
            SELECT * FROM ai_trade_outcomes
            WHERE uid=?
            ORDER BY as_of_date ASC, id ASC
            """,
            (int(uid),),
        ).fetchall()

    rows = []
    pnl_values = []
    wins = 0
    for row in decisions:
        matching = [o for o in outcomes if o["code"] == row["code"] and str(o["as_of_date"]) >= str(row["as_of_date"])]
        outcome = matching[0] if matching else None
        pnl = _to_float(outcome["pnl_pct"], None) if outcome else None
        if pnl is not None:
            pnl_values.append(pnl)
            if pnl > 0:
                wins += 1
        rows.append(
            {
                "as_of_date": row["as_of_date"],
                "code": row["code"],
                "name": row["name"],
                "action": row["action"],
                "confidence": _to_float(row["confidence"], 0.0),
                "pnl_pct": pnl,
                "outcome": _loads(outcome["outcome_json"]) if outcome else {},
            }
        )
    evaluated = len(pnl_values)
    return {
        "uid": int(uid),
        "start": str(start or ""),
        "end": str(end or ""),
        "decision_count": len(rows),
        "evaluated_count": evaluated,
        "win_rate": round(wins / evaluated, 4) if evaluated else 0.0,
        "avg_pnl_pct": round(sum(pnl_values) / evaluated, 4) if evaluated else 0.0,
        "total_pnl_pct": round(sum(pnl_values), 4) if evaluated else 0.0,
        "max_loss_pct": round(min(pnl_values), 4) if evaluated else 0.0,
        "rows": rows,
    }


def _nearest_outcome(as_of_date: str, outcomes) -> sqlite3.Row | None:
    later = [o for o in outcomes if str(o["as_of_date"]) >= str(as_of_date)]
    return later[-1] if later else (outcomes[0] if outcomes else None)


def _similarity(a: dict, b: dict) -> float:
    keys = sorted(set(a) & set(b))
    if not keys:
        return 0.0
    dist = 0.0
    used = 0
    for key in keys:
        av = _to_float(a.get(key), None)
        bv = _to_float(b.get(key), None)
        if av is None or bv is None:
            continue
        scale = max(abs(av), abs(bv), 1.0)
        dist += ((av - bv) / scale) ** 2
        used += 1
    if not used:
        return 0.0
    return round(1.0 / (1.0 + math.sqrt(dist / used)), 6)


def _to_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
