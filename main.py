from __future__ import annotations

import os as _os
# ── 清除系统代理（akshare 走国内接口，代理反而导致连接失败）──────────
for _k in ("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy",
           "ALL_PROXY","all_proxy","SOCKS_PROXY","socks_proxy"):
    _os.environ.pop(_k, None)

from flask import Flask, jsonify, request, session, Response
from flask_cors import CORS
import threading, logging, secrets, json, queue as _queue
from datetime import datetime
from broker.ths_operator import THSOperator
from config import DRY_RUN, INITIAL_CAPITAL
try:
    from config import DABAN_STOP_LOSS, DABAN_TRAIL, DABAN_MAX_HOLD, DABAN_HOLD_LIMITUP
except ImportError:
    DABAN_STOP_LOSS, DABAN_TRAIL, DABAN_MAX_HOLD, DABAN_HOLD_LIMITUP = -0.05, 0.07, 10, True
from strategy_engine import strategy_manager as sm
from analytics import data_manager as dm
from analytics import ai_analyst as ai
from analytics import factor_manager as fm
from broker import paper_broker as pb
from core import user_manager as um
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent


def _load_or_create_secret_key() -> str:
    """SECRET_KEY 解析顺序：环境变量 → data/.secret_key → 新建并持久化。

    之前未设环境变量时每次启动都随机生成，导致重启后所有 session 失效。
    """
    env = _os.environ.get("SECRET_KEY")
    if env:
        return env
    key_file = BASE_DIR / "data" / ".secret_key"
    try:
        if key_file.exists():
            k = key_file.read_text(encoding="utf-8").strip()
            if k:
                return k
        key_file.parent.mkdir(parents=True, exist_ok=True)
        k = secrets.token_hex(32)
        key_file.write_text(k, encoding="utf-8")
        try:
            _os.chmod(key_file, 0o600)
        except Exception:
            pass
        logger.info("已生成并持久化 SECRET_KEY 到 data/.secret_key")
        return k
    except Exception as e:
        logger.warning(f"SECRET_KEY 持久化失败，回退到内存随机值（重启会使 session 失效）: {e}")
        return secrets.token_hex(32)


app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")
app.secret_key = _load_or_create_secret_key()

# CORS：携带凭证时不能用通配来源。配置 CORS_ORIGINS（逗号分隔）才允许跨域；
# 不配置则仅同源（同源请求不受 CORS 限制，内置前端正常工作）。
_cors_origins = [o.strip() for o in _os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    CORS(app, supports_credentials=True, origins=_cors_origins)
    logger.info(f"CORS 允许跨域来源: {_cors_origins}")
else:
    CORS(app, supports_credentials=True, origins=[])
    logger.info("CORS 未配置跨域来源，仅允许同源访问（设 CORS_ORIGINS 可放行指定来源）")

# ── 安全加固：Cookie 硬化 + 安全响应头（详见 app_security.py）──
from app_security import init_security
init_security(app)
logger.info("安全加固已启用（Cookie 硬化 / 安全响应头 / 限流就绪）"
            + ("；HTTPS Secure Cookie 已开启" if _os.environ.get("SESSION_COOKIE_SECURE", "0") in ("1", "true", "True") else "；SESSION_COOKIE_SECURE 未开（HTTPS 就绪后再开）"))

# ── Swagger / flasgger（生产默认关闭，设 ENABLE_SWAGGER=1 才暴露）──
if _os.environ.get("ENABLE_SWAGGER", "0").strip().lower() in ("1", "true", "yes", "on"):
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

# import platform 必须留模块级：多个路由（查实盘账户/资金/账户快照）用 platform.system()
import platform
# operator 始终是模块全局（路由层引用）。纯导入测试时保持 None。
operator = None
# 纯导入标志（测试/工具）：跳过建表/迁移/operator/调度等 import 时副作用，使 main 可被安全 import。
# 生产（gunicorn / python main.py）不设此环境变量 → 一切照常初始化，行为与原内联完全一致。
_TEST_IMPORT = _os.environ.get("QUANT_TEST_IMPORT") == "1"


def _init_runtime():
    """import 时的运行态初始化：建表 / 迁移 / 模拟盘库 / 同花顺操作器。

    与原内联初始化逐句等价，仅多一层函数封装 + `global operator`。
    """
    global operator
    # ── 交易模式醒目日志（安全默认值改造：实盘必须 .env 显式双开关）──
    from config import AUTO_CONFIRM_ORDER, AUTO_CANCEL_UNFILLED, MAX_ORDER_AMOUNT
    logger.warning(
        "交易模式=%s | 委托自动确认=%s | 未成交自动撤=%s | 单笔上限=¥%s%s",
        "模拟盘(DRY_RUN)" if DRY_RUN else "★实盘★",
        "开" if AUTO_CONFIRM_ORDER else "关",
        "开" if AUTO_CANCEL_UNFILLED else "关",
        f"{MAX_ORDER_AMOUNT:,.0f}",
        "（实盘需在 .env 设 QUANT_LIVE_TRADING=1）" if DRY_RUN else "",
    )
    # ── 初始化数据库 ──
    um.init_db()
    # 问题反馈表（与 users.db 同库，幂等建表）
    from core import feedback_manager as fbm
    fbm.init_db()
    from core import changelog_manager as clm
    clm.init_db()
    # 迁移必须在 pb.init_paper_db() 之前，先给旧表加 user_id 列
    try:
        from core import db_migration as _dbm
        _dbm.migrate_paper_trading_db()
        _dbm.migrate_users_db()
    except Exception as _e:
        logger.warning(f"[db_migration] {_e}")
    pb.init_paper_db()

    if platform.system() == "Windows":
        try:
            operator = THSOperator()
        except Exception as _ope:
            logger.warning(f"本地同花顺操作器初始化失败: {_ope}")
            operator = None
    else:
        logger.info("云端 Linux 环境，跳过同花顺 GUI 操作器初始化")
        operator = None
    # 蓝图经 app_state.get_operator() 访问同一实例（工单4：路由迁出 main 后不再直接引用本模块全局）
    import app_state as _app_state
    _app_state.set_operator(operator)


if not _TEST_IMPORT:
    _init_runtime()

# ── 共享运行态/通用辅助（见 app_state.py，与各 web 蓝图共用同一份内存状态）──
from app_state import (
    _sse_subscribers, _sse_lock,
    get_user_state, _persist_state, _add_log,
    require_auth, is_root_admin_session, current_admin_permissions, _deduct,
)

# ── 注册 web 蓝图（非交易 API 已从本文件抽出到 web/）──
from web.auth import bp as _auth_bp
from web.user import bp as _user_bp
from web.admin import bp as _admin_bp
from web.pages import bp as _pages_bp
from web.market import bp as _market_bp
from web.data import bp as _data_bp
from web.ai import bp as _ai_bp
from web.search import bp as _search_bp
from web.notify import bp as _notify_bp
from web.feedback import bp as _feedback_bp
from web.review import bp as _review_bp
from web.research import bp as _research_bp
from web.lhb import bp as _lhb_bp
from web.ai_trader import bp as _aitrader_bp
app.register_blueprint(_auth_bp)
app.register_blueprint(_user_bp)
app.register_blueprint(_admin_bp)
app.register_blueprint(_pages_bp)
app.register_blueprint(_market_bp)
app.register_blueprint(_data_bp)
app.register_blueprint(_ai_bp)
app.register_blueprint(_search_bp)
app.register_blueprint(_notify_bp)
app.register_blueprint(_feedback_bp)
app.register_blueprint(_review_bp)
app.register_blueprint(_research_bp)
app.register_blueprint(_lhb_bp)
app.register_blueprint(_aitrader_bp)
from web.bridge import bp as _bridge_bp
app.register_blueprint(_bridge_bp)


# 请求参数解析助手统一在 web/_params.py（工单4 去重：原 6 份复制粘贴）
from web._params import (
    _bounded_int_arg, _positive_float_value, _float_value, _int_value,  # noqa: F401
)


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "status": "healthy",
        "service": "jwalpha",
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })




