import os as _os
# ── 清除系统代理（akshare 走国内接口，代理反而导致连接失败）──────────
for _k in ("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy",
           "ALL_PROXY","all_proxy","SOCKS_PROXY","socks_proxy"):
    _os.environ.pop(_k, None)

from flask import Flask, jsonify, request, send_from_directory, session
from flask_cors import CORS
from functools import wraps
import threading, schedule, time, logging, uuid, secrets
from typing import Optional
from datetime import datetime
from market_data import is_trading_time
from strategy import scan_all
from ths_operator import THSOperator
from config import STOCK_POOL, SCAN_INTERVAL, DRY_RUN, INITIAL_CAPITAL, POSITION_RATIO, MAX_DAILY_TRADES
import strategy_manager as sm
import data_manager as dm
import ai_analyst as ai
import factor_manager as fm
import stock_scorer as scorer
import market_context as mctx
import paper_broker as pb
import user_manager as um
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")
app.secret_key = _os.environ.get("SECRET_KEY") or secrets.token_hex(32)
CORS(app, supports_credentials=True)

# ── 初始化用户数据库 ──────────────────────────────────────
um.init_db()


# ── 认证装饰器 ────────────────────────────────────────────
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
    """扣积分，返回 None 表示成功；返回 dict 表示失败（直接作为响应）"""
    uid = session.get("user_id")
    if not uid:
        return {"ok": False, "error": "请先登录", "code": 401}
    r = um.deduct_points(uid, action, desc)
    if not r["ok"]:
        return r
    return None

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


# ══════════════════════════════════════════════════════════
# 认证 API
# ══════════════════════════════════════════════════════════
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
        "ok":       True,
        "logged_in": True,
        "user_id":  user["id"],
        "username": user["username"],
        "points":   user["points"],
        "is_admin": bool(user["is_admin"]),
    })


# ══════════════════════════════════════════════════════════
# 用户自助 API
# ══════════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════════
# 管理员 API
# ══════════════════════════════════════════════════════════
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
    # 禁止管理员封禁自己
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


@app.route("/api/user/change_password", methods=["POST"])
@require_auth
def user_change_password():
    d = request.json or {}
    return jsonify(um.change_password(
        session["user_id"],
        d.get("old_password", ""),
        d.get("new_password", ""),
    ))

state = {
    "running": False,
    "pool": [], "queue": [], "logs": [],
    "scanned": 0, "executed": 0,
    "active_strategy": None,
    # ── 账户与仓位管理 ──────────────────────────
    "capital":          INITIAL_CAPITAL,
    "position_ratio":   POSITION_RATIO,
    "max_daily_trades": MAX_DAILY_TRADES,
    "today_trades":     0,
    "today_date":       "",
    "positions":        {},
    # ── 自动执行开关（默认关，需手动开启）───────
    "auto_execute":     False,
    # ── 扫描进度 ─────────────────────────────
    "scan_progress":    {"running": False, "stage": "", "current": 0, "total": 0},
}
operator = THSOperator()


def _reset_daily_if_needed():
    """每个交易日开始时清零今日计数"""
    today = datetime.now().strftime("%Y-%m-%d")
    if state["today_date"] != today:
        state["today_date"]   = today
        state["today_trades"] = 0
        add_log(f"📅 新交易日 {today}，今日开仓次数已清零")


def add_log(msg, level="info"):
    t = datetime.now().strftime("%H:%M:%S")
    state["logs"].insert(0, {"time": t, "msg": msg, "level": level})
    state["logs"] = state["logs"][:100]
    logger.info(msg)


def do_scan():
    if not state["running"] or not is_trading_time():
        return
    _reset_daily_if_needed()
    _check_paper_risk()   # 先检查风控，触发的卖出会记入模拟盘

    active = state.get("active_strategy")
    is_daban = (active == "da_ban")

    # 所有策略均扫描全市场（codes=[] 由 strategy_manager 处理）
    scan_codes = []
    scan_label = "全市场"
    add_log(f"扫描 {scan_label} 策略=[{active or '默认'}]...")

    # 更新扫描进度
    state["scan_progress"] = {
        "running": True,
        "stage": f"扫描{scan_label}",
        "current": 0,
        "total": 5200 if is_daban else len(STOCK_POOL),
    }

    if active:
        ret     = sm.run(active, scan_codes)
        results = [{"code": r.get("code",""), "name": r.get("name",""),
                    "price": r.get("price",0), "change": r.get("change",0),
                    "pass_count": 5,
                    "signal":       r.get("signal", True),
                    "consec_limit": r.get("consec_limit", 0),
                    "consec_label": r.get("consec_label", ""),
                    "reason": r.get("reason",""), "conditions": {}}
                   for r in ret.get("results", [])]
        state["scanned"] += ret.get("scanned", ret.get("count", 0))
    else:
        results = scan_all(STOCK_POOL)
        state["scanned"] += len(STOCK_POOL)

    state["pool"] = results

    # 日内限制：打板策略每日最多 max_daily_trades 只
    remaining = state["max_daily_trades"] - state["today_trades"]
    if is_daban and remaining <= 0:
        add_log(f"⛔ 今日已开仓 {state['today_trades']} 只，达到日内上限，跳过入队")
        return

    added = 0
    # 只有 signal=True 的才加入买入队列
    buy_candidates = [r for r in results if r.get("signal", True)]
    for r in buy_candidates:
        if is_daban and added >= remaining:
            break
        if not any(q["code"] == r["code"] for q in state["queue"]):
            # 计算本次买入股数（仓位比例 × 可用资金 / 价格，向下取整到100股）
            price = r["price"] or 1
            budget = state["capital"] * state["position_ratio"]
            shares = int(budget / price / 100) * 100
            shares = max(shares, 100)   # 至少100股

            item = {
                "code":    r["code"],
                "name":    r["name"],
                "price":   price,
                "shares":  shares,
                "budget":  round(budget, 2),
                "pass_count": r.get("pass_count", 5),
                "reason":  r.get("reason",""),
                "action":  "buy",
                "status":  "pending",
            }
            state["queue"].append(item)
            add_log(f"📌 {r['code']} {r['name']} 进入队列 | "
                    f"计划买入{shares}股 预算¥{budget:,.0f}", "buy")
            added += 1

            # ── 自动执行 ─────────────────────────────
            if state.get("auto_execute"):
                _auto_buy(item)

    buy_signals = len([r for r in results if r.get("signal", True)])
    display_only = len(results) - buy_signals
    msg = f"扫描完成，打板信号 {buy_signals} 只"
    if display_only > 0:
        msg += f"，已封板参考 {display_only} 只"
    if is_daban:
        msg += f"，今日剩余可开仓 {remaining-added} 次"
    add_log(msg)
    state["scan_progress"] = {"running": False, "stage": "完成", "current": 0, "total": 0}


