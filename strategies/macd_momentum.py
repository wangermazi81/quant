# META: {"name": "MACD动量", "desc": "MACD金叉+零轴上方+价格强势，捕捉中期上涨动量", "author": "内置", "builtin": true}
"""
MACD 动量策略 — 用 BaseStrategy / BaseBuyFactor 重写（abu 风格）
"""
from base import BaseBuyFactor, BaseStrategy, Signal          # noqa: E402
from indicators import macd, cross_up                          # noqa: E402


class MACDMomentumBuyFactor(BaseBuyFactor):
    """MACD 金叉 + DIF 在零轴上方 + 当日涨幅 ≥ min_chg%"""
    name = "MACDMomentumBuy"
    desc = "MACD 金叉零轴上方，捕捉中期上涨动量"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9,
                 min_chg_pct: float = 1.0):
        super().__init__(fast=fast, slow=slow, signal=signal, min_chg_pct=min_chg_pct)
        self.fast, self.slow, self.signal_period = fast, slow, signal
        self.min_chg_pct = min_chg_pct

    def match_buy(self, code, kline, quote):
        if len(kline) < self.slow + self.signal_period + 2:
            return None

        close = kline["close"]
        dif, dea, _hist = macd(close, self.fast, self.slow, self.signal_period)

        # 今日金叉
        if not bool(cross_up(dif, dea).iloc[-1]):
            return None
        # DIF 在零轴上方
        if float(dif.iloc[-1]) <= 0:
            return None

        chg = float(quote.get("涨跌幅") or 0)
        if chg < self.min_chg_pct:
            return None

        price = float(quote.get("最新价") or close.iloc[-1])
        name  = str(quote.get("名称") or "")
        strength = float(dif.iloc[-1])

        return Signal(
            code=code, name=name, price=price, change=chg,
            score=round(strength * 100, 1),
            reason=f"MACD金叉零轴上方，DIF={strength:.3f}，涨幅{chg:.1f}%",
            meta={"dif": round(strength, 4)},
        )


class MACDMomentumStrategy(BaseStrategy):
    name = "MACD动量"
    desc = "MACD金叉 + 零轴之上 + 价格强势"
    top_n = 50

    def __init__(self):
        super().__init__()
        self.buy_factors = [MACDMomentumBuyFactor()]


_strategy_instance = MACDMomentumStrategy()


def scan(codes: list, get_kline, get_quotes) -> list:
    return _strategy_instance.scan(codes, get_kline, get_quotes)