# ══════════════════════════════════════════════════════════════
# 核心交易逻辑已下沉（工单4）：
#   统一买卖/加仓/风控自动卖出 → core/trade_service.py
#   扫描编排 do_scan / _scan_thread_worker → strategy_engine/scan_service.py
# 此处保留同名绑定：路由与既有测试仍从 main 引用这些名字。
# 注意打桩目标变更：DRY_RUN/is_trading_time 现绑定在下沉模块内。
# ══════════════════════════════════════════════════════════════
from core.trade_service import (  # noqa: E402,F401
    _reset_daily_if_needed, execute_buy, execute_sell,
    _check_add_position, _auto_buy, _check_live_daban_ride,
    _check_daban_ride, _check_paper_risk,
)
from strategy_engine.scan_service import do_scan, _scan_thread_worker  # noqa: E402,F401


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
        # 两阶段进度合并显示：
        #   阶段 A: ths_quote 全市场批量拉行情（universe 构建，30秒）
        #   阶段 B: strategy_manager 策略扫描（逐只 get_kline，2-25 分钟）
        # 哪个 running 就显示哪个，避免阶段 A 结束后卡在 100% 的体验问题
        from market import ths_quote as tq
        tdx_prog   = tq.get_scan_progress()
        strat_prog = sm.get_scan_progress()
        if tdx_prog.get("running"):
            s["scan_progress"].update({
                "running": True,
                "stage":   "行情扫描",
                "current": tdx_prog.get("current", 0),
                "total":   tdx_prog.get("total",   0),
            })
        elif strat_prog.get("running"):
            s["scan_progress"].update({
                "running": True,
                "stage":   "策略扫描",
                "current": strat_prog.get("current", 0),
                "total":   strat_prog.get("total",   0),
                "code":    strat_prog.get("code",    ""),
            })
    except Exception:
        pass
    try:
        from analytics.factors_zoo import activator as _vibe_act
        _vibe_cfg = _vibe_act.get(uid) or {}
        s["vibe_active"] = bool(_vibe_cfg.get("enabled"))
        s["vibe_factor"] = _vibe_cfg.get("factor_id") or ""
    except Exception:
        s["vibe_active"] = False
        s["vibe_factor"] = ""
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


# ══════════════════════════════════════════════════════════════
# 本地同花顺交易桥接 API (Cloud-Local Bridge)
# ══════════════════════════════════════════════════════════════

# ── 实盘下单总闸已下沉 core/bridge_state.py（工单4）──
# 桥接 API（/api/bridge/*）已迁至 web/bridge.py（工单4）


@app.route("/api/execute", methods=["POST"])
@require_auth
def execute():
    uid    = session["user_id"]
    s      = get_user_state(uid)
    data   = request.json or {}
    code   = data.get("code")
    action = data.get("action")
    item   = next((q for q in s["queue"] if q["code"] == code), None)

    if not item:
        if action == "buy":
            price  = _positive_float_value(data.get("price"))
            if price is None:
                return jsonify({"ok": False, "msg": "invalid price"})
            name   = data.get("name", code)
            shares = data.get("shares")
            if not shares:
                budget = s["capital"] * s["position_ratio"]
                shares = int(budget / price / 100) * 100 if price > 0 else 100
                shares = max(shares, 100)
            shares = _int_value(shares)
            if shares is None:
                return jsonify({"ok": False, "msg": "invalid shares"})
            res = execute_buy(uid, code, name, price, shares, reason="手动买入")
            return jsonify(res)
        return jsonify({"ok": False, "msg": "未找到"})

    if action == "skip":
        item["status"] = "skip"
    elif action == "buy":
        price  = item["price"]
        name   = item.get("name", code)
        shares = item.get("shares", 100)
        res = execute_buy(uid, code, name, price, shares, reason="队列买入",
                          strategy=item.get("strategy") or "")
        if not res.get("ok"):
            item["status"] = "error"
            item["error_msg"] = res.get("msg", "")
        elif res.get("signal_id"):
            item["status"] = "submitted"          # 实盘：已报，等交易机回传成交确认
            item["signal_id"] = res["signal_id"]
        else:
            item["status"] = "done"               # 模拟：即时成交

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
        "max_positions":             s.get("max_positions", 5),
        "single_position_max_ratio": s.get("single_position_max_ratio", 0.20),
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
    # ── 仓位护栏（前端可调）：单笔上限 + 最多持仓数 ──────────────
    if "max_positions" in d:
        try:
            mp = int(d["max_positions"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "max_positions 必须是整数"}), 400
        if mp < 1 or mp > 50:
            return jsonify({"ok": False, "error": "最多持仓数须在 1~50 之间"}), 400
        s["max_positions"] = mp
    if "single_position_max_ratio" in d:
        try:
            sr = float(d["single_position_max_ratio"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "single_position_max_ratio 必须是数字"}), 400
        if sr <= 0 or sr > 1:
            return jsonify({"ok": False, "error": "单笔上限须在 (0, 1] 之间"}), 400
        s["single_position_max_ratio"] = sr
    _persist_state(uid,
        capital=s["capital"], position_ratio=s["position_ratio"],
        max_positions=s["max_positions"],
        single_position_max_ratio=s["single_position_max_ratio"])
    _add_log(uid,
        f"账户设置更新: 资金¥{s['capital']:,.0f} 仓位{s['position_ratio']*100:.0f}% "
        f"| 护栏 单笔≤{s['single_position_max_ratio']*100:.0f}% 最多持{s['max_positions']}只")
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
    d     = request.json or {}
    code  = d.get("code", "")
    price = _positive_float_value(d.get("price"))
    name  = d.get("name", code)
    if not code:
        return jsonify({"ok": False, "msg": "code 缺失"})
    if price is None:
        return jsonify({"ok": False, "msg": "invalid price"})
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
        from market import ths_quote as tq
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
        # 优先拉 ths_quote 实时价（避免前端 cur_price fallback 到 cost_price 导致
        # 卖出价≈买入价、盈亏只剩费用的假象）
        price = 0.0
        try:
            from market import ths_quote as tq
            client = tq.get_client()
            if client and client._ok:
                qs = client.get_quotes([code])
                if qs:
                    price = _positive_float_value(qs[0].get("price"), 0.0)
        except Exception:
            pass
        if not price:
            price = (
                _positive_float_value(d.get("price"), 0.0)
                or _positive_float_value(bp.get("cur_price"), 0.0)
                or _positive_float_value(bp.get("cost_price"), 0.0)
            )
    else:
        pos = s["positions"].get(code)
        if not pos:
            return jsonify({"ok": False, "msg": "持仓不存在"})
        price = _positive_float_value(d.get("price"), _positive_float_value(pos.get("cost"), 0.0))
    if not price:
        return jsonify({"ok": False, "msg": "无法获取实时价格，请稍后重试"})
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
def _paper_live_prices(broker) -> dict:
    """为模拟盘持仓拉取 TDX 实时价 {code: price}，用于实时市值/盈亏。失败返回 {}。"""
    try:
        positions = broker.get_positions()
        if not positions:
            return {}
        from market import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            codes = [p["code"] for p in positions]
            qs    = client.get_quotes(codes)
            return {q["code"]: q.get("price", 0) for q in qs if q.get("price", 0) > 0}
    except Exception:
        pass
    return {}


@app.route("/api/paper/account")
@require_auth
def paper_account():
    uid    = session["user_id"]
    broker = pb.get_broker(uid)
    return jsonify({**broker.get_account(cur_prices=_paper_live_prices(broker)), "ok": True})


@app.route("/api/paper/positions")
@require_auth
def paper_positions():
    uid        = session["user_id"]
    broker     = pb.get_broker(uid)
    return jsonify(broker.get_positions(cur_prices=_paper_live_prices(broker)))


@app.route("/api/account/funds")
@require_auth
def account_funds():
    """统一「可用资金」查询，方便下单前知道还能买多少。

    模拟盘 / DRY_RUN：从模拟盘账本读现金 + 实时权益（可靠、实时）。
    实盘：调用 xiadan 操作器尽力读「可用资金」（GUI 抓取，可能需现场校准）。
    返回统一字段 available（可下单金额），便于前端直接用。
    """
    uid         = session["user_id"]
    broker_cfg  = um.get_user_broker_config(uid)
    broker_type = broker_cfg.get("broker_type", "paper")

    if DRY_RUN or broker_type == "paper":
        broker = pb.get_broker(uid)
        acct   = broker.get_account(cur_prices=_paper_live_prices(broker))
        return jsonify({
            "ok":           True,
            "mode":         "paper",
            "available":    acct["cash"],          # 可用资金 = 现金（可下单金额）
            "cash":         acct["cash"],
            "market_value": acct["market_value"],
            "equity":       acct["equity"],
            "total_return": acct["total_return"],
        })

    # ── 实盘：读 xiadan ──
    if platform.system() != "Windows":
        return jsonify({"ok": False, "mode": "real", "msg": "服务端非 Windows，无法读 xiadan 资金"})
    if not operator:
        return jsonify({"ok": False, "mode": "real", "msg": "xiadan 操作员未初始化（DRY_RUN 或 Linux）"})
    try:
        import time as _time
        force = (request.args.get("force") or "").strip().lower() in ("1", "true", "yes")
        cache = getattr(operator, "_funds_cache", None)
        # ⚠️ 事件驱动 + 只读缓存：实盘资金缓存由「下单成功后」和「手动查实盘账户」更新。
        # 这个轮询接口默认只返回缓存，绝不激活 xiadan 窗口——彻底告别每 10 秒弹窗。
        if cache and cache.get("data") and not force:
            age = round(_time.time() - cache.get("ts", 0))
            return jsonify({**cache["data"], "cached": True, "age_sec": age})
        if not force:
            # 无缓存且非强制 → 不弹窗，返回占位；交易后会自动有值，或手动刷新一次
            return jsonify({
                "ok": True, "mode": "real", "available": None, "total_asset": None,
                "cached": False,
                "msg": "暂无资金缓存：下单成功后自动更新，或打开「📊 查实盘账户」刷新一次",
            })
        # 仅当 ?force=1（用户主动）才真正去读 xiadan（会激活窗口一次）
        r = operator.query_funds()
        r.setdefault("mode", "real")
        if r.get("ok"):
            prev = (cache or {}).get("data", {}) if cache else {}
            operator._funds_cache = {"ts": _time.time(), "data": {
                "ok": True, "mode": "real",
                "available":   r.get("available"),
                "total_asset": prev.get("total_asset"),  # query_funds 不含总资产，保留旧值
            }}
            return jsonify(operator._funds_cache["data"])
        return jsonify(r)
    except Exception as e:
        logger.exception(f"读 xiadan 资金失败: {e}")
        return jsonify({"ok": False, "mode": "real", "msg": f"调用异常: {e}"})


