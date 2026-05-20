import os as _os
import _pathsetup  # noqa: F401  # 必须在其它项目模块 import 之前
# ── 清除系统代理（akshare 走国内接口，代理反而导致连接失败）──────────
for _k in ("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy",
           "ALL_PROXY","all_proxy","SOCKS_PROXY","socks_proxy"):
    _os.environ.pop(_k, None)

from flask import Flask, jsonify, request, send_from_directory, session, Response
from flask_cors import CORS
from functools import wraps
import threading, schedule, time, logging, uuid, secrets, json, queue as _queue
from typing import Optional
from datetime import datetime
from market_data import is_trading_time
from strategy import scan_all
from ths_operator import THSOperator
from config import STOCK_POOL, SCAN_INTERVAL, DRY_RUN, INITIAL_CAPITAL, POSITION_RATIO
import strategy_manager as sm
import data_manager as dm
import ai_analyst as ai
import factor_manager as fm
import stock_scorer as scorer
import market_context as mctx
import paper_broker as pb
import user_manager as um
import notification_manager as nm
import search_manager as search_mgr
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")
app.secret_key = _os.environ.get("SECRET_KEY") or secrets.token_hex(32)
CORS(app, supports_credentials=True)

# ── Swagger / flasgger ────────────────────────────────────────
try:
    from flasgger import Swagger
    swagger_cfg = {
        "title": "见微 Alpha API",
        "version": "2.0.0",
        "description": "见微 Alpha REST API 文档",
        "uiversion": 3,
        "specs_route": "/apidocs/",
    }
    Swagger(app, config={"headers": [], "specs": [{
        "endpoint": "apispec", "route": "/apispec.json",
        "rule_filter": lambda rule: True, "model_filter": lambda tag: True,
    }], "static_url_path": "/flasgger_static", "swagger_ui": True,
        "specs_route": "/apidocs/"}, template={"info": swagger_cfg})
    logger.info("Swagger UI 已启用: /apidocs/")
except ImportError:
    logger.info("flasgger 未安装，跳过 Swagger UI")

# ── 初始化数据库 ──────────────────────────────────────────────
um.init_db()
# 迁移必须在 pb.init_paper_db() 之前，先给旧表加 user_id 列
try:
    import db_migration as _dbm
    _dbm.migrate_paper_trading_db()
    _dbm.migrate_users_db()
except Exception as _e:
    logger.warning(f"[db_migration] {_e}")
pb.init_paper_db()

operator = THSOperator()

# ══════════════════════════════════════════════════════════════
# 多用户状态管理
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
        "today_trades":   0,
        "today_date":     "",
        "positions":      {},
        "auto_execute":   False,
        "scan_progress":  {"running": False, "stage": "", "current": 0, "total": 0},
    }


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
                s["active_strategy"]  = settings.get("active_strategy") or None
            except Exception:
                pass
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


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "请先登录", "code": 401}), 401
        if not session.get("is_admin"):
            return jsonify({"ok": False, "error": "无管理员权限", "code": 403}), 403
        return f(*args, **kwargs)
    return wrapper


def _deduct(action: str, desc: str = "") -> Optional[dict]:
    """扣积分，返回 None 表示成功，返回 dict 表示失败。"""
    uid = session.get("user_id")
    if not uid:
        return {"ok": False, "error": "请先登录", "code": 401}
    r = um.deduct_points(uid, action, desc)
    if not r["ok"]:
        return r
    return None


# ══════════════════════════════════════════════════════════════
# 静态页面
# ══════════════════════════════════════════════════════════════
@app.route("/")
def index():
    resp = send_from_directory(str(BASE_DIR), "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/admin")
def admin_page():
    resp = send_from_directory(str(BASE_DIR), "admin.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ══════════════════════════════════════════════════════════════
# 认证 API
# ══════════════════════════════════════════════════════════════
@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    d = request.json or {}
    r = um.register(d.get("username", ""), d.get("email", ""), d.get("password", ""))
    return jsonify(r)


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    d = request.json or {}
    r = um.login(d.get("username", ""), d.get("password", ""))
    if r["ok"]:
        session["user_id"]  = r["user_id"]
        session["username"] = r["username"]
        session["is_admin"] = r["is_admin"]
        session.permanent   = True
    return jsonify(r)


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/me")
def auth_me():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"ok": False, "logged_in": False})
    user = um.get_user(uid)
    if not user:
        session.clear()
        return jsonify({"ok": False, "logged_in": False})
    return jsonify({
        "ok":        True,
        "logged_in": True,
        "user_id":   user["id"],
        "username":  user["username"],
        "points":    user["points"],
        "is_admin":  bool(user["is_admin"]),
    })


# ══════════════════════════════════════════════════════════════
# 用户自助 API
# ══════════════════════════════════════════════════════════════
@app.route("/api/user/points")
@require_auth
def user_points():
    uid = session["user_id"]
    return jsonify({"ok": True, "points": um.get_points(uid)})


@app.route("/api/user/points/history")
@require_auth
def user_points_history():
    uid = session["user_id"]
    return jsonify({"ok": True, "history": um.get_point_history(uid, 50)})


@app.route("/api/user/change_password", methods=["POST"])
@require_auth
def user_change_password():
    d = request.json or {}
    return jsonify(um.change_password(
        session["user_id"],
        d.get("old_password", ""),
        d.get("new_password", ""),
    ))


@app.route("/api/user/settings", methods=["GET"])
@require_auth
def user_get_settings():
    uid = session["user_id"]
    s   = get_user_state(uid)
    return jsonify({
        "ok":             True,
        "running":        s["running"],
        "auto_execute":   s["auto_execute"],
        "capital":        s["capital"],
        "position_ratio": s["position_ratio"],
        "active_strategy": s["active_strategy"],
    })


@app.route("/api/user/settings", methods=["POST"])
@require_auth
def user_save_settings():
    uid = session["user_id"]
    d   = request.json or {}
    s   = get_user_state(uid)
    if "capital" in d:
        s["capital"]        = float(d["capital"])
    if "position_ratio" in d:
        s["position_ratio"] = float(d["position_ratio"])
    if "auto_execute" in d:
        s["auto_execute"]   = bool(d["auto_execute"])
    _persist_state(uid,
        capital        = s["capital"],
        position_ratio = s["position_ratio"],
        auto_execute   = int(s["auto_execute"]),
    )
    _add_log(uid, f"账户设置更新: 资金¥{s['capital']:,.0f} "
             f"仓位{s['position_ratio']*100:.0f}%")
    return jsonify({"ok": True})


@app.route("/api/user/logs")
@require_auth
def user_get_logs():
    uid   = session["user_id"]
    limit = int(request.args.get("limit", 200))
    logs  = um.get_user_logs(uid, limit=limit)
    return jsonify({"ok": True, "logs": logs})


@app.route("/api/user/ai_config", methods=["GET"])
@require_auth
def user_get_ai_config():
    uid = session["user_id"]
    cfg = um.get_user_ai_config(uid) or {}
    key = cfg.get("api_key", "")
    if len(key) > 8:
        cfg["api_key_masked"] = key[:4] + "****" + key[-4:]
    else:
        cfg["api_key_masked"] = "****" if key else ""
    return jsonify({"ok": True, **cfg})


@app.route("/api/user/ai_config", methods=["POST"])
@require_auth
def user_save_ai_config():
    uid = session["user_id"]
    d   = request.json or {}
    um.save_user_ai_config(uid, **{k: v for k, v in d.items()
                                   if k in ("api_key", "base_url", "model", "provider", "enabled")})
    _add_log(uid, f"AI配置已更新: provider={d.get('provider','')}")
    return jsonify({"ok": True})


@app.route("/api/user/broker_config", methods=["GET"])
@require_auth
def user_get_broker_config():
    uid = session["user_id"]
    cfg = um.get_user_broker_config(uid)
    # 隐藏密码/token
    inner = cfg.get("config", {})
    if "password" in inner:
        inner = {**inner, "password": "****"}
    if "access_token" in inner:
        inner = {**inner, "access_token": inner["access_token"][:8] + "****" if inner["access_token"] else ""}
    return jsonify({"ok": True, "broker_type": cfg.get("broker_type", "paper"), "config": inner})


@app.route("/api/user/broker_config", methods=["POST"])
@require_auth
def user_save_broker_config():
    uid = session["user_id"]
    d   = request.json or {}
    um.save_user_broker_config(uid, d.get("broker_type", "paper"), d.get("config", {}))
    _add_log(uid, f"券商配置已更新: {d.get('broker_type', 'paper')}")
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════
# 公告 API
# ══════════════════════════════════════════════════════════════
@app.route("/api/announcements")
def get_announcements():
    uid  = session.get("user_id")
    items = um.get_active_announcements()
    if uid:
        for item in items:
            item["read"] = um.is_announcement_read(uid, item["id"])
    return jsonify({"ok": True, "announcements": items})


