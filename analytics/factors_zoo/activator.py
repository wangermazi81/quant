# -*- coding: utf-8 -*-
"""Vibe Alpha 因子激活管理器

激活后每 5 分钟（交易时段）自动调用因子计算 → 取 top K → 风险护栏过滤 → 下单。
通过 main.execute_buy 入口走，自动按用户 broker_type 分流 paper/live。

风险护栏（铁律，所有 enabled 因子共享）：
- 单笔仓位:    paper_config.cash * position_pct（默认 20%）
- 单日上限:    daily_limit 只（默认 3）
- 分数阈值:    截面 percentile rank > score_percentile（默认 0.8 即 top 20%）
- 单日熔断:    当日浮动盈亏 < stop_loss_pct（默认 -3%）→ halted=1 当天不再开仓
- 隔日重置:    每天首次 run_tick 时检查 reset_date 自动 reset 计数和 halted

每个用户最多激活 1 个 Vibe 因子（避免多因子冲突）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# DB 在 ths_auto_trader/data/users.db（和 user_manager 共享）
_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "users.db"
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(_DB_PATH, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def ensure_table():
    """惰性建表（首次调用激活 API 时跑）"""
    with _lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS vibe_activations (
                user_id           INTEGER PRIMARY KEY,
                factor_id         TEXT NOT NULL,
                mode              TEXT DEFAULT 'market_top',
                codes_json        TEXT DEFAULT '[]',
                max_codes         INTEGER DEFAULT 500,
                daily_limit       INTEGER DEFAULT 3,
                position_pct      REAL DEFAULT 0.2,
                score_percentile  REAL DEFAULT 0.8,
                stop_loss_pct     REAL DEFAULT -0.03,
                interval_sec      INTEGER DEFAULT 120,
                enabled           INTEGER DEFAULT 1,
                daily_count       INTEGER DEFAULT 0,
                daily_pnl_pct     REAL DEFAULT 0.0,
                halted            INTEGER DEFAULT 0,
                halt_reason       TEXT DEFAULT '',
                reset_date        TEXT DEFAULT '',
                last_run_at       TEXT DEFAULT '',
                last_signals_json TEXT DEFAULT '[]',
                created_at        TEXT DEFAULT (datetime('now'))
            )
        """)
        # 增量迁移：若旧表无 interval_sec 列，加上
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(vibe_activations)").fetchall()]
            if "interval_sec" not in cols:
                c.execute("ALTER TABLE vibe_activations ADD COLUMN interval_sec INTEGER DEFAULT 120")
        except Exception:
            pass
        c.commit()


def activate(uid: int, factor_id: str, params: dict | None = None) -> dict:
    """激活/更新用户的 Vibe 因子配置（每用户最多 1 个）"""
    ensure_table()
    p = params or {}
    mode             = p.get("mode", "market_top")
    codes            = p.get("codes", [])
    max_codes        = max(10, min(2000, int(p.get("max_codes", 500))))
    daily_limit      = max(1, min(10, int(p.get("daily_limit", 3))))
    position_pct     = max(0.01, min(0.5, float(p.get("position_pct", 0.2))))
    score_percentile = max(0.5, min(0.99, float(p.get("score_percentile", 0.8))))
    stop_loss_pct    = max(-0.2, min(-0.005, float(p.get("stop_loss_pct", -0.03))))
    interval_sec     = max(30, min(600, int(p.get("interval_sec", 120))))
    codes_json = json.dumps([str(c) for c in codes if c], ensure_ascii=False)
    today = date.today().isoformat()
    with _lock, _conn() as c:
        c.execute("""
            INSERT INTO vibe_activations (
                user_id, factor_id, mode, codes_json, max_codes,
                daily_limit, position_pct, score_percentile, stop_loss_pct, interval_sec,
                enabled, daily_count, daily_pnl_pct, halted, halt_reason, reset_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0.0, 0, '', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                factor_id=excluded.factor_id,
                mode=excluded.mode,
                codes_json=excluded.codes_json,
                max_codes=excluded.max_codes,
                daily_limit=excluded.daily_limit,
                position_pct=excluded.position_pct,
                score_percentile=excluded.score_percentile,
                stop_loss_pct=excluded.stop_loss_pct,
                interval_sec=excluded.interval_sec,
                enabled=1
        """, (uid, factor_id, mode, codes_json, max_codes,
              daily_limit, position_pct, score_percentile, stop_loss_pct, interval_sec, today))
        c.commit()
    return get(uid)


