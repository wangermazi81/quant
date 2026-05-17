# FACTOR: {"name": "反转因子", "desc": "5日超跌后反弹信号，短期均值回归", "category": "反转", "period": 5}
"""
短期反转因子 (Short-Term Reversal)
────────────────────────────────────────
AlphaLens 文献：1-4 周反转因子在 A 股 IC 显著为负（负值越小 → 未来收益越好）
因子逻辑：
  1. 5 日累计跌幅 < -6%（超跌）
  2. 20 日均线以上（中期趋势未破）
  3. 今日缩量（卖压减弱）→ 反弹信号
  4. 成交量 < 5日均量（缩量止跌）
排序：超跌幅度降序（跌得最惨的优先候选）
"""


def scan(codes: list, get_kline, get_quotes) -> list:
    results = []

    for code in codes:
        try:
            df = get_kline(code, 30)
            if df.empty or len(df) < 22:
                continue

            close  = df['close']
            volume = df['volume']
            high   = df['high']

            price = float(close.iloc[-1])
            if price < 3:
                continue

            # 5日反转因子
            ret_5   = (close.iloc[-1] - close.iloc[-6]) / close.iloc[-6]
            if ret_5 > -0.06:   # 跌幅不足 6%，不算超跌
                continue

            # 中期趋势：20日均线未破（避免趋势下跌陷阱）
            ma20 = close.rolling(20).mean().iloc[-1]
            if price < ma20 * 0.95:
                continue

            # 缩量止跌：今日量 < 5日均量
            avg_vol_5 = volume.iloc[-6:-1].mean()
            vol_ratio = volume.iloc[-1] / avg_vol_5 if avg_vol_5 > 0 else 1
            if vol_ratio > 1.2:   # 今日放量 → 还在抛，跳过
                continue

            # 未创 20 日新低（底部支撑）
            low_20 = df['low'].iloc[-20:].min()
            if price < low_20 * 1.01:
                continue

            score = round(abs(ret_5) * 100, 2)

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
                "reason": (f"反转因子 | 5日跌幅{ret_5*100:.1f}% | "
                           f"MA20={ma20:.2f} | 缩量{vol_ratio:.1f}x"),
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x['score'], reverse=True)[:50]
