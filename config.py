# -*- coding: utf-8 -*-
"""集中配置。

⚠️ 危险交易开关（实盘/自动确认/自动撤单）不再写死在代码里：
默认全部为安全值（模拟盘、不自动确认），只有通过环境变量或本目录 .env
显式设置 QUANT_LIVE_TRADING=1 才进入实盘。变量清单见 .env.example。
"""
from __future__ import annotations
import os as _os
from pathlib import Path as _Path


def _load_dotenv() -> None:
    """轻量 .env 解析：KEY=VALUE、# 注释；不覆盖已存在的环境变量。"""
    p = _Path(__file__).resolve().parent / ".env"
    if not p.exists():
        return
    try:
        for _line in p.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            k, v = _line.split("=", 1)
            _os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass  # .env 读取失败不阻断启动 → 落到安全默认（模拟盘）


_load_dotenv()


# ── 环境变量访问器（A-12）──────────────────────────────────────
# 为什么要有这一层：`.env` 只在 **import config** 时被加载。任何新入口
# （独立脚本、调度进程、工具）若直接读 os.environ 而没有先 import config，
# .env 里的开关就静默读不到，落到各调用点自己的默认值 —— 对
# QUANT_LIVE_TRADING 这类资金开关来说，这是「以为关着其实开着」或反之。
# 业务模块一律用这些访问器，别自己碰 os.environ。
_TRUTHY = ("1", "true", "yes", "on")


def env_bool(name: str, default: bool = False) -> bool:
    v = _os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in _TRUTHY


