# META: {"name": "MACD动量", "desc": "MACD金叉+零轴上方+价格强势，捕捉中期上涨动量", "author": "内置", "builtin": true}

def scan(codes: list, get_kline, get_quotes) -> list:
    """
    MACD动量策略
    条件：MACD金叉 + DIF在零轴上方 + 今日涨幅 > 1%
    """
    quotes_df = get_quotes(codes)
    results = []

    for code in codes:
        try:
            df = get_kline(code, 90)
            if df.empty or len(df) < 35:
                continue

            close = df['close']

            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            dif   = ema12 - ema26
            dea   = dif.ewm(span=9, adjust=False).mean()

            # 金叉：DIF上穿DEA
            cross = (dif.iloc[-1] > dea.iloc[-1]) and (dif.iloc[-2] <= dea.iloc[-2])
            if not cross:
                continue

            # DIF在零轴上方（强势信号）
            if dif.iloc[-1] <= 0:
                continue

            row   = quotes_df[quotes_df['代码'] == code]
            name  = row.iloc[0]['名称'] if not row.empty else ''
            price = float(row.iloc[0]['最新价']) if not row.empty else float(close.iloc[-1])
            chg   = float(row.iloc[0]['涨跌幅']) if not row.empty else 0

            if chg < 1.0:
                continue

            strength = float(dif.iloc[-1])
            results.append({
                "code":   code,
                "name":   name,
                "price":  round(price, 2),
                "change": round(chg, 2),
                "score":  round(strength * 100, 1),
                "reason": f"MACD金叉零轴上方，DIF={dif.iloc[-1]:.3f}，涨幅{chg:.1f}%"
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x['score'], reverse=True)
