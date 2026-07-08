"""
共享运行态与通用辅助 —— 从 main.py 抽出，供 main 与各 web Blueprint 共同导入。

设计要点：本模块不创建 Flask app、不导入 main，因此不会产生循环依赖。
路由蓝图与后台调度器都从这里取同一份内存状态（user_states / SSE 订阅等）。
"""
from __future__ import annotations

import threading
import logging
from functools import wraps
from datetime import datetime
from pathlib import Path

from flask import session, jsonify

import user_manager as um
from config import (
    INITIAL_CAPITAL, POSITION_RATIO,
    MAX_POSITIONS, SINGLE_POSITION_MAX_RATIO,
)

logger = logging.getLogger("app_state")

# 项目根目录（index.html / admin.html / data/ / logs/ 所在处）。
# 各 web 蓝图统一从这里取，避免在子目录里反复 parent.parent 出错。
BASE_DIR = Path(__file__).resolve().parent

# ══════════════════════════════════════════════════════════════
# 多用户运行态（进程内内存，单 worker）
# ══════════════════════════════════════════════════════════════
user_states: dict[int, dict] = {}
_user_state_lock = threading.Lock()

# SSE 订阅者: uid -> list of queue.Queue
_sse_subscribers: dict[int, list] = {}
_sse_lock = threading.Lock()


def _default_user_state() -> dict:
    return {
        "running":        False,
        "pool":           [],
        "queue":          [],
        "logs":           [],
        "scanned":        0,
        "executed":       0,
        "active_strategy": None,
        "capital":        INITIAL_CAPITAL,
        "position_ratio": POSITION_RATIO,
        "max_positions":             MAX_POSITIONS,             # 仓位护栏：最多持仓只数
        "single_position_max_ratio": SINGLE_POSITION_MAX_RATIO, # 仓位护栏：单笔占比上限
        "today_trades":   0,
        "today_date":     "",
        "positions":      {},
        "auto_execute":   False,
        "scan_progress":  {"running": False, "stage": "", "current": 0, "total": 0},
    }


def _restore_positions_from_paper(uid: int, s: dict) -> None:
    """从模拟盘 SQLite 把持仓恢复到内存 s["positions"]。

    修复「模拟盘重启后持仓清空」：execute_buy 会同时写内存 s["positions"] 和
    模拟盘 DB（paper_positions 表），但旧版启动只恢复 running 标志、内存持仓为空，
    于是 /api/status、/api/positions 重启后显示空仓（数据其实还在 DB 里）。
    这里在用户状态首次创建时，把 DB 持仓映射回内存结构。
    实盘用户的模拟盘 DB 通常为空 → 返回空，无副作用。
    """
    try:
        import paper_broker as pb_mod
        broker = pb_mod.get_broker(uid)
        db_positions = broker.get_positions()   # 不传实时价，cost 即可；浮动盈亏由读取端按实时价算
        restored = {}
        for p in db_positions:
            code = p.get("code")
            if not code:
                continue
            restored[code] = {
                "name":   p.get("name", "") or code,
                "shares": p.get("shares", 0),
                "cost":   p.get("cost_price", 0),
                "date":   (p.get("buy_time") or "")[:10],
            }
        if restored:
            s["positions"] = restored
            logger.info(f"[startup] uid={uid} 从模拟盘恢复 {len(restored)} 个持仓")
    except Exception as e:
        logger.warning(f"[restore_positions uid={uid}] {e}")


def get_user_state(uid: int) -> dict:
    """获取或初始化用户运行状态，首次访问时从数据库加载持久化设置。"""
    with _user_state_lock:
        if uid not in user_states:
            s = _default_user_state()
            try:
                settings = um.get_user_settings(uid)
                s["running"]          = bool(settings.get("running", 0))
                s["auto_execute"]     = bool(settings.get("auto_execute", 0))
                s["capital"]          = float(settings.get("capital", INITIAL_CAPITAL))
                s["position_ratio"]   = float(settings.get("position_ratio", POSITION_RATIO))
                s["max_positions"]    = int(settings.get("max_positions", MAX_POSITIONS) or MAX_POSITIONS)
                s["single_position_max_ratio"] = float(
                    settings.get("single_position_max_ratio", SINGLE_POSITION_MAX_RATIO) or SINGLE_POSITION_MAX_RATIO)
                s["active_strategy"]  = settings.get("active_strategy") or None
            except Exception:
                pass
            # 首次创建时从模拟盘 DB 恢复持仓（修复重启后持仓清空）
            _restore_positions_from_paper(uid, s)
            user_states[uid] = s
        return user_states[uid]


