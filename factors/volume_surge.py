# FACTOR: {"name": "量价共振因子", "desc": "成交量异常放大配合价格突破，识别资金流入", "category": "量价", "period": 10}
"""
量价共振因子 (Volume-Price Synergy)
────────────────────────────────────────
AlphaLens 量价因子研究：成交量 × 价格动量的组合因子预测力强于单独因子
因子逻辑：
  1. 近 5 日成交量 > 20 日均量 × 2.0（量能持续放大）
  2. 收盘价突破 10 日高点（价格确认）
  3. 量增价升（近 5 日量和价同向）
  4. 过滤：近 3 日无大跌（保证量增价升不是补跌）
因子分 = 量比 × 价格突破程度
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

            # 量比：近 5 日均量 vs 20 日均量
            avg_vol_5  = volume.iloc[-6:-1].mean()
            avg_vol_20 = volume.iloc[-21:-1].mean()
            vol_ratio  = avg_vol_5 / avg_vol_20 if avg_vol_20 > 0 else 0
            if vol_ratio < 2.0:
                continue

            # 价格突破 10 日高点
            high_10   = high.iloc[-11:-1].max()
            breakout  = (close.iloc[-1] - high_10) / high_10
            if breakout < 0:
                continue

            # 量增价升确认：5 日价格相关性（量价同向）
            ret_5_list = close.iloc[-5:].pct_change().dropna().tolist()
            vol_5_list = volume.iloc[-5:].pct_change().dropna().tolist()
            if len(ret_5_list) >= 3:
                same_dir = sum(1 for r, v in zip(ret_5_list, vol_5_list)
                               if r * v > 0)
                if same_dir < 2:
                    continue

            # 近 3 日无大跌
            min_3day = min(close.iloc[-3:].pct_change().dropna().tolist() or [0])
            if min_3day < -0.05:
                continue

            score = round(vol_ratio * (1 + breakout * 10), 2)

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
                "reason": (f"量价共振 | 量比{vol_ratio:.1f}x | "
                           f"突破{breakout*100:.1f}% | 量价同向"),
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x['score'], reverse=True)[:50]
