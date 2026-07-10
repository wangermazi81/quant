import pandas as pd
import logging
from market_data import get_realtime_quotes, get_kline
from config import STRATEGY, MARKET_FILTER

logger = logging.getLogger(__name__)


def calc_ma(series, period):
    return series.rolling(window=period).mean()


def calc_macd(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast).mean()
    ema_slow = close.ewm(span=slow).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal).mean()
    return dif, dea


def _prefilter_quotes(quotes: pd.DataFrame) -> list:
    """
    第一阶段：从全市场实时行情中快速过滤出候选股票代码。
    仅使用行情数据（无需 K 线），速度极快。
    返回: 通过预过滤的股票代码列表
    """
    if quotes.empty:
        return []

    mf = MARKET_FILTER
    df = quotes.copy()

    # 列名映射（兼容 akshare 返回列名）
    col_code    = "代码"
    col_name    = "名称"
    col_change  = "涨跌幅"
    col_price   = "最新价"
    col_vol     = "成交量"

    # 基础字段存在性检查
    for col in [col_code, col_change, col_price]:
        if col not in df.columns:
            logger.warning(f"[预过滤] 缺少列: {col}")
            return []

    # 1. 涨幅区间
    df = df[
        (df[col_change] >= mf.get("min_change", 2.0)) &
        (df[col_change] <= mf.get("max_change", 9.8))
    ]

    # 2. 价格区间（排除仙股和超高价股）
    df = df[
        (df[col_price] >= mf.get("min_price", 3.0)) &
        (df[col_price] <= mf.get("max_price", 200.0))
    ]

    # 3. 成交量过滤
    if col_vol in df.columns:
        df = df[df[col_vol] >= mf.get("min_volume", 5000)]

    # 4. 排除 ST / 退市
    if mf.get("exclude_st", True) and col_name in df.columns:
        df = df[~df[col_name].str.contains(r"ST|退市|退", na=False)]

    # 5. 排除北交所（代码以 8 或 4 开头）
    if mf.get("exclude_bj", True):
        df = df[~df[col_code].str.startswith(("8", "4"), na=False)]

    codes = df[col_code].tolist()
    max_c = mf.get("max_candidates", 300)
    if len(codes) > max_c:
        codes = codes[:max_c]

    logger.info(f"[预过滤] 全市场 {len(quotes)} 只 → 候选 {len(codes)} 只")
    return codes


def check_conditions(code: str, realtime_row: pd.Series) -> dict:
    """第二阶段：对单只股票做完整 K 线技术条件检查"""
    result = {
        "code":       code,
        "name":       realtime_row.get("名称", ""),
        "price":      realtime_row.get("最新价", 0),
        "change":     realtime_row.get("涨跌幅", 0),
        "conditions": {},
        "pass_count": 0,
        "signal":     False,
    }

    df = get_kline(code)
    if df is None or df.empty or len(df) < 30:
        return result

    close  = df["close"]
    volume = df["volume"]

    # 条件1: MA5上穿MA20
    ma5  = calc_ma(close, STRATEGY["ma_short"])
    ma20 = calc_ma(close, STRATEGY["ma_long"])
    ma_cross = (ma5.iloc[-1] > ma20.iloc[-1]) and (ma5.iloc[-2] <= ma20.iloc[-2])
    result["conditions"]["MA均线金叉"] = {
        "pass": ma_cross,
        "value": f"MA5:{ma5.iloc[-1]:.2f} MA20:{ma20.iloc[-1]:.2f}",
    }

    # 条件2: 成交量放大
    avg_vol   = volume.iloc[-20:-1].mean()
    vol_ratio = float(volume.iloc[-1]) / avg_vol if avg_vol > 0 else 0
    vol_ok    = vol_ratio >= STRATEGY["vol_multiplier"]
    result["conditions"]["成交量放大"] = {
        "pass": vol_ok,
        "value": f"{vol_ratio:.1f}倍",
    }

    # 条件3: MACD金叉
    dif, dea = calc_macd(close)
    macd_cross = (dif.iloc[-1] > dea.iloc[-1]) and (dif.iloc[-2] <= dea.iloc[-2])
    result["conditions"]["MACD金叉"] = {
        "pass": macd_cross,
        "value": f"DIF:{dif.iloc[-1]:.3f}",
    }

    # 条件4: 突破前高
    recent_high = close.iloc[-20:-1].max()
    break_high  = close.iloc[-1] > recent_high
    result["conditions"]["突破前高"] = {
        "pass": break_high,
        "value": f"前高:{recent_high:.2f}",
    }

    # 条件5: 涨幅在合理区间
    chg    = result["change"]
    chg_ok = STRATEGY["min_change"] <= chg <= STRATEGY["max_change"]
    result["conditions"]["涨幅区间"] = {
        "pass": chg_ok,
        "value": f"{chg:+.2f}%",
    }

    result["pass_count"] = sum(1 for c in result["conditions"].values() if c["pass"])
    result["signal"]     = result["pass_count"] >= STRATEGY["min_conditions"]
    return result


def scan_all(codes: list = None) -> list:
    """
    两阶段全市场扫描：
    Phase 1: 批量获取全市场行情，按涨幅/量/价格快速过滤（无 K 线调用）
    Phase 2: 对候选股逐一拉取 K 线并做技术条件检查
    codes 为空时扫描全市场；非空时仅扫描指定股票池。
    """
    # Phase 1: 获取行情并预过滤
    quotes = get_realtime_quotes(codes or [])
    if quotes.empty:
        logger.warning("[scan_all] 无法获取行情数据")
        return []

    if codes:
        # 指定股票池：跳过涨幅预过滤，直接做技术检查
        candidates = codes
    else:
        candidates = _prefilter_quotes(quotes)

    if not candidates:
        return []

    # Phase 2: K 线技术条件检查
    results = []
    quote_index = {}
    if not quotes.empty and "代码" in quotes.columns:
        for _, row in quotes.iterrows():
            quote_index[str(row.get("代码", ""))] = row

    for code in candidates:
        row = quote_index.get(code)
        if row is None:
            continue
        r = check_conditions(code, row)
        if r["signal"]:
            results.append(r)
            logger.info(f"✓ {code} {r['name']} 满足{r['pass_count']}/5条件")

    logger.info(f"[scan_all] Phase2 检查 {len(candidates)} 只，命中 {len(results)} 只")
    return results
