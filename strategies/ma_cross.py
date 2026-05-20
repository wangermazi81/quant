# META: {"name": "均线金叉", "desc": "MA5上穿MA20，配合量能放大，捕捉趋势启动点", "author": "内置", "builtin": true}
"""
均线金叉策略 — 用 BaseStrategy / BaseBuyFactor 重写（abu 风格）

向后兼容：
    导出顶层 `scan(codes, get_kline, get_quotes)` 函数与旧版完全等价，
    strategy_manager 调用方无感知。
"""
from base import BaseBuyFactor, BaseStrategy, Signal   # noqa: E402  via _pathsetup
from indicators import sma, cross_up, volume_ratio     # noqa: E402


# ── 买入因子 ────────────────────────────────────────
class MACrossBuyFactor(BaseBuyFactor):
    """MA5 上穿 MA20 且量比 ≥ vol_thresh"""
    name = "MACrossBuy"
    desc = "短均线上穿长均线 + 量能放大"

    def __init__(self, fast: int = 5, slow: int = 20,
                 vol_lookback: int = 20, vol_thresh: float = 1.5):
        super().__init__(fast=fast, slow=slow,
                         vol_lookback=vol_lookback, vol_thresh=vol_thresh)
        self.fast = fast
        self.slow = slow
        self.vol_lookback = vol_lookback
        self.vol_thresh   = vol_thresh

    def match_buy(self, code, kline, quote):
        if len(kline) < max(self.slow + 2, self.vol_lookback + 1):
            return None

        close, volume = kline["close"], kline["volume"]
        ma_fast = sma(close, self.fast)
        ma_slow = sma(close, self.slow)

        # 今日触发金叉
        if not bool(cross_up(ma_fast, ma_slow).iloc[-1]):
            return None

        # 量比
        vr = volume_ratio(volume, self.vol_lookback).iloc[-1]
        if vr is None or float(vr) < self.vol_thresh:
            return None

        price = float(quote.get("最新价") or close.iloc[-1])
        chg   = float(quote.get("涨跌幅") or 0)
        name  = str(quote.get("名称") or "")

        return Signal(
            code=code,
            name=name,
            price=price,
            change=chg,
            score=round(float(vr) * 20, 1),
            reason=f"MA{self.fast}金叉MA{self.slow}，量能{float(vr):.1f}倍",
            meta={"vol_ratio": round(float(vr), 2)},
        )


# ── 策略 ───────────────────────────────────────────
class MAGoldenCrossStrategy(BaseStrategy):
    name = "均线金叉"
    desc = "MA5 上穿 MA20 + 成交量放大 1.5 倍"
    top_n = 50

    def __init__(self):
        super().__init__()
        self.buy_factors = [MACrossBuyFactor()]


# ── 向后兼容入口：与旧版函数签名等价 ───────────────
_strategy_instance = MAGoldenCrossStrategy()


def scan(codes: list, get_kline, get_quotes) -> list:
    return _strategy_instance.scan(codes, get_kline, get_quotes)