def deactivate(uid: int) -> dict:
    """关闭激活（保留记录，仅设 enabled=0）"""
    with _lock, _conn() as c:
        c.execute("UPDATE vibe_activations SET enabled=0 WHERE user_id=?", (uid,))
        c.commit()
    return get(uid)


def get(uid: int) -> dict | None:
    """获取用户的激活状态（None 表示从未激活过）"""
    ensure_table()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM vibe_activations WHERE user_id=?", (uid,)
        ).fetchone()
    return dict(row) if row else None


def list_all_enabled() -> list[dict]:
    """所有 enabled=1 的激活记录（调度器用）"""
    ensure_table()
    with _conn() as c:
        rows = c.execute("SELECT * FROM vibe_activations WHERE enabled=1").fetchall()
    return [dict(r) for r in rows]


def _reset_if_new_day(uid: int) -> None:
    """跨日自动重置 daily_count / daily_pnl / halted"""
    today = date.today().isoformat()
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT reset_date FROM vibe_activations WHERE user_id=?", (uid,)
        ).fetchone()
        if row and row["reset_date"] != today:
            c.execute("""
                UPDATE vibe_activations
                SET daily_count=0, daily_pnl_pct=0.0, halted=0, halt_reason='', reset_date=?
                WHERE user_id=?
            """, (today, uid))
            c.commit()
            logger.info(f"[vibe.activator] uid={uid} 跨日重置完成")


def _mark_halt(uid: int, reason: str) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE vibe_activations SET halted=1, halt_reason=? WHERE user_id=?",
                  (reason, uid))
        c.commit()
    logger.warning(f"[vibe.activator] uid={uid} HALT: {reason}")


def _increment_count(uid: int, signal: dict) -> None:
    """开单成功后调用：自增 daily_count + 追加 last_signals_json"""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT daily_count, last_signals_json FROM vibe_activations WHERE user_id=?",
            (uid,),
        ).fetchone()
        if not row:
            return
        try:
            sigs = json.loads(row["last_signals_json"] or "[]")
        except Exception:
            sigs = []
        sigs.append({**signal, "ts": datetime.now().isoformat()})
        sigs = sigs[-20:]  # 只保留最近 20 条
        c.execute("""
            UPDATE vibe_activations
            SET daily_count=daily_count+1,
                last_signals_json=?,
                last_run_at=datetime('now')
            WHERE user_id=?
        """, (json.dumps(sigs, ensure_ascii=False), uid))
        c.commit()


def update_daily_pnl(uid: int, pnl_pct: float) -> None:
    """外部喂入当日浮动盈亏百分比（由 main 调用，每分钟一次）
    若 < stop_loss_pct 自动 halt
    """
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT stop_loss_pct, halted FROM vibe_activations WHERE user_id=?",
            (uid,),
        ).fetchone()
        if not row:
            return
        c.execute("UPDATE vibe_activations SET daily_pnl_pct=? WHERE user_id=?",
                  (float(pnl_pct), uid))
        c.commit()
        if not row["halted"] and pnl_pct < row["stop_loss_pct"]:
            _mark_halt(uid, f"单日亏损 {pnl_pct*100:.2f}% 触发熔断（阈值 {row['stop_loss_pct']*100:.1f}%）")