def env_str(name: str, default: str = "") -> str:
    v = _os.environ.get(name)
    return default if v is None else str(v).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(str(_os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(str(_os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def _resolve_trading_flags(env) -> tuple[bool, bool, bool]:
    """从环境解析 (实盘, 自动确认委托, 自动撤未成交)。

    双闸设计：自动确认/自动撤单仅在实盘开关同时打开时才可能为 True，
    防止单个变量误设就产生真实资金动作。纯函数，便于单测。
    """
    def _b(name: str) -> bool:
        v = env.get(name)
        return v is not None and str(v).strip().lower() in ("1", "true", "yes", "on")

    live = _b("QUANT_LIVE_TRADING")
    return (live,
            live and _b("QUANT_AUTO_CONFIRM_ORDER"),
            live and _b("QUANT_AUTO_CANCEL_UNFILLED"))


# 策略参数配置
# STOCK_POOL 为空列表表示全市场扫描（两阶段预过滤）
STOCK_POOL = []

# 全市场两阶段预过滤参数
MARKET_FILTER = {
    "min_change":    2.0,    # 最低涨幅%（第一阶段过滤）
    "max_change":    9.8,    # 最高涨幅%（排除涨停封板）
    "min_volume":    5000,   # 最小成交量（手）
    "min_price":     3.0,    # 最低股价（排除仙股）
    "max_price":     200.0,  # 最高股价
    "exclude_st":    True,   # 排除ST/退市股
    "exclude_bj":    True,   # 排除北交所
    "max_candidates": 300,   # 第二阶段最多处理只数
}

STRATEGY = {
    "ma_short": 5,
    "ma_long": 20,
    "vol_multiplier": 1.5,   # 成交量放大倍数
    "min_change": 2.0,        # 最小涨幅%
    "max_change": 7.0,        # 最大涨幅%
    "min_conditions": 3,      # 最少满足条件数
}

RISK = {
    "max_position": 0.2,      # 单票最大仓位20%
    "stop_loss": -0.05,       # 止损-5%
    "daily_max_loss": -0.03,  # 单日最大亏损3%
    "max_trades": 5,          # 单日最多交易次数
}

# ── 账户与仓位配置 ─────────────────────────────
INITIAL_CAPITAL  = 1_000_000  # 初始资金（元），可在界面修改
POSITION_RATIO   = 0.20       # 单次建仓比例 20%
# MAX_DAILY_TRADES 已移除 —— 由资金管理自然控制开仓数量

SCAN_INTERVAL = 30            # 扫描间隔秒

# ── 交易模式（安全默认：模拟盘）────────────────────────────────
# 实盘必须显式设 QUANT_LIVE_TRADING=1（环境变量或 .env），否则一律模拟盘。
LIVE_TRADING, _AUTO_CONFIRM_ENV, _AUTO_CANCEL_ENV = _resolve_trading_flags(_os.environ)
DRY_RUN = not LIVE_TRADING    # True=模拟盘/paper trading，False=实盘（让桥接器真按键操作 xiadan）
# 用户已校准 xiadan 快捷键 F1=买 F2=卖；ths_operator 已修复 2 位小数 bug

# ── 交易客户端 GUI 自动化配置（仅 DRY_RUN=False 时生效）──────────
# 不同券商客户端的快捷键和窗口标题不一样，必须先校准：
#   场景 A — 同花顺独立委托 (各家券商发的"同花顺"分支):
#     THS_WINDOW_TITLE = "同花顺"  THS_BUY_KEY="F2"  THS_SELL_KEY="F3"
#   场景 B — 通用"网上股票交易系统5.0" (xiadan.exe，金证/恒生底层):
#     THS_WINDOW_TITLE = "网上股票交易系统"  THS_BUY_KEY="F1"  THS_SELL_KEY="F2"
#   pywinauto backend:
#     "uia"   = 标准现代 UI 自动化，多数 64 位新软件 OK
#     "win32" = Win32 经典 API，32 位老 GUI（xiadan、同花顺独立委托）更稳
THS_WINDOW_TITLE = "网上股票交易系统"  # xiadan.exe 主窗口标题含此字串
THS_BUY_KEY      = "f1"               # xiadan 标准：F1=买入
THS_SELL_KEY     = "f2"               # xiadan 标准：F2=卖出
THS_BACKEND      = "win32"            # xiadan 是 32 位老程序，win32 比 uia 稳

# ── 全自动下单（含委托确认）配置 ──
# ⚠️ AUTO_CONFIRM_ORDER=True 后桥接器会自动按 Y 确认委托对话框 → 真成交！
# 启用前务必确认 (1) 账户余额、(2) MAX_ORDER_AMOUNT 足够低、(3) 已校准策略不会出错单
# 默认 False；需 QUANT_LIVE_TRADING=1 且 QUANT_AUTO_CONFIRM_ORDER=1 双开关才为 True。
AUTO_CONFIRM_ORDER = _AUTO_CONFIRM_ENV
AUTO_CONFIRM_DELAY = 0.8              # Enter 后等委托确认框弹出的秒数
try:
    MAX_ORDER_AMOUNT = float(_os.environ.get("QUANT_MAX_ORDER_AMOUNT", "") or 50000.0)
except ValueError:
    MAX_ORDER_AMOUNT = 50000.0        # 单笔买入金额上限（元）, broker 层硬拒过大订单

# ── 仓位护栏（防一次大面腰斩）───────────────────────────────────
# 打板真实形态是"多数小亏、少数连板大赚"，最差单笔可达 -20%+（次日一字跌停跑不掉）。
# 尾部风险无法靠选股消除，只能靠仓位控制。自动扫描入队/下单时硬性约束：
#   ① 单笔上限：任一笔买入金额 ≤ 总资金 × SINGLE_POSITION_MAX_RATIO（即使用户把仓位比
#      调更高也会被夹到这个上限）。单股 -50% 腰斩 → 账户最多回撤 SINGLE_POSITION_MAX_RATIO×50%。
#   ② 最多持仓数：同时最多持有 MAX_POSITIONS 只（含队列里待成交的买单），名额满则不再开新仓。
# 例：单笔≤20% + 最多5只 → 满仓也只占 100%，单只腰斩对账户冲击 ≤10%。
MAX_POSITIONS             = 5         # 同时最多持仓只数（含待成交队列），名额满则停止开新仓
SINGLE_POSITION_MAX_RATIO = 0.20      # 单笔买入占总资金比例硬上限（夹住 position_ratio）

# ── 未成交自动撤单（订单生命周期）配置 ──
# ⚠️ AUTO_CANCEL_UNFILLED=True 后：每笔买/卖下单成功 N 秒后，若仍未成交则自动撤单
#    并刷新资金。撤单走撤单页(F3)，仅当该笔是「全场唯一可撤单」时用全撤(Z)，多笔则
#    为安全跳过(记日志)，绝不误撤。默认 False（需自行确认策略+账户后再开）。
# 默认 False；需 QUANT_LIVE_TRADING=1 且 QUANT_AUTO_CANCEL_UNFILLED=1 双开关才为 True。
AUTO_CANCEL_UNFILLED = _AUTO_CANCEL_ENV
AUTO_CANCEL_DELAY    = 15.0           # 下单成功后等多少秒再查、未成交则撤（秒）

# ── 打板策略卖出规则（回测 Ride：让利润奔跑，正确的非对称盈亏比）──
# 核心：吃连板（当日仍封板就持有，让利润奔跑）+ 紧止损（砍得快）+ 从最高回落才走（保护利润）。
# 回测(主板,2025-26,含连板)：胜率约 50-55%、盈亏比≈2.5、期望+3%/笔；连板期望更高(3板+5.4%)。
# ⚠ 打板真实形态就是"多数小亏、少数连板大赚"；最差单笔可达-20%+(连板次日一字跌停跑不掉)，
#   靠仓位控制(单笔≤20%、最多持3-5只)而非策略消除尾部风险。
DABAN_STOP_LOSS    = -0.05   # 初始止损 -5%（跌破成本5%即卖，亏损砍得快）
DABAN_TRAIL        = 0.07    # 从持仓期最高价回落 7% 即卖（吃完连板后保护利润）
DABAN_MAX_HOLD     = 10      # 最长持有天数（backstop，连板结束后不恋战）
DABAN_HOLD_LIMITUP = True    # 当日仍封板(连板)则继续持有 → 让利润奔跑

# ── 模拟盘（Paper Trading）配置 ─────────────────────
PAPER_COMMISSION_RATE = 0.0003   # 佣金率万3（买卖双向）
PAPER_MIN_COMMISSION  = 5.0      # 最低佣金5元
PAPER_TAX_RATE        = 0.001    # 印花税0.1%（仅卖出）
PAPER_SLIPPAGE_BPS    = 3        # 滑点3bps
PAPER_STOP_LOSS       = -0.05    # 止损-5%
PAPER_TAKE_PROFIT     = 0.10     # 止盈+10%
PAPER_MAX_HOLD_DAYS   = 10       # 最大持仓天数

# ── 下单诊断截图保留策略（自动清理，避免 logs/screenshots 无限增长）──
SCREENSHOT_RETENTION_DAYS = 3      # 仅保留最近几天的下单截图
SCREENSHOT_MAX_FILES      = 500    # 截图数量硬上限，超出按时间删最旧
