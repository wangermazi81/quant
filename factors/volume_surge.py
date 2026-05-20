# FACTOR: {"name": "量价共振因子", "desc": "成交量异常放大配合价格突破，识别资金流入", "category": "量价", "period": 10}
"""
量价共振因子 (Volume-Price Synergy) — abu 风格重写
────────────────────────────────────────
1. 近 5 日均量 / 20 日均量 ≥ 2.0
2. 收盘突破 10 日高点
3. 量增价升同向
4. 近 3 日无大跌（>5%）
"""


def scan(codes: list, get_kline, get_quotes) -> list:
    results = []

    for code in codes:
        try:
            df = get_kline(code, 30)
            if df.empty or len(df) < 22:
                continue

            close, volume, high = df['close'], df['volume'], df['high']
            price = float(close.iloc[-1])
            if price < 3:
                continue

            avg_vol_5  = float(volume.iloc[-6:-1].mean())
            avg_vol_20 = float(volume.iloc[-21:-1].mean())
            vol_ratio  = avg_vol_5 / avg_vol_20 if avg_vol_20 > 0 else 0
            if vol_ratio < 2.0:
                continue

            high_10  = float(high.iloc[-11:-1].max())
            breakout = (float(close.iloc[-1]) - high_10) / high_10
            if breakout < 0:
                continue

            # 量增价升确认（同向天数 ≥ 2）
            ret_5 = close.iloc[-5:].pct_change().dropna()
            vol_5 = volume.iloc[-5:].pct_change().dropna()
            if len(ret_5) >= 3:
                same_dir = int(((ret_5.values * vol_5.values) > 0).sum())
                if same_dir < 2:
                    continue

            min_3day = float(close.iloc[-3:].pct_change().dropna().min())
            if min_3day < -0.05:
                continue

            score = round(vol_ratio * (1 + breakout * 10), 2)

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
                "reason": (f"量价共振 | 量比{vol_ratio:.1f}x | "
                           f"突破{breakout*100:.1f}% | 量价同向"),
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x['score'], reverse=True)[:50]