@app.route("/api/account/snapshot")
@require_auth
def account_snapshot():
    """实盘资金 + 持仓快照（一次性返回 可用资金 / 总资产 / 持仓列表）。

    模拟盘 / DRY_RUN：从模拟盘账本读现金 + 持仓（可靠、实时）。
    实盘：调用 xiadan 操作器按 F4 截图 + 本地 OCR 解析「资金股票」页：
          - 可用资金 / 总资产：顶部工具栏 OCR，已在真账户验证可靠；
          - 持仓代码：词坐标行聚类，可靠；
          - 名称：OCR 易错，这里用本地名称库按 code 纠正；
          - 数字列（股数/成本/盈亏）：自绘表格 OCR 仅供参考，附截图供复核。
    统一字段：available（可用资金）/ total_asset（总资产）/ positions（持仓列表）。
    """
    uid         = session["user_id"]
    broker_cfg  = um.get_user_broker_config(uid)
    broker_type = broker_cfg.get("broker_type", "paper")

    if DRY_RUN or broker_type == "paper":
        broker    = pb.get_broker(uid)
        prices    = _paper_live_prices(broker)
        acct      = broker.get_account(cur_prices=prices)
        positions = broker.get_positions(cur_prices=prices)
        return jsonify({
            "ok":           True,
            "mode":         "paper",
            "available":    acct["cash"],
            "total_asset":  acct["equity"],
            "market_value": acct["market_value"],
            "positions":    positions,
        })

    # ── 实盘：F4 截图 + 本地 OCR ──
    if platform.system() != "Windows":
        return jsonify({"ok": False, "mode": "real", "msg": "服务端非 Windows，无法读 xiadan 账户"})
    if not operator:
        return jsonify({"ok": False, "mode": "real", "msg": "xiadan 操作员未初始化（DRY_RUN 或 Linux）"})
    try:
        r = operator.query_account_snapshot()
        r.setdefault("mode", "real")
        # code 可靠、OCR 名称易错 → 用本地名称库按 code 纠正
        for p in r.get("positions", []):
            code = (p.get("code") or "").strip()
            if code:
                fixed = dm.get_stock_name(code)
                if fixed and fixed != code:
                    p["name"] = fixed
        return jsonify(r)
    except Exception as e:
        logger.exception(f"读 xiadan 账户快照失败: {e}")
        return jsonify({"ok": False, "mode": "real", "msg": f"调用异常: {e}"})


@app.route("/api/account/trade_settings", methods=["GET", "POST"])
@require_auth
def account_trade_settings():
    """读/写 实盘交易自动化开关（运行时切换，免改 config）：
      - auto_confirm_order   下单后自动按 Y 提交(True) / 下单后等人工按 Y(False)
      - auto_cancel_unfilled 未成交自动撤单(True) / 手动撤单(False)
      - auto_cancel_delay    未成交多少秒后撤
    这是驱动单一 xiadan 客户端的全局设置，持久化到 data/trade_settings.json（重启仍生效）。
    """
    if not operator:
        return jsonify({"ok": False, "msg": "xiadan 操作员未初始化（DRY_RUN 或 Linux），无实盘设置"})
    if request.method == "POST":
        d = request.json or {}
        s = operator.set_trade_settings(
            auto_confirm_order=d.get("auto_confirm_order"),
            auto_cancel_unfilled=d.get("auto_cancel_unfilled"),
            auto_cancel_delay=d.get("auto_cancel_delay"),
        )
        uid = session.get("user_id")
        _add_log(uid,
                 f"⚙️ 实盘自动化设置: 下单{'自动确认' if s['auto_confirm_order'] else '手动确认'} · "
                 f"{'自动撤单' if s['auto_cancel_unfilled'] else '手动撤单'}"
                 f"(未成交{s['auto_cancel_delay']:.0f}秒)", "WARN")
        return jsonify({"ok": True, **s})
    return jsonify({"ok": True, **operator.get_trade_settings()})


@app.route("/api/paper/trades")
@require_auth
def paper_trades():
    uid    = session["user_id"]
    limit  = _bounded_int_arg("limit", 100, 1, 500)
    broker = pb.get_broker(uid)
    return jsonify(broker.get_trades(limit=limit))


@app.route("/api/paper/orders")
@require_auth
def paper_orders():
    uid    = session["user_id"]
    limit  = _bounded_int_arg("limit", 100, 1, 500)
    broker = pb.get_broker(uid)
    return jsonify(broker.get_orders(limit=limit))


@app.route("/api/seal_tracker")
@require_auth
def seal_tracker_dump():
    """SealTracker 状态查询（调试/前端展示用）。
    /api/seal_tracker          → 列出所有被追踪股的强度评分（按 score 降序）
    /api/seal_tracker?code=XXX → 单只详情（含 samples 时间线）
    """
    try:
        from analytics.seal_tracker import get_tracker
        tr = get_tracker()
    except Exception as e:
        return jsonify({"ok": False, "msg": f"tracker 不可用: {e}"})
    code = request.args.get("code", "").strip()
    if code:
        st = tr.status(code)
        if not st:
            return jsonify({"ok": False, "msg": f"{code} 无 tracker 记录"})
        # 加 samples 时间线
        with tr._lock:
            d = tr._data.get(code, {})
            st["samples"] = [
                {"ts": s["ts"], "seal": s["seal"], "price": s["price"],
                 "lu": s["lu"], "sealed": s["sealed"]}
                for s in list(d.get("samples", []))
            ]
        return jsonify({"ok": True, "code": code, "status": st})
    # 全量列表
    out = []
    for c in tr.all_tracked():
        s = tr.status(c)
        s["code"] = c
        out.append(s)
    out.sort(key=lambda x: x.get("score", 0), reverse=True)
    return jsonify({"ok": True, "count": len(out), "items": out})


@app.route("/api/paper/stats")
@require_auth
def paper_stats():
    uid    = session["user_id"]
    broker = pb.get_broker(uid)
    stats  = broker.get_stats()
    acct   = broker.get_account(cur_prices=_paper_live_prices(broker))
    return jsonify({**stats, **acct, "ok": True})


@app.route("/api/paper/equity")
@require_auth
def paper_equity():
    uid    = session["user_id"]
    days   = _bounded_int_arg("days", 90, 1, 3650)
    broker = pb.get_broker(uid)
    return jsonify(broker.get_equity_curve(days=days))


