# META: {"name": "量价突破", "desc": "股价突破20日高点，同时成交量显著放大", "author": "内置", "builtin": true}
"""
量价突破策略 — 用 BaseStrategy / BaseBuyFactor 重写（abu 风格）
"""
from base import BaseBuyFactor, BaseStrategy, Signal     # noqa: E402
from indicators import volume_ratio                       # noqa: E402


class VolumeBreakoutBuyFactor(BaseBuyFactor):
    """突破 N 日高点 + 量能放大"""
    name = "VolumeBreakoutBuy"
    desc = "突破前期高点 + 成交量放大"

    def __init__(self, high_window: int = 20, vol_thresh: float = 2.0):
        super().__init__(high_window=high_window, vol_thresh=vol_thresh)
        self.high_window = high_window
        self.vol_thresh  = vol_thresh

    def match_buy(self, code, kline, quote):
        if len(kline) < self.high_window + 2:
            return None

        close, high, volume = kline["close"], kline["high"], kline["volume"]

        recent_high = float(high.iloc[-(self.high_window + 1):-1].max())
        today_close = float(close.iloc[-1])
        if today_close <= recent_high:
            return None

        vr = float(volume_ratio(volume, self.high_window).iloc[-1] or 0)
        if vr < self.vol_thresh:
            return None

        breakout_pct = (today_close - recent_high) / recent_high * 100
        price = float(quote.get("最新价") or today_close)
        chg   = float(quote.get("涨跌幅") or 0)
        name  = str(quote.get("名称") or "")

        return Signal(
            code=code, name=name, price=price, change=chg,
            score=round(vr * breakout_pct, 1),
            reason=f"突破前高{recent_high:.2f}，超越{breakout_pct:.1f}%，量能{vr:.1f}倍",
            meta={"breakout_pct": round(breakout_pct, 2),
                  "vol_ratio":    round(vr, 2)},
        )


class VolumeBreakoutStrategy(BaseStrategy):
    name = "量价突破"
    desc = "突破 20 日高点 + 量能放大 2 倍"
    top_n = 50

    def __init__(self):
        super().__init__()
        self.buy_factors = [VolumeBreakoutBuyFactor()]


_strategy_instance = VolumeBreakoutStrategy()


def scan(codes: list, get_kline, get_quotes) -> list:
    return _strategy_instance.scan(codes, get_kline, get_quotes)
