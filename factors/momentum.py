# FACTOR: {"name": "动量因子", "desc": "20日价格动量（强者恒强），过滤低流动性标的", "category": "动量", "period": 20}
"""
动量因子 (Price Momentum) — abu 风格重写
────────────────────────────────────────
参考 AlphaLens momentum 研究：近 N 日收益率 IC 显著为正
因子值 = (close_t / close_{t-N}) - 1
过滤条件：
  - 价格 > 5 元
  - N 日成交量均值 > 500 手（流动性）
  - 量能未明显萎缩
"""
from indicators import pct_change   # noqa: E402


def scan(codes: list, get_kline, get_quotes) -> list:
    results = []
    N = 20  # 动量窗口

    for code in codes:
        try:
            df = get_kline(code, N + 10)
            if df.empty or len(df) < N + 2:
                continue

            close, volume = df['close'], df['volume']

            if float(close.iloc[-1]) < 5:
                continue
            avg_vol = float(volume.iloc[-N:].mean())
            if avg_vol < 500:
                continue

            ret_N = float(pct_change(close, N).iloc[-1]) / 100.0    # 转回小数
            ret_5 = float(pct_change(close, 5).iloc[-1]) / 100.0
            if ret_N <= 0:
                continue

            recent_vol_ratio = float(volume.iloc[-5:].mean()) / avg_vol if avg_vol > 0 else 0
            if recent_vol_ratio < 0.8:
                continue

            score = round(ret_N * 100, 2)

            quotes = get_quotes([code])
            row    = quotes[quotes['代码'] == code] if not quotes.empty else None
            name   = row.iloc[0]['名称']  if row is not None and not row.empty else ''
            price  = float(row.iloc[0]['最新价']) if row is not None and not row.empty else float(close.iloc[-1])
            chg    = float(row.iloc[0]['涨跌幅']) if row is not None and not row.empty else 0

            results.append({
                "code":   code,
                "name":   name,
                "price":  round(price, 2),
                "change": round(chg, 2),
                "score":  score,
                "reason": (f"动量因子 | {N}日涨幅{ret_N*100:.1f}% | "
                           f"5日涨幅{ret_5*100:.1f}% | 量比{recent_vol_ratio:.1f}x"),
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x['score'], reverse=True)[:50]