@app.route("/api/paper/reset", methods=["POST"])
@require_auth
def paper_reset():
    uid    = session["user_id"]
    s      = get_user_state(uid)
    cap    = _positive_float_value((request.json or {}).get("initial_capital"), INITIAL_CAPITAL)
    if cap is None:
        return jsonify({"ok": False, "error": "invalid initial_capital"}), 400
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
    payload = request.json or {}
    raw_code = payload.get("code", "")
    code_for_validation = raw_code.strip() if isinstance(raw_code, str) else ""
    if not code_for_validation:
        return jsonify({"ok": False, "msg": "missing code"})
    if _positive_float_value(payload.get("price")) is None:
        return jsonify({"ok": False, "msg": "invalid price"})
    if _int_value(payload.get("shares"), 0) is None:
        return jsonify({"ok": False, "msg": "invalid shares"})
    uid    = session["user_id"]
    s      = get_user_state(uid)
    d      = request.json or {}
    code   = d.get("code", "").strip()
    name   = d.get("name", code)
    price  = _positive_float_value(d.get("price"))
    shares = _int_value(d.get("shares"), 0)
    # 防御：name 含 "?" / 乱码 / 为空，从本地 stock_names.json 查
    if not name or "?" in name or "??" in name or name == code or len(name.strip()) < 2:
        try:
            from pathlib import Path
            import json as _json
            f = Path(__file__).parent / "data" / "stock_names.json"
            if f.exists():
                _names = _json.loads(f.read_text(encoding="utf-8"))
                name = _names.get(code, name) or code
        except Exception:
            name = name or code
    if not code:
        return jsonify({"ok": False, "msg": "请输入股票代码"})
    if price is None:
        return jsonify({"ok": False, "msg": "价格必须大于0"})
    # 非交易时段提示但仍允许（方便用户测试 + 复盘）
    # 严格模式：取消注释下面 4 行让非交易时段拒单
    # from market_data import is_trading_time
    # if not is_trading_time():
    #     return jsonify({"ok": False, "msg": "非交易时段，无法下单（仅交易日 9:30-11:30 / 13:00-15:00）"})
    if shares is None:
        return jsonify({"ok": False, "msg": "invalid shares"})
    try:
        from market.market_data import is_trading_time
        if not is_trading_time():
            _add_log(uid, f"⚠️ 非交易时段手动下单: {code} ({name}) — 已按上次收盘价模拟成交", "warning")
    except Exception:
        pass
    if shares <= 0:
        # 仓位基数：实盘走 live_cash（xiadan 真账户余额），模拟走 paper_broker.cash
        broker_cfg = um.get_user_broker_config(uid)
        broker_type = broker_cfg.get("broker_type", "paper")
        if broker_type == "paper" or DRY_RUN:
            broker = pb.get_broker(uid)
            acct   = broker.get_account()
            base_cash = float(acct.get("cash") or 0)
        else:
            # 实盘：从 broker_config.config.live_cash 取 xiadan 真账户余额
            live_cash = float((broker_cfg.get("config") or {}).get("live_cash") or 0)
            if live_cash <= 0:
                return jsonify({
                    "ok": False,
                    "msg": "实盘模式下未配置真账户余额（live_cash），请在顶部切换实盘开关时输入 xiadan 当前可用资金",
                    "need_live_cash": True,
                }), 400
            base_cash = live_cash
        # 单笔上限护栏：把仓位比夹到 single_position_max_ratio 内（防一次大面腰斩）
        _ratio = float(s.get("position_ratio", 0.20))
        _cap   = float(s.get("single_position_max_ratio", 0.20))
        _eff   = min(_ratio, _cap)
        budget = base_cash * _eff
        shares = max(int(budget / price / 100) * 100, 100)
        if _eff < _ratio:
            _add_log(uid, f"🛡 单笔护栏：仓位比 {_ratio*100:.0f}% > 单笔上限 {_cap*100:.0f}%，按 {_eff*100:.0f}% 建仓")
        _add_log(uid, f"💰 仓位计算: {broker_type} base_cash=¥{base_cash:,.0f} × {_eff*100:.0f}% = ¥{budget:,.0f} → {shares}股 @{price:.2f}")
    err = _deduct("paper_trade", "模拟盘下单")
    if err:
        return jsonify(err), 402
    res = execute_buy(uid, code, name, price, shares, reason="模拟盘手动买入")
    if res.get("ok"):
        # 透传 execute_buy 所有字段（含实盘的 signal_id，前端轮询用）
        return jsonify({
            "ok":         True,
            "shares":     shares,
            "msg":        res.get("msg") or f"已买入 {code} {shares}股 @{price}",
            "signal_id":  res.get("signal_id"),     # 实盘模式才有
            "exec_price": res.get("exec_price"),    # 模拟模式才有
            "total_cost": res.get("total_cost"),
        })
    return jsonify({"ok": False, "msg": res.get("msg") or "买入失败，请检查资金或持仓（详见日志）"})


@app.route("/api/paper/sell", methods=["POST"])
@require_auth
def paper_manual_sell():
    uid   = session["user_id"]
    d     = request.json
    code  = d.get("code", "")
    # 优先拉 ths_quote 实时价（前端传的 price 可能是 cur_price fallback 到 cost_price
    # 的脏值，会导致卖出价≈买入价、盈亏只剩费用 -0.22% 的假象）
    price = 0.0
    try:
        from market import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            qs = client.get_quotes([code])
            if qs:
                price = float(qs[0].get("price", 0))
    except Exception:
        pass
    # 实时价拉不到时，再用前端传的 price 作 fallback
    if not price:
        price = _positive_float_value(d.get("price"), 0.0)
    if not price:
        return jsonify({"ok": False, "msg": "无法获取实时价格，请稍后重试"})
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
@app.route("/api/announcements")
@require_auth
def api_announcements():
    """用户端：拉取当前有效公告（含是否已读标记），供登录后弹窗。
    ★ 修复：之前只有 admin 端公告 CRUD，没有任何用户端拉取路由，
    导致管理员发的公告其他用户登录时永远弹不出来。"""
    uid = session["user_id"]
    return jsonify({"ok": True, "announcements": um.get_active_announcements(uid)})


@app.route("/api/announcements/<int:ann_id>/read", methods=["POST"])
@require_auth
def api_announcement_read(ann_id):
    """用户端：标记公告已读（关闭弹窗时调用，下次登录不再重复弹）。"""
    try:
        um.mark_announcement_read(session["user_id"], ann_id)
    except Exception as e:
        logger.warning(f"[公告已读] uid={session.get('user_id')} ann={ann_id}: {e}")
    return jsonify({"ok": True})


@app.route("/api/strategies")
def api_list():
    items = sm.list_strategies()
    # ★ 策略隐私：非 admin 看不到内置(自研)策略的简介(desc)，只保留名称供选择/激活
    if not session.get("is_admin"):
        for it in items:
            if it.get("builtin"):
                it["desc"] = "🔒 内置策略，逻辑保密"
    return jsonify(items)


@app.route("/api/strategies/<filename>")
@require_auth
def api_get(filename):
    meta = sm.get_meta(filename)
    # ★ 策略隐私：内置策略（自研）的源码仅 admin 或被授权管理员可见；其他云端用户拿到"受保护"占位，
    #   防止被查看/复制复用。策略仍可正常激活运行（运行/回测走 sm 内部真实代码，不经此路由）。
    if meta.get("builtin") and not _can_view_protected_source():
        nm = meta.get("name", filename)
        placeholder = (
            '# META: {"name": "%s", "builtin": true}\n'
            '"""\n'
            '🔒 内置策略「%s」— 源码与逻辑均受保护\n\n'
            '这是平台内置策略，选股/交易逻辑及说明均不对外展示。\n'
            '你可以直接「激活」运行它，但无法查看其源码或策略细节。\n'
            '如需自定义策略，请点「+ 新建」编写你自己的（你自建的可正常查看/编辑）。\n'
            '"""\n'
        ) % (nm, nm)
        return jsonify({"code": placeholder, "builtin": True, "locked": True})
    return jsonify({"code": sm.get_code(filename),
                    "builtin": bool(meta.get("builtin")), "locked": False})


def _can_view_protected_source() -> bool:
    return is_root_admin_session() or "strategy_source_view" in current_admin_permissions()


def _inject_builtin_flag(code: str, is_admin: bool) -> str:
    """在策略 META 行注入 builtin 标记，自动落实"admin 创建的策略一律受保护、
    普通用户自建的可见"：admin 存→builtin:true，普通用户存→builtin:false。
    保留 META 其它字段；无 META 行则补一行。"""
    protect = bool(is_admin)
    lines = code.split("\n", 1)
    first = lines[0].strip() if lines else ""
    rest  = lines[1] if len(lines) > 1 else ""
    if first.startswith("# META:"):
        try:
            meta = json.loads(first[len("# META:"):].strip())
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
        meta["builtin"] = protect
        new_first = "# META: " + json.dumps(meta, ensure_ascii=False)
        return new_first + ("\n" + rest if rest != "" or len(lines) > 1 else "\n")
    # 没有 META 行：补一行
    meta = {"name": "策略", "desc": "", "author": ("admin" if protect else "用户"), "builtin": protect}
    return "# META: " + json.dumps(meta, ensure_ascii=False) + "\n" + code