@app.route("/api/announcements/<int:ann_id>/read", methods=["POST"])
@require_auth
def mark_announcement_read(ann_id):
    uid = session["user_id"]
    um.mark_announcement_read(uid, ann_id)
    return jsonify({"ok": True})


@app.route("/api/admin/announcements", methods=["GET"])
@require_admin
def admin_list_announcements():
    items = um.list_all_announcements()
    return jsonify({"ok": True, "announcements": items})


@app.route("/api/admin/announcements", methods=["POST"])
@require_admin
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


@app.route("/api/admin/announcements/<int:ann_id>", methods=["PUT"])
@require_admin
def admin_update_announcement(ann_id):
    d = request.json or {}
    r = um.update_announcement(ann_id, **{k: v for k, v in d.items()
                                          if k in ("title", "content", "type", "active", "expires_at")})
    return jsonify(r)


@app.route("/api/admin/announcements/<int:ann_id>", methods=["DELETE"])
@require_admin
def admin_delete_announcement(ann_id):
    return jsonify(um.delete_announcement(ann_id))


@app.route("/api/admin/logs")
@require_admin
def admin_get_logs():
    uid_filter = request.args.get("uid")
    limit      = int(request.args.get("limit", 500))
    logs = um.get_all_logs(uid=int(uid_filter) if uid_filter else None, limit=limit)
    return jsonify({"ok": True, "logs": logs})


# ══════════════════════════════════════════════════════════════
# 管理员 API
# ══════════════════════════════════════════════════════════════
@app.route("/api/admin/stats")
@require_admin
def admin_stats():
    return jsonify({"ok": True, **um.stats()})


@app.route("/api/admin/users")
@require_admin
def admin_users():
    page   = int(request.args.get("page", 1))
    search = request.args.get("search", "")
    return jsonify({"ok": True, **um.list_users(page, 20, search)})


@app.route("/api/admin/users/<int:uid>/points", methods=["POST"])
@require_admin
def admin_add_points(uid):
    d      = request.json or {}
    amount = int(d.get("amount", 0))
    desc   = d.get("description", "管理员充值")
    r = um.add_points(uid, amount, desc)
    return jsonify(r)


@app.route("/api/admin/users/<int:uid>/active", methods=["POST"])
@require_admin
def admin_set_active(uid):
    active = request.json.get("active", True)
    if uid == session.get("user_id") and not active:
        return jsonify({"ok": False, "error": "不能禁用自己的账号"})
    return jsonify(um.set_active(uid, active))


@app.route("/api/admin/users/<int:uid>/admin", methods=["POST"])
@require_admin
def admin_set_admin(uid):
    is_admin = request.json.get("is_admin", False)
    return jsonify(um.set_admin(uid, is_admin))


@app.route("/api/admin/transactions")
@require_admin
def admin_transactions():
    page = int(request.args.get("page", 1))
    return jsonify({"ok": True, **um.all_point_transactions(page, 30)})


@app.route("/api/admin/users/<int:uid>/reset_password", methods=["POST"])
@require_admin
def admin_reset_password(uid):
    new_pw = (request.json or {}).get("new_password", "")
    return jsonify(um.admin_reset_password(uid, new_pw))


# ══════════════════════════════════════════════════════════════
# 核心交易逻辑（per-user）
# ══════════════════════════════════════════════════════════════
def _reset_daily_if_needed(uid: int):
    """每个交易日开始时清零今日计数。"""
    today = datetime.now().strftime("%Y-%m-%d")
    s = get_user_state(uid)
    if s["today_date"] != today:
        s["today_date"]   = today
        s["today_trades"] = 0
        _add_log(uid, f"📅 新交易日 {today}，今日开仓次数已清零")


def execute_buy(uid: int, code: str, name: str, price: float, shares: int,
                reason: str = "") -> bool:
    """统一买入入口（模拟盘 / 实盘）。"""
    s = get_user_state(uid)
    if DRY_RUN:
        broker = pb.get_broker(uid)
        result = broker.submit_buy(code, name, price, shares, reason=reason)
        if result.get("ok"):
            s["executed"]    += 1
            s["today_trades"] = s.get("today_trades", 0) + 1
            s["positions"][code] = {
                "name": name, "shares": shares, "cost": result["exec_price"],
                "date": datetime.now().strftime("%Y-%m-%d"),
            }
            _add_log(uid,
                f"✅ [模拟] 买入 {code} {name} {shares}股 @{result['exec_price']:.2f} "
                f"花费¥{result['total_cost']:,.0f} 佣金¥{result['commission']:.1f}", "buy")
            return True
        else:
            _add_log(uid, f"❌ [模拟] 买入拒绝 {code}: {result.get('msg','')}", "error")
            return False
    else:
        ok = operator.buy(code, price, shares)
        if ok:
            s["executed"]    += 1
            s["today_trades"] = s.get("today_trades", 0) + 1
            s["positions"][code] = {
                "name": name, "shares": shares, "cost": price,
                "date": datetime.now().strftime("%Y-%m-%d"),
            }
        return ok


def execute_sell(uid: int, code: str, price: float, sell_reason: str = "手动卖出") -> dict:
    """统一卖出入口。"""
    s   = get_user_state(uid)
    pos = s["positions"].get(code)
    if DRY_RUN:
        broker = pb.get_broker(uid)
        result = broker.submit_sell(code, price, sell_reason=sell_reason)
        if result.get("ok"):
            s["executed"] += 1
            s["positions"].pop(code, None)
            pnl  = result.get("pnl", 0)
            sign = "✅" if pnl >= 0 else "❌"
            _add_log(uid,
                f"{sign} [模拟] 卖出 {code} @{result['exec_price']:.2f} "
                f"盈亏¥{pnl:+,.2f}({result.get('pnl_pct',0):+.2f}%) 原因:{sell_reason}",
                "buy" if pnl >= 0 else "error")
        return result
    else:
        shares = pos["shares"] if pos else 0
        ok     = operator.sell(code, price, shares)
        if ok:
            pnl = round((price - pos["cost"]) * shares, 2) if pos else 0
            s["executed"] += 1
            s["positions"].pop(code, None)
            return {"ok": True, "pnl": pnl}
        return {"ok": False, "msg": "实盘卖出失败"}


def _auto_buy(uid: int, item: dict):
    """自动买入（信号触发后立即执行）。"""
    code   = item["code"]
    name   = item.get("name", "")
    price  = item["price"]
    shares = item.get("shares", 100)
    try:
        _add_log(uid, f"🤖 自动买入触发: {code} {name} {shares}股 @{price}", "buy")
        ok = execute_buy(uid, code, name, price, shares, reason="自动买入")
        item["status"] = "done" if ok else "error"
    except Exception as e:
        item["status"] = "error"
        _add_log(uid, f"❌ 自动买入异常: {code} — {e}", "error")


def _check_paper_risk(uid: int):
    """检查模拟盘持仓风控（止损/止盈/超期）。"""
    if not DRY_RUN:
        return
    try:
        broker    = pb.get_broker(uid)
        positions = broker.get_positions()
        if not positions:
            return
        codes      = [p["code"] for p in positions]
        cur_prices = {}
        try:
            import ths_quote as tq
            client = tq.get_client()
            if client and client._ok:
                qs = client.get_quotes(codes)
                cur_prices = {q["code"]: q.get("price", 0) for q in qs if q.get("price", 0) > 0}
        except Exception:
            pass
        triggered = broker.check_risk_rules(cur_prices)
        s = get_user_state(uid)
        for t in triggered:
            pnl  = t.get("pnl", 0)
            sign = "✅" if pnl >= 0 else "❌"
            _add_log(uid,
                f"{sign} [风控] {t['code']} {t['name']} 触发{t['reason']} 盈亏¥{pnl:+,.2f}",
                "buy" if pnl >= 0 else "error")
            s["positions"].pop(t["code"], None)
    except Exception as e:
        logger.warning(f"[风控检查 uid={uid}] {e}")