def _persist_state(uid: int, **fields):
    """将指定字段保存到 user_settings 表。"""
    try:
        um.save_user_settings(uid, **fields)
    except Exception as e:
        logger.warning(f"[persist_state uid={uid}] {e}")


def _add_log(uid: int, msg: str, level: str = "INFO"):
    """用户日志：写内存 + 写数据库 + 推 SSE。"""
    t = datetime.now().strftime("%H:%M:%S")
    entry = {"time": t, "msg": msg, "level": level.lower()}
    s = get_user_state(uid)
    s["logs"].insert(0, entry)
    s["logs"] = s["logs"][:200]
    logger.info(f"[uid={uid}] {msg}")
    try:
        um.add_user_log(uid, msg, level)
    except Exception:
        pass
    _sse_push(uid, "log", entry)


def _sse_push(uid: int, event: str, data: dict):
    """向该用户所有 SSE 连接推送事件。"""
    with _sse_lock:
        subscribers = _sse_subscribers.get(uid, [])
        dead = []
        for q in subscribers:
            try:
                q.put_nowait({"event": event, "data": data})
            except Exception:
                dead.append(q)
        for q in dead:
            subscribers.remove(q)


def _restore_all_user_states():
    """服务启动时恢复所有 running=1 的用户运行状态。"""
    try:
        running_users = um.get_running_users()
        for uid in running_users:
            s = get_user_state(uid)
            s["running"] = True
            _add_log(uid, "服务重启，自动恢复运行状态", "WARN")
            logger.info(f"[startup] 恢复用户 uid={uid} 运行状态")
    except Exception as e:
        logger.warning(f"[restore_states] {e}")


# ── 认证装饰器 ────────────────────────────────────────────────
def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "请先登录", "code": 401}), 401
        return f(*args, **kwargs)
    return wrapper


def current_session_user():
    uid = session.get("user_id")
    if not uid:
        return None
    user = um.get_user(uid)
    if not user:
        return None
    session["username"] = user.get("username")
    session["is_admin"] = bool(user.get("is_admin"))
    return user


def current_session_is_admin() -> bool:
    user = current_session_user()
    return bool(user and user.get("is_admin"))


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "请先登录", "code": 401}), 401
        if not current_session_is_admin():
            return jsonify({"ok": False, "error": "无管理员权限", "code": 403}), 403
        return f(*args, **kwargs)
    return wrapper


def is_root_admin_session() -> bool:
    return um.is_root_admin_user(current_session_user())


def current_admin_permissions() -> list[str]:
    uid = session.get("user_id")
    if not uid:
        return []
    return um.get_admin_permissions(uid)


def require_root_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "please login", "code": 401}), 401
        if not is_root_admin_session():
            return jsonify({
                "ok": False,
                "error": "root admin required",
                "code": "root_admin_required",
            }), 403
        return f(*args, **kwargs)
    return wrapper


def require_admin_perm(perm_key: str):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("user_id"):
                return jsonify({"ok": False, "error": "please login", "code": 401}), 401
            if not current_session_is_admin():
                return jsonify({"ok": False, "error": "admin required", "code": 403}), 403
            if not is_root_admin_session() and perm_key not in current_admin_permissions():
                return jsonify({
                    "ok": False,
                    "error": "missing admin permission",
                    "code": 403,
                    "missing_permission": perm_key,
                }), 403
            return f(*args, **kwargs)
        return wrapper
    return deco


def _deduct(action: str, desc: str = ""):
    """扣积分，返回 None 表示成功，返回 dict 表示失败。"""
    uid = session.get("user_id")
    if not uid:
        return {"ok": False, "error": "请先登录", "code": 401}
    r = um.deduct_points(uid, action, desc)
    if not r["ok"]:
        return r
    return None