@app.route("/api/strategies/save", methods=["POST"])
@require_auth
def api_save():
    uid = session["user_id"]
    d   = request.json
    fn  = d.get("filename", "")
    is_admin = bool(session.get("is_admin"))
    # ★ 内置(admin)策略受保护：非 admin 不可保存/覆盖
    if sm.get_meta(fn).get("builtin") and not is_admin:
        return jsonify({"ok": False, "msg": "内置策略受保护，不可修改或覆盖。请点『+ 新建』编写你自己的策略"})
    # ★ 自动打标记：admin 创建/保存的策略一律受保护(builtin:true)，普通用户自建的可见(false)
    code = _inject_builtin_flag(d.get("code", ""), is_admin)
    r   = sm.save(fn, code)
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
    uid = session["user_id"]
    d   = request.get_json(silent=True) or {}
    fn  = (d.get("filename") or "").strip()
    if not fn:
        return jsonify({"ok": False, "error": "invalid filename"}), 400
    cs  = d.get("codes") or []
    # 前端可传 max_codes 控制扫描范围：
    #   0       = 全市场（约 5000 只，2-3 分钟）
    #   600     = 快速扫描（按成交量取前 600 只，约 15 秒）
    #   不传    = 默认全市场
    try:
        mc = int(d.get("max_codes", 0))
    except (TypeError, ValueError):
        mc = 0
    err = _deduct("scan", "全市场选股扫描")
    if err:
        return jsonify(err), 402
    scope_label = "全市场" if not cs else f"{len(cs)}只"
    if not cs and mc > 0:
        scope_label = f"活跃前{mc}只"
    _add_log(uid, f"执行选股: [{fn}] {scope_label}...")
    r       = sm.run(fn, cs, max_codes=mc)
    scanned = r.get("scanned", r.get("count", 0))
    _add_log(uid, f"选股完成，扫描 {scanned} 只，命中 {r.get('count',0)} 只")
    return jsonify(r)


@app.route("/api/strategies/run_async", methods=["POST"])
@require_auth
def api_run_async():
    """异步选股：立即返回 task_id，前端轮询 /strategies/progress 和 /strategies/result。"""
    import uuid as _uuid
    uid = session["user_id"]
    d   = request.get_json(silent=True) or {}
    fn  = (d.get("filename") or "").strip()
    if not fn:
        return jsonify({"ok": False, "error": "invalid filename"}), 400
    cs  = d.get("codes") or []
    try:
        mc = int(d.get("max_codes", 0))
    except (TypeError, ValueError):
        mc = 0
    err = _deduct("scan", "全市场选股扫描")
    if err:
        return jsonify(err), 402
    task_id = _uuid.uuid4().hex[:12]
    with _strat_tasks_lock:
        _strat_tasks[task_id] = {"status": "pending", "uid": uid, "fn": fn}
    scope_label = "全市场" if not cs else f"{len(cs)}只"
    if not cs and mc > 0:
        scope_label = f"活跃前{mc}只"
    _add_log(uid, f"执行选股(异步): [{fn}] {scope_label}... task={task_id}")
    def _worker():
        try:
            with _strat_tasks_lock:
                _strat_tasks[task_id]["status"] = "running"
            r = sm.run(fn, cs, max_codes=mc)
            with _strat_tasks_lock:
                _strat_tasks[task_id].update({"status": "done", "result": r})
            scanned = r.get("scanned", r.get("count", 0))
            _add_log(uid, f"选股完成(异步)，扫描 {scanned} 只，命中 {r.get('count',0)} 只")
        except Exception as e:
            logger.exception(f"[uid={uid}] async scan 失败 task={task_id}")
            with _strat_tasks_lock:
                _strat_tasks[task_id].update({"status": "error", "error": str(e)})
    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/api/strategies/progress/<task_id>")
@require_auth
def api_strat_progress(task_id):
    """查询异步选股任务状态（前端可 1-2s 轮询）"""
    with _strat_tasks_lock:
        task = _strat_tasks.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "任务不存在"})
    snap = {"ok": True, "task_id": task_id, "status": task.get("status")}
    # 借用 strategy_manager 的全局扫描进度
    try:
        from market import ths_quote as tq
        tdx_prog = tq.get_scan_progress()
        strat_prog = sm.get_scan_progress()
        prog = tdx_prog if tdx_prog.get("running") else strat_prog
        if isinstance(prog, dict) and prog.get("running"):
            snap["scan_current"] = int(prog.get("current") or 0)
            snap["scan_total"]   = int(prog.get("total") or 0)
            snap["scan_code"]    = prog.get("code") or ""
            snap["scan_stage"]   = prog.get("stage") or ("行情扫描" if prog is tdx_prog else "策略扫描")
    except Exception:
        pass
    if task.get("status") == "error":
        snap["error"] = task.get("error", "未知错误")
    return jsonify(snap)


@app.route("/api/strategies/result/<task_id>")
@require_auth
def api_strat_result(task_id):
    """获取异步选股结果（任务完成后调用）"""
    with _strat_tasks_lock:
        task = _strat_tasks.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "任务不存在"})
    if task["status"] == "done":
        return jsonify(task.get("result", {"ok": False}))
    if task["status"] == "error":
        return jsonify({"ok": False, "error": task.get("error", "未知错误")})
    return jsonify({"ok": False, "status": task["status"], "msg": "任务未完成"})


@app.route("/api/strategies/activate", methods=["POST"])
@require_auth
def api_activate():
    uid       = session["user_id"]
    s         = get_user_state(uid)
    fn        = request.json.get("filename")
    was_running = bool(s.get("running"))
    s["active_strategy"] = fn
    if fn:
        s["running"] = True
        _persist_state(uid, active_strategy=fn, running=1)
    else:
        _persist_state(uid, active_strategy="")
    meta_list = sm.list_strategies()
    meta      = next((m for m in meta_list if m["file"] == fn), {})
    label     = meta.get('name', fn) if fn else '默认'
    if fn and not was_running:
        _add_log(uid, f"自动交易策略切换并启动监控: {label}")
    else:
        _add_log(uid, f"自动交易策略切换: {label}")
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
    try:
        mc = int(d.get("max_codes", 0))
    except (TypeError, ValueError):
        mc = 0
    scope_label = "全市场" if not cs else f"{len(cs)}只"
    if not cs and mc > 0:
        scope_label = f"活跃前{mc}只"
    _add_log(uid, f"执行因子选股: [{fn}] {scope_label}...")
    r       = fm.run(fn, cs, max_codes=mc)
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
    # 全局配置为权威，per-user 仅兜底（与 ai_analyze 一致，避免旧 key 顶掉新配置导致 401）
    cfg = ai.resolve_config(um.get_user_ai_config(uid) or {})
    r = ai.generate_factor(desc, config_override=cfg)
    if r.get("ok"):
        _add_log(uid, f"AI因子生成完成，共{r.get('length', len(r.get('code','')))}字符")
    return jsonify(r)