def do_scan(uid: int):
    """对单个用户执行一次扫描。"""
    s = get_user_state(uid)
    if not s["running"] or not is_trading_time():
        return
    _reset_daily_if_needed(uid)
    _check_paper_risk(uid)

    active    = s.get("active_strategy")
    is_daban  = (active == "da_ban")
    scan_label = "全市场"
    _add_log(uid, f"扫描 {scan_label} 策略=[{active or '默认'}]...")

    s["scan_progress"] = {
        "running": True,
        "stage":   f"扫描{scan_label}",
        "current": 0,
        "total":   5200 if is_daban else 0,
    }

    if active:
        ret     = sm.run(active, [])
        results = [{
            "code":        r.get("code", ""),
            "name":        r.get("name", ""),
            "price":       r.get("price", 0),
            "change":      r.get("change", 0),
            "pass_count":  5,
            "signal":      r.get("signal", True),
            "consec_limit": r.get("consec_limit", 0),
            "consec_label": r.get("consec_label", ""),
            "reason":      r.get("reason", ""),
            "conditions":  {},
        } for r in ret.get("results", [])]
        s["scanned"] += ret.get("scanned", ret.get("count", 0))
    else:
        results = scan_all([])
        s["scanned"] += len(results)

    s["pool"] = results

    added = 0
    buy_candidates = [r for r in results if r.get("signal", True)]
    for r in buy_candidates:
        code  = r["code"]
        price = r["price"] or 1
        if not any(q["code"] == code for q in s["queue"]):
            budget = s["capital"] * s["position_ratio"]
            shares = max(int(budget / price / 100) * 100, 100)
            item = {
                "code":       code,
                "name":       r["name"],
                "price":      price,
                "shares":     shares,
                "budget":     round(budget, 2),
                "pass_count": r.get("pass_count", 5),
                "reason":     r.get("reason", ""),
                "action":     "buy",
                "status":     "pending",
            }
            s["queue"].append(item)
            _add_log(uid,
                f"📌 {code} {r['name']} 进入队列 | 计划买入{shares}股 预算¥{budget:,.0f}", "buy")
            added += 1
            if s.get("auto_execute"):
                _auto_buy(uid, item)

    buy_signals = len(buy_candidates)
    _add_log(uid, f"扫描完成，信号 {buy_signals} 只，已入队 {added} 只")
    s["scan_progress"] = {"running": False, "stage": "完成", "current": 0, "total": 0}
    _sse_push(uid, "scan_done", {"pool_size": len(results), "signals": buy_signals})

    # ── 推送通知（有买入信号时推送）──────────────────
    if buy_signals > 0:
        try:
            top = buy_candidates[:5]
            detail = "\n".join(
                f"• {r.get('name', r['code'])}({r['code']}) "
                f"¥{r.get('price', 0):.2f} {r.get('change', 0):+.2f}%"
                for r in top
            )
            notify_content = (
                f"策略：{active or '默认'}\n"
                f"时间：{datetime.now().strftime('%H:%M:%S')}\n"
                f"信号数：{buy_signals} 只\n\n"
                f"前5只：\n{detail}"
            )
            threading.Thread(
                target=nm.send_notification,
                args=(f"📈 扫描到 {buy_signals} 只买入信号",  notify_content),
                daemon=True,
            ).start()
        except Exception as _ne:
            logger.debug(f"[notify] 推送失败: {_ne}")


# ══════════════════════════════════════════════════════════════
# 监控 API（per-user）
# ══════════════════════════════════════════════════════════════
@app.route("/api/status")
def get_status():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"running": False, "logged_in": False})
    s   = get_user_state(uid)
    try:
        import ths_quote as tq
        tdx_prog = tq.get_scan_progress()
        if tdx_prog.get("running"):
            s["scan_progress"].update({
                "running": True,
                "current": tdx_prog.get("current", 0),
                "total":   tdx_prog.get("total",   0),
            })
    except Exception:
        pass
    return jsonify(s)


@app.route("/api/start", methods=["POST"])
@require_auth
def start():
    uid = session["user_id"]
    s   = get_user_state(uid)
    s["running"] = True
    _persist_state(uid, running=1)
    _add_log(uid, "监控启动")
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
@require_auth
def stop():
    uid = session["user_id"]
    s   = get_user_state(uid)
    s["running"] = False
    _persist_state(uid, running=0)
    _add_log(uid, "监控停止", "WARN")
    return jsonify({"ok": True})


@app.route("/api/auto_execute", methods=["POST"])
@require_auth
def set_auto_execute():
    uid     = session["user_id"]
    s       = get_user_state(uid)
    enabled = request.json.get("enabled", False)
    s["auto_execute"] = bool(enabled)
    _persist_state(uid, auto_execute=int(enabled))
    mode = "开启" if enabled else "关闭"
    _add_log(uid,
        f"🤖 自动执行已{mode} — {'信号触发即自动下单' if enabled else '手动确认模式'}", "WARN")
    return jsonify({"ok": True, "auto_execute": s["auto_execute"]})


@app.route("/api/execute", methods=["POST"])
@require_auth
def execute():
    uid    = session["user_id"]
    s      = get_user_state(uid)
    data   = request.json
    code   = data.get("code")
    action = data.get("action")
    item   = next((q for q in s["queue"] if q["code"] == code), None)

    if not item:
        if action == "buy":
            price  = float(data.get("price", 0))
            name   = data.get("name", code)
            shares = data.get("shares")
            if not shares:
                budget = s["capital"] * s["position_ratio"]
                shares = int(budget / price / 100) * 100 if price > 0 else 100
                shares = max(shares, 100)
            ok = execute_buy(uid, code, name, price, int(shares), reason="手动买入")
            return jsonify({"ok": ok})
        return jsonify({"ok": False, "msg": "未找到"})

    if action == "skip":
        item["status"] = "skip"
    elif action == "buy":
        price  = item["price"]
        name   = item.get("name", code)
        shares = item.get("shares", 100)
        ok = execute_buy(uid, code, name, price, shares, reason="队列买入")
        item["status"] = "done" if ok else "error"

    return jsonify({"ok": True})


# ── 账户 API ──────────────────────────────────────────────────
@app.route("/api/account", methods=["GET"])
@require_auth
def get_account():
    uid = session["user_id"]
    s   = get_user_state(uid)
    return jsonify({
        "capital":        s["capital"],
        "position_ratio": s["position_ratio"],
        "today_trades":   s["today_trades"],
        "positions":      s["positions"],
    })


@app.route("/api/account", methods=["POST"])
@require_auth
def set_account():
    uid = session["user_id"]
    s   = get_user_state(uid)
    d   = request.json
    if "capital" in d:
        s["capital"]        = float(d["capital"])
    if "position_ratio" in d:
        s["position_ratio"] = float(d["position_ratio"])
    _persist_state(uid, capital=s["capital"], position_ratio=s["position_ratio"])
    _add_log(uid, f"账户设置更新: 资金¥{s['capital']:,.0f} 仓位{s['position_ratio']*100:.0f}%")
    return jsonify({"ok": True})


@app.route("/api/queue/clear", methods=["POST"])
@require_auth
def clear_queue():
    uid = session["user_id"]
    s   = get_user_state(uid)
    s["queue"] = []
    _add_log(uid, "🗑 队列已清空", "WARN")
    return jsonify({"ok": True})


@app.route("/api/queue/add", methods=["POST"])
@require_auth
def queue_add():
    uid   = session["user_id"]
    s     = get_user_state(uid)
    d     = request.json
    code  = d.get("code", "")
    price = float(d.get("price", 0))
    name  = d.get("name", code)
    if not code:
        return jsonify({"ok": False, "msg": "code 缺失"})
    if any(q["code"] == code for q in s["queue"]):
        return jsonify({"ok": True, "msg": "已在队列"})
    budget = s["capital"] * s["position_ratio"]
    shares = max(int(budget / price / 100) * 100 if price > 0 else 100, 100)
    item   = {
        "code": code, "name": name, "price": price,
        "shares": shares, "budget": round(budget, 2),
        "pass_count": 5, "reason": d.get("reason", "手动加入"),
        "action": "buy", "status": "pending",
    }
    s["queue"].append(item)
    _add_log(uid, f"📌 手动加入队列: {code} {name} 计划{shares}股 预算¥{budget:,.0f}", "buy")
    if s.get("auto_execute"):
        _auto_buy(uid, item)
    return jsonify({"ok": True, "msg": f"{code} {name} 已入队，计划{shares}股"})


# ── 持仓 API ──────────────────────────────────────────────────
@app.route("/api/positions")
@require_auth
def get_positions_api():
    uid       = session["user_id"]
    s         = get_user_state(uid)
    positions = s.get("positions", {})
    if not positions:
        return jsonify([])
    live = {}
    try:
        import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            qs   = client.get_quotes(list(positions.keys()))
            live = {q["code"]: q["price"] for q in qs if q.get("price", 0) > 0}
    except Exception:
        pass
    result = []
    for code, pos in positions.items():
        cost      = pos.get("cost", 0)
        cur_price = live.get(code, cost)
        shares    = pos.get("shares", 0)
        pnl_pct   = round((cur_price - cost) / cost * 100, 2) if cost > 0 else 0
        pnl_amt   = round((cur_price - cost) * shares, 2)
        name      = pos.get("name", "") or dm.get_stock_name(code) or code
        result.append({
            "code":         code, "name":         name,
            "shares":       shares, "cost":        round(cost, 2),
            "cur_price":    round(cur_price, 2),   "pnl_pct":     pnl_pct,
            "pnl_amount":   pnl_amt, "market_value": round(cur_price * shares, 2),
            "date":         pos.get("date", ""),
        })
    return jsonify(result)


@app.route("/api/positions/sell", methods=["POST"])
@require_auth
def sell_position():
    uid  = session["user_id"]
    s    = get_user_state(uid)
    d    = request.json
    code = d.get("code", "")
    if DRY_RUN:
        broker           = pb.get_broker(uid)
        broker_positions = {p["code"]: p for p in broker.get_positions()}
        bp = broker_positions.get(code)
        if not bp:
            return jsonify({"ok": False, "msg": "模拟盘持仓不存在"})
        price = float(d.get("price", bp["cur_price"] or bp["cost_price"]))
    else:
        pos = s["positions"].get(code)
        if not pos:
            return jsonify({"ok": False, "msg": "持仓不存在"})
        price = float(d.get("price", pos.get("cost", 0)))
    return jsonify(execute_sell(uid, code, price, sell_reason="手动卖出"))


