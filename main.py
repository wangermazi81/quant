import os as _os
import _pathsetup  # noqa: F401  # 必须在其它项目模块 import 之前
# ── 清除系统代理（akshare 走国内接口，代理反而导致连接失败）──────────
for _k in ("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy",
           "ALL_PROXY","all_proxy","SOCKS_PROXY","socks_proxy"):
    _os.environ.pop(_k, None)

from flask import Flask, jsonify, request, session, Response
from flask_cors import CORS
from functools import wraps
import threading, schedule, time, logging, uuid, secrets, json, queue as _queue
from typing import Optional
from datetime import datetime
from market_data import is_trading_time
from strategy import scan_all
from ths_operator import THSOperator
from config import STOCK_POOL, SCAN_INTERVAL, DRY_RUN, INITIAL_CAPITAL, POSITION_RATIO
try:
    from config import DABAN_STOP_LOSS, DABAN_TRAIL, DABAN_MAX_HOLD, DABAN_HOLD_LIMITUP
except ImportError:
    DABAN_STOP_LOSS, DABAN_TRAIL, DABAN_MAX_HOLD, DABAN_HOLD_LIMITUP = -0.05, 0.07, 10, True
import strategy_manager as sm
import data_manager as dm
import ai_analyst as ai
import factor_manager as fm
import paper_broker as pb
import user_manager as um
import notification_manager as nm
import order_sizing  # 下单仓位/手数纯函数（从 do_scan 抽出，有回归测试 tests/test_order_sizing.py）
import daban_ride    # 打板 Ride 卖出决策纯函数（从本文件抽出，有回归测试 tests/test_daban_ride.py）
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
    # ── 初始化数据库 ──
    um.init_db()
    # 问题反馈表（与 users.db 同库，幂等建表）
    import feedback_manager as fbm
    fbm.init_db()
    import changelog_manager as clm
    clm.init_db()
    # 迁移必须在 pb.init_paper_db() 之前，先给旧表加 user_id 列
    try:
        import db_migration as _dbm
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


if not _TEST_IMPORT:
    _init_runtime()