def execute_buy(code: str, name: str, price: float, shares: int, reason: str = "") -> bool:
    """
    统一买入入口：
      DRY_RUN=True  → paper_broker 模拟盘（持久化）
      DRY_RUN=False → 实盘操作（THSOperator）
    """
    if DRY_RUN:
        broker = pb.get_broker()
        result = broker.submit_buy(code, name, price, shares, reason=reason)
        if result.get("ok"):
            state["executed"]    += 1
            state["today_trades"] = state.get("today_trades", 0) + 1
            # 同步持仓到 state（兼容旧的 /api/positions API）
            state["positions"][code] = {
                "name": name, "shares": shares, "cost": result["exec_price"],
                "date": datetime.now().strftime("%Y-%m-%d")
            }
            add_log(
                f"✅ [模拟] 买入 {code} {name} {shares}股 @{result['exec_price']:.2f} "
                f"花费¥{result['total_cost']:,.0f} 佣金¥{result['commission']:.1f} "
                f"(今日第{state['today_trades']}只)", "buy"
            )
            return True
        else:
            add_log(f"❌ [模拟] 买入拒绝 {code}: {result.get('msg','')}", "error")
            return False
    else:
        ok = operator.buy(code, price, shares)
        if ok:
            state["executed"]    += 1
            state["today_trades"] = state.get("today_trades", 0) + 1
            state["positions"][code] = {
                "name": name, "shares": shares, "cost": price,
                "date": datetime.now().strftime("%Y-%m-%d")
            }
        return ok


def execute_sell(code: str, price: float, sell_reason: str = "手动卖出") -> dict:
    """
    统一卖出入口。
    返回 {"ok": bool, "pnl": float, ...}
    """
    pos = state["positions"].get(code)
    if DRY_RUN:
        broker = pb.get_broker()
        result = broker.submit_sell(code, price, sell_reason=sell_reason)
        if result.get("ok"):
            state["executed"] += 1
            state["positions"].pop(code, None)
            pnl = result.get("pnl", 0)
            sign = "✅" if pnl >= 0 else "❌"
            add_log(f"{sign} [模拟] 卖出 {code} @{result['exec_price']:.2f} "
                    f"盈亏¥{pnl:+,.2f}({result.get('pnl_pct',0):+.2f}%) "
                    f"原因:{sell_reason}",
                    "buy" if pnl >= 0 else "error")
        return result
    else:
        shares = pos["shares"] if pos else 0
        ok     = operator.sell(code, price, shares)
        if ok:
            pnl = round((price - pos["cost"]) * shares, 2) if pos else 0
            state["executed"] += 1
            state["positions"].pop(code, None)
            return {"ok": True, "pnl": pnl}
        return {"ok": False, "msg": "实盘卖出失败"}


def _auto_buy(item: dict):
    """自动买入：信号触发后立即执行，无需手动确认"""
    code   = item["code"]
    name   = item.get("name", "")
    price  = item["price"]
    shares = item.get("shares", 100)
    try:
        add_log(f"🤖 自动买入触发: {code} {name} {shares}股 @{price}", "buy")
        ok = execute_buy(code, name, price, shares, reason="自动买入")
        item["status"] = "done" if ok else "error"
        if not ok and not DRY_RUN:
            add_log(f"❌ 自动买入失败: {code} @{price}，请检查同花顺窗口", "error")
    except Exception as e:
        item["status"] = "error"
        add_log(f"❌ 自动买入异常: {code} — {e}", "error")