@app.route("/api/positions/close", methods=["POST"])
@require_auth
def close_position():
    uid  = session["user_id"]
    s    = get_user_state(uid)
    code = request.json.get("code", "")
    if code in s["positions"]:
        del s["positions"][code]
        _add_log(uid, f"🔻 平仓: {code}", "WARN")
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════
# 模拟盘 API（per-user）
# ══════════════════════════════════════════════════════════════
@app.route("/api/paper/account")
@require_auth
def paper_account():
    uid    = session["user_id"]
    broker = pb.get_broker(uid)
    return jsonify({**broker.get_account(), "ok": True})


@app.route("/api/paper/positions")
@require_auth
def paper_positions():
    uid        = session["user_id"]
    broker     = pb.get_broker(uid)
    cur_prices = {}
    try:
        positions = broker.get_positions()
        if positions:
            import ths_quote as tq
            client = tq.get_client()
            if client and client._ok:
                codes      = [p["code"] for p in positions]
                qs         = client.get_quotes(codes)
                cur_prices = {q["code"]: q.get("price", 0) for q in qs if q.get("price", 0) > 0}
    except Exception:
        pass
    return jsonify(broker.get_positions(cur_prices=cur_prices))


@app.route("/api/paper/trades")
@require_auth
def paper_trades():
    uid    = session["user_id"]
    limit  = int(request.args.get("limit", 100))
    broker = pb.get_broker(uid)
    return jsonify(broker.get_trades(limit=limit))


@app.route("/api/paper/orders")
@require_auth
def paper_orders():
    uid    = session["user_id"]
    limit  = int(request.args.get("limit", 100))
    broker = pb.get_broker(uid)
    return jsonify(broker.get_orders(limit=limit))


@app.route("/api/paper/stats")
@require_auth
def paper_stats():
    uid    = session["user_id"]
    broker = pb.get_broker(uid)
    stats  = broker.get_stats()
    acct   = broker.get_account()
    return jsonify({**stats, **acct, "ok": True})


@app.route("/api/paper/equity")
@require_auth
def paper_equity():
    uid    = session["user_id"]
    days   = int(request.args.get("days", 90))
    broker = pb.get_broker(uid)
    return jsonify(broker.get_equity_curve(days=days))


@app.route("/api/paper/reset", methods=["POST"])
@require_auth
def paper_reset():
    uid    = session["user_id"]
    s      = get_user_state(uid)
    cap    = float((request.json or {}).get("initial_capital", INITIAL_CAPITAL))
    broker = pb.get_broker(uid)
    result = broker.reset(initial_capital=cap)
    s["positions"]    = {}
    s["today_trades"] = 0
    s["executed"]     = 0
    _add_log(uid, f"🔄 模拟盘已重置，初始资金¥{cap:,.0f}", "WARN")
    return jsonify(result)


@app.route("/api/paper/config", methods=["GET"])
@require_auth
def paper_get_config():
    uid    = session["user_id"]
    broker = pb.get_broker(uid)
    return jsonify({**broker.get_config(), "ok": True})


@app.route("/api/paper/config", methods=["POST"])
@require_auth
def paper_set_config():
    uid    = session["user_id"]
    broker = pb.get_broker(uid)
    result = broker.set_config(request.json)
    _add_log(uid, f"模拟盘参数已更新: {list(request.json.keys())}")
    return jsonify(result)


@app.route("/api/paper/buy", methods=["POST"])
@require_auth
def paper_manual_buy():
    err = _deduct("paper_trade", "模拟盘下单")
    if err:
        return jsonify(err), 402
    uid    = session["user_id"]
    s      = get_user_state(uid)
    d      = request.json
    code   = d.get("code", "").strip()
    name   = d.get("name", code)
    price  = float(d.get("price", 0))
    shares = int(d.get("shares", 0))
    if not code:
        return jsonify({"ok": False, "msg": "请输入股票代码"})
    if price <= 0:
        return jsonify({"ok": False, "msg": "价格必须大于0"})
    if shares <= 0:
        broker = pb.get_broker(uid)
        acct   = broker.get_account()
        budget = acct["cash"] * s.get("position_ratio", 0.20)
        shares = max(int(budget / price / 100) * 100, 100)
    ok = execute_buy(uid, code, name, price, shares, reason="模拟盘手动买入")
    if ok:
        return jsonify({"ok": True, "shares": shares, "msg": f"已买入 {code} {shares}股 @{price}"})
    return jsonify({"ok": False, "msg": "买入失败，请检查资金或持仓（详见日志）"})


@app.route("/api/paper/sell", methods=["POST"])
@require_auth
def paper_manual_sell():
    uid   = session["user_id"]
    d     = request.json
    code  = d.get("code", "")
    price = float(d.get("price", 0))
    if not price:
        try:
            import ths_quote as tq
            client = tq.get_client()
            if client and client._ok:
                qs = client.get_quotes([code])
                if qs:
                    price = float(qs[0].get("price", 0))
        except Exception:
            pass
    if not price:
        return jsonify({"ok": False, "msg": "无法获取实时价格，请手动输入价格"})
    return jsonify(execute_sell(uid, code, price, sell_reason="手动卖出"))


@app.route("/api/paper/stream")
@require_auth
def paper_stream():
    """SSE 实时推送：日志、持仓更新、扫描完成事件。"""
    uid = session["user_id"]

    def event_stream():
        q = _queue.Queue(maxsize=200)
        with _sse_lock:
            _sse_subscribers.setdefault(uid, []).append(q)
        try:
            # 首次连接推送当前状态快照
            s = get_user_state(uid)
            yield f"event: snapshot\ndata: {json.dumps({'running': s['running'], 'logs': s['logs'][:20]})}\n\n"
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
                except _queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with _sse_lock:
                subs = _sse_subscribers.get(uid, [])
                if q in subs:
                    subs.remove(q)

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ══════════════════════════════════════════════════════════════
# 策略 API
# ══════════════════════════════════════════════════════════════
@app.route("/api/strategies")
def api_list():
    return jsonify(sm.list_strategies())


@app.route("/api/strategies/<filename>")
def api_get(filename):
    return jsonify({"code": sm.get_code(filename)})


@app.route("/api/strategies/save", methods=["POST"])
@require_auth
def api_save():
    uid = session["user_id"]
    d   = request.json
    r   = sm.save(d.get("filename", ""), d.get("code", ""))
    if r["ok"]:
        _add_log(uid, f"策略已保存: {r['file']}")
    return jsonify(r)


@app.route("/api/strategies/delete", methods=["POST"])
@require_auth
def api_delete():
    uid = session["user_id"]
    s   = get_user_state(uid)
    fn  = request.json.get("filename", "")
    r   = sm.delete(fn)
    if r["ok"]:
        _add_log(uid, f"策略已删除: {fn}", "WARN")
        if s.get("active_strategy") == fn:
            s["active_strategy"] = None
    return jsonify(r)


@app.route("/api/strategies/run", methods=["POST"])
@require_auth
def api_run():
    err = _deduct("scan", "全市场选股扫描")
    if err:
        return jsonify(err), 402
    uid = session["user_id"]
    d   = request.json
    fn  = d.get("filename", "")
    cs  = d.get("codes") or []
    # 前端可传 max_codes 控制扫描范围：
    #   0       = 全市场（约 5000 只，2-3 分钟）
    #   600     = 快速扫描（按成交量取前 600 只，约 15 秒）
    #   不传    = 默认全市场
    try:
        mc = int(d.get("max_codes", 0))
    except (TypeError, ValueError):
        mc = 0
    scope_label = "全市场" if not cs else f"{len(cs)}只"
    if not cs and mc > 0:
        scope_label = f"活跃前{mc}只"
    _add_log(uid, f"执行选股: [{fn}] {scope_label}...")
    r       = sm.run(fn, cs, max_codes=mc)
    scanned = r.get("scanned", r.get("count", 0))
    _add_log(uid, f"选股完成，扫描 {scanned} 只，命中 {r.get('count',0)} 只")
    return jsonify(r)


@app.route("/api/strategies/activate", methods=["POST"])
@require_auth
def api_activate():
    uid       = session["user_id"]
    s         = get_user_state(uid)
    fn        = request.json.get("filename")
    s["active_strategy"] = fn
    _persist_state(uid, active_strategy=fn or "")
    meta_list = sm.list_strategies()
    meta      = next((m for m in meta_list if m["file"] == fn), {})
    _add_log(uid, f"自动交易策略切换: {meta.get('name', fn) if fn else '默认'}")
    return jsonify({"ok": True, "active": fn})