# ── 共享运行态/通用辅助（见 app_state.py，与各 web 蓝图共用同一份内存状态）──
from app_state import (
    user_states, _user_state_lock, _sse_subscribers, _sse_lock,
    _default_user_state, get_user_state, _persist_state, _add_log,
    _sse_push, _restore_all_user_states, require_auth, require_admin, _deduct,
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




# ══════════════════════════════════════════════════════════════
# 核心交易逻辑（per-user）
# ══════════════════════════════════════════════════════════════
def _reset_daily_if_needed(uid: int):
    """每个交易日开始时清零今日计数 + 过期清理隔日遗留信号。"""
    today = datetime.now().strftime("%Y-%m-%d")
    s = get_user_state(uid)
    if s["today_date"] != today:
        s["today_date"]   = today
        s["today_trades"] = 0
        _add_log(uid, f"📅 新交易日 {today}，今日开仓次数已清零")
        # 隔日未成交的 pending/sent/submitted 信号批量置 expired，
        # 否则会累进资金检查（曾出现"已用仓位9848%"封死开仓）且永久阻塞同名票去重
        try:
            n = um.expire_stale_signals(uid)
            if n:
                _add_log(uid, f"🧹 清理隔日遗留信号 {n} 条（已置为过期）")
        except Exception as _ee:
            logger.warning(f"[uid={uid}] 清理隔日信号失败: {_ee}")


def execute_buy(uid: int, code: str, name: str, price: float, shares: int,
                reason: str = "") -> dict:
    """统一买入入口（模拟盘 / 实盘）。返回 {"ok": bool, "msg": str, ...}"""
    s = get_user_state(uid)
    
    # 获取用户当前的券商配置
    broker_cfg = um.get_user_broker_config(uid)
    broker_type = broker_cfg.get("broker_type", "paper")
    
    if DRY_RUN or broker_type == "paper":
        # ── 模拟盘逻辑 ──
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
            return {"ok": True, "exec_price": result["exec_price"],
                    "total_cost": result["total_cost"]}
        else:
            msg = result.get("msg", "")
            _add_log(uid, f"❌ [模拟] 买入拒绝 {code}: {msg}", "error")
            return {"ok": False, "msg": msg}
    else:
        # ── 实盘逻辑：写入信号队列 ──
        signal_id = um.add_trade_signal(uid, code, name, "BUY", price, shares)
        _add_log(uid, f"⏳ 实盘买入信号已发送队列: {code} {name} {shares}股 @{price:.2f}", "info")
        return {"ok": True, "msg": "已生成实盘买入信号，等待本地执行...", "signal_id": signal_id}


def execute_sell(uid: int, code: str, price: float, sell_reason: str = "手动卖出") -> dict:
    """统一卖出入口。"""
    s   = get_user_state(uid)
    pos = s["positions"].get(code)
    
    # 获取用户当前的券商配置
    broker_cfg = um.get_user_broker_config(uid)
    broker_type = broker_cfg.get("broker_type", "paper")
    
    if DRY_RUN or broker_type == "paper":
        # ── 模拟盘逻辑 ──
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
        # ── 实盘逻辑：写入信号队列 ──
        shares = pos["shares"] if pos else 0
        name = pos["name"] if pos else ""
        if shares <= 0:
            return {"ok": False, "msg": "无持仓"}
        signal_id = um.add_trade_signal(uid, code, name, "SELL", price, shares)
        _add_log(uid, f"⏳ 实盘卖出信号已发送队列: {code} {name} {shares}股 @{price:.2f}", "info")
        return {"ok": True, "msg": "已生成实盘卖出信号，等待本地执行...", "signal_id": signal_id}


# ── 加仓逻辑（涨幅到9%时以涨停价追板）──────────────────────
_ADD_POSITION_LAYERS: dict = {}  # (uid, code) -> 已加仓层数

def _check_add_position(uid: int, s: dict, broker_type: str, base_cash: float):
    """检查持仓票是否涨到9%，触发加仓。每次扫描时调用。"""
    positions = s.get("positions", {})
    if not positions:
        return
    try:
        import ths_quote as tq
        client = tq.get_client()
        if not client or not client._ok:
            return
        codes = list(positions.keys())
        quotes = client.get_quotes(codes) or []
        for q in quotes:
            try:
                code = q.get("code", "")
                if not code:
                    continue
                price = float(q.get("price", 0) or 0)
                last_close = float(q.get("last_close", 0) or 0)
                limit_up = float(q.get("limit_up", 0) or 0)
                if price <= 0 or last_close <= 0 or limit_up <= 0:
                    continue
                change_pct = (price - last_close) / last_close * 100
                # 涨幅到9%触发加仓
                if change_pct < 9.0:
                    continue
                # 检查已加仓层数（最多2.5层）
                key = (uid, code)
                current_layers = _ADD_POSITION_LAYERS.get(key, 1.0)  # 初始1层
                if current_layers >= 3.0:
                    continue
                # 计算加仓金额（从当前层加到3层=25%）
                target_ratio = 0.25
                current_ratio = current_layers * 0.08
                add_ratio = target_ratio - current_ratio
                add_amount = base_cash * add_ratio
                add_shares = max(int(add_amount / limit_up / 100) * 100, 100)
                if add_shares <= 0:
                    continue
                # 以涨停价挂单
                _add_log(uid, f"?? 加仓触发: {code} 涨幅{change_pct:.1f}% ≥ 9%，以涨停价{limit_up:.2f}追板，加仓至25%", "buy")
                name = positions[code].get("name", code)
                res = execute_buy(uid, code, name, limit_up, add_shares, reason="加仓追板")
                if res.get("ok"):
                    _ADD_POSITION_LAYERS[key] = 3.0  # 加到3层=25%
                    _add_log(uid, f"?? 加仓成功: {code} 已加至3层(25%)", "info")
                else:
                    _add_log(uid, f"? 加仓失败: {code} {res.get('msg', '')}", "error")
            except Exception as _e:
                _logger.debug(f"加仓检查异常 {q.get('code')}: {_e}")
                continue
    except Exception as e:
        _logger.debug(f"加仓检查整体异常: {e}")


def _auto_buy(uid: int, item: dict):
    """自动买入（信号触发后立即执行）。

    队列状态必须反映真实情况：
      模拟盘成交即 done（已买-模拟）；
      实盘只是生成了信号 → submitted（已报实盘，等交易机回传），
      成交确认后由 bridge_report 把队列项推进到 executed。
    """
    code   = item["code"]
    name   = item.get("name", "")
    price  = item["price"]
    shares = item.get("shares", 100)
    try:
        _add_log(uid, f"🤖 自动买入触发: {code} {name} {shares}股 @{price}", "buy")
        res = execute_buy(uid, code, name, price, shares, reason="自动买入")
        if not res.get("ok"):
            item["status"] = "error"
            item["error_msg"] = res.get("msg", "")
        elif res.get("signal_id"):
            item["status"] = "submitted"          # 实盘：已报，未确认成交
            item["signal_id"] = res["signal_id"]
        else:
            item["status"] = "done"               # 模拟：即时成交
    except Exception as e:
        item["status"]    = "error"
        item["error_msg"] = str(e)
        logger.exception(f"[uid={uid}] 自动买入异常: {code}")
        _add_log(uid, f"❌ 自动买入异常: {code} — {e}", "error")


_DABAN_PEAKS: dict = {}   # (uid, code) -> 持仓期最高价（Ride 追踪止盈用）
_DABAN_PEAKS_FILE = BASE_DIR / "data" / "daban_peaks.json"


def _load_daban_peaks():
    """启动时加载模拟盘持仓期峰值。
    ★ 跟实盘 um.update_live_peak 一致：peak 持久化，重启不丢失，
    否则回落止盈会因 peak 被重置成成本价而失真（高位回落判断算不准、漏卖）。"""
    try:
        if _DABAN_PEAKS_FILE.exists():
            raw = json.loads(_DABAN_PEAKS_FILE.read_text(encoding="utf-8"))
            for k, v in raw.items():
                uid_s, code = k.split("|", 1)
                _DABAN_PEAKS[(int(uid_s), code)] = float(v)
            logger.info(f"[daban] 已加载持仓峰值缓存 {len(_DABAN_PEAKS)} 条")
    except Exception as e:
        logger.warning(f"[daban] 加载峰值缓存失败: {e}")


def _save_daban_peaks():
    try:
        raw = {f"{uid}|{code}": v for (uid, code), v in _DABAN_PEAKS.items()}
        _DABAN_PEAKS_FILE.write_text(json.dumps(raw), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[daban] 保存峰值缓存失败: {e}")


_load_daban_peaks()


# 打板涨停幅度 / Ride 卖出决策已抽到 core/daban_ride.py（纯函数，测试见 tests/test_daban_ride.py）

_LIVE_SELL_PENDING: set = set()   # (uid, code) 已生成卖出信号、等成交确认，避免每轮重复发


def _daban_ride_decision(code: str, cost: float, cur: float, last_close: float,
                         hold_days: int, peak: float):
    """打板 Ride 卖出决策（薄包装；逻辑与默认阈值见 core/daban_ride.ride_decision）。"""
    return daban_ride.ride_decision(code, cost, cur, last_close, hold_days, peak)


def _check_live_daban_ride(uid: int):
    """实盘自动卖出（覆盖台账全部持仓，不再只管 da_ban）：
      - strategy=da_ban 的持仓 → 打板 Ride 卖法（吃连板+紧止损+回落卖）；
      - 其他持仓（含无策略标签的历史买入）→ 账户默认止损/止盈/超期。
        旧版只过滤 da_ban，无标签持仓从来不会被自动卖出（-17%还躺着就是这个原因）。

    触发则生成 SELL 信号（桥接器在 xiadan 执行）。遵守 T+1。已发卖出信号未成交的
    持仓不重复发（_LIVE_SELL_PENDING 去重，卖出成交/失败时在 report 处清除）。
    """
    try:
        positions = um.get_live_positions(uid)
    except Exception as e:
        logger.warning(f"[实盘Ride uid={uid}] 读台账失败: {e}")
        return
    if not positions:
        return
    # 账户默认风控参数（非打板持仓用；与模拟盘 check_risk_rules 同一套设置）
    try:
        _bcfg = (um.get_user_broker_config(uid) or {}).get("config") or {}
    except Exception:
        _bcfg = {}
    _acct_stop = float(_bcfg.get("stop_loss") or -0.05)
    _acct_take = float(_bcfg.get("take_profit") or 0.10)
    _acct_hold = int(_bcfg.get("max_hold_days") or 10)

    codes = [p["code"] for p in positions]
    qmap = {}
    try:
        import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            for q in client.get_quotes(codes):
                if q.get("code"):
                    qmap[q["code"]] = q
    except Exception as e:
        logger.debug(f"[实盘Ride uid={uid}] 行情获取失败: {e}")
        return
    import datetime as _dt
    today = _dt.date.today()
    for p in positions:
        code = p["code"]
        if (uid, code) in _LIVE_SELL_PENDING:        # 已发卖出信号、等成交
            continue
        q = qmap.get(code)
        if not q:
            continue
        cur = float(q.get("price") or 0)
        last_close = float(q.get("last_close") or 0)
        cost = float(p.get("buy_price", 0) or 0)
        if cur <= 0 or cost <= 0:
            continue
        try:
            bd = _dt.datetime.strptime(str(p.get("buy_date"))[:10], "%Y-%m-%d").date()
            hold = (today - bd).days
        except Exception:
            hold = 1
        if hold < 1:                                  # T+1：当天买入不卖
            continue

        is_daban = (p.get("strategy") or "") == "da_ban"
        if is_daban:
            sell, reason, new_peak = _daban_ride_decision(
                code, cost, cur, last_close, hold, float(p.get("peak") or cost))
            if new_peak > float(p.get("peak") or 0):
                try:
                    um.update_live_peak(uid, code, new_peak)
                except Exception:
                    pass
        else:
            # 账户默认止损/止盈/超期（用户设置的买卖规则，旧版实盘从不执行）
            pnl = (cur - cost) / cost
            sell, reason = False, None
            if pnl <= _acct_stop:
                sell, reason = True, f"止损({pnl*100:.1f}%≤{_acct_stop*100:.0f}%)"
            elif pnl >= _acct_take:
                sell, reason = True, f"止盈({pnl*100:.1f}%≥{_acct_take*100:.0f}%)"
            elif hold >= _acct_hold:
                sell, reason = True, f"超期({hold}天≥{_acct_hold}天)"
        if not sell:
            continue
        shares = int(p.get("shares", 0) or 0)
        if shares <= 0:
            continue
        try:
            tag = "实盘Ride" if is_daban else "实盘风控"
            sid = um.add_trade_signal(uid, code, p.get("name", ""), "SELL", cur, shares)
            _LIVE_SELL_PENDING.add((uid, code))
            _add_log(uid, f"📤 [{tag}] {code} {p.get('name','')} {reason} → 生成卖出信号"
                          f"(#{sid}, {shares}股 @{cur:.2f})，等桥接器执行", "info")
        except Exception as e:
            logger.warning(f"[实盘风控 uid={uid}] 生成卖出信号失败 {code}: {e}")


def _check_daban_ride(uid: int, broker, positions: list, qmap: dict):
    """打板 Ride 卖出：吃连板（当日仍封板则持有，让利润奔跑）+ 初始止损 + 从最高回落卖。

    这才是打板正确的非对称盈亏比卖法（回测盈亏比≈2.5、期望+3%/笔）：
      - 当日仍封板(连板继续) → 持有，让利润奔跑（不卖）；
      - 跌破成本 DABAN_STOP_LOSS(-5%) → 止损（砍得快）；
      - 从持仓期最高价回落 ≥ DABAN_TRAIL(7%) → 卖（吃完连板后保护利润）；
      - 持有满 DABAN_MAX_HOLD 天 → 卖（backstop）。
    遵守 T+1（当天买入不卖，submit_sell 也会兜底拒绝）。
    """
    s = get_user_state(uid)
    for p in positions:
        code = p.get("code", "")
        cost = float(p.get("cost_price", 0) or 0)
        q    = qmap.get(code)
        if not q or cost <= 0:
            continue
        cur = float(q.get("price") or 0)
        last_close = float(q.get("last_close") or 0)
        if cur <= 0:
            continue
        hold = int(p.get("hold_days", 0) or 0)
        if hold < 1:                       # T+1：当天买入不卖
            continue
        key  = (uid, code)
        sell, reason, new_peak = _daban_ride_decision(
            code, cost, cur, last_close, hold, _DABAN_PEAKS.get(key, cost))
        if _DABAN_PEAKS.get(key) != new_peak:
            _DABAN_PEAKS[key] = new_peak
            _save_daban_peaks()          # 峰值变化即持久化（跟实盘一致，重启不丢）
        if not sell:
            continue
        res = broker.submit_sell(code, cur, sell_reason=reason)
        if res.get("ok"):
            pnl_amt = res.get("pnl", 0)
            sign = "✅" if pnl_amt >= 0 else "❌"
            _add_log(uid, f"{sign} [打板Ride] {code} {p.get('name','')} {reason} "
                          f"盈亏¥{pnl_amt:+,.2f}({res.get('pnl_pct',0):+.1f}%)",
                     "buy" if pnl_amt >= 0 else "error")
            s["positions"].pop(code, None)
            _DABAN_PEAKS.pop(key, None)
            _save_daban_peaks()


def _check_paper_risk(uid: int):
    """检查模拟盘持仓风控并自动卖出。打板策略用 Ride 卖法（吃连板+紧止损+回落卖，
    正确的非对称盈亏比），其它策略沿用账户默认（止损/止盈/超期）。
    仅对「模拟盘持仓」生效（实盘持仓在 xiadan，走另一条卖出链）。
    """
    try:
        broker_cfg = um.get_user_broker_config(uid)
        is_paper   = DRY_RUN or broker_cfg.get("broker_type", "paper") == "paper"
    except Exception:
        is_paper = DRY_RUN
    if not is_paper:
        return
    try:
        broker    = pb.get_broker(uid)
        positions = broker.get_positions()
        if not positions:
            return
        codes = [p["code"] for p in positions]
        qmap  = {}
        try:
            import ths_quote as tq
            client = tq.get_client()
            if client and client._ok:
                for q in client.get_quotes(codes):
                    if q.get("code"):
                        qmap[q["code"]] = q
        except Exception as _qe:
            logger.debug(f"[风控 uid={uid}] 实时行情获取失败: {_qe}")

        s = get_user_state(uid)
        if s.get("active_strategy") == "da_ban":
            _check_daban_ride(uid, broker, positions, qmap)   # 打板 Ride 卖法
            return
        # 其它策略：账户默认止损/止盈/超期
        cur_prices = {c: float(q.get("price") or 0) for c, q in qmap.items()}
        triggered = broker.check_risk_rules(cur_prices)
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
    _check_paper_risk(uid)        # 模拟盘持仓风控/Ride 自动卖出
    _check_live_daban_ride(uid)   # 实盘打板持仓 Ride 自动卖出（生成卖出信号）

    # ── 仓位规定跟随策略（写在策略 META 的 position 字段）────────────
    # 原硬编码"80%兜底+保留20%现金"已移除：它按金额累加待成交信号，实盘信号
    # 卡在已报状态时每轮扫描刷屏"已用仓位90%"且与策略口径冲突。
    # 现在：策略声明 max_daily（如打板=每日最多5个平均仓）→ 按当日开仓「数量」限额。
    _broker_cfg = um.get_user_broker_config(uid)
    _broker_type = _broker_cfg.get("broker_type", "paper")
    if _broker_type == "live" and not DRY_RUN:
        _live_cash = float((_broker_cfg.get("config") or {}).get("live_cash") or 0)
        _base_cash = _live_cash if _live_cash > 0 else s["capital"]
    else:
        _base_cash = s["capital"]

    _pos_cfg = sm.get_position_config(s.get("active_strategy") or "")
    _max_daily = int(_pos_cfg.get("max_daily") or 0)
    if _max_daily > 0:
        if _broker_type == "live" and not DRY_RUN:
            _opened_today = um.count_today_open_codes(uid)   # 已报未成交的也占名额
        else:
            _opened_today = int(s.get("today_trades") or 0)
        if _opened_today >= _max_daily:
            _today_str = datetime.now().strftime("%Y-%m-%d")
            if s.get("_daily_limit_logged") != _today_str:    # 只提示一次，不刷屏
                _add_log(uid, f"🛡 策略日内限额：今日已开 {_opened_today}/{_max_daily} 仓，"
                              f"按策略规定不再开新仓（明日自动重置）", "info")
                s["_daily_limit_logged"] = _today_str
            s["scan_progress"] = {"running": False, "stage": "今日仓位已满", "current": 0, "total": 0}
            return

    # ★ 加仓追板：仅当策略 META 显式声明 add_on=true 才启用。
    #   打板策略=每日5个平均仓位，不加仓 —— 原无条件加仓到25%与策略口径冲突，已停用。
    if _pos_cfg.get("add_on"):
        _check_add_position(uid, s, _broker_type, _base_cash)

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
        # max_codes=0 触发真·全市场扫描（不再裁剪 600 只）
        ret     = sm.run(active, [], max_codes=0)
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
    # 仓位基数：实盘走 live_cash，模拟走 s["capital"]（500000 虚拟资金）
    _broker_cfg = um.get_user_broker_config(uid)
    _broker_type = _broker_cfg.get("broker_type", "paper")
    if _broker_type == "live" and not DRY_RUN:
        _live_cash = float((_broker_cfg.get("config") or {}).get("live_cash") or 0)
        if _live_cash > 0:
            _base_cash = _live_cash
            _add_log(uid, f"💰 [自动扫描] 实盘资金基数 ¥{_base_cash:,.0f} × {s['position_ratio']*100:.0f}% 仓位", "info")
        else:
            _add_log(uid, "⚠️ 实盘模式但未配置 live_cash，本轮自动入队跳过。请在顶部切换实盘 Modal 输入 xiadan 真账户余额", "error")
            s["scan_progress"] = {"running": False, "stage": "等待配置", "current": 0, "total": 0}
            return
    else:
        _base_cash = s["capital"]

    # ★ 资金检查：可用资金只做"够不够下这一单"的闸门，不再作为仓位比例基数。
    # 旧逻辑 _base_cash = 可用资金 → 每仓 = 可用×比例，越买后面的仓越小
    # （实测5仓变成 45k/45k/10k/10k/9k，完全不是"平均仓位"）。
    # 现在：每仓 = 全仓总资金 × 比例，所有仓位等大；可用不足该仓金额时停止开仓。
    _pending_buy_total = um.get_pending_buy_total(uid)
    _available_cash = _base_cash - _pending_buy_total
    if _available_cash < 1000:  # 最小一手金额估算（不足则本轮不开新仓）
        _add_log(uid, f"⏭ 资金不足：总额¥{_base_cash:,.0f} - 待成交¥{_pending_buy_total:,.0f} = 可用¥{_available_cash:,.0f}，本轮跳过", "info")
        s["scan_progress"] = {"running": False, "stage": "资金不足", "current": 0, "total": 0}
        return

    # ── 仓位规则：策略 META 优先，账户面板设置兜底 ─────────────────────
    # 策略声明了 position 字段（如打板=每日5个平均仓）时以策略为准；
    # 未声明的策略沿用前端面板/config 默认值。
    try:
        from config import MAX_POSITIONS as _CFG_POS, SINGLE_POSITION_MAX_RATIO as _CFG_RATIO
    except Exception:
        _CFG_POS, _CFG_RATIO = 5, 0.20
    _q_pos_cfg = sm.get_position_config(s.get("active_strategy") or "")
    _q_max_daily = int(_q_pos_cfg.get("max_daily") or 0)
    _MAX_POS  = int(_q_pos_cfg.get("max_hold") or 0) or int(s.get("max_positions") or _CFG_POS)
    _SP_RATIO = float(s.get("single_position_max_ratio") or _CFG_RATIO)
    # 平均仓位：每仓 = 可用资金 / max_daily（策略规定的口径，优先于面板仓位比）
    _strat_ratio_equal = (1.0 / _q_max_daily) if (
        _q_max_daily > 0 and (_q_pos_cfg.get("sizing") or "equal") == "equal") else 0.0
    # 已占用的持仓名额 = 实际持仓 + 队列里待成交/已下单的买单（按 code 去重，避免双算）
    if _broker_type == "live":
        try:
            _occupied = {p["code"] for p in um.get_live_positions(uid)}
        except Exception:
            _occupied = set()
        # OCR 实际持仓（查实盘账户时缓存，当日有效）：实际已买入的票绝不再次买入，
        # 即使买入回传丢失/误报失败、台账里没有
        _ocr = s.get("live_ocr_pos") or {}
        if _ocr.get("date") == datetime.now().strftime("%Y-%m-%d"):
            _occupied |= {c for c in (_ocr.get("codes") or []) if c}
    else:
        _occupied = set((s.get("positions") or {}).keys())
    # 仅计 pending（待执行）买单：done 的买单已落到持仓/台账，由上面持仓侧统计，
    # 再从队列计会在"会话内买入又卖出"后被 done 残留记录永久错占名额。
    _occupied |= {q["code"] for q in s["queue"]
                  if q.get("action") == "buy" and q.get("status") == "pending"}
    # 单笔仓位：策略平均仓位优先（策略明确规定的口径不再被面板/护栏夹），
    # 策略未声明时用面板 position_ratio 并夹到护栏上限内
    if _strat_ratio_equal > 0:
        _eff_ratio = _strat_ratio_equal
    else:
        _eff_ratio = min(float(s["position_ratio"]), _SP_RATIO)
        if _eff_ratio < float(s["position_ratio"]):
            _add_log(uid,
                f"🛡 单笔护栏：仓位比 {s['position_ratio']*100:.0f}% 超过单笔上限 "
                f"{_SP_RATIO*100:.0f}%，本轮按 {_eff_ratio*100:.0f}% 建仓", "info")
    _guard_full_logged = False
    _avail_left = _available_cash    # 剩余可用资金（含扣除在途买单），逐仓递减

    for r in buy_candidates:
        code  = r["code"]
        price = r["price"] or 1
        if code in _occupied:
            continue   # 已持有 / 已在队列 → 不重复开仓
        # ★ 数据库去重：
        #   实盘 = 当日一码一买（不论信号最终状态）。GUI 偶发把实际已挂单误报 failed，
        #   只看未完成状态的旧去重会立即放行 → 已买的票反复挂单重复买入。
        #   模拟 = 仍按未完成信号去重（模拟即时成交，持仓本身就能挡重复）。
        if _broker_type == "live" and not DRY_RUN:
            if um.has_today_buy_signal(uid, code):
                continue
        elif um.has_pending_signal(uid, code, "BUY"):
            continue
        # 护栏①：最多持仓数 —— 名额满则停止开新仓（含待成交队列）
        if len(_occupied) >= _MAX_POS:
            if not _guard_full_logged:
                _add_log(uid,
                    f"🛡 持仓护栏：已占用 {len(_occupied)}/{_MAX_POS} 个名额，"
                    f"本轮剩余信号不再开新仓", "info")
                _guard_full_logged = True
            continue
        if not any(q["code"] == code for q in s["queue"]):
            # 单笔金额：信号自带分层仓位 > 策略平均仓位（原样执行）> 面板比例（夹护栏）
            # 决策逻辑见 core/order_sizing.pick_ratio（与原内联 if/elif/else 严格等价，有等价性测试）
            _strat_ratio = float(r.get("position_ratio", 0) or 0)
            _use_ratio = order_sizing.pick_ratio(
                float(s["position_ratio"]), _SP_RATIO,
                strat_ratio=_strat_ratio, strat_equal=_strat_ratio_equal)
            budget = _base_cash * _use_ratio    # 基数=全仓总资金，各仓等大
            shares = order_sizing.lot_shares(budget, price)   # 整手取整、最少1手（等价于原 max(int(budget/price/100)*100,100)）
            # 可用资金闸门：剩余可用不够本仓金额 → 停止开新仓（不缩仓，保持等大）
            if price * shares > _avail_left:
                if not _guard_full_logged:
                    _add_log(uid, f"⏭ 可用资金不足：剩余¥{_avail_left:,.0f} < 本仓¥{price*shares:,.0f}，"
                                  f"本轮不再开新仓", "info")
                    _guard_full_logged = True
                continue
            # ★ 实盘模式：单笔金额护栏（超 MAX_ORDER_AMOUNT 自动缩仓；连 1 手都超则跳过）★
            #   缩仓/拒单决策见 core/order_sizing.clamp_to_max_order —— 修复了原内联实现的 bug
            #   （单价×100 股已超上限时仍会强制 100 股照下单，靠 broker 层硬拒兜底）。
            if _broker_type == "live":
                try:
                    from config import MAX_ORDER_AMOUNT as _MAX_ORD
                except Exception:
                    _MAX_ORD = 50000
                _est = price * shares
                _safe_shares, _clamped = order_sizing.clamp_to_max_order(shares, price, _MAX_ORD)
                if _clamped:
                    if _safe_shares == 0:
                        _add_log(uid, f"⏭ {code} 单股 @¥{price:.2f} × 100股 = ¥{price*100:.0f} 已超 ¥{_MAX_ORD} 护栏，跳过", "error")
                        continue
                    _add_log(uid, f"⚠️ {code} 预估 ¥{_est:,.0f} > 护栏 ¥{_MAX_ORD}，自动缩仓 {shares}→{_safe_shares} 股", "info")
                    shares = _safe_shares
                    budget = price * shares
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
            _occupied.add(code)   # 占用一个名额，后续信号据此判断是否已满
            _avail_left -= price * shares   # 扣减剩余可用，防止本轮超买
            _add_log(uid,
                f"📌 {code} {r['name']} 进入队列 | 计划买入{shares}股 预算¥{budget:,.0f} "
                f"| 名额 {len(_occupied)}/{_MAX_POS}", "buy")
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
        # 两阶段进度合并显示：
        #   阶段 A: ths_quote 全市场批量拉行情（universe 构建，30秒）
        #   阶段 B: strategy_manager 策略扫描（逐只 get_kline，2-25 分钟）
        # 哪个 running 就显示哪个，避免阶段 A 结束后卡在 100% 的体验问题
        import ths_quote as tq
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

# ── 实盘下单总闸（前端可一键开关；关闭时不给桥接器任何信号 = 彻底停下单）──
_BRIDGE_FLAGS_FILE = BASE_DIR / "data" / "bridge_flags.json"
_live_trade_enabled = {}   # uid -> bool（缺省视为 True=开）


def _load_bridge_flags():
    global _live_trade_enabled
    try:
        if _BRIDGE_FLAGS_FILE.exists():
            _live_trade_enabled = {int(k): bool(v) for k, v in
                                   json.loads(_BRIDGE_FLAGS_FILE.read_text(encoding="utf-8")).items()}
    except Exception as e:
        logger.warning(f"[bridge闸门] 加载失败: {e}")


def _save_bridge_flags():
    try:
        _BRIDGE_FLAGS_FILE.write_text(
            json.dumps({str(k): v for k, v in _live_trade_enabled.items()}), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[bridge闸门] 保存失败: {e}")


def _live_enabled(uid) -> bool:
    return _live_trade_enabled.get(uid, True)


_load_bridge_flags()


@app.route("/api/bridge/signals", methods=["GET"])
@require_auth
def bridge_get_signals():
    uid = session["user_id"]
    # ★ 实盘下单总闸：关闭时不给桥接器任何信号（桥接器照常轮询但拿不到单 → 不下单）
    if not _live_enabled(uid):
        return jsonify({"ok": True, "signals": [], "live_disabled": True})
    signals = um.get_pending_signals(uid)
    return jsonify({"ok": True, "signals": signals})


@app.route("/api/bridge/live_enabled", methods=["GET"])
@require_auth
def get_live_enabled():
    return jsonify({"ok": True, "enabled": _live_enabled(session["user_id"])})


@app.route("/api/bridge/live_enabled", methods=["POST"])
@require_auth
def set_live_enabled():
    uid = session["user_id"]
    en  = bool((request.json or {}).get("enabled", True))
    _live_trade_enabled[uid] = en
    _save_bridge_flags()
    _add_log(uid, ("✅ 已开启实盘下单（桥接器恢复执行信号）" if en
                   else "🛑 已关闭实盘下单（桥接器不再收到任何信号，不会下单到同花顺）"), "WARN")
    return jsonify({"ok": True, "enabled": en})


@app.route("/api/bridge/today_orders", methods=["GET"])
@require_auth
def bridge_today_orders():
    """读 xiadan 当日委托表，返回每笔精确状态（已成/已报/部成/已撤/废单）

    Query 参数:
      code     - 可选, 只查该 code 的委托
      limit    - 可选, 最多读多少行（默认 50）
      navigate - 可选, 1/true 时 F4 后自校准点击左侧「当日委托」再截图（拿真实成交
                 状态）；默认不导航=只截 F4 默认的资金股票页（不向实盘发送鼠标点击）

    返回:
      {ok, screenshot, abs_path, orders:[{code,name,action,status,numbers,raw}],
       funds, positions, tabs:[...], ocr_lines:[...], msg}

    实现：xiadan 委托表是自绘控件读不到，桥接器按 F4 切到查询页 → 截图 →
    本地 Windows OCR(winocr) 逐行识别 → 解析结构化 → 按 F1 切回买入页（约 2-3 秒）。
    未装 winocr 时 orders 为空，仅返回截图。
    """
    if platform.system() != "Windows":
        return jsonify({"ok": False, "msg": "服务端非 Windows，无法读 xiadan"})
    if not operator:
        return jsonify({"ok": False, "msg": "xiadan 操作员未初始化（DRY_RUN 或 Linux）"})

    code = (request.args.get("code") or "").strip() or None
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    # navigate=1：F4 后自校准点击左侧「当日委托」再截图（拿真实成交状态）。
    # 默认 0=只截 F4 默认的资金股票页（安全，不向实盘客户端发送鼠标点击）。
    navigate = (request.args.get("navigate") or "").strip().lower() in ("1", "true", "yes", "on")

    try:
        r = operator.query_today_orders(code_filter=code, max_rows=limit, navigate=navigate)
        # code 可靠、OCR 名称易错或缺失 → 用本地名称库按 code 纠正/补全（持仓同样处理）
        for lst in (r.get("orders") or [], r.get("positions") or []):
            for it in lst:
                c = (it.get("code") or "").strip()
                if c:
                    nm = dm.get_stock_name(c)
                    if nm and nm != c:
                        it["name"] = nm
        # ★ 缓存 OCR 实际持仓（当日有效）：自动扫描把这些票算"已持有"不再买入。
        #   兜住"实际已买入但回传丢失/误报失败"的票——只要查过一次实盘账户就拦得住。
        try:
            if r.get("ok") and r.get("positions"):
                _s = get_user_state(session["user_id"])
                _s["live_ocr_pos"] = {
                    "date":  datetime.now().strftime("%Y-%m-%d"),
                    "codes": [p.get("code") for p in r["positions"] if p.get("code")],
                }
        except Exception:
            pass
        return jsonify(r)
    except Exception as e:
        logger.exception(f"读当日委托失败: {e}")
        return jsonify({"ok": False, "msg": f"调用异常: {e}"})


@app.route("/api/bridge/signals/<int:signal_id>/status", methods=["GET"])
@require_auth
def bridge_signal_status(signal_id):
    """前端轮询单条信号当前状态（pending/sent/executed/failed/cancelled）"""
    uid = session["user_id"]
    sig = um.get_trade_signal(signal_id)
    if not sig:
        return jsonify({"ok": False, "error": "信号不存在"})
    # 仅允许查自己的信号
    if sig.get("user_id") != uid:
        return jsonify({"ok": False, "error": "无权查看此信号"}), 403
    return jsonify({
        "ok": True,
        "signal": {
            "id":          sig["id"],
            "code":        sig["code"],
            "name":        sig["name"],
            "action":      sig["action"],
            "price":       sig["price"],
            "shares":      sig["shares"],
            "status":      sig["status"],     # pending/sent/executed/failed/cancelled
            "report_msg":  sig.get("msg") or "",
            "exec_price":  sig.get("exec_price") or sig["price"],
            "created_at":  sig.get("created_at"),
            "updated_at":  sig.get("updated_at"),
        }
    })


@app.route("/api/bridge/signals/<int:signal_id>/report", methods=["POST"])
@require_auth
def bridge_report_signal(signal_id):
    uid = session["user_id"]
    data = request.json or {}
    status = data.get("status")
    msg = data.get("msg", "")
    
    # 校验并更新状态
    res = um.report_trade_signal(signal_id, status, msg)
    if not res.get("ok"):
        return jsonify(res)
        
    action_label = "买入" if res["action"] == "BUY" else "卖出"
    # 三态显示：executed=✅ 成功 / submitted=⏳ 已提交未确认 / 其他=❌ 失败
    if status == "executed":
        sign = "✅"; log_level = "buy"
    elif status == "submitted":
        sign = "⏳"; log_level = "info"   # 中间态，不当失败标红
    else:
        sign = "❌"; log_level = "error"
    log_msg = f"{action_label} {res['code']} {res['name'] or ''} {res['shares']}股 @{res['price']:.2f} — 执行完毕: {msg}"
    _add_log(uid, f"{sign} [实盘回传] {log_msg}", log_level)
    
    # 同步更新云端的仓位视图缓存
    s = get_user_state(uid)
    code = res["code"]
    if status == "executed":
        if res["action"] == "BUY":
            s["executed"] += 1
            s["today_trades"] = s.get("today_trades", 0) + 1
            s["positions"][code] = {
                "name": res["name"],
                "shares": res["shares"],
                "cost": res["price"],
                "date": datetime.now().strftime("%Y-%m-%d"),
            }
            # ── 实盘持仓台账（打板 Ride 自动卖出的数据源）──
            # 买入成交确认 → 记台账（带当前策略，da_ban 才会被 Ride 自动管理卖出）。
            # OCR 读不到成本价，但这里是我们自己下的单，价/量/日精确。
            try:
                stg = s.get("active_strategy") or ""
                um.record_live_position(uid, code, res["name"] or "",
                                        float(res["price"]), int(res["shares"]), strategy=stg)
            except Exception as _e:
                logger.warning(f"[live台账 uid={uid}] 记录买入失败（不影响下单）: {_e}")
            # 同步执行队列状态：找到这条队列项，标记 executed（含旧版误标 done 的）
            for q in s.get("queue", []):
                if q.get("code") == code and q.get("status") in ("pending", "submitted", "sent", "done"):
                    q["status"] = "executed"
                    break
        elif res["action"] == "SELL":
            s["executed"] += 1
            s["positions"].pop(code, None)
            # 卖出成交 → 移除台账 + 清峰值 + 解锁卖出去重
            try:
                um.remove_live_position(uid, code)
            except Exception as _e:
                logger.warning(f"[live台账 uid={uid}] 移除持仓失败: {_e}")
            _DABAN_PEAKS.pop((uid, code), None)
            _LIVE_SELL_PENDING.discard((uid, code))
    elif status == "submitted":
        # 已报未成交：同步队列为 submitted，前端如实显示"已报实盘"而非"已买"
        for q in s.get("queue", []):
            if q.get("code") == code and q.get("status") in ("pending", "sent", "done"):
                q["status"] = "submitted"
                break
    elif status == "failed":
        # 卖出失败：解锁去重，下轮可重发；若失败原因是"无持仓"说明买单其实没成交=幽灵→清台账
        if res["action"] == "SELL":
            _LIVE_SELL_PENDING.discard((uid, code))
            if any(k in (msg or "") for k in ("无持仓", "持仓不足", "可用不足", "可用为0", "数量为0")):
                try:
                    um.remove_live_position(uid, code)
                except Exception:
                    pass
                _DABAN_PEAKS.pop((uid, code), None)
        # ★ 桥接器执行失败：把执行队列里的"已买"假象清掉 ★
        # 之前 _auto_buy 把队列项标成 sent/submitted，前端显示"已入队/已买"
        # 现在桥接器明确失败 → 重置回 pending 状态，让用户能看清楚没买进去
        for q in s.get("queue", []):
            if q.get("code") == code and q.get("status") in ("pending", "submitted", "sent"):
                q["status"] = "failed"
                q["fail_msg"] = (msg or "桥接器执行失败")[:80]
                break
    return jsonify({"ok": True})


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
            res = execute_buy(uid, code, name, price, int(shares), reason="手动买入")
            return jsonify(res)
        return jsonify({"ok": False, "msg": "未找到"})

    if action == "skip":
        item["status"] = "skip"
    elif action == "buy":
        price  = item["price"]
        name   = item.get("name", code)
        shares = item.get("shares", 100)
        res = execute_buy(uid, code, name, price, shares, reason="队列买入")
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
        # 优先拉 ths_quote 实时价（避免前端 cur_price fallback 到 cost_price 导致
        # 卖出价≈买入价、盈亏只剩费用的假象）
        price = 0.0
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
            price = float(d.get("price", 0)) or float(bp["cur_price"] or bp["cost_price"])
    else:
        pos = s["positions"].get(code)
        if not pos:
            return jsonify({"ok": False, "msg": "持仓不存在"})
        price = float(d.get("price", pos.get("cost", 0)))
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
        import ths_quote as tq
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
    if price <= 0:
        return jsonify({"ok": False, "msg": "价格必须大于0"})
    # 非交易时段提示但仍允许（方便用户测试 + 复盘）
    # 严格模式：取消注释下面 4 行让非交易时段拒单
    # from market_data import is_trading_time
    # if not is_trading_time():
    #     return jsonify({"ok": False, "msg": "非交易时段，无法下单（仅交易日 9:30-11:30 / 13:00-15:00）"})
    try:
        from market_data import is_trading_time
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
        import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            qs = client.get_quotes([code])
            if qs:
                price = float(qs[0].get("price", 0))
    except Exception:
        pass
    # 实时价拉不到时，再用前端传的 price 作 fallback
    if not price:
        price = float(d.get("price", 0))
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
def api_get(filename):
    meta = sm.get_meta(filename)
    # ★ 策略隐私：内置策略（自研）的源码仅 admin 可见；其他云端用户拿到"受保护"占位，
    #   防止被查看/复制复用。策略仍可正常激活运行（运行/回测走 sm 内部真实代码，不经此路由）。
    if meta.get("builtin") and not session.get("is_admin"):
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


@app.route("/api/strategies/run_async", methods=["POST"])
@require_auth
def api_run_async():
    """异步选股：立即返回 task_id，前端轮询 /strategies/progress 和 /strategies/result。"""
    import uuid as _uuid
    err = _deduct("scan", "全市场选股扫描")
    if err:
        return jsonify(err), 402
    uid = session["user_id"]
    d   = request.json or {}
    fn  = d.get("filename", "")
    cs  = d.get("codes") or []
    try:
        mc = int(d.get("max_codes", 0))
    except (TypeError, ValueError):
        mc = 0
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
        prog = getattr(sm, "_scan_prog", None) or getattr(sm, "_tdx_prog", {})
        if isinstance(prog, dict) and prog.get("running"):
            snap["scan_current"] = int(prog.get("current") or 0)
            snap["scan_total"]   = int(prog.get("total") or 0)
            snap["scan_code"]    = prog.get("code") or ""
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
        import strategy_manager as _sm_mod
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


# 后台调度器：每 30 秒检查一次所有 enabled 激活，达到 interval_sec 间隔且交易时段就跑
# 默认 120 秒（2 分钟），用户可在激活配置里通过 interval_sec 字段自定义
_VIBE_INTERVAL_SEC = 120   # 默认 2 分钟（旧值 5 分钟太长，错过短线机会）
_VIBE_INTERVAL_MIN = 30    # 最快 30 秒一次（防接口压力）
_VIBE_INTERVAL_MAX = 600   # 最慢 10 分钟一次
_vibe_last_run: dict = {}  # uid -> ts


def _vibe_get_paper_cash(uid: int) -> float:
    """给 activator 用的 paper 可用资金回调"""
    try:
        broker = pb.get_broker(uid)
        return float(broker.cash) if broker else 0.0
    except Exception:
        return 0.0


def _vibe_compute_daily_pnl(uid: int) -> float | None:
    """计算用户当日浮动盈亏比例（持仓 mtm 比初始资金）
    返回 None 表示无法计算
    """
    try:
        broker = pb.get_broker(uid)
        if not broker:
            return None
        positions = broker.get_positions()
        if not positions:
            return 0.0
        total_pnl = 0.0
        for code, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            cost = float(pos.get("cost") or 0)
            shares = int(pos.get("shares") or 0)
            cur = float(pos.get("cur_price") or cost)
            total_pnl += (cur - cost) * shares
        # 用 paper_config.cash + 持仓市值 还原总资产 →  pnl / 总资产
        total_value = broker.cash + sum(
            float(p.get("cur_price") or p.get("cost") or 0) * int(p.get("shares") or 0)
            for p in positions.values() if isinstance(p, dict)
        )
        if total_value <= 0:
            return None
        return total_pnl / total_value
    except Exception as e:
        logger.debug(f"_vibe_compute_daily_pnl uid={uid} 失败: {e}")
        return None


def run_vibe_scheduler():
    """独立线程：每 30s 检查 vibe_activations enabled，达自定义间隔 + 交易时段则跑"""
    logger.info(f"[vibe.scheduler] 启动 默认间隔 {_VIBE_INTERVAL_SEC}s")
    while True:
        try:
            time.sleep(30)   # 检查频率（不是扫描频率）
            if not is_trading_time():
                continue
            from analytics.factors_zoo import activator as _act
            actives = _act.list_all_enabled()
            if not actives:
                continue
            now_ts = time.time()
            for cfg in actives:
                uid = cfg["user_id"]
                # 更新当日盈亏（每分钟一次，触发熔断）
                pnl = _vibe_compute_daily_pnl(uid)
                if pnl is not None:
                    _act.update_daily_pnl(uid, pnl)

                # 取每个激活配置自己的 interval_sec（用户可在激活时自定义）
                try:
                    _user_interval = int((cfg or {}).get("interval_sec") or _VIBE_INTERVAL_SEC)
                except (TypeError, ValueError):
                    _user_interval = _VIBE_INTERVAL_SEC
                _user_interval = max(_VIBE_INTERVAL_MIN, min(_VIBE_INTERVAL_MAX, _user_interval))

                # 满间隔才扫一次
                last = _vibe_last_run.get(uid, 0)
                if now_ts - last < _user_interval:
                    continue
                _vibe_last_run[uid] = now_ts
                try:
                    result = _act.run_tick(uid, execute_buy, lambda: _vibe_get_paper_cash(uid))
                    placed = result.get("placed", [])
                    if placed:
                        _add_log(uid, f"📚 Vibe调度: 开仓 {len(placed)} 只 (今日 {result.get('daily_count')}/{result.get('daily_limit')})")
                    elif result.get("phase") not in ("done", "skip_disabled"):
                        logger.info(f"[vibe.scheduler] uid={uid} phase={result.get('phase')}")
                except Exception as e:
                    logger.exception(f"[vibe.scheduler] uid={uid} run_tick 抛错")
        except Exception:
            logger.exception("[vibe.scheduler] 循环抛错")
            time.sleep(60)


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
    except Exception as _e:
        logger.exception(f"[uid={uid}] do_scan 异常崩溃: {_e}")
        try:
            _s = get_user_state(uid)
            _add_log(uid, f"❌ 扫描异常崩溃: {_e}", "error")
        except Exception:
            pass
    finally:
        _scan_running[uid] = False
        # ★ 强制清进度条假状态（防止异常崩溃后前端永远显示"扫描中"）★
        try:
            _s = get_user_state(uid)
            _s["scan_progress"] = {"running": False, "stage": "已结束", "current": 0, "total": 0}
        except Exception:
            pass


def _snap_all_paper_equity():
    """收盘后为所有模拟盘用户拍一张净值快照（实时收盘价）。

    没有这一步，净值曲线只在'有成交的那天'才落点，不交易的日子全是空洞；
    且盘中快照可能被成本价覆盖。收盘统一补一张含浮盈的快照。"""
    try:
        import sqlite3 as _sq
        con = _sq.connect(str(pb.DB_PATH), timeout=10)
        all_uids = [r[0] for r in con.execute(
            "SELECT DISTINCT user_id FROM paper_positions")]
        con.close()
    except Exception as e:
        logger.warning(f"[收盘快照] 读取模拟盘用户失败: {e}")
        return
    for uid in all_uids:
        try:
            pb.get_broker(uid)._snap_equity()   # 不传价 → 内部自动拉实时行情
        except Exception as e:
            logger.debug(f"[收盘快照 uid={uid}] {e}")
    if all_uids:
        logger.info(f"[收盘快照] 已为 {len(all_uids)} 个模拟盘用户落净值点")


def _on_market_close():
    """收盘(15:00)自动终止所有用户的自动交易：停 running + 清残留未执行信号 + 持久化。
    避免收盘后桥接器还在拉 pending 信号去下单。仅在下午真收盘触发（午休 11:30 不触发）。"""
    _snap_all_paper_equity()    # 先落净值快照（与是否有用户在自动交易无关）
    with _user_state_lock:
        uids = [uid for uid, s in user_states.items() if s.get("running")]
    if not uids:
        return
    for uid in uids:
        try:
            s = get_user_state(uid)
            s["running"] = False
            # 清掉本地队列里 pending 的买单（收盘后不再执行）
            try:
                s["queue"] = [q for q in s.get("queue", []) if q.get("status") != "pending"]
            except Exception:
                pass
            _persist_state(uid, running=0)
            # 取消实盘 pending 交易信号，避免桥接器收盘后继续拉去下单
            try:
                n = um.cancel_pending_signals(uid)
            except Exception:
                n = 0
            _add_log(uid, f"🔔 收盘(15:00)自动终止：已停止自动交易"
                          + (f"，清理未执行实盘信号 {n} 条" if n else ""), "WARN")
        except Exception as e:
            logger.warning(f"[收盘终止 uid={uid}] {e}")
    logger.info(f"[收盘] 已自动终止 {len(uids)} 个用户的自动交易")


def run_scheduler():
    """
    主调度循环：
    - 每秒检查所有 running 用户
    - 普通策略：每 SCAN_INTERVAL 秒触发一次
    - 打板策略：每 DABAN_INTERVAL 秒触发一次
    - 收盘(15:00)自动终止所有自动交易（午休 11:30 不触发）
    """
    now_ts = time.time()
    _was_trading = is_trading_time()        # 收盘下降沿检测
    while True:
        now_ts = time.time()
        # ★ 收盘自动终止：交易时段→非交易时段 的下降沿，且已到下午收盘(>=15:00)
        _now_trading = is_trading_time()
        if _was_trading and not _now_trading:
            _tc = datetime.now()
            if _tc.hour * 100 + _tc.minute >= 1500:
                try:
                    _on_market_close()
                except Exception as _ce:
                    logger.warning(f"[收盘终止] 调度异常: {_ce}")
        _was_trading = _now_trading

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
# 后台服务启动
# 本地 `python main.py` 与云端 `gunicorn main:app` 都会在模块导入时执行一次。
# 之前仅写在 __main__ 内，导致 gunicorn 下调度器从不启动（自动扫描失效）。
# ══════════════════════════════════════════════════════════════
_bg_lock = threading.Lock()
_bg_started = False


def _start_background_services():
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True
    _restore_all_user_states()
    threading.Thread(target=run_scheduler, daemon=True).start()
    # Vibe Alpha 因子激活调度器（独立线程，每 60s 检查，5 分钟跑一次扫描）
    threading.Thread(target=run_vibe_scheduler, daemon=True, name="vibe-scheduler").start()
    # 财经新闻聚合（盘中每3分钟拉快讯，19:40 抓新闻联播）
    try:
        import news_center
        threading.Thread(target=news_center.run_news_scheduler, daemon=True, name="news-center").start()
    except Exception as _ne:
        logger.warning(f"新闻聚合调度器启动失败: {_ne}")
    # 每日复盘（交易日 15:10 收盘版 → 19:50 晚间版，含 AI 总结归档）
    try:
        import review_engine
        threading.Thread(target=review_engine.run_review_scheduler, daemon=True, name="review-engine").start()
    except Exception as _re:
        logger.warning(f"复盘调度器启动失败: {_re}")
    # 产业概念日更（08:30/15:10 自动刷新并过滤交易信号类，保证每日新概念可见）
    try:
        import research_center
        research_center.run_concept_scheduler()
    except Exception as _rc:
        logger.warning(f"概念日更调度器启动失败: {_rc}")
    # 启动时全量清理隔日遗留信号（兜底：内存态 today_date 初始化即今天时，
    # _reset_daily_if_needed 不触发，僵尸信号会继续累进资金检查）
    try:
        _n_expired = um.expire_stale_signals()
        if _n_expired:
            logger.info(f"启动清理：{_n_expired} 条隔日遗留交易信号已置为过期")
    except Exception as _ex:
        logger.warning(f"启动清理隔日信号失败: {_ex}")
    logger.info(f"后台调度已启动 DRY_RUN={'开启' if DRY_RUN else '关闭'}")


# 模块级启动：gunicorn 导入 main:app 时不进入 __main__，必须在此启动。
# 设 THS_NO_SCHEDULER=1 或 QUANT_TEST_IMPORT=1 可禁用（供测试/纯导入场景）。
if _os.environ.get("THS_NO_SCHEDULER") != "1" and not _TEST_IMPORT:
    _start_background_services()


if __name__ == "__main__":
    app.run(port=5001, debug=False)