def _check_paper_risk():
    """
    每次扫描前检查模拟盘持仓风控（止损/止盈/超期）。
    仅在 DRY_RUN=True 时运行。
    """
    if not DRY_RUN:
        return
    try:
        broker = pb.get_broker()
        # 获取持仓代码 → 批量拉实时价格
        positions = broker.get_positions()
        if not positions:
            return
        codes = [p["code"] for p in positions]
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
        for t in triggered:
            pnl   = t.get("pnl", 0)
            sign  = "✅" if pnl >= 0 else "❌"
            add_log(f"{sign} [风控] {t['code']} {t['name']} 触发{t['reason']} "
                    f"盈亏¥{pnl:+,.2f}",
                    "buy" if pnl >= 0 else "error")
            # 同步 state
            state["positions"].pop(t["code"], None)
    except Exception as e:
        logger.warning(f"[风控检查] 异常: {e}")


# ── 监控 API ──────────────────────────────────────
@app.route("/api/status")
def get_status():
    # 合并 ths_quote 的实时扫描进度（每80只/批更新一次）
    try:
        import ths_quote as tq
        tdx_prog = tq.get_scan_progress()
        if tdx_prog.get("running"):
            state["scan_progress"].update({
                "running": True,
                "current": tdx_prog.get("current", 0),
                "total":   tdx_prog.get("total",   0),
            })
    except Exception:
        pass
    return jsonify(state)

@app.route("/api/start", methods=["POST"])
def start():
    state["running"] = True
    add_log("监控启动")
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def stop():
    state["running"] = False
    add_log("监控停止", "warn")
    return jsonify({"ok": True})

@app.route("/api/auto_execute", methods=["POST"])
def set_auto_execute():
    enabled = request.json.get("enabled", False)
    state["auto_execute"] = bool(enabled)
    mode = "开启" if enabled else "关闭"
    add_log(f"🤖 自动执行已{mode} — {'信号触发即自动下单' if enabled else '手动确认模式'}", "warn")
    return jsonify({"ok": True, "auto_execute": state["auto_execute"]})

@app.route("/api/execute", methods=["POST"])
def execute():
    data   = request.json
    code   = data.get("code")
    action = data.get("action")
    item   = next((q for q in state["queue"] if q["code"] == code), None)

    if not item:
        if action == "buy":
            price  = float(data.get("price", 0))
            name   = data.get("name", code)
            shares = data.get("shares")
            if not shares:
                budget = state["capital"] * state["position_ratio"]
                shares = int(budget / price / 100) * 100 if price > 0 else 100
                shares = max(shares, 100)
            shares = int(shares)
            ok = execute_buy(code, name, price, shares, reason="手动买入")
            if not ok and not DRY_RUN:
                add_log(f"❌ 买入失败 {code} {name} {shares}股 @{price:.2f}", "error")
            return jsonify({"ok": ok})
        return jsonify({"ok": False, "msg": "未找到"})

    if action == "skip":
        item["status"] = "skip"

    elif action == "buy":
        price  = item["price"]
        name   = item.get("name", code)
        shares = item.get("shares", 100)
        ok = execute_buy(code, name, price, shares, reason="队列买入")
        item["status"] = "done" if ok else "error"
        if not ok and not DRY_RUN:
            add_log(f"❌ 买入失败 {code} @{price}", "error")

    return jsonify({"ok": True})


# ── 账户 API ──────────────────────────────────────
@app.route("/api/account", methods=["GET"])
def get_account():
    return jsonify({
        "capital":          state["capital"],
        "position_ratio":   state["position_ratio"],
        "max_daily_trades": state["max_daily_trades"],
        "today_trades":     state["today_trades"],
        "positions":        state["positions"],
    })

@app.route("/api/account", methods=["POST"])
def set_account():
    d = request.json
    if "capital" in d:
        state["capital"] = float(d["capital"])
    if "position_ratio" in d:
        state["position_ratio"] = float(d["position_ratio"])
    if "max_daily_trades" in d:
        state["max_daily_trades"] = int(d["max_daily_trades"])
    add_log(f"账户设置更新: 资金¥{state['capital']:,.0f} "
            f"仓位{state['position_ratio']*100:.0f}% "
            f"日限{state['max_daily_trades']}只")
    return jsonify({"ok": True})

@app.route("/api/queue/clear", methods=["POST"])
def clear_queue():
    state["queue"] = []
    add_log("🗑 队列已清空", "warn")
    return jsonify({"ok": True})

@app.route("/api/queue/add", methods=["POST"])
def queue_add():
    """仅将股票加入队列（不立即执行），供前端手动操作"""
    d = request.json
    code  = d.get("code","")
    price = float(d.get("price", 0))
    name  = d.get("name", code)
    if not code:
        return jsonify({"ok": False, "msg": "code 缺失"})
    if any(q["code"] == code for q in state["queue"]):
        return jsonify({"ok": True, "msg": "已在队列"})
    budget = state["capital"] * state["position_ratio"]
    shares = int(budget / price / 100) * 100 if price > 0 else 100
    shares = max(shares, 100)
    item = {
        "code": code, "name": name, "price": price,
        "shares": shares, "budget": round(budget, 2),
        "pass_count": 5, "reason": d.get("reason","手动加入"),
        "action": "buy", "status": "pending",
    }
    state["queue"].append(item)
    add_log(f"📌 手动加入队列: {code} {name} 计划{shares}股 预算¥{budget:,.0f}", "buy")
    # 若自动执行已开启，立即买入
    if state.get("auto_execute"):
        _auto_buy(item)
    return jsonify({"ok": True, "msg": f"{code} {name} 已入队，计划{shares}股"})