@app.route("/api/strategies/rename", methods=["POST"])
@require_auth
def api_rename():
    uid    = session["user_id"]
    s      = get_user_state(uid)
    d      = request.json
    old_fn = d.get("old", "")
    new_fn = d.get("new", "")
    r      = sm.rename(old_fn, new_fn)
    if r["ok"]:
        _add_log(uid, f"策略重命名: {old_fn} → {r['file']}")
        if s.get("active_strategy") == old_fn:
            s["active_strategy"] = r["file"]
    return jsonify(r)


# ── 因子库 API ────────────────────────────────────────────────
@app.route("/api/factors")
def api_factor_list():
    return jsonify(fm.list_factors())


@app.route("/api/factors/template")
def api_factor_template():
    return jsonify({"code": fm.new_factor_template()})


@app.route("/api/factors/run", methods=["POST"])
@require_auth
def api_factor_run():
    uid = session["user_id"]
    d   = request.json
    fn  = d.get("filename", "")
    cs  = d.get("codes") or []
    _add_log(uid, f"执行因子选股: [{fn}] {'全市场' if not cs else f'{len(cs)}只'}...")
    r       = fm.run(fn, cs)
    scanned = r.get("scanned", r.get("count", 0))
    _add_log(uid, f"因子选股完成，扫描 {scanned} 只，命中 {r.get('count', 0)} 只")
    return jsonify(r)


@app.route("/api/factors/save", methods=["POST"])
@require_auth
def api_factor_save():
    uid  = session["user_id"]
    d    = request.json
    fn   = d.get("filename", "").strip()
    code = d.get("code", "")
    if not fn:
        return jsonify({"ok": False, "msg": "文件名不能为空"})
    r = fm.save(fn, code)
    if r.get("ok"):
        _add_log(uid, f"因子已保存: [{fn}]")
    return jsonify(r)


@app.route("/api/factors/delete", methods=["POST"])
@require_auth
def api_factor_delete():
    uid = session["user_id"]
    fn  = (request.json or {}).get("filename", "").strip()
    r   = fm.delete(fn)
    if r.get("ok"):
        _add_log(uid, f"因子已删除: [{fn}]")
    return jsonify(r)


@app.route("/api/factors/ai_generate", methods=["POST"])
@require_auth
def api_factor_ai_generate():
    uid  = session["user_id"]
    desc = (request.json or {}).get("description", "").strip()
    if not desc:
        return jsonify({"ok": False, "error": "描述不能为空"})
    _add_log(uid, f"AI生成因子策略: {desc[:30]}...")
    user_ai_cfg = um.get_user_ai_config(uid) or {}
    cfg = ai.load_config()
    if user_ai_cfg.get("api_key"):
        cfg.update({k: v for k, v in user_ai_cfg.items() if v})
    r = ai.generate_factor(desc, config_override=cfg)
    if r.get("ok"):
        _add_log(uid, f"AI因子生成完成，共{r.get('length', len(r.get('code','')))}字符")
    return jsonify(r)


@app.route("/api/factors/<filename>")
def api_factor_get(filename):
    return jsonify({"code": fm.get_code(filename)})


