# 策略参数配置
STOCK_POOL = [
    "000001", "002594", "600519", "300750", "600036",
    "000858", "601318", "300015", "002415", "600900"
]

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
MAX_DAILY_TRADES = 3          # 打板策略每日最多买入只数

SCAN_INTERVAL = 30            # 扫描间隔秒
DRY_RUN = True                # True=模拟盘/paper trading，False=实盘
THS_WINDOW_TITLE = "同花顺"

# ── 模拟盘（Paper Trading）配置 ─────────────────────
PAPER_COMMISSION_RATE = 0.0003   # 佣金率万3（买卖双向）
PAPER_MIN_COMMISSION  = 5.0      # 最低佣金5元
PAPER_TAX_RATE        = 0.001    # 印花税0.1%（仅卖出）
PAPER_SLIPPAGE_BPS    = 3        # 滑点3bps
PAPER_STOP_LOSS       = -0.05    # 止损-5%
PAPER_TAKE_PROFIT     = 0.10     # 止盈+10%
PAPER_MAX_HOLD_DAYS   = 10       # 最大持仓天数