# ── 持仓 API ──────────────────────────────────────
@app.route("/api/positions")
def get_positions_api():
    positions = state.get("positions", {})
    if not positions:
        return jsonify([])
    result = []
    # 尝试获取实时价格
    live = {}
    try:
        import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            qs = client.get_quotes(list(positions.keys()))
            live = {q["code"]: q["price"] for q in qs if q.get("price", 0) > 0}
    except Exception:
        pass

    for code, pos in positions.items():
        cost      = pos.get("cost", 0)
        shares    = pos.get("shares", 0)
        cur_price = live.get(code, cost)
        pnl_pct   = round((cur_price - cost) / cost * 100, 2) if cost > 0 else 0
        pnl_amt   = round((cur_price - cost) * shares, 2)
        # 名称：先从持仓记录找，再从 ths_quote 缓存找，再从 dm 找
        name = pos.get("name", "")
        if not name:
            try:
                import ths_quote as tq
                name = tq.get_stock_name(code)
            except Exception:
                pass
        if not name:
            name = dm.get_stock_name(code) or code
        result.append({
            "code":         code,
            "name":         name,
            "shares":       shares,
            "cost":         round(cost, 2),
            "cur_price":    round(cur_price, 2),
            "pnl_pct":      pnl_pct,
            "pnl_amount":   pnl_amt,
            "market_value": round(cur_price * shares, 2),
            "date":         pos.get("date", ""),
        })
    return jsonify(result)

@app.route("/api/positions/sell", methods=["POST"])
def sell_position():
    d     = request.json
    code  = d.get("code","")
    pos   = state["positions"].get(code)
    if not pos and not DRY_RUN:
        return jsonify({"ok": False, "msg": "持仓不存在"})
    # 模拟盘：从 paper_broker 获取持仓价格
    if DRY_RUN:
        broker  = pb.get_broker()
        broker_positions = {p["code"]: p for p in broker.get_positions()}
        bp = broker_positions.get(code)
        if not bp:
            return jsonify({"ok": False, "msg": "模拟盘持仓不存在"})
        price = float(d.get("price", bp["cur_price"] or bp["cost_price"]))
    else:
        price = float(d.get("price", pos.get("cost", 0)))
    result = execute_sell(code, price, sell_reason="手动卖出")
    return jsonify(result)

@app.route("/api/positions/close", methods=["POST"])
def close_position():
    """平仓（模拟模式直接移除持仓记录）"""
    code = request.json.get("code","")
    if code in state["positions"]:
        del state["positions"][code]
        add_log(f"🔻 平仓: {code}", "warn")
    return jsonify({"ok": True})


# ── 策略 API ──────────────────────────────────────
@app.route("/api/strategies")
def api_list():
    return jsonify(sm.list_strategies())

@app.route("/api/strategies/<filename>")
def api_get(filename):
    return jsonify({"code": sm.get_code(filename)})

@app.route("/api/strategies/save", methods=["POST"])
def api_save():
    d = request.json
    r = sm.save(d.get("filename",""), d.get("code",""))
    if r["ok"]:
        add_log(f"策略已保存: {r['file']}")
    return jsonify(r)

@app.route("/api/strategies/delete", methods=["POST"])
def api_delete():
    fn = request.json.get("filename","")
    r  = sm.delete(fn)
    if r["ok"]:
        add_log(f"策略已删除: {fn}", "warn")
        if state.get("active_strategy") == fn:
            state["active_strategy"] = None
    return jsonify(r)

@app.route("/api/strategies/run", methods=["POST"])
@require_auth
def api_run():
    err = _deduct("scan", "全市场选股扫描")
    if err:
        return jsonify(err), 402
    d  = request.json
    fn = d.get("filename","")
    # codes 为空 → strategy_manager 自动全市场两阶段扫描
    cs = d.get("codes") or []
    add_log(f"执行选股: [{fn}] {'全市场' if not cs else f'{len(cs)}只'}...")
    r  = sm.run(fn, cs)
    scanned = r.get("scanned", r.get("count", 0))
    add_log(f"选股完成，扫描 {scanned} 只，命中 {r.get('count',0)} 只")
    return jsonify(r)

@app.route("/api/strategies/activate", methods=["POST"])
def api_activate():
    fn = request.json.get("filename")
    state["active_strategy"] = fn
    meta_list = sm.list_strategies()
    meta      = next((m for m in meta_list if m["file"] == fn), {})
    add_log(f"自动交易策略切换: {meta.get('name', fn) if fn else '默认'}")
    return jsonify({"ok": True, "active": fn})

@app.route("/api/strategies/rename", methods=["POST"])
def api_rename():
    d      = request.json
    old_fn = d.get("old", "")
    new_fn = d.get("new", "")
    r      = sm.rename(old_fn, new_fn)
    if r["ok"]:
        add_log(f"策略重命名: {old_fn} → {r['file']}")
        if state.get("active_strategy") == old_fn:
            state["active_strategy"] = r["file"]
    return jsonify(r)