@app.route("/api/factors/evaluate", methods=["POST"])
@require_auth
def api_factor_evaluate():
    """
    因子评价：计算 IC / ICIR / RankIC / t-stat。
    ---
    parameters:
      - in: body
        schema:
          properties:
            filename: {type: string}
            codes:    {type: array, items: {type: string}}
            period:   {type: integer, default: 20}
    responses:
      200:
        description: 因子评价指标
    """
    uid = session["user_id"]
    d   = request.json or {}
    fn  = d.get("filename", "")
    cs  = d.get("codes") or []
    period = int(d.get("period", 20))
    _add_log(uid, f"因子评价: [{fn}] period={period}")
    try:
        r = fm.evaluate(fn, cs, period=period)
        return jsonify(r)
    except AttributeError:
        return jsonify({"ok": False, "error": "因子管理器暂不支持 evaluate 方法"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── 回测 API ──────────────────────────────────────────────────
@app.route("/api/backtest", methods=["POST"])
@require_auth
def api_backtest():
    err = _deduct("backtest", "策略回测")
    if err:
        return jsonify(err), 402
    uid      = session["user_id"]
    d        = request.json
    fn       = d.get("filename", "")
    code_str = sm.get_code(fn) if fn else ""
    if not code_str:
        return jsonify({"ok": False, "error": "策略代码为空"})

    raw_codes = d.get("codes")
    if raw_codes and isinstance(raw_codes, str):
        codes = [c.strip() for c in raw_codes.replace("，", ",").split(",") if c.strip()]
    elif raw_codes and isinstance(raw_codes, list):
        codes = raw_codes
    else:
        codes = []

    full_market = not codes
    mode_label  = "全市场" if full_market else f"{len(codes)}只"
    bt_kwargs   = dict(
        strategy_code   = code_str,
        codes           = codes,
        start_date      = d.get("start_date", "2024-01-01"),
        end_date        = d.get("end_date",   "2024-12-31"),
        initial_capital = float(d.get("capital",        100000)),
        max_positions   = int(d.get("max_positions",    5)),
        stop_loss       = float(d.get("stop_loss",      -0.05)),
        take_profit     = float(d.get("take_profit",    0.10)),
    )

    if full_market:
        from backtest import start_backtest_async
        task_id = start_backtest_async(**bt_kwargs)
        _add_log(uid, f"全市场回测已启动 [{fn}] {d.get('start_date')}~{d.get('end_date')} task={task_id}")
        return jsonify({"ok": True, "async": True, "task_id": task_id})
    else:
        from backtest import run_backtest
        _add_log(uid, f"回测: [{fn}] {d.get('start_date')}~{d.get('end_date')} {mode_label}")
        result = run_backtest(**bt_kwargs)
        if result.get("ok"):
            m = result["metrics"]
            _add_log(uid, f"回测完成 收益={m['total_return']}% 回撤={m['max_drawdown']}% "
                     f"胜率={m['win_rate']}% Sharpe={m['sharpe']}")
        else:
            _add_log(uid, f"回测失败: {result.get('error','')}", "error")
        return jsonify(result)


@app.route("/api/backtest/task/<task_id>")
def api_backtest_task(task_id):
    from backtest import get_backtest_task
    return jsonify(get_backtest_task(task_id))


@app.route("/api/backtest/cancel/<task_id>", methods=["POST"])
@require_auth
def api_backtest_cancel(task_id):
    uid = session["user_id"]
    from backtest import cancel_backtest_task
    result = cancel_backtest_task(task_id)
    if result.get("ok"):
        _add_log(uid, f"⏹ 回测已终止: task={task_id}", "WARN")
    return jsonify(result)


# ── 投资组合优化 API ──────────────────────────────────────────
@app.route("/api/portfolio/optimize", methods=["POST"])
@require_auth
def api_portfolio_optimize():
    """
    投资组合优化。
    ---
    parameters:
      - in: body
        schema:
          properties:
            codes:  {type: array, items: {type: string}}
            mode:   {type: string, enum: [equal_weight, max_sharpe, min_variance, risk_parity]}
            capital:{type: number}
    responses:
      200:
        description: 各股权重及建议仓位
    """
    uid = session["user_id"]
    d   = request.json or {}
    codes   = d.get("codes", [])
    mode    = d.get("mode", "equal_weight")
    capital = float(d.get("capital", get_user_state(uid)["capital"]))
    if not codes:
        return jsonify({"ok": False, "error": "codes 不能为空"})
    try:
        from portfolio_optimizer import optimize_portfolio
        result = optimize_portfolio(codes, mode=mode, capital=capital)
        _add_log(uid, f"组合优化完成: mode={mode} codes={len(codes)}")
        return jsonify(result)
    except ImportError:
        return jsonify({"ok": False, "error": "portfolio_optimizer 模块未找到"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── 市场环境 API ──────────────────────────────────────────────
@app.route("/api/market/context")
def api_market_context():
    try:
        force = request.args.get("refresh", "0") == "1"
        ctx   = mctx.get_market_regime(force_refresh=force)
        try:
            import ths_quote as tq
            client = tq.get_client()
            if client and client._ok:
                index_pairs = [(1, "000001"), (0, "399001"), (0, "399006")]
                name_map    = {"000001": "上证指数", "399001": "深证成指", "399006": "创业板指"}
                qs = client.get_index_quotes(index_pairs)
                for q in qs:
                    nm = name_map.get(q.get("code", ""), "")
                    if nm:
                        ctx.setdefault("indices", {}).setdefault(nm, {}).update({
                            "price":  round(float(q.get("price") or 0), 2),
                            "change": round(float(q.get("change_pct") or 0), 2),
                            "code":   q.get("code", ""),
                        })
        except Exception:
            pass
        return jsonify({"ok": True, **ctx})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/market/regime", methods=["POST"])
@require_auth
def api_market_regime():
    """
    市场环境检测（牛市/熊市/震荡/急跌）+ 建议仓位系数。
    ---
    responses:
      200:
        description: regime, score, position_multiplier
    """
    try:
        from market_regime import MarketRegimeDetector
        detector = MarketRegimeDetector()
        result   = detector.detect()
        return jsonify({"ok": True, **result})
    except ImportError:
        # 降级：使用 market_context
        ctx = mctx.get_market_regime(force_refresh=True)
        return jsonify({"ok": True, **ctx})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── 数据管理 API ──────────────────────────────────────────────
@app.route("/api/data/stocks")
def api_data_list():
    return jsonify(dm.list_stocks())


@app.route("/api/data/download", methods=["POST"])
@require_auth
def api_data_download():
    uid        = session["user_id"]
    d          = request.json
    raw_codes  = d.get("codes", "")
    start_date = d.get("start_date", "2020-01-01")
    if isinstance(raw_codes, str):
        codes = [c.strip() for c in raw_codes.replace("，", ",").split(",") if c.strip()]
    else:
        codes = raw_codes
    if not codes:
        return jsonify({"ok": False, "msg": "请输入股票代码"})
    task_id = str(uuid.uuid4())[:8]
    _add_log(uid, f"开始下载 {len(codes)} 只股票历史数据...")
    dm.start_download(codes, start_date, task_id)
    return jsonify({"ok": True, "task_id": task_id, "total": len(codes)})


@app.route("/api/data/progress/<task_id>")
def api_data_progress(task_id):
    return jsonify(dm.get_progress(task_id))


@app.route("/api/data/delete", methods=["POST"])
@require_auth
def api_data_delete():
    uid  = session["user_id"]
    code = request.json.get("code", "")
    r    = dm.delete_stock(code)
    if r["ok"]:
        _add_log(uid, f"已删除本地数据: {code}", "WARN")
    return jsonify(r)


@app.route("/api/data/refresh_names", methods=["POST"])
@require_auth
def api_refresh_names():
    uid = session["user_id"]
    threading.Thread(target=dm.refresh_name_cache, daemon=True).start()
    _add_log(uid, "开始刷新股票名称缓存...")
    return jsonify({"ok": True, "msg": "后台刷新中"})


@app.route("/api/data/sector/<code>")
def api_stock_sector(code):
    r       = dm.get_stock_sector(code)
    r["name"] = dm.get_stock_name(code)
    return jsonify(r)


# ── K 线 API ──────────────────────────────────────────────────
@app.route("/api/kline/<code>")
def api_kline(code):
    try:
        df = dm.get_local(code)
        if df is not None and not df.empty and len(df) >= 10:
            cols = [c for c in ["date", "open", "close", "high", "low", "volume"] if c in df.columns]
            sub  = df[cols].tail(120).copy()
            sub["date"] = sub["date"].apply(
                lambda d: d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
            )
            name = dm.get_stock_name(code) or code
            return jsonify({"ok": True, "code": code, "name": name,
                            "source": "local", "data": sub.to_dict("records")})
    except Exception:
        pass
    try:
        import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            bars = client.get_kline(code, 120)
            if bars:
                name = tq.get_stock_name(code) or code
                return jsonify({"ok": True, "code": code, "name": name, "source": "tdx", "data": bars})
    except Exception as e:
        logger.warning(f"K 线 TDX 失败: {e}")
    return jsonify({"ok": False, "data": [], "msg": "暂无数据，请先在「数据管理」下载"})


# ── AI 分析 API ───────────────────────────────────────────────
@app.route("/api/ai/config", methods=["GET"])
def ai_get_config():
    """全局 AI 配置（管理员设置的默认配置）。"""
    cfg = ai.load_config()
    key = cfg.get("api_key", "")
    if len(key) > 8:
        cfg["api_key_masked"] = key[:4] + "****" + key[-4:]
    else:
        cfg["api_key_masked"] = "****" if key else ""
    return jsonify({**cfg, "presets": ai.PROVIDER_PRESETS})


@app.route("/api/ai/config", methods=["POST"])
def ai_save_config():
    d   = request.json
    cfg = ai.load_config()
    for k in ("provider", "base_url", "model", "enabled"):
        if k in d:
            cfg[k] = d[k]
    if d.get("api_key"):
        cfg["api_key"] = d["api_key"]
    ai.save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/stocks/score", methods=["POST"])
def api_stock_score():
    d    = request.json
    code = d.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "请输入股票代码"})
    df = dm.get_local(code)
    if df is None or df.empty:
        try:
            import ths_quote as tq
            import pandas as pd
            client = tq.get_client()
            if client and client._ok:
                bars = client.get_kline(code, 60)
                if bars:
                    df = pd.DataFrame(bars)
                    df["date"] = pd.to_datetime(df["date"])
        except Exception:
            pass
    if df is None or df.empty:
        return jsonify({"ok": False, "error": "无数据，请先下载历史数据"})
    result         = scorer.score_stock(df)
    result["code"] = code
    result["name"] = dm.get_stock_name(code) or code
    return jsonify(result)


@app.route("/api/ai/analyze", methods=["POST"])
@require_auth
def ai_analyze():
    err = _deduct("ai_analysis", f"AI分析 {(request.json or {}).get('code','')}")
    if err:
        return jsonify(err), 402
    uid  = session["user_id"]
    d    = request.json
    code = d.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "请输入股票代码"})
    name = dm.get_stock_name(code) or code
    df   = dm.get_local(code)
    # 修复：本地 CSV 不存在 / 为空 / 行数 < 30（"半残"）都触发 pytdx 补抓
    # 原逻辑只在 None 或 empty 时补抓，半残 CSV 会绕过 pytdx 让 ai_analyst 直接报"数据不足"
    if df is None or df.empty or len(df) < 30:
        try:
            import ths_quote as tq
            import pandas as pd
            client = tq.get_client()
            if client and client._ok:
                bars = client.get_kline(code, 120)
                if bars:
                    df = pd.DataFrame(bars)
                    df["date"] = pd.to_datetime(df["date"])
                    # 拉到数据后保存到本地 CSV 作为缓存
                    # 下次同一只票分析时秒读，不必每次都打 TDX 服务器
                    try:
                        from pathlib import Path
                        daily_dir = Path(__file__).parent / "data" / "daily"
                        daily_dir.mkdir(parents=True, exist_ok=True)
                        df.to_csv(daily_dir / f"{code}.csv", index=False, encoding="utf-8")
                        logger.info(f"[ai_analyze] 已缓存 {code} K 线到 data/daily/{code}.csv ({len(df)} 行)")
                    except Exception as _e:
                        logger.debug(f"[ai_analyze] 缓存 {code} CSV 失败（不致命）: {_e}")
        except Exception:
            pass

    realtime_price = realtime_change = None
    try:
        import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            qs = client.get_quotes([code])
            if qs:
                q  = qs[0]
                rp = float(q.get("price") or q.get("now") or 0)
                if rp > 0:
                    realtime_price  = round(rp, 2)
                    realtime_change = round(float(q.get("change_pct", 0)), 2)
                    qname = q.get("name", "")
                    if qname and name == code:
                        name = qname
    except Exception:
        pass

    _add_log(uid, f"AI分析: {code} {name}")

    # 三级 API Key 优先级：用户 key → 管理员 key → 报错
    user_ai_cfg = um.get_user_ai_config(uid) or {}
    cfg = ai.load_config()
    if user_ai_cfg.get("api_key"):
        cfg.update({k: v for k, v in user_ai_cfg.items() if v})

    result = ai.run_analysis(code, name, df, config_override=cfg)

    if realtime_price and result.get("ok"):
        result["realtime_price"]  = realtime_price
        result["realtime_change"] = realtime_change
        if isinstance(result.get("indicators"), dict):
            result["indicators"]["price"] = realtime_price
        sc = result.get("score", {})
        if isinstance(sc.get("indicators"), dict):
            sc["indicators"]["price"] = realtime_price

    action    = result.get("summary", {}).get("action") or result.get("score", {}).get("signal") or "—"
    score_val = result.get("score", {}).get("total", "—")
    if result.get("ok"):
        _add_log(uid, f"AI分析完成: {code} {name} → {action}（评分{score_val}）")
    else:
        _add_log(uid, f"AI分析失败: {code} — {result.get('error','')}", "error")
    # 修复：递归把 NaN/Inf 换成 None，否则 Python jsonify 输出字面量 NaN，
    # 浏览器 JSON.parse 拒收（SyntaxError），前端误显示"分析失败"
    return jsonify(ai._json_safe(result))


@app.route("/api/ai/test", methods=["POST"])
def ai_test_conn():
    d   = request.json or {}
    cfg = ai.load_config()
    if d.get("api_key"):
        cfg["api_key"]  = d["api_key"]
    if d.get("base_url"):
        cfg["base_url"] = d["base_url"]
    if d.get("model"):
        cfg["model"]    = d["model"]
    if not cfg.get("api_key"):
        return jsonify({"ok": False, "error": "未配置 API Key，请先填写并保存"})
    if not cfg.get("base_url"):
        return jsonify({"ok": False, "error": "未配置 Base URL，请先填写并保存"})
    try:
        reply = ai._call_llm(cfg, [{"role": "user", "content": "请回复'OK'，不要任何其他内容。"}],
                             temperature=0.1, max_tokens=16)
        return jsonify({"ok": True, "reply": reply.strip(),
                        "model": cfg.get("model"), "base_url": cfg.get("base_url")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e),
                        "base_url": cfg.get("base_url"), "model": cfg.get("model")})


