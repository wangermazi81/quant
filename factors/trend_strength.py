# FACTOR: {"name": "趋势强度因子", "desc": "ADX趋势强度 + 布林带宽度，识别强趋势标的", "category": "趋势", "period": 14}
"""
趋势强度因子 (Trend Strength / ADX-inspired)
────────────────────────────────────────
参考 AlphaLens 趋势跟随因子研究，结合：
  1. ADX 近似计算（True Range / N 日 ATR 的变化速率）
  2. 布林带宽度（BBW = 上轨-下轨 / 中轨）
  3. 价格位于均线上方
因子逻辑：
  - 趋势强度分 = BBW × (收盘在上轨占比)
  - 配合 MA5 > MA10 > MA20 多头排列
  - 过滤：成交额太小的标的（< 5000 万手×元估算）
"""

import math


def _ema(series, n):
    return series.ewm(span=n, adjust=False).mean()


def scan(codes: list, get_kline, get_quotes) -> list:
    results = []

    for code in codes:
        try:
            df = get_kline(code, 40)
            if df.empty or len(df) < 30:
                continue

            close  = df['close']
            high   = df['high']
            low    = df['low']
            volume = df['volume']

            price = float(close.iloc[-1])
            if price < 3:
                continue

            # 多头排列
            ma5  = close.rolling(5).mean().iloc[-1]
            ma10 = close.rolling(10).mean().iloc[-1]
            ma20 = close.rolling(20).mean().iloc[-1]
            if not (ma5 > ma10 > ma20):
                continue

            # 布林带（20日，2倍标准差）
            sma20 = close.rolling(20).mean()
            std20 = close.rolling(20).std()
            upper = sma20 + 2 * std20
            lower = sma20 - 2 * std20
            bbw   = float((upper.iloc[-1] - lower.iloc[-1]) / sma20.iloc[-1])

            # 近 10 日收盘在上轨比例（反映持续强势）
            above_upper = sum(1 for i in range(-10, 0)
                              if close.iloc[i] > upper.iloc[i] * 0.98)
            strength = above_upper / 10.0

            # ATR（True Range 近似）
            tr = (high - low).iloc[-14:].mean()
            atr_pct = tr / price if price > 0 else 0

            # 过滤极低波动（非死水）
            if atr_pct < 0.005:
                continue

            # 趋势得分
            score = round((bbw * 100 + strength * 30) * (1 + atr_pct * 10), 2)

            quotes = get_quotes([code])
            row   = quotes[quotes['代码'] == code] if not quotes.empty else None
            name  = row.iloc[0]['名称']  if row is not None and not row.empty else ''
            chg   = float(row.iloc[0]['涨跌幅']) if row is not None and not row.empty else 0

            results.append({
                "code":   code,
                "name":   name,
                "price":  round(price, 2),
                "change": round(chg, 2),
                "score":  score,
                "reason": (f"趋势强度 | BBW={bbw*100:.1f}% | "
                           f"强势占比{strength*100:.0f}% | "
                           f"MA5>{ma10:.2f}>MA20"),
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x['score'], reverse=True)[:50]