# ── 因子库 API ─────────────────────────────────────
@app.route("/api/factors")
def api_factor_list():
    return jsonify(fm.list_factors())

# ── 注意：具体路由必须在 /<filename> 之前注册 ──────────────────
@app.route("/api/factors/template")
def api_factor_template():
    return jsonify({"code": fm.new_factor_template()})

@app.route("/api/factors/run", methods=["POST"])
def api_factor_run():
    d  = request.json
    fn = d.get("filename", "")
    cs = d.get("codes") or []
    add_log(f"执行因子选股: [{fn}] {'全市场' if not cs else f'{len(cs)}只'}...")
    r  = fm.run(fn, cs)
    scanned = r.get("scanned", r.get("count", 0))
    add_log(f"因子选股完成，扫描 {scanned} 只，命中 {r.get('count', 0)} 只")
    return jsonify(r)

@app.route("/api/factors/save", methods=["POST"])
def api_factor_save():
    d  = request.json
    fn = d.get("filename", "").strip()
    code = d.get("code", "")
    if not fn:
        return jsonify({"ok": False, "msg": "文件名不能为空"})
    r = fm.save(fn, code)
    if r.get("ok"):
        add_log(f"因子已保存: [{fn}]")
    return jsonify(r)

@app.route("/api/factors/delete", methods=["POST"])
def api_factor_delete():
    d  = request.json
    fn = d.get("filename", "").strip()
    r  = fm.delete(fn)
    if r.get("ok"):
        add_log(f"因子已删除: [{fn}]")
    return jsonify(r)

@app.route("/api/factors/ai_generate", methods=["POST"])
def api_factor_ai_generate():
    d   = request.json
    desc = d.get("description", "").strip()
    if not desc:
        return jsonify({"ok": False, "error": "描述不能为空"})
    add_log(f"AI生成因子策略: {desc[:30]}...")
    r = ai.generate_factor(desc)
    if r.get("ok"):
        add_log("AI因子生成完成")
    return jsonify(r)

@app.route("/api/factors/<filename>")
def api_factor_get(filename):
    return jsonify({"code": fm.get_code(filename)})


# ── 回测 API ──────────────────────────────────────
@app.route("/api/backtest", methods=["POST"])
@require_auth
def api_backtest():
    err = _deduct("backtest", "策略回测")
    if err:
        return jsonify(err), 402
    d        = request.json
    fn       = d.get("filename","")
    code_str = sm.get_code(fn) if fn else ""
    if not code_str:
        return jsonify({"ok": False, "error": "策略代码为空"})

    # 支持前端传入自定义股票代码列表
    raw_codes = d.get("codes")
    if raw_codes and isinstance(raw_codes, str):
        codes = [c.strip() for c in raw_codes.replace("，",",").split(",") if c.strip()]
    elif raw_codes and isinstance(raw_codes, list):
        codes = raw_codes
    else:
        codes = []   # 空 → 全市场模式

    full_market = not codes
    mode_label  = "全市场" if full_market else f"{len(codes)}只"

    bt_kwargs = dict(
        strategy_code   = code_str,
        codes           = codes,
        start_date      = d.get("start_date","2024-01-01"),
        end_date        = d.get("end_date","2024-12-31"),
        initial_capital = float(d.get("capital",100000)),
        max_positions   = int(d.get("max_positions",5)),
        stop_loss       = float(d.get("stop_loss",-0.05)),
        take_profit     = float(d.get("take_profit",0.10)),
    )

    if full_market:
        # 全市场模式：数据加载需 60-120 秒，异步运行
        from backtest import start_backtest_async
        task_id = start_backtest_async(**bt_kwargs)
        add_log(f"全市场回测已启动 [{fn}] {d.get('start_date')}~{d.get('end_date')} task={task_id}")
        return jsonify({"ok": True, "async": True, "task_id": task_id})
    else:
        # 指定股票模式：同步运行（快速）
        from backtest import run_backtest
        add_log(f"回测: [{fn}] {d.get('start_date')}~{d.get('end_date')} {mode_label}")
        result = run_backtest(**bt_kwargs)
        if result.get("ok"):
            m = result["metrics"]
            add_log(f"回测完成 收益={m['total_return']}% 回撤={m['max_drawdown']}% "
                    f"胜率={m['win_rate']}% Sharpe={m['sharpe']}")
        else:
            add_log(f"回测失败: {result.get('error','')}", "error")
        return jsonify(result)


@app.route("/api/backtest/task/<task_id>")
def api_backtest_task(task_id):
    """轮询异步回测进度/结果"""
    from backtest import get_backtest_task
    return jsonify(get_backtest_task(task_id))

@app.route("/api/backtest/cancel/<task_id>", methods=["POST"])
def api_backtest_cancel(task_id):
    """终止正在运行的回测任务"""
    from backtest import cancel_backtest_task
    result = cancel_backtest_task(task_id)
    if result.get("ok"):
        add_log(f"⏹ 回测已终止: task={task_id}", "warn")
    return jsonify(result)