@app.route("/api/factors/<filename>")
@require_auth
def api_factor_get(filename):
    meta = fm.get_meta(filename) if hasattr(fm, "get_meta") else {"name": filename, "builtin": True}
    if meta.get("builtin", True) and not _can_view_protected_source():
        name = meta.get("name") or filename
        placeholder = (
            '# FACTOR: {"name": "%s", "builtin": true}\n'
            '"""\n'
            'Protected built-in factor. Source code and core logic are not exposed through Web/API.\n'
            'Create a custom factor if you need editable source.\n'
            '"""\n'
        ) % name
        return jsonify({"code": placeholder, "builtin": True, "locked": True})
    return jsonify({"code": fm.get_code(filename), "builtin": bool(meta.get("builtin")), "locked": False})


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
    d   = request.get_json(silent=True) or {}
    fn  = d.get("filename", "")
    cs  = d.get("codes") or []
    period = _int_value(d.get("period"), 20)
    if period is None or period <= 0:
        return jsonify({"ok": False, "error": "invalid period"}), 400
    period = max(1, min(250, period))
    _add_log(uid, f"因子评价: [{fn}] period={period}")
    try:
        r = fm.evaluate(fn, cs, period=period)
        return jsonify(r)
    except AttributeError:
        return jsonify({"ok": False, "error": "因子管理器暂不支持 evaluate 方法"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ──────────────────────────────────────────────────────────────
#  Vibe Alpha 因子库 (HKUDS/Vibe-Trading 移植)
#  - /api/factors_zoo/list:           列出所有可用 alpha（带元数据）
#  - /api/factors_zoo/compute:        同步算分（< 1000 只，前端等结果）
#  - /api/factors_zoo/compute_async:  异步算分（启动后台任务，返回 task_id）
#  - /api/factors_zoo/progress/<id>:  查询任务进度
#  - /api/factors_zoo/result/<id>:    取任务结果
#  完全独立于 factor_manager，不参与自动监控扫描/下单链路
# ──────────────────────────────────────────────────────────────
# 策略选股异步任务表（进程内，重启丢失）
_strat_tasks: dict = {}            # task_id -> {status, result, error, ...}
_strat_tasks_lock = threading.Lock()

# ──────────────────────────────────────────────────────────────
# 异步任务表（进程内，重启丢失；阶段 1 够用）
_fz_tasks: dict = {}            # task_id -> {status, progress, total, ...}
_fz_tasks_lock = threading.Lock()
@app.route("/api/factors_zoo/list")
@require_auth
def api_factors_zoo_list():
    """列出 Vibe Alpha 因子库的所有可用因子"""
    try:
        from analytics.factors_zoo import registry as _fz_registry
        reg = _fz_registry()
        items = []
        for aid in reg.list():
            try:
                h = reg.get(aid)
                m = h.meta or {}
                items.append({
                    "id":              h.id,
                    "zoo":             h.zoo,
                    "nickname":        m.get("nickname") or h.id,
                    "description_cn":  m.get("description_cn", ""),
                    "theme":           m.get("theme", []),
                    "formula":         m.get("formula_latex", ""),
                    "universe":        m.get("universe", []),
                    "frequency":       m.get("frequency", []),
                    "decay_horizon":   m.get("decay_horizon", 0),
                    "min_warmup_bars": m.get("min_warmup_bars", 0),
                    "columns_required": m.get("columns_required", []),
                    "notes":           m.get("notes", ""),
                })
            except Exception as e:
                logger.warning(f"[factors_zoo] meta 读取失败 {aid}: {e}")
        return jsonify({
            "ok": True,
            "factors": sorted(items, key=lambda x: x["id"]),
            "health": reg.health(),
        })
    except Exception as e:
        logger.exception("[factors_zoo] list 失败")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/factors_zoo/compute", methods=["POST"])
@require_auth
def api_factors_zoo_compute():
    """对一组股票算指定 alpha 的分数

    入参:
      factor_id:  str           必填，例 "gtja191_001"
      mode:       str           "manual" (默认) | "market_top"
      codes:      list[str]     mode=manual 时必填，例 ["600519","000001"]
      max_codes:  int           mode=market_top 时取活跃前 N 只（默认 500，上限 1000）
      days:       int           回看天数（默认 60，最少 30，最多 250）
    返回:
      ok:           bool
      factor_meta:  dict
      scores:       {code: float|None}  按分数降序，None 列在末尾
      ranked:       [{code,name,score,rank}]  含名称的排序结果
      scanned:      int       实际算到 panel 里的股票数
      took_ms:      int       服务端总耗时
    """
    import time as _time
    uid = session["user_id"]
    d   = request.json or {}
    factor_id = (d.get("factor_id") or "").strip()
    mode      = (d.get("mode") or "manual").strip()
    try:
        days = max(30, min(250, int(d.get("days", 60))))
    except (TypeError, ValueError):
        days = 60
    try:
        # 0 = 全市场无上限（~5000+ 只）；>0 = 取活跃前 N 只
        max_codes = int(d.get("max_codes", 500))
        if max_codes != 0:
            max_codes = max(10, min(10000, max_codes))
    except (TypeError, ValueError):
        max_codes = 500

    if not factor_id:
        return jsonify({"ok": False, "error": "factor_id 必填"})

    t_start = _time.time()
    try:
        from analytics.factors_zoo import registry as _fz_registry, build_panel, latest_score
        from market.market_data import get_kline as _get_kline
        reg = _fz_registry()
        if factor_id not in reg.list():
            return jsonify({"ok": False, "error": f"因子 {factor_id} 不存在，已注册: {reg.list()[:10]}"})

        # ── 确定股票池 ──
        if mode == "market_top":
            try:
                universe, _qcache = sm._full_market_universe_with_quotes(max_codes=max_codes)
            except Exception as e:
                return jsonify({"ok": False, "error": f"全市场拉取失败: {e}"})
            if not universe:
                return jsonify({"ok": False, "error": "无法获取全市场行情（TDX 未连接），请稍后重试或手动输入代码"})
            codes = list(universe)
            scope_label = f"活跃前{len(codes)}只"
        else:
            raw_codes = d.get("codes") or []
            if isinstance(raw_codes, str):
                codes = [c.strip() for c in raw_codes.replace("，", ",").split(",") if c.strip()]
            else:
                codes = [str(c).strip() for c in raw_codes if str(c).strip()]
            if not codes:
                return jsonify({"ok": False, "error": "manual 模式 codes 必填"})
            if len(codes) > 1000:
                return jsonify({"ok": False, "error": f"代码数 {len(codes)} 超过 1000 上限"})
            scope_label = f"{len(codes)}只"
            _qcache = {}

        _add_log(uid, f"Vibe因子计算: [{factor_id}] {scope_label} days={days}")

        # ── 预热检查（友好错误，比"NaN 占比过高"更好理解）──
        try:
            _meta_check = reg.get(factor_id).meta or {}
            _warmup = int(_meta_check.get("min_warmup_bars", 0) or 0)
            if _warmup > 0 and days < _warmup + 5:
                return jsonify({
                    "ok": False,
                    "error": f"因子 {factor_id} 需要至少 {_warmup} 天历史数据进行预热（当前回看 {days} 天）。"
                             f"请把「回看天数」调到 {_warmup + 20} 天以上再算。",
                    "min_warmup_bars": _warmup,
                    "suggested_days": _warmup + 20,
                })
        except Exception:
            pass

        # ── 拼面板 + 计算 ──
        # 全市场用 12 线程并行拉 K 线（手动模式 ≤ 50 只用 8 线程足够）
        _workers = 12 if mode == "market_top" else 8
        panel = build_panel(codes, _get_kline, days=days, workers=_workers)
        if not panel or "close" not in panel:
            return jsonify({"ok": False, "error": "没有任何股票能拿到 K 线数据，请检查网络/缓存"})

        # 检查实际拿到的 K 线行数是否够预热
        _actual_rows = panel["close"].shape[0]
        if _warmup > 0 and _actual_rows < _warmup:
            return jsonify({
                "ok": False,
                "error": f"实际拉到的 K 线只有 {_actual_rows} 天，不足因子 {factor_id} 所需的 {_warmup} 天预热。"
                         f"原因：股票上市不足 / 缓存数据不全 / 数据源限流。建议换一只历史悠久的股票或减小回看要求。",
                "actual_rows": _actual_rows,
                "min_warmup_bars": _warmup,
            })

        df = reg.compute(factor_id, panel)
        scores = latest_score(df, mode="per_column")
        scanned = panel["close"].shape[1]

        # ── 拼名称 + 价格（用于直接灌入选股池）+ 排序 ──
        # market_top 模式 _qcache 已有 quotes；manual 模式现拉一次
        quote_map: dict = {}
        if mode == "market_top" and _qcache:
            for q in _qcache.values():
                if isinstance(q, dict) and q.get("code"):
                    quote_map[q["code"]] = q
        else:
            try:
                import market.ths_quote as _tq
                _client = _tq.get_client()
                if _client and _client._ok:
                    for q in (_client.get_quotes(codes) or []):
                        if q and q.get("code"):
                            quote_map[q["code"]] = q
            except Exception as e:
                logger.debug(f"[factors_zoo] manual 模式 quote 拉取失败: {e}")

        # 现价 fallback：quote 拿不到时退到 panel.close 最新值（非交易时段必备）
        _close_panel = panel.get("close")
        ranked = []
        for code, val in scores.items():
            q = quote_map.get(code, {})
            price = float(q.get("price") or 0)
            if price <= 0 and _close_panel is not None and code in _close_panel.columns:
                try:
                    _last_close = _close_panel[code].dropna()
                    if len(_last_close) > 0:
                        price = float(_last_close.iloc[-1])
                except Exception:
                    pass
            ranked.append({
                "code":   code,
                "name":   q.get("name", ""),
                "price":  price,
                "change": float(q.get("change_pct") or 0),
                "score":  val,
            })
        # None 排在最后，其余按降序
        ranked.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
        for i, r in enumerate(ranked):
            r["rank"] = i + 1

        meta = reg.get(factor_id).meta or {}
        took_ms = int((_time.time() - t_start) * 1000)
        _add_log(uid, f"Vibe因子完成: [{factor_id}] scanned={scanned} took={took_ms}ms")
        return jsonify({
            "ok": True,
            "factor_id": factor_id,
            "factor_meta": {
                "nickname":        meta.get("nickname") or factor_id,
                "description_cn":  meta.get("description_cn", ""),
                "theme":           meta.get("theme", []),
                "formula":         meta.get("formula_latex", ""),
                "universe":        meta.get("universe", []),
                "min_warmup_bars": meta.get("min_warmup_bars", 0),
                "notes":           meta.get("notes", ""),
            },
            "scores":  scores,
            "ranked":  ranked,
            "scanned": scanned,
            "requested": len(codes),
            "took_ms": took_ms,
        })
    except Exception as e:
        logger.exception(f"[factors_zoo] compute 失败 factor_id={factor_id}")
        return jsonify({"ok": False, "error": str(e), "took_ms": int((_time.time() - t_start) * 1000)})


def _fz_run_async_compute(task_id: str, uid: int, factor_id: str, codes: list,
                          quote_map: dict, days: int):
    """后台线程执行因子计算，结果存到 _fz_tasks[task_id]"""
    import time as _time
    from analytics.factors_zoo import registry as _fz_registry, build_panel, latest_score
    from market.market_data import get_kline as _get_kline
    t0 = _time.time()
    try:
        # 注意：build_panel 内部用 factor_manager 的 _scan_prog 上报进度
        # 我们这里只把任务从 init → running → done 三态切
        with _fz_tasks_lock:
            _fz_tasks[task_id]["status"] = "running"
            _fz_tasks[task_id]["phase"]  = "拉取K线"
        panel = build_panel(codes, _get_kline, days=days, workers=12)
        if not panel or "close" not in panel:
            with _fz_tasks_lock:
                _fz_tasks[task_id].update({"status": "error", "error": "无任何 K 线数据"})
            return

        with _fz_tasks_lock:
            _fz_tasks[task_id]["phase"] = "计算因子分数"
        reg = _fz_registry()
        df = reg.compute(factor_id, panel)
        scores = latest_score(df, mode="per_column")
        scanned = panel["close"].shape[1]

        with _fz_tasks_lock:
            _fz_tasks[task_id]["phase"] = "整理结果"
        # 现价 fallback：quote 拿不到时退到 panel.close 最新值（非交易时段必备）
        _close_panel = panel.get("close")
        ranked = []
        for code, val in scores.items():
            q = quote_map.get(code, {})
            price = float(q.get("price") or 0)
            if price <= 0 and _close_panel is not None and code in _close_panel.columns:
                try:
                    _last_close = _close_panel[code].dropna()
                    if len(_last_close) > 0:
                        price = float(_last_close.iloc[-1])
                except Exception:
                    pass
            ranked.append({
                "code":   code,
                "name":   q.get("name", ""),
                "price":  price,
                "change": float(q.get("change_pct") or 0),
                "score":  val,
            })
        ranked.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
        for i, r in enumerate(ranked):
            r["rank"] = i + 1

        meta = reg.get(factor_id).meta or {}
        took_ms = int((_time.time() - t0) * 1000)
        result = {
            "ok": True,
            "factor_id": factor_id,
            "factor_meta": {
                "nickname":        meta.get("nickname") or factor_id,
                "description_cn":  meta.get("description_cn", ""),
                "theme":           meta.get("theme", []),
                "formula":         meta.get("formula_latex", ""),
                "universe":        meta.get("universe", []),
                "min_warmup_bars": meta.get("min_warmup_bars", 0),
            },
            "scores":  scores,
            "ranked":  ranked,
            "scanned": scanned,
            "requested": len(codes),
            "took_ms": took_ms,
        }
        with _fz_tasks_lock:
            _fz_tasks[task_id].update({"status": "done", "result": result, "took_ms": took_ms})
        _add_log(uid, f"Vibe异步完成: [{factor_id}] scanned={scanned} took={took_ms}ms")
    except Exception as e:
        logger.exception(f"[factors_zoo] async compute 失败 task={task_id}")
        with _fz_tasks_lock:
            _fz_tasks[task_id].update({"status": "error", "error": str(e)})


@app.route("/api/factors_zoo/compute_async", methods=["POST"])
@require_auth
def api_factors_zoo_compute_async():
    """启动异步因子计算（适合全市场扫描）

    入参同 /compute，多支持 max_codes=0（全市场无上限 ~5000+ 只）
    返回：{ok, task_id} —— 客户端凭 task_id 轮询 /progress 和 /result
    """
    import uuid as _uuid
    uid = session["user_id"]
    d   = request.json or {}
    factor_id = (d.get("factor_id") or "").strip()
    mode      = (d.get("mode") or "manual").strip()
    try:
        days = max(30, min(250, int(d.get("days", 60))))
    except (TypeError, ValueError):
        days = 60
    try:
        max_codes = int(d.get("max_codes", 0))
        if max_codes != 0:
            max_codes = max(10, min(10000, max_codes))
    except (TypeError, ValueError):
        max_codes = 0

    if not factor_id:
        return jsonify({"ok": False, "error": "factor_id 必填"})

    try:
        from analytics.factors_zoo import registry as _fz_registry
        reg = _fz_registry()
        if factor_id not in reg.list():
            return jsonify({"ok": False, "error": f"因子 {factor_id} 不存在"})

        # 确定股票池 + quote
        quote_map = {}
        if mode == "market_top":
            try:
                universe, _qcache = sm._full_market_universe_with_quotes(max_codes=max_codes)
            except Exception as e:
                return jsonify({"ok": False, "error": f"全市场拉取失败: {e}"})
            if not universe:
                return jsonify({"ok": False, "error": "无法获取全市场行情"})
            codes = list(universe)
            for q in (_qcache or {}).values():
                if isinstance(q, dict) and q.get("code"):
                    quote_map[q["code"]] = q
        else:
            raw_codes = d.get("codes") or []
            if isinstance(raw_codes, str):
                codes = [c.strip() for c in raw_codes.replace("，", ",").split(",") if c.strip()]
            else:
                codes = [str(c).strip() for c in raw_codes if str(c).strip()]
            if not codes:
                return jsonify({"ok": False, "error": "manual 模式 codes 必填"})

        task_id = _uuid.uuid4().hex[:12]
        scope_label = f"活跃{max_codes or '全市场'}只" if mode == "market_top" else f"{len(codes)}只"
        with _fz_tasks_lock:
            _fz_tasks[task_id] = {
                "status":    "pending",
                "phase":     "排队中",
                "factor_id": factor_id,
                "scope":     scope_label,
                "total":     len(codes),
                "created":   _time.time() if (_time := __import__("time")) else 0,
            }
        threading.Thread(
            target=_fz_run_async_compute,
            args=(task_id, uid, factor_id, codes, quote_map, days),
            daemon=True,
            name=f"fz-{task_id}",
        ).start()
        _add_log(uid, f"Vibe异步启动: [{factor_id}] {scope_label} task={task_id}")
        return jsonify({"ok": True, "task_id": task_id, "scope": scope_label, "total": len(codes)})
    except Exception as e:
        logger.exception(f"[factors_zoo] compute_async 启动失败")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/factors_zoo/progress/<task_id>")
@require_auth
def api_factors_zoo_progress(task_id):
    """查询异步任务进度（轻量级，前端可 1s 轮询）"""
    with _fz_tasks_lock:
        task = _fz_tasks.get(task_id)
        if not task:
            return jsonify({"ok": False, "error": "任务不存在"})
        snap = {
            "ok":        True,
            "task_id":   task_id,
            "status":    task.get("status"),
            "phase":     task.get("phase", ""),
            "scope":     task.get("scope", ""),
            "factor_id": task.get("factor_id", ""),
            "total":     task.get("total", 0),
        }
    # 借用 sm._scan_prog（factor_manager / strategy_manager 共享的全局扫描进度）
    try:
        from strategy_engine import strategy_manager as _sm_mod
        prog = getattr(_sm_mod, "_scan_prog", None) or getattr(_sm_mod, "_tdx_prog", {})
        if isinstance(prog, dict) and prog.get("running"):
            snap["scan_current"] = int(prog.get("current") or 0)
            snap["scan_total"]   = int(prog.get("total") or 0)
            snap["scan_code"]    = prog.get("code") or ""
    except Exception:
        pass
    if snap["status"] == "error":
        with _fz_tasks_lock:
            snap["error"] = _fz_tasks[task_id].get("error", "未知错误")
    return jsonify(snap)


@app.route("/api/factors_zoo/backtest", methods=["POST"])
@require_auth
def api_factors_zoo_backtest():
    """因子回测（同步，前端等结果；大池子用 codes 限制规模）

    入参:
      factor_id:      str    必填
      codes:          list/str  股票池（manual 模式）
      mode:           "manual" | "market_top"
      max_codes:      int    market_top 模式时取活跃前 N（默认 100，回测建议 ≤ 500）
      days:           int    历史天数（默认 200）
      top_k:          int    每次调仓选前 K 只（默认 10）
      rebalance_days: int    调仓周期（默认 5）
      hold_days:      int    持有天数（默认 5）
      fee_rate:       float  单边费率（默认 0）
    """
    uid = session["user_id"]
    d   = request.json or {}
    factor_id = (d.get("factor_id") or "").strip()
    mode      = (d.get("mode") or "manual").strip()
    try:    days = max(60, min(400, int(d.get("days", 200))))
    except: days = 200
    try:    top_k = max(1, min(50, int(d.get("top_k", 10))))
    except: top_k = 10
    try:    rebalance_days = max(1, min(30, int(d.get("rebalance_days", 5))))
    except: rebalance_days = 5
    try:    hold_days = max(1, min(rebalance_days, int(d.get("hold_days", 5))))
    except: hold_days = 5
    try:    fee_rate = max(0.0, min(0.01, float(d.get("fee_rate", 0))))
    except: fee_rate = 0.0
    try:    max_codes = max(10, min(1000, int(d.get("max_codes", 100))))
    except: max_codes = 100

    if not factor_id:
        return jsonify({"ok": False, "error": "factor_id 必填"})

    try:
        if mode == "market_top":
            universe, _qcache = sm._full_market_universe_with_quotes(max_codes=max_codes)
            codes = list(universe or [])
            if not codes:
                return jsonify({"ok": False, "error": "全市场拉取失败"})
        else:
            raw = d.get("codes") or []
            if isinstance(raw, str):
                codes = [c.strip() for c in raw.replace("，", ",").split(",") if c.strip()]
            else:
                codes = [str(c).strip() for c in raw if str(c).strip()]
            if not codes:
                return jsonify({"ok": False, "error": "codes 必填"})

        _add_log(uid, f"Vibe因子回测: [{factor_id}] {len(codes)}只 days={days} top_k={top_k} hold={hold_days}")
        from analytics.factors_zoo.backtest import backtest_factor
        from market.market_data import get_kline as _get_kline
        result = backtest_factor(
            factor_id=factor_id,
            codes=codes,
            get_kline=_get_kline,
            days=days,
            top_k=top_k,
            rebalance_days=rebalance_days,
            hold_days=hold_days,
            fee_rate=fee_rate,
        )
        if result.get("ok"):
            s = result["stats"]
            _add_log(uid, f"回测完成: 总收益={s['total_return']*100:.2f}% 胜率={s['win_rate']*100:.1f}% IC={s['ic_mean']:.3f}")
        return jsonify(result)
    except Exception as e:
        logger.exception(f"[factors_zoo] backtest 失败")
        return jsonify({"ok": False, "error": str(e)})


# ──────────────────────────────────────────────────────────────
#  Vibe 因子激活 API（激活后调度器每 5 分钟自动算分+开单）
# ──────────────────────────────────────────────────────────────
@app.route("/api/vibe_activations/me", methods=["GET"])
@require_auth
def api_vibe_activation_me():
    uid = session["user_id"]
    try:
        from analytics.factors_zoo import activator as _act
        cfg = _act.get(uid)
        return jsonify({"ok": True, "activation": cfg})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/vibe_activations/activate", methods=["POST"])
@require_auth
def api_vibe_activation_activate():
    uid = session["user_id"]
    d   = request.json or {}
    factor_id = (d.get("factor_id") or "").strip()
    if not factor_id:
        return jsonify({"ok": False, "error": "factor_id 必填"})
    # 二次保护：检查因子是否存在 + 是否做过回测且胜率 ≥ 55%（强烈建议但不强制）
    try:
        from analytics.factors_zoo import registry as _r, activator as _act
        if factor_id not in _r().list():
            return jsonify({"ok": False, "error": f"因子 {factor_id} 不存在"})
        params = {
            "mode":             d.get("mode", "market_top"),
            "codes":            d.get("codes", []),
            "max_codes":        d.get("max_codes", 500),
            "daily_limit":      d.get("daily_limit", 3),
            "position_pct":     d.get("position_pct", 0.2),
            "score_percentile": d.get("score_percentile", 0.8),
            "stop_loss_pct":    d.get("stop_loss_pct", -0.03),
            "interval_sec":     d.get("interval_sec", 120),
        }
        cfg = _act.activate(uid, factor_id, params)
        _add_log(uid, f"📚 Alpha因子激活: [{factor_id}] 仓位={params['position_pct']*100:.0f}% 日限={params['daily_limit']} 阈值=top{(1-params['score_percentile'])*100:.0f}% 止损={params['stop_loss_pct']*100:.1f}% 每{params['interval_sec']}秒")
        return jsonify({"ok": True, "activation": cfg})
    except Exception as e:
        logger.exception("[vibe.activate] 失败")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/vibe_activations/deactivate", methods=["POST"])
@require_auth
def api_vibe_activation_deactivate():
    uid = session["user_id"]
    try:
        from analytics.factors_zoo import activator as _act
        cfg = _act.deactivate(uid)
        _add_log(uid, "📚 Alpha 因子已停用")
        return jsonify({"ok": True, "activation": cfg})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# Vibe Alpha 因子激活调度已下沉 core/schedulers.py（工单4）


@app.route("/api/factors_zoo/result/<task_id>")
@require_auth
def api_factors_zoo_result(task_id):
    """取异步任务结果（仅 status=done 时返回完整 ranked）"""
    with _fz_tasks_lock:
        task = _fz_tasks.get(task_id)
        if not task:
            return jsonify({"ok": False, "error": "任务不存在"})
        if task.get("status") != "done":
            return jsonify({"ok": False, "error": f"任务尚未完成: status={task.get('status')}"})
        return jsonify(task["result"])


# ── 回测 API ──────────────────────────────────────────────────
@app.route("/api/backtest", methods=["POST"])
@require_auth
def api_backtest():
    uid      = session["user_id"]
    d        = request.get_json(silent=True) or {}
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
    initial_capital = _positive_float_value(d.get("capital"), 100000)
    max_positions = _int_value(d.get("max_positions"), 5)
    stop_loss = _float_value(d.get("stop_loss"), -0.05)
    take_profit = _float_value(d.get("take_profit"), 0.10)
    if initial_capital is None:
        return jsonify({"ok": False, "error": "invalid capital"}), 400
    if max_positions is None or max_positions <= 0:
        return jsonify({"ok": False, "error": "invalid max_positions"}), 400
    if stop_loss is None:
        return jsonify({"ok": False, "error": "invalid stop_loss"}), 400
    if take_profit is None:
        return jsonify({"ok": False, "error": "invalid take_profit"}), 400

    bt_kwargs   = dict(
        strategy_code   = code_str,
        codes           = codes,
        start_date      = d.get("start_date", "2024-01-01"),
        end_date        = d.get("end_date",   "2024-12-31"),
        initial_capital = initial_capital,
        max_positions   = max_positions,
        stop_loss       = stop_loss,
        take_profit     = take_profit,
    )
    err = _deduct("backtest", "策略回测")
    if err:
        return jsonify(err), 402

    if full_market:
        from strategy_engine.backtest import start_backtest_async
        task_id = start_backtest_async(**bt_kwargs)
        _add_log(uid, f"全市场回测已启动 [{fn}] {d.get('start_date')}~{d.get('end_date')} task={task_id}")
        return jsonify({"ok": True, "async": True, "task_id": task_id})
    else:
        from strategy_engine.backtest import run_backtest
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
    from strategy_engine.backtest import get_backtest_task
    return jsonify(get_backtest_task(task_id))


@app.route("/api/backtest/cancel/<task_id>", methods=["POST"])
@require_auth
def api_backtest_cancel(task_id):
    uid = session["user_id"]
    from strategy_engine.backtest import cancel_backtest_task
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
    d   = request.get_json(silent=True) or {}
    codes   = d.get("codes", [])
    mode    = d.get("mode", "equal_weight")
    capital = _positive_float_value(d.get("capital"), get_user_state(uid)["capital"])
    if capital is None:
        return jsonify({"ok": False, "error": "invalid capital"}), 400
    if not codes:
        return jsonify({"ok": False, "error": "codes 不能为空"})
    try:
        from analytics.portfolio_optimizer import optimize_portfolio
        result = optimize_portfolio(codes, mode=mode, capital=capital)
        _add_log(uid, f"组合优化完成: mode={mode} codes={len(codes)}")
        return jsonify(result)
    except ImportError:
        return jsonify({"ok": False, "error": "portfolio_optimizer 模块未找到"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ══════════════════════════════════════════════════════════════
# 调度器已下沉 core/schedulers.py（工单4；为调度进程独立做准备）：
#   run_scheduler / run_vibe_scheduler / 收盘快照与自动终止 / start_background_services
# ══════════════════════════════════════════════════════════════
from core.schedulers import start_background_services as _start_background_services  # noqa: E402


# 模块级启动：gunicorn 导入 main:app 时不进入 __main__，必须在此启动。
# 设 THS_NO_SCHEDULER=1 或 QUANT_TEST_IMPORT=1 可禁用（供测试/纯导入场景）。
if _os.environ.get("THS_NO_SCHEDULER") != "1" and not _TEST_IMPORT:
    _start_background_services()


if __name__ == "__main__":
    app.run(port=5001, debug=False)
