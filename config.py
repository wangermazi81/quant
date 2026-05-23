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
INITIAL_CAPITAL  = 500_000    # 初始资金（元），可在界面修改
POSITION_RATIO   = 0.20       # 单次建仓比例 20%
# MAX_DAILY_TRADES 已移除 —— 由资金管理自然控制开仓数量

SCAN_INTERVAL = 30            # 扫描间隔秒
DRY_RUN = True                # True=模拟盘/paper trading，False=实盘
# ⚠️ 切 False 前必须先校验本机交易客户端的买入/卖出快捷键

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

# ── 模拟盘（Paper Trading）配置 ─────────────────────
PAPER_COMMISSION_RATE = 0.0003   # 佣金率万3（买卖双向）
PAPER_MIN_COMMISSION  = 5.0      # 最低佣金5元
PAPER_TAX_RATE        = 0.001    # 印花税0.1%（仅卖出）
PAPER_SLIPPAGE_BPS    = 3        # 滑点3bps
PAPER_STOP_LOSS       = -0.05    # 止损-5%
PAPER_TAKE_PROFIT     = 0.10     # 止盈+10%
PAPER_MAX_HOLD_DAYS   = 10       # 最大持仓天数