@app.route("/api/ai/generate_strategy", methods=["POST"])
@require_auth
def ai_gen_strategy():
    uid  = session["user_id"]
    d    = request.json or {}
    desc = d.get("description", "").strip()
    if not desc:
        return jsonify({"ok": False, "error": "请描述策略需求"})
    _add_log(uid, f"AI写策略: {desc[:40]}...")
    # 用户 Key → 全局 Key 优先级（与 ai_analyze 路由一致）
    user_ai_cfg = um.get_user_ai_config(uid) or {}
    cfg = ai.load_config()
    if user_ai_cfg.get("api_key"):
        cfg.update({k: v for k, v in user_ai_cfg.items() if v})
    result = ai.generate_strategy(desc, config_override=cfg)
    if result.get("ok"):
        _add_log(uid, f"AI策略生成完成，共{result.get('length', len(result.get('code','')))}字符")
    else:
        _add_log(uid, f"AI写策略失败: {result.get('error','')}", "error")
    return jsonify(result)


# ── 策略模板 API ──────────────────────────────────────────────
@app.route("/api/strategy_templates")
def api_strategy_templates():
    """获取预设策略模板列表"""
    tpl_file = BASE_DIR / "data" / "strategy_templates.json"
    try:
        templates = json.loads(tpl_file.read_text(encoding="utf-8"))
        return jsonify({"ok": True, "templates": templates})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "templates": []})


# ── 通知配置 API ──────────────────────────────────────────────
@app.route("/api/user/notify_config", methods=["GET"])
@require_auth
def user_get_notify_config():
    cfg = nm.load_notify_config()
    # 隐藏敏感字段
    safe = json.loads(json.dumps(cfg))
    for ch in ("wecom", "feishu", "dingtalk"):
        wh = safe.get(ch, {}).get("webhook", "")
        if wh and len(wh) > 20:
            safe[ch]["webhook"] = wh[:12] + "****" + wh[-8:]
    tg = safe.get("telegram", {})
    if tg.get("token") and len(tg["token"]) > 10:
        tg["token"] = tg["token"][:8] + "****"
    return jsonify({"ok": True, **safe})


@app.route("/api/user/notify_config", methods=["POST"])
@require_auth
def user_save_notify_config():
    uid = session["user_id"]
    d   = request.json or {}
    # 加载已有配置，合并更新（避免覆盖 ****）
    existing = nm.load_notify_config()
    for channel in ("wecom", "feishu", "dingtalk", "telegram"):
        if channel in d and isinstance(d[channel], dict):
            if channel not in existing:
                existing[channel] = {}
            for k, v in d[channel].items():
                # 忽略含 **** 的字段（前端回传了脱敏值）
                if "****" not in str(v):
                    existing[channel][k] = v
    nm.save_notify_config(existing)
    _add_log(uid, "消息推送配置已更新")
    return jsonify({"ok": True})


@app.route("/api/notify/test", methods=["POST"])
@require_auth
def api_notify_test():
    """测试推送（发送测试消息到所有启用渠道）"""
    uid = session["user_id"]
    result = nm.send_notification(
        title="🔔 见微 Alpha 推送测试",
        content=f"测试消息发送成功！\n用户：{session.get('username','')}\n时间：{datetime.now().strftime('%H:%M:%S')}",
    )
    _add_log(uid, f"推送测试: {'成功' if result.get('ok') else '失败'} {result.get('results', {})}")
    return jsonify(result)


@app.route("/api/stock/news/<code>")
@require_auth
def api_stock_news(code):
    """获取个股新闻 + 情绪分析（search_manager 五级降级）"""
    try:
        name = dm.get_stock_name(code) or code
        from news_fetcher import get_stock_news, _simple_sentiment, news_to_context_text
        news = get_stock_news(code, limit=8, name=name)
        sentiment = _simple_sentiment(news)
        return jsonify({
            "ok": True,
            "news": news,
            "sentiment": sentiment,
            "context_text": news_to_context_text(news, code),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "news": [], "sentiment": {}})


# ── 搜索 API ──────────────────────────────────────────
@app.route("/api/search/news/<code>")
@require_auth
def api_search_news(code):
    """个股新闻搜索（Tavily→SerpAPI→直接抓取）"""
    name  = dm.get_stock_name(code) or request.args.get("name", "")
    limit = int(request.args.get("limit", 8))
    force = request.args.get("refresh", "0") == "1"
    try:
        results = search_mgr.search_stock_news(code, name=name, limit=limit,
                                               force_refresh=force)
        return jsonify({"ok": True, "results": results, "count": len(results),
                        "code": code, "name": name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "results": []})


@app.route("/api/search/web", methods=["POST"])
@require_auth
def api_search_web():
    """通用网页搜索（Tavily→SerpAPI）"""
    d     = request.json or {}
    query = d.get("query", "").strip()
    if not query:
        return jsonify({"ok": False, "error": "query 不能为空"})
    topic = d.get("topic", "general")   # general | news | research
    limit = int(d.get("limit", 10))
    force = bool(d.get("force_refresh", False))
    try:
        results = search_mgr.search_web(query, topic=topic, limit=limit,
                                        force_refresh=force)
        return jsonify({"ok": True, "results": results, "count": len(results)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "results": []})


@app.route("/api/search/stock_info/<code>")
@require_auth
def api_search_stock_info(code):
    """综合股票信息（新闻+网页+TwelveData报价）"""
    name = dm.get_stock_name(code) or code
    try:
        result = search_mgr.search_stock_info(code, name=name)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/search/twelvedata/quote/<code>")
@require_auth
def api_td_quote(code):
    """TwelveData 实时报价（A股）"""
    try:
        q = search_mgr.td_get_quote(code)
        return jsonify({"ok": True, **q})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/search/twelvedata/kline/<code>")
