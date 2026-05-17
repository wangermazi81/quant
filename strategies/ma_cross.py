# META: {"name": "均线金叉", "desc": "MA5上穿MA20，配合量能放大，捕捉趋势启动点", "author": "内置", "builtin": true}

def scan(codes: list, get_kline, get_quotes) -> list:
    """
    均线金叉策略
    条件：MA5上穿MA20（今天穿，昨天未穿） + 成交量 > 20日均量×1.5
    """
    quotes_df = get_quotes(codes)
    results = []

    for code in codes:
        try:
            df = get_kline(code, 60)
            if df.empty or len(df) < 22:
                continue

            close  = df['close']
            volume = df['volume']

            ma5  = close.rolling(5).mean()
            ma20 = close.rolling(20).mean()

            # 金叉：今天 MA5 > MA20，昨天 MA5 <= MA20
            cross_up = (ma5.iloc[-1] > ma20.iloc[-1]) and (ma5.iloc[-2] <= ma20.iloc[-2])
            if not cross_up:
                continue

            # 量能放大
            avg_vol   = volume.iloc[-21:-1].mean()
            vol_ratio = volume.iloc[-1] / avg_vol if avg_vol > 0 else 0
            if vol_ratio < 1.5:
                continue

            row  = quotes_df[quotes_df['代码'] == code]
            name = row.iloc[0]['名称'] if not row.empty else ''
            price = float(row.iloc[0]['最新价']) if not row.empty else float(close.iloc[-1])
            chg   = float(row.iloc[0]['涨跌幅']) if not row.empty else 0

            results.append({
                "code":   code,
                "name":   name,
                "price":  round(price, 2),
                "change": round(chg, 2),
                "score":  round(vol_ratio * 20, 1),
                "reason": f"MA5金叉MA20，量能{vol_ratio:.1f}倍"
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x['score'], reverse=True)
