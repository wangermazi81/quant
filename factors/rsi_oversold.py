# FACTOR: {"name": "RSI金叉因子", "desc": "RSI从超卖区回升，结合KDJ底背离", "category": "动量", "period": 14}
"""
RSI 超卖回升因子 (RSI Oversold Recovery)
────────────────────────────────────────
AlphaLens 研究：RSI<30 的超卖区域后续 1-4 周正收益概率较高
因子逻辑：
  1. RSI(14) 3 日前 < 35（进入超卖区）
  2. 当前 RSI > 35 且上升（从超卖区回升）
  3. KDJ 的 K 值上穿 D 值（底背离确认）
  4. 价格在 60 日低点以上 5%（有底部支撑）
因子分 = RSI 回升速度 × 底部强度
"""


def _rsi(series, n=14):
    delta  = series.diff()
    gain   = delta.clip(lower=0).rolling(n).mean()
    loss   = (-delta.clip(upper=0)).rolling(n).mean()
    rs     = gain / loss.replace(0, float('inf'))
    return 100 - 100 / (1 + rs)


def _kdj(high, low, close, n=9):
    """简化 KDJ：RSV → K → D"""
    low_n  = low.rolling(n).min()
    high_n = high.rolling(n).max()
    rsv    = (close - low_n) / (high_n - low_n).replace(0, 1) * 100
    K      = rsv.ewm(com=2, adjust=False).mean()
    D      = K.ewm(com=2, adjust=False).mean()
    return K, D


def scan(codes: list, get_kline, get_quotes) -> list:
    results = []

    for code in codes:
        try:
            df = get_kline(code, 60)
            if df.empty or len(df) < 30:
                continue

            close  = df['close']
            high   = df['high']
            low    = df['low']
            volume = df['volume']

            price = float(close.iloc[-1])
            if price < 3:
                continue

            # RSI 计算
            rsi = _rsi(close, 14)
            rsi_now  = float(rsi.iloc[-1])
            rsi_prev = float(rsi.iloc[-4])   # 3日前

            # 必须从超卖区回升
            if not (rsi_prev < 35 and rsi_now > 35):
                continue

            # KDJ 底部金叉
            K, D = _kdj(high, low, close)
            k_now, d_now   = float(K.iloc[-1]), float(D.iloc[-1])
            k_prev, d_prev = float(K.iloc[-2]), float(D.iloc[-2])
            kdj_cross = (k_now > d_now) and (k_prev <= d_prev)   # K 上穿 D

            # 流动性过滤
            avg_vol = volume.iloc[-20:].mean()
            if avg_vol < 300:
                continue

            # 底部支撑：不创 60 日新低
            low_60 = low.iloc[-60:].min()
            if price < low_60 * 1.03:
                continue

            # 因子分
            rsi_speed  = rsi_now - rsi_prev   # RSI 回升速度
            kdj_bonus  = 20 if kdj_cross else 0
            score      = round(rsi_speed + kdj_bonus, 2)

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
                "reason": (f"RSI金叉 | RSI {rsi_prev:.0f}→{rsi_now:.0f} | "
                           f"{'KDJ金叉 ' if kdj_cross else ''}| K={k_now:.1f} D={d_now:.1f}"),
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x['score'], reverse=True)[:50]