@require_auth
def api_td_kline(code):
    """TwelveData K线数据（A股）"""
    days     = int(request.args.get("days", 60))
    interval = request.args.get("interval", "1day")
    try:
        bars = search_mgr.td_get_kline(code, days=days, interval=interval)
        return jsonify({"ok": True, "data": bars, "count": len(bars)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/search/symbol_search")
def api_symbol_search():
    """TwelveData 股票代码/名称搜索"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"ok": False, "error": "q 不能为空"})
    try:
        results = search_mgr.td_symbol_search(q)
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "results": []})


@app.route("/api/search/stats")
@require_auth
def api_search_stats():
    """搜索服务用量统计"""
    return jsonify({"ok": True, **search_mgr.get_stats()})


@app.route("/api/search/status")
def api_search_status():
    """搜索服务状态（是否有可用额度）"""
    return jsonify({"ok": True, **search_mgr.get_service_status()})


@app.route("/api/search/config", methods=["GET"])
@require_admin
def api_search_config_get():
    """获取搜索服务配置（管理员）"""
    cfg = search_mgr.get_config()
    # 脱敏 key
    safe = json.loads(json.dumps(cfg))
    for svc in ("tavily", "serpapi", "twelvedata"):
        for k in safe.get(svc, {}).get("keys", []):
            raw = k.get("key", "")
            if len(raw) > 12:
                k["key_masked"] = raw[:6] + "****" + raw[-4:]
    return jsonify({"ok": True, "config": safe})


@app.route("/api/search/config", methods=["POST"])
@require_admin
def api_search_config_save():
    """保存搜索服务配置（管理员）"""
    d = request.json or {}
    existing = search_mgr.get_config()
    # 合并（保留未变更的 key）
    for svc in ("tavily", "serpapi", "twelvedata"):
        if svc in d:
            if svc not in existing:
                existing[svc] = {}
            for k2, v2 in d[svc].items():
                if k2 == "keys":
                    # 按 label 匹配更新（不覆盖 ****）
                    for new_key in v2:
                        for old_key in existing[svc].get("keys", []):
                            if old_key.get("label") == new_key.get("label"):
                                for field, val in new_key.items():
                                    if field != "key" or "****" not in str(val):
                                        old_key[field] = val
                else:
                    existing[svc][k2] = v2
    search_mgr.save_config(existing)
    return jsonify({"ok": True})


# ── 日报 API ──────────────────────────────────────────────────

def _get_market_review() -> dict:
    """大盘复盘：指数行情 + 市场宽度 + 板块轮动（含缓存）"""
    import pandas as _pd
    from datetime import datetime as _dt
    review = {
        "date":    _dt.now().strftime("%Y-%m-%d"),
        "time":    _dt.now().strftime("%H:%M"),
        "indices": {},
        "breadth": {},
        "sectors": {"top_gainers": [], "top_losers": []},
    }

    # 1. 指数实时价格 —— 优先 TDX，降级 akshare
    index_meta = [(1, "000001", "上证指数"), (0, "399001", "深证成指"), (0, "399006", "创业板指")]
    try:
        import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            pairs    = [(mkt, code) for mkt, code, _ in index_meta]
            qs       = client.get_index_quotes(pairs)
            name_map = {code: name for _, code, name in index_meta}
            for q in qs:
                nm = name_map.get(q.get("code", ""), "")
                if nm:
                    review["indices"][nm] = {
                        "code":   q.get("code", ""),
                        "price":  round(float(q.get("price") or 0), 2),
                        "change": round(float(q.get("change_pct") or 0), 2),
                    }
    except Exception:
        pass

    if not review["indices"]:
        try:
            import akshare as ak
            code_map = {"000001": "上证指数", "399001": "深证成指", "399006": "创业板指"}
            df_all = ak.stock_zh_index_spot_em()
            if df_all is not None and not df_all.empty:
                for _, row in df_all.iterrows():
                    code = str(row.get("代码", ""))
                    nm   = code_map.get(code, "")
                    if nm:
                        review["indices"][nm] = {
                            "code":   code,
                            "price":  round(float(row.get("最新价", 0) or 0), 2),
                            "change": round(float(row.get("涨跌幅", 0) or 0), 2),
                        }
        except Exception:
            pass

    # 2. 市场宽度（全市场快照）
    try:
        import akshare as ak
        df_spot = ak.stock_zh_a_spot_em()
        if df_spot is not None and not df_spot.empty and "涨跌幅" in df_spot.columns:
            chg = _pd.to_numeric(df_spot["涨跌幅"], errors="coerce").dropna()
            review["breadth"] = {
                "up":       int((chg > 0).sum()),
                "down":     int((chg < 0).sum()),
                "flat":     int((chg == 0).sum()),
                "limit_up": int((chg >= 9.9).sum()),
                "limit_dn": int((chg <= -9.9).sum()),
                "total":    len(chg),
            }
    except Exception:
        pass

    # 3. 板块轮动（行业板块）
    try:
        import akshare as ak
        df_sec = ak.stock_board_industry_rank_em(symbol="沪深A股")
        if df_sec is not None and not df_sec.empty:
            chg_col  = next((c for c in df_sec.columns if "涨跌幅" in c), None)
            name_col = df_sec.columns[0]
            if chg_col:
                df_sec[chg_col] = _pd.to_numeric(df_sec[chg_col], errors="coerce")
                df_s = df_sec.dropna(subset=[chg_col]).sort_values(chg_col, ascending=False)
                review["sectors"]["top_gainers"] = df_s.head(3)[name_col].tolist()
                review["sectors"]["top_losers"]  = df_s.tail(3)[name_col].tolist()
    except Exception:
        pass

    return review


@app.route("/api/market/review")
def api_market_review():
    """大盘复盘（公开接口）"""
    try:
        return jsonify({"ok": True, **_get_market_review()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/dashboard/generate", methods=["POST"])
@require_auth
def api_dashboard_generate():
    """
    决策仪表盘生成（批量评分 + 新闻情绪 + 可选AI摘要）。
    请求体: {"codes": ["000657", "600105"], "with_ai": true}
    """
    d       = request.json or {}
    codes   = [str(c).strip() for c in d.get("codes", []) if str(c).strip()]
    with_ai = bool(d.get("with_ai", True))
    if not codes or len(codes) > 20:
        return jsonify({"ok": False, "error": "codes 需要 1~20 个"})

    uid  = session["user_id"]
    try:
        cfg = um.get_user_ai_config(uid) or {}
    except Exception:
        cfg = {}
    has_ai = bool(cfg.get("api_key")) and with_ai

    results = []
    import news_fetcher as _nf
    import traceback as _tb

    for code in codes:
        name = dm.get_stock_name(code) or code
        item = {
            "code": code, "name": name,
            "score": None, "signal": "N/A", "signal_color": "#888",
            "sentiment": "中性", "sentiment_score": 0,
            "news": [], "risks": [], "catalysts": [],
            "ai_summary": "", "ai_outlook": "", "latest": "",
        }

        # ── 六维评分 ────────────────────────────────────────
        try:
            df_k = dm.get_local(code)
            if df_k is not None and not df_k.empty:
                scored = scorer.score_stock(df_k)
                if scored.get("ok"):
                    item["score"]        = scored["total"]
                    item["signal"]       = scored["signal"]
                    item["signal_color"] = scored.get("signal_color", "#888")
                    item["risks"]        = scored.get("risks", [])
                    item["catalysts"]    = scored.get("catalysts", [])
        except Exception as ex:
            logger.debug(f"[dashboard] score {code}: {ex}")

        # ── 新闻 + 情绪 ─────────────────────────────────────
        try:
            nd = _nf.get_news_with_sentiment(code, limit=5)
            item["news"]            = nd.get("news", [])
            sent                    = nd.get("sentiment", {})
            item["sentiment"]       = sent.get("label", "中性")
            item["sentiment_score"] = sent.get("score", 0)
            if item["news"]:
                item["latest"] = item["news"][0].get("title", "")
        except Exception as ex:
            logger.debug(f"[dashboard] news {code}: {ex}")

        # ── 可选 AI 摘要 ─────────────────────────────────────
        if has_ai and item["news"]:
            try:
                news_text = "\n".join(f"- {n['title']}" for n in item["news"][:4])
                prompt = (
                    f"股票：{name}（{code}）\n"
                    f"六维评分：{item['score']}，信号：{item['signal']}\n"
                    f"近期新闻：\n{news_text}\n\n"
                    "请各用一句话回答：\n"
                    "舆情：<市场情绪摘要>\n"
                    "预期：<业绩/基本面预期>"
                )
                import ai_analyst as _aa
                reply = _aa._call_llm(cfg, [
                    {"role": "system", "content": "你是A股分析助手，中文简洁回答，每句话不超过50字。"},
                    {"role": "user",   "content": prompt},
                ], max_tokens=150, timeout=30)
                # 解析舆情/预期
                for line in reply.splitlines():
                    if line.startswith("舆情"):
                        item["ai_summary"] = line.split("：", 1)[-1].strip()
                    elif line.startswith("预期"):
                        item["ai_outlook"] = line.split("：", 1)[-1].strip()
                if not item["ai_summary"]:
                    item["ai_summary"] = reply.strip()[:80]
            except Exception as ex:
                logger.debug(f"[dashboard] AI summary {code}: {ex}")

        results.append(item)

    try:
        buy   = sum(1 for r in results if r["signal"] in ("买入", "强烈买入"))
        watch = sum(1 for r in results if r["signal"] == "观望")
        hold  = sum(1 for r in results if r["signal"] == "持有")
        sell  = sum(1 for r in results if r["signal"] == "卖出")

        from datetime import datetime as _dt
        return jsonify({
            "ok": True,
            "date":   _dt.now().strftime("%Y-%m-%d"),
            "time":   _dt.now().strftime("%H:%M"),
            "total":  len(results),
            "buy":    buy,
            "watch":  watch,
            "hold":   hold,
            "sell":   sell,
            "stocks": results,
        })
    except Exception as ex:
        logger.error(f"[dashboard] jsonify error: {ex}\n{_tb.format_exc()}")
        return jsonify({"ok": False, "error": f"序列化失败: {ex}"})


# ══════════════════════════════════════════════════════════════
# 调度器（多用户并发扫描）
# ══════════════════════════════════════════════════════════════
DABAN_INTERVAL = 5

_last_scan:     dict[int, float] = {}
_scan_running:  dict[int, bool]  = {}


def _scan_thread_worker(uid: int):
    """单用户扫描线程入口，防止重复并发。"""
    if _scan_running.get(uid):
        return
    _scan_running[uid] = True
    try:
        do_scan(uid)
    finally:
        _scan_running[uid] = False


def run_scheduler():
    """
    主调度循环：
    - 每秒检查所有 running 用户
    - 普通策略：每 SCAN_INTERVAL 秒触发一次
    - 打板策略：每 DABAN_INTERVAL 秒触发一次
    """
    now_ts = time.time()
    while True:
        now_ts = time.time()
        with _user_state_lock:
            active_uids = [uid for uid, s in user_states.items() if s.get("running")]

        for uid in active_uids:
            s       = user_states.get(uid, {})
            active  = s.get("active_strategy")
            is_dbn  = (active == "da_ban")
            interval = DABAN_INTERVAL if is_dbn else SCAN_INTERVAL
            last     = _last_scan.get(uid, 0)
            if now_ts - last >= interval:
                _last_scan[uid] = now_ts
                threading.Thread(target=_scan_thread_worker, args=(uid,), daemon=True).start()

        time.sleep(1)


# ══════════════════════════════════════════════════════════════
# 启动
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    _restore_all_user_states()
    threading.Thread(target=run_scheduler, daemon=True).start()
    logger.info(f"服务启动 DRY_RUN={'开启' if DRY_RUN else '关闭'}")
    app.run(port=5001, debug=False)
