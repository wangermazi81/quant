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

重构（abu 风格）：用统一 indicators 库替代内嵌 _rsi / _kdj 实现。
"""
from indicators import rsi as _rsi, kdj as _kdj, cross_up   # noqa: E402


def scan(codes: list, get_kline, get_quotes) -> list:
    results = []

    for code in codes:
        try:
            df = get_kline(code, 60)
            if df.empty or len(df) < 30:
                continue

            close, high, low, volume = df['close'], df['high'], df['low'], df['volume']

            price = float(close.iloc[-1])
            if price < 3:
                continue

            # RSI 走势
            r = _rsi(close, 14)
            rsi_now  = float(r.iloc[-1])
            rsi_prev = float(r.iloc[-4])
            if not (rsi_prev < 35 and rsi_now > 35):
                continue

            # KDJ 底部金叉
            k, d, _j = _kdj(high, low, close)
            kdj_cross_today = bool(cross_up(k, d).iloc[-1])
            k_now, d_now    = float(k.iloc[-1]), float(d.iloc[-1])

            # 流动性
            if volume.iloc[-20:].mean() < 300:
                continue

            # 底部支撑：不创 60 日新低
            low_60 = float(low.iloc[-60:].min())
            if price < low_60 * 1.03:
                continue

            rsi_speed = rsi_now - rsi_prev
            score     = round(rsi_speed + (20 if kdj_cross_today else 0), 2)

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
                "reason": (f"RSI金叉 | RSI {rsi_prev:.0f}→{rsi_now:.0f} | "
                           f"{'KDJ金叉 ' if kdj_cross_today else ''}| "
                           f"K={k_now:.1f} D={d_now:.1f}"),
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x['score'], reverse=True)[:50]
