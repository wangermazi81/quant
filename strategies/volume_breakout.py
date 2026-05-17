# META: {"name": "量价突破", "desc": "股价突破20日高点，同时成交量显著放大", "author": "内置", "builtin": true}

def scan(codes: list, get_kline, get_quotes) -> list:
    """
    量价突破策略
    条件：今日收盘 > 前20日最高价 + 今日量 > 20日均量×2
    """
    quotes_df = get_quotes(codes)
    results = []

    for code in codes:
        try:
            df = get_kline(code, 60)
            if df.empty or len(df) < 22:
                continue

            close  = df['close']
            high   = df['high']
            volume = df['volume']

            recent_high = high.iloc[-21:-1].max()
            today_close = close.iloc[-1]
            avg_vol     = volume.iloc[-21:-1].mean()
            vol_ratio   = volume.iloc[-1] / avg_vol if avg_vol > 0 else 0

            if today_close <= recent_high:
                continue
            if vol_ratio < 2.0:
                continue

            row   = quotes_df[quotes_df['代码'] == code]
            name  = row.iloc[0]['名称'] if not row.empty else ''
            price = float(row.iloc[0]['最新价']) if not row.empty else float(today_close)
            chg   = float(row.iloc[0]['涨跌幅']) if not row.empty else 0

            breakout_pct = (today_close - recent_high) / recent_high * 100

            results.append({
                "code":   code,
                "name":   name,
                "price":  round(price, 2),
                "change": round(chg, 2),
                "score":  round(vol_ratio * breakout_pct, 1),
                "reason": f"突破前高{recent_high:.2f}，超越{breakout_pct:.1f}%，量能{vol_ratio:.1f}倍"
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x['score'], reverse=True)