# ── 模拟盘 API ─────────────────────────────────────
@app.route("/api/paper/account")
def paper_account():
    """返回模拟盘资金概况"""
    broker = pb.get_broker()
    return jsonify({**broker.get_account(), "ok": True})


@app.route("/api/paper/positions")
def paper_positions():
    """返回模拟盘当前持仓（含实时估值）"""
    broker = pb.get_broker()
    # 批量拉实时价格
    cur_prices = {}
    try:
        positions = broker.get_positions()
        if positions:
            import ths_quote as tq
            client = tq.get_client()
            if client and client._ok:
                codes = [p["code"] for p in positions]
                qs = client.get_quotes(codes)
                cur_prices = {q["code"]: q.get("price", 0) for q in qs if q.get("price", 0) > 0}
    except Exception:
        pass
    return jsonify(broker.get_positions(cur_prices=cur_prices))


@app.route("/api/paper/trades")
def paper_trades():
    """返回历史成交记录（最近100笔）"""
    limit = int(request.args.get("limit", 100))
    broker = pb.get_broker()
    return jsonify(broker.get_trades(limit=limit))


@app.route("/api/paper/orders")
def paper_orders():
    """返回委托记录（最近100笔）"""
    limit = int(request.args.get("limit", 100))
    broker = pb.get_broker()
    return jsonify(broker.get_orders(limit=limit))


@app.route("/api/paper/stats")
def paper_stats():
    """返回模拟盘统计指标：胜率、盈亏比、最大回撤、夏普等"""
    broker = pb.get_broker()
    stats  = broker.get_stats()
    acct   = broker.get_account()
    return jsonify({**stats, **acct, "ok": True})


@app.route("/api/paper/equity")
def paper_equity():
    """返回净值曲线（最近90天）"""
    days = int(request.args.get("days", 90))
    broker = pb.get_broker()
    return jsonify(broker.get_equity_curve(days=days))


@app.route("/api/paper/reset", methods=["POST"])
def paper_reset():
    """重置模拟盘（清空所有数据）"""
    cap = float(request.json.get("initial_capital", INITIAL_CAPITAL))
    broker = pb.get_broker()
    result = broker.reset(initial_capital=cap)
    # 同步清空 state 持仓
    state["positions"]    = {}
    state["today_trades"] = 0
    state["executed"]     = 0
    add_log(f"🔄 模拟盘已重置，初始资金¥{cap:,.0f}", "warn")
    return jsonify(result)


@app.route("/api/paper/config", methods=["GET"])
def paper_get_config():
    """获取模拟盘参数"""
    broker = pb.get_broker()
    return jsonify({**broker.get_config(), "ok": True})


@app.route("/api/paper/config", methods=["POST"])
def paper_set_config():
    """更新模拟盘参数（佣金率/止损/止盈等）"""
    broker = pb.get_broker()
    result = broker.set_config(request.json)
    add_log(f"模拟盘参数已更新: {list(request.json.keys())}")
    return jsonify(result)


@app.route("/api/paper/buy", methods=["POST"])
@require_auth
def paper_manual_buy():
    """模拟盘手动买入"""
    err = _deduct("paper_trade", "模拟盘下单")
    if err:
        return jsonify(err), 402
    d      = request.json
    code   = d.get("code", "").strip()
    name   = d.get("name", code)
    price  = float(d.get("price", 0))
    shares = int(d.get("shares", 0))

    if not code:
        return jsonify({"ok": False, "msg": "请输入股票代码"})
    if price <= 0:
        return jsonify({"ok": False, "msg": "价格必须大于0"})

    # 若未传股数，按当前仓位比例计算
    if shares <= 0:
        broker = pb.get_broker()
        acct   = broker.get_account()
        budget = acct["cash"] * state.get("position_ratio", 0.20)
        shares = int(budget / price / 100) * 100
        shares = max(shares, 100)

    ok = execute_buy(code, name, price, shares, reason="模拟盘手动买入")
    if ok:
        return jsonify({"ok": True, "shares": shares, "msg": f"已买入 {code} {shares}股 @{price}"})
    else:
        # execute_buy already logged the error; return broker details
        return jsonify({"ok": False, "msg": "买入失败，请检查资金或持仓（详见日志）"})


@app.route("/api/paper/sell", methods=["POST"])
@require_auth
def paper_manual_sell():
    """手动卖出模拟盘持仓"""
    d    = request.json
    code = d.get("code", "")
    # 获取实时价
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
    result = execute_sell(code, price, sell_reason="手动卖出")
    return jsonify(result)


# ── 数据管理 API ──────────────────────────────────
@app.route("/api/data/stocks")
def api_data_list():
    return jsonify(dm.list_stocks())

@app.route("/api/data/download", methods=["POST"])
def api_data_download():
    d          = request.json
    raw_codes  = d.get("codes","")
    start_date = d.get("start_date","2020-01-01")
    if isinstance(raw_codes, str):
        codes = [c.strip() for c in raw_codes.replace("，",",").split(",") if c.strip()]
    else:
        codes = raw_codes
    if not codes:
        return jsonify({"ok": False, "msg": "请输入股票代码"})
    task_id = str(uuid.uuid4())[:8]
    add_log(f"开始下载 {len(codes)} 只股票历史数据...")
    dm.start_download(codes, start_date, task_id)
    return jsonify({"ok": True, "task_id": task_id, "total": len(codes)})