def run_tick(uid: int, execute_buy_fn, get_paper_cash_fn) -> dict:
    """单次扫描+下单（被外层调度器每 5 分钟调用一次）

    Args:
        uid:                用户 id
        execute_buy_fn:     main.execute_buy(uid, code, name, price, shares, reason)
                            内部自动按 broker_type 走 paper / live
        get_paper_cash_fn:  () -> float  当前可用资金（paper_broker.cash）

    Returns:
        {"ok": bool, "phase": str, "details": {...}}
    """
    _reset_if_new_day(uid)
    cfg = get(uid)
    if not cfg or not cfg["enabled"]:
        return {"ok": False, "phase": "skip_disabled"}
    if cfg["halted"]:
        return {"ok": False, "phase": "skip_halted", "halt_reason": cfg["halt_reason"]}
    if cfg["daily_count"] >= cfg["daily_limit"]:
        return {"ok": False, "phase": "skip_daily_limit",
                "details": {"count": cfg["daily_count"], "limit": cfg["daily_limit"]}}

    factor_id = cfg["factor_id"]

    # 准备股票池
    from analytics.factors_zoo import registry, build_panel, latest_score
    from market.market_data import get_kline as _get_kline
    reg = registry()
    if factor_id not in reg.list():
        return {"ok": False, "phase": "factor_not_found", "factor_id": factor_id}

    if cfg["mode"] == "market_top":
        try:
            import strategy_manager as sm
            universe, _qc = sm._full_market_universe_with_quotes(max_codes=cfg["max_codes"])
            codes = list(universe or [])
            quote_map = {q["code"]: q for q in (_qc or {}).values() if isinstance(q, dict) and q.get("code")}
        except Exception as e:
            return {"ok": False, "phase": "universe_fail", "error": str(e)}
    else:
        try:
            codes = json.loads(cfg["codes_json"] or "[]")
        except Exception:
            codes = []
        quote_map = {}
    if not codes:
        return {"ok": False, "phase": "no_codes"}

    # 算因子分数
    panel = build_panel(codes, _get_kline, days=60, workers=12)
    if not panel or "close" not in panel:
        return {"ok": False, "phase": "panel_empty"}
    try:
        df = reg.compute(factor_id, panel)
    except Exception as e:
        return {"ok": False, "phase": "compute_fail", "error": str(e)}
    scores = latest_score(df, mode="per_column")
    valid = [(c, v) for c, v in scores.items() if v is not None]
    if not valid:
        return {"ok": False, "phase": "no_valid_scores"}

    # 截面 percentile 阈值
    import numpy as np
    vals = np.array([v for _, v in valid], dtype=np.float64)
    threshold = float(np.quantile(vals, cfg["score_percentile"]))
    candidates = sorted(
        [(c, v) for c, v in valid if v >= threshold],
        key=lambda x: -x[1],
    )

    # 过滤已持仓（兼容 get_positions 返回 list 或 dict 两种格式）
    from broker import paper_broker as pb
    broker = pb.get_broker(uid)
    held_codes = set()
    if broker:
        try:
            _pos = broker.get_positions()
            if isinstance(_pos, dict):
                held_codes = set(_pos.keys())
            elif isinstance(_pos, list):
                # list 格式：每项是 dict 或对象，code 在 'code'/'股票代码' 字段
                for p in _pos:
                    if isinstance(p, dict):
                        c = p.get("code") or p.get("股票代码") or p.get("symbol")
                        if c: held_codes.add(str(c))
                    elif hasattr(p, "code"):
                        held_codes.add(str(p.code))
        except Exception as _pe:
            import logging
            logging.getLogger(__name__).debug(f"get_positions 失败: {_pe}")
    candidates = [(c, v) for c, v in candidates if c not in held_codes]
    if not candidates:
        return {"ok": False, "phase": "no_candidate_after_filter",
                "details": {"threshold": round(threshold, 4)}}

    # 计算可开仓数（剩余日额度）
    remaining = cfg["daily_limit"] - cfg["daily_count"]
    cash = float(get_paper_cash_fn() or 0)
    if cash <= 0:
        return {"ok": False, "phase": "no_cash"}
    position_size = cash * cfg["position_pct"]

    # 逐只开仓（最多到 remaining）
    placed = []
    for code, score in candidates[:remaining]:
        q = quote_map.get(code, {})
        price = float(q.get("price") or 0)
        if price <= 0:
            # quote 没有 price，跳过（避免 0 价下单）
            continue
        # 100 股为单位
        shares = int(position_size // (price * 100)) * 100
        if shares < 100:
            continue
        name = q.get("name", "")
        reason = f"📚 Vibe[{factor_id}] score={score:.4f}>{threshold:.4f}"
        result = execute_buy_fn(uid, code, name, price, shares, reason)
        if result.get("ok"):
            placed.append({"code": code, "name": name, "price": price,
                           "shares": shares, "score": round(score, 4)})
            _increment_count(uid, {"code": code, "name": name, "score": round(score, 4),
                                    "price": price, "shares": shares})
        else:
            logger.warning(f"[vibe.activator] uid={uid} 买 {code} 失败: {result.get('msg')}")

    return {
        "ok": True,
        "phase": "done",
        "factor_id": factor_id,
        "threshold": round(threshold, 4),
        "candidates_count": len(candidates),
        "placed": placed,
        "daily_count": cfg["daily_count"] + len(placed),
        "daily_limit": cfg["daily_limit"],
    }
