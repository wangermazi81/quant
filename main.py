from __future__ import annotations

import os as _os
# ── 清除系统代理（akshare 走国内接口，代理反而导致连接失败）──────────
for _k in ("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy",
           "ALL_PROXY","all_proxy","SOCKS_PROXY","socks_proxy"):
    _os.environ.pop(_k, None)

from flask import Flask, jsonify, request, session
from flask_cors import CORS
import threading, logging, secrets, json
from datetime import datetime
from broker.ths_operator import THSOperator
from config import DRY_RUN
try:
    from config import DABAN_STOP_LOSS, DABAN_TRAIL, DABAN_MAX_HOLD, DABAN_HOLD_LIMITUP
except ImportError:
    DABAN_STOP_LOSS, DABAN_TRAIL, DABAN_MAX_HOLD, DABAN_HOLD_LIMITUP = -0.05, 0.07, 10, True
from strategy_engine import strategy_manager as sm
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
from web.trade import bp as _trade_bp
app.register_blueprint(_trade_bp)
from web.paper import bp as _paper_bp
app.register_blueprint(_paper_bp)


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
# 已迁出的路由（工单4）：
#   监控/交易/账户/持仓/seal_tracker → web/trade.py（16 条）
#   模拟盘 /api/paper/*             → web/paper.py（12 条）
#   公告 /api/announcements*        → web/user.py（B2 修复：补 require_auth，
#                                     删除本文件与 user.py 的双重定义）
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# 策略 API
# ══════════════════════════════════════════════════════════════
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