@app.route("/api/data/progress/<task_id>")
def api_data_progress(task_id):
    return jsonify(dm.get_progress(task_id))

@app.route("/api/data/delete", methods=["POST"])
def api_data_delete():
    code = request.json.get("code","")
    r    = dm.delete_stock(code)
    if r["ok"]:
        add_log(f"已删除本地数据: {code}", "warn")
    return jsonify(r)

@app.route("/api/data/refresh_names", methods=["POST"])
def api_refresh_names():
    """刷新全市场股票名称缓存（后台线程）"""
    import threading
    threading.Thread(target=dm.refresh_name_cache, daemon=True).start()
    add_log("开始刷新股票名称缓存...")
    return jsonify({"ok": True, "msg": "后台刷新中"})

@app.route("/api/data/sector/<code>")
def api_stock_sector(code):
    """查询单只股票的行业/概念板块"""
    r = dm.get_stock_sector(code)
    r["name"] = dm.get_stock_name(code)
    return jsonify(r)


# ── K线 API ───────────────────────────────────────
@app.route("/api/kline/<code>")
def api_kline(code):
    """
    优先级：本地CSV → TDX实时拉取
    返回 [{date,open,close,high,low,volume}, ...]，按日期升序
    """
    # 1. 本地历史数据
    try:
        df = dm.get_local(code)
        if df is not None and not df.empty and len(df) >= 10:
            cols = [c for c in ["date","open","close","high","low","volume"] if c in df.columns]
            sub  = df[cols].tail(120).copy()
            # 强制将 date 转为 "YYYY-MM-DD" 字符串
            # Flask 旧版本会把 pandas Timestamp 序列化为 "Wed, 31 Dec 2025 00:00:00 GMT"
            sub["date"] = sub["date"].apply(
                lambda d: d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
            )
            data = sub.to_dict("records")
            name = dm.get_stock_name(code) or code
            return jsonify({"ok": True, "code": code, "name": name, "source": "local", "data": data})
    except Exception:
        pass

    # 2. TDX 实时拉取日K
    try:
        import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            bars = client.get_kline(code, 120)
            if bars:
                name = tq.get_stock_name(code) or code
                return jsonify({"ok": True, "code": code, "name": name, "source": "tdx", "data": bars})
    except Exception as e:
        logger.warning(f"K线 TDX 失败: {e}")

    return jsonify({"ok": False, "data": [], "msg": "暂无数据，请先在「数据管理」下载"})


# ── AI 分析 API ───────────────────────────────────
@app.route("/api/ai/config", methods=["GET"])
def ai_get_config():
    cfg = ai.load_config()
    # 隐藏 api_key 中间部分
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
    # 只更新前端传来的字段；空 api_key 不覆盖已有的
    for k in ("provider", "base_url", "model", "enabled"):
        if k in d:
            cfg[k] = d[k]
    if d.get("api_key"):
        cfg["api_key"] = d["api_key"]
    ai.save_config(cfg)
    add_log(f"AI配置已更新: provider={cfg['provider']} enabled={cfg['enabled']}")
    return jsonify({"ok": True})

@app.route("/api/market/context")
def api_market_context():
    """获取当前大盘市场环境（牛市/震荡/熊市 + 强度评分 + 实时指数行情）"""
    try:
        force = request.args.get("refresh", "0") == "1"
        ctx = mctx.get_market_regime(force_refresh=force)
        # 强制刷新实时指数行情（使用正确市场编号，不受缓存影响）
        try:
            import ths_quote as tq
            client = tq.get_client()
            if client and client._ok:
                # 000001=上证指数(SH=1), 399001/399006=深交所(SZ=0)
                index_pairs = [(1, "000001"), (0, "399001"), (0, "399006")]
                name_map    = {"000001": "上证指数", "399001": "深证成指", "399006": "创业板指"}
                qs = client.get_index_quotes(index_pairs)
                for q in qs:
                    nm = name_map.get(q.get("code", ""), "")
                    if nm:
                        if nm not in ctx.get("indices", {}):
                            ctx.setdefault("indices", {})[nm] = {}
                        ctx["indices"][nm]["price"]  = round(float(q.get("price")  or 0), 2)
                        ctx["indices"][nm]["change"] = round(float(q.get("change_pct") or 0), 2)
                        ctx["indices"][nm]["code"]   = q.get("code", "")
        except Exception:
            pass
        return jsonify({"ok": True, **ctx})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/stocks/score", methods=["POST"])
def api_stock_score():
    """对单只股票进行六维量化评分（无需AI，基于本地/TDX数据）"""
    d    = request.json
    code = d.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "请输入股票代码"})
    df = dm.get_local(code)
    if df is None or df.empty:
        # 尝试TDX
        try:
            import ths_quote as tq
            client = tq.get_client()
            if client and client._ok:
                bars = client.get_kline(code, 60)
                if bars:
                    import pandas as pd
                    df = pd.DataFrame(bars)
                    df["date"] = pd.to_datetime(df["date"])
        except Exception:
            pass
    if df is None or df.empty:
        return jsonify({"ok": False, "error": "无数据，请先下载历史数据"})
    result = scorer.score_stock(df)
    result["code"] = code
    result["name"] = dm.get_stock_name(code) or code
    return jsonify(result)


