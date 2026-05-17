# FACTOR: {"name": "低波动因子", "desc": "历史波动率最低的股票，低风险溢价", "category": "波动", "period": 20}
"""
低波动因子 (Low Volatility)
────────────────────────────────────────
AlphaLens / MSCI 因子研究：低波动股票在风险调整后收益更优（波动率异象）
因子值 = 20 日日收益率标准差（年化）
过滤：
  - 价格 > 5 元，非 ST
  - 成交量充足（流动性）
  - 中期趋势向上（MA20 斜率 > 0）
  - 排除近期大幅下跌的票（20日跌幅 > -10%）
排序：年化波动率升序（波动越低得分越高）
"""

import math


def scan(codes: list, get_kline, get_quotes) -> list:
    results = []

    for code in codes:
        try:
            df = get_kline(code, 30)
            if df.empty or len(df) < 22:
                continue

            close  = df['close']
            volume = df['volume']

            price = float(close.iloc[-1])
            if price < 5:
                continue

            # 流动性过滤
            avg_vol = volume.iloc[-20:].mean()
            if avg_vol < 300:
                continue

            # 计算 20 日日收益率序列
            rets = close.pct_change().dropna().iloc[-20:]
            if len(rets) < 15:
                continue

            vol_20  = float(rets.std()) * math.sqrt(252) * 100   # 年化波动率 (%)
            if vol_20 > 35:   # 波动超 35% 不算低波动
                continue

            # 趋势向上（MA20 斜率正）
            ma20_now  = close.rolling(20).mean().iloc[-1]
            ma20_prev = close.rolling(20).mean().iloc[-5]
            if ma20_now < ma20_prev:
                continue

            # 中期不能太差（20日跌幅限制）
            ret_20 = (close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]
            if ret_20 < -0.10:
                continue

            score = round(100 - vol_20, 2)   # 波动越低，得分越高

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
                "reason": (f"低波动因子 | 年化波动{vol_20:.1f}% | "
                           f"20日涨幅{ret_20*100:.1f}% | 趋势向上"),
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x['score'], reverse=True)[:50]
