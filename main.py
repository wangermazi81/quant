from __future__ import annotations

import os as _os
# ── 清除系统代理（akshare 走国内接口，代理反而导致连接失败）──────────
for _k in ("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy",
           "ALL_PROXY","all_proxy","SOCKS_PROXY","socks_proxy"):
    _os.environ.pop(_k, None)

from flask import Flask
from flask_cors import CORS
import logging, secrets
from broker.ths_operator import THSOperator
from config import DRY_RUN
try:
    from config import DABAN_STOP_LOSS, DABAN_TRAIL, DABAN_MAX_HOLD, DABAN_HOLD_LIMITUP
except ImportError:
    DABAN_STOP_LOSS, DABAN_TRAIL, DABAN_MAX_HOLD, DABAN_HOLD_LIMITUP = -0.05, 0.07, 10, True
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
from web.strategies import bp as _strategies_bp
app.register_blueprint(_strategies_bp)
from web.factors import bp as _factors_bp
app.register_blueprint(_factors_bp)
from web.misc import bp as _misc_bp
app.register_blueprint(_misc_bp)


# ══════════════════════════════════════════════════════════════
# 全部业务路由已迁出（工单4 蓝图化完成）：
#   策略 /api/strategies/*        → web/strategies.py（10 条）
#   因子 /api/factors* + factors_zoo → web/factors.py（14 条）
#   健康/vibe激活/回测/组合优化    → web/misc.py（8 条）
#   其余见 web/ 各蓝图；main 仅保留 app 组装与运行时初始化。
# ══════════════════════════════════════════════════════════════

# ── 核心交易/扫描函数的兼容绑定 ─────────────────────────────────
# 真身在 core/trade_service.py 与 strategy_engine/scan_service.py；
# 保留 main.execute_buy / main.do_scan 等绑定供集成测试与外部脚本引用。
from core.trade_service import (  # noqa: E402,F401
    _reset_daily_if_needed, execute_buy, execute_sell,
    _check_add_position, _auto_buy, _check_live_daban_ride,
    _check_daban_ride, _check_paper_risk,
)
from strategy_engine.scan_service import do_scan, _scan_thread_worker  # noqa: E402,F401


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