@app.route("/api/ai/analyze", methods=["POST"])
@require_auth
def ai_analyze():
    err = _deduct("ai_analysis", f"AI分析 {request.json.get('code','')}")
    if err:
        return jsonify(err), 402
    d    = request.json
    code = d.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "请输入股票代码"})
    name = dm.get_stock_name(code) or code
    df   = dm.get_local(code)
    # 若无本地数据，尝试 TDX 拉取日线
    if df is None or df.empty:
        try:
            import ths_quote as tq
            import pandas as pd
            client = tq.get_client()
            if client and client._ok:
                bars = client.get_kline(code, 120)
                if bars:
                    df = pd.DataFrame(bars)
                    df["date"] = pd.to_datetime(df["date"])
        except Exception:
            pass

    # ── 获取 TDX 实时行情（注入到分析结果，确保现价准确）──
    realtime_price  = None
    realtime_change = None
    try:
        import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            qs = client.get_quotes([code])
            if qs:
                q = qs[0]
                rp = float(q.get("price") or q.get("now") or 0)
                if rp > 0:
                    realtime_price  = round(rp, 2)
                    realtime_change = round(float(q.get("change_pct", 0)), 2)
                    # 顺便更新股票名称
                    qname = q.get("name", "")
                    if qname and name == code:
                        name = qname
    except Exception:
        pass

    add_log(f"AI分析: {code} {name}")
    result = ai.run_analysis(code, name, df)

    # ── 将实时价格注入结果，覆盖历史收盘价 ──────────────
    if realtime_price and result.get("ok"):
        result["realtime_price"]  = realtime_price
        result["realtime_change"] = realtime_change
        # 覆盖 indicators.price
        if isinstance(result.get("indicators"), dict):
            result["indicators"]["price"] = realtime_price
            result["indicators"]["chg_5d_pct"]  = result["indicators"].get("chg_5d_pct",  0)
            result["indicators"]["chg_20d_pct"] = result["indicators"].get("chg_20d_pct", 0)
        # 覆盖 score.indicators.price
        sc = result.get("score", {})
        if isinstance(sc.get("indicators"), dict):
            sc["indicators"]["price"] = realtime_price

    action    = result.get("summary", {}).get("action") or result.get("score", {}).get("signal") or "—"
    score_val = result.get("score", {}).get("total", "—")
    if result.get("ok"):
        add_log(f"AI分析完成: {code} {name} → {action}（评分{score_val}，实时{realtime_price or '无'}）")
    else:
        add_log(f"AI分析失败: {code} — {result.get('error','')}", "error")
    return jsonify(result)


@app.route("/api/ai/test", methods=["POST"])
def ai_test_conn():
    """真实测试 LLM 连通性：发一条最小请求，返回成功/失败及具体错误。
    允许传入临时的 api_key/base_url/model 覆盖已保存配置（方便保存前先测试）。"""
    d   = request.json or {}
    cfg = ai.load_config()
    # 允许前端传入当前输入框中的值覆盖已保存配置（不落地，只用于本次测试）
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
    # 注意：不检查 enabled，连通性测试无需 AI 开关打开
    try:
        reply = ai._call_llm(cfg, [
            {"role": "user", "content": "请回复'OK'，不要任何其他内容。"},
        ], temperature=0.1, max_tokens=16)
        return jsonify({"ok": True, "reply": reply.strip(),
                        "model": cfg.get("model"), "base_url": cfg.get("base_url")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e),
                        "base_url": cfg.get("base_url"), "model": cfg.get("model")})


@app.route("/api/ai/generate_strategy", methods=["POST"])
def ai_gen_strategy():
    d    = request.json
    desc = d.get("description", "").strip()
    if not desc:
        return jsonify({"ok": False, "error": "请描述策略需求"})
    add_log(f"AI写策略: {desc[:40]}...")
    result = ai.generate_strategy(desc)
    if result["ok"]:
        add_log(f"AI策略生成完成，共{len(result.get('code',''))}字符")
    else:
        add_log(f"AI写策略失败: {result.get('error','')}", "error")
    return jsonify(result)


DABAN_INTERVAL = 5   # 打板策略专用轮询间隔（秒），最快能稳定跑的频率

def run_scheduler():
    """
    双频调度：
    - 普通策略：每 SCAN_INTERVAL 秒（默认30s）
    - 打板策略：每 DABAN_INTERVAL 秒（5s），用独立计时器，不依赖 schedule
    """
    schedule.every(SCAN_INTERVAL).seconds.do(do_scan)

    last_daban = 0.0
    while True:
        if state["running"]:
            active = state.get("active_strategy")
            now = time.time()
            if active == "da_ban":
                # 打板模式：5 秒轮询，不走 schedule（schedule 精度不够）
                if now - last_daban >= DABAN_INTERVAL:
                    last_daban = now
                    threading.Thread(target=do_scan, daemon=True).start()
            else:
                schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    add_log(f"服务启动 DRY_RUN={'开启' if DRY_RUN else '关闭'}")
    app.run(port=5001, debug=False)
