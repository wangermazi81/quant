# FACTOR: {"name": "趋势强度因子", "desc": "ADX趋势强度 + 布林带宽度，识别强趋势标的", "category": "趋势", "period": 14}
"""
趋势强度因子 (Trend Strength) — abu 风格重写
────────────────────────────────────────
组合 3 个维度：
  1. 布林带宽度 BBW = (上轨 - 下轨) / 中轨
  2. 收盘价突破上轨的持续性（近 10 日）
  3. ATR 反映的真实波动率
配合 MA 多头排列过滤。
"""
from indicators import sma, boll, atr   # noqa: E402


def scan(codes: list, get_kline, get_quotes) -> list:
    results = []

    for code in codes:
        try:
            df = get_kline(code, 40)
            if df.empty or len(df) < 30:
                continue

            close, high, low = df['close'], df['high'], df['low']
            price = float(close.iloc[-1])
            if price < 3:
                continue

            ma5  = float(sma(close, 5).iloc[-1])
            ma10 = float(sma(close, 10).iloc[-1])
            ma20 = float(sma(close, 20).iloc[-1])
            if not (ma5 > ma10 > ma20):
                continue

            upper, mid, lower = boll(close, 20, 2.0)
            bbw = float((upper.iloc[-1] - lower.iloc[-1]) / mid.iloc[-1])

            # 近 10 日收盘接近/突破上轨的占比
            above_upper = int(((close.iloc[-10:] > upper.iloc[-10:] * 0.98)).sum())
            strength = above_upper / 10.0

            atr_now = float(atr(high, low, close, 14).iloc[-1])
            atr_pct = atr_now / price if price > 0 else 0
            if atr_pct < 0.005:
                continue

            score = round((bbw * 100 + strength * 30) * (1 + atr_pct * 10), 2)

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
                "reason": (f"趋势强度 | BBW={bbw*100:.1f}% | "
                           f"强势占比{strength*100:.0f}% | "
                           f"MA5={ma5:.2f}>MA10={ma10:.2f}>MA20={ma20:.2f}"),
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x['score'], reverse=True)[:50]
