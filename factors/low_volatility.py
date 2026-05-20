# FACTOR: {"name": "低波动因子", "desc": "历史波动率最低的股票，低风险溢价", "category": "波动", "period": 20}
"""
低波动因子 (Low Volatility) — abu 风格重写
────────────────────────────────────────
基于波动率异象 (MSCI / AlphaLens)：低波动股票在风险调整后收益更优
因子值 = 20 日日收益率标准差 × √252（年化）
"""
import math                          # noqa: E402
from indicators import sma           # noqa: E402


def scan(codes: list, get_kline, get_quotes) -> list:
    results = []

    for code in codes:
        try:
            df = get_kline(code, 30)
            if df.empty or len(df) < 22:
                continue

            close, volume = df['close'], df['volume']

            price = float(close.iloc[-1])
            if price < 5:
                continue
            if float(volume.iloc[-20:].mean()) < 300:
                continue

            rets = close.pct_change().dropna().iloc[-20:]
            if len(rets) < 15:
                continue

            vol_20 = float(rets.std()) * math.sqrt(252) * 100
            if vol_20 > 35:
                continue

            ma20_series = sma(close, 20)
            if float(ma20_series.iloc[-1]) < float(ma20_series.iloc[-5]):
                continue

            ret_20 = (close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]
            if ret_20 < -0.10:
                continue

            score = round(100 - vol_20, 2)

            quotes = get_quotes([code])
            row    = quotes[quotes['代码'] == code] if not quotes.empty else None
            name   = row.iloc[0]['名称']  if row is not None and not row.empty else ''
            chg    = float(row.iloc[0]['涨跌幅']) if row is not None and not row.empty else 0

            results.append({
                "code":   code,
                "name":   name,
                "price":  round(price, 2),
                "change": round(chg, 2),
                "score":  score,
                "reason": (f"低波动因子 | 年化波动{vol_20:.1f}% | "
                           f"20日涨幅{float(ret_20)*100:.1f}% | 趋势向上"),
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x['score'], reverse=True)[:50]
