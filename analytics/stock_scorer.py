# -*- coding: utf-8 -*-
"""
股票六维评分引擎
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
参考 ZhuLinsen/daily_stock_analysis 评分体系，适配 A 股市场。

六个维度（满分 100 分）：
  1. 趋势评估  30 分 — MA 多头排列、价格在均线上方
  2. 偏离分析  20 分 — 价格偏离 MA5 的距离（入场纪律）
  3. 量能评估  15 分 — 缩量回调 or 放量突破
  4. 支撑验证  10 分 — 价格贴近关键均线支撑
  5. MACD 确认 15 分 — 零轴金叉 / DIF>DEA
  6. RSI 极值  10 分 — 超卖区（最佳）/ 中性区 / 超买区（危险）

信号阈值（参考 daily_stock_analysis）：
  ≥ 75 → 强烈买入
  60-74 → 买入
  45-59 → 持有
  30-44 → 观望
  ≤ 29  → 卖出
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import math
import pandas as pd

# 信号阈值表 (最低分, 标签, 颜色, 英文)
SIGNAL_LEVELS = [
    (75, "强烈买入", "#238636", "STRONG BUY"),
    (60, "买入",    "#1f6feb", "BUY"),
    (45, "持有",    "#e3b341", "HOLD"),
    (30, "观望",    "#bd561d", "WAIT"),
    (0,  "卖出",    "#da3633", "SELL"),
]


def get_signal(score: float) -> tuple:
    """返回 (中文标签, 颜色, 英文标签)"""
    for threshold, label, color, eng in SIGNAL_LEVELS:
        if score >= threshold:
            return label, color, eng
    return "卖出", "#da3633", "SELL"


def score_stock(df: pd.DataFrame) -> dict:
    """
    对单只股票进行六维评分。

    参数：df — 日K DataFrame，需含 date/open/close/high/low/volume
    返回：{
        ok, total, signal, signal_color, signal_eng,
        dimensions: {trend, deviation, volume, support, macd, rsi},
        entry_price, stop_loss, target1, target2,
        indicators: {price, ma5, ma10, ma20, ma60, rsi14, macd_dif, macd_dea,
                     vol_ratio, chg5d, chg20d},
        catalysts, risks
    }
    """
    if df is None or df.empty or len(df) < 22:
        return {"ok": False, "msg": "数据不足（需≥22日）"}

    n      = len(df)
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]
    price  = float(close.iloc[-1])

    def sv(s, idx=-1):
        """安全取值"""
        try:
            v = float(s.iloc[idx])
            return None if math.isnan(v) else round(v, 4)
        except Exception:
            return None

    # ── 均线 ───────────────────────────────────────────────
    ma5  = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean() if n >= 60 else pd.Series([None] * n, index=close.index)

    ma5_v  = sv(ma5)  or price
    ma10_v = sv(ma10) or price
    ma20_v = sv(ma20) or price
    ma60_v = sv(ma60)

    # ── MACD（EMA12/26，Signal=9）────────────────────────
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif   = ema12 - ema26
    dea   = dif.ewm(span=9, adjust=False).mean()
    hist  = (dif - dea) * 2

    dif_v  = sv(dif) or 0
    dea_v  = sv(dea) or 0
    hist_v = sv(hist) or 0
    hist_p = sv(hist, -2) or 0

    # ── RSI(14) ──────────────────────────────────────────
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, float("inf"))
    rsi   = 100 - 100 / (1 + rs)
    rsi_v = sv(rsi) or 50.0

    # ── 成交量比 ─────────────────────────────────────────
    avg_vol_20 = float(volume.iloc[-21:-1].mean()) if n > 21 else float(volume.mean())
    vol_ratio  = float(volume.iloc[-1]) / avg_vol_20 if avg_vol_20 > 0 else 1.0

    # ── 近期涨跌 ─────────────────────────────────────────
    chg5d  = (price / float(close.iloc[-6])  - 1) * 100 if n >= 6  else 0.0
    chg20d = (price / float(close.iloc[-21]) - 1) * 100 if n >= 21 else 0.0

    dims = {}

    # ═══ 维度1：趋势评估（30分）═══════════════════════════
    ts = 0
    if price > ma5_v:         ts += 8   # 站在 MA5 上方
    if price > ma20_v:        ts += 7   # 站在 MA20 上方
    if ma5_v  > ma10_v:       ts += 5   # MA5 > MA10
    if ma10_v > ma20_v:       ts += 5   # MA10 > MA20（中期趋势）
    if ma60_v and price > ma60_v: ts += 5  # 站在 MA60 上方（长期趋势）
    trend_desc = "多头" if ts >= 20 else ("偏多" if ts >= 12 else "空头")
    dims["trend"] = {
        "score": ts, "max": 30, "label": "趋势评估",
        "detail": f"{trend_desc}排列 | MA5={ma5_v:.2f} MA10={ma10_v:.2f} MA20={ma20_v:.2f}",
        "pct": round(ts / 30 * 100),
    }

    # ═══ 维度2：偏离分析（20分）═══════════════════════════
    dev = abs(price - ma5_v) / ma5_v if ma5_v > 0 else 1.0
    if   dev < 0.02: ds = 20
    elif dev < 0.05: ds = 16
    elif dev < 0.08: ds = 10
    elif dev < 0.12: ds = 5
    else:            ds = 0
    dims["deviation"] = {
        "score": ds, "max": 20, "label": "偏离分析",
        "detail": f"偏离MA5 {dev*100:.1f}% | {'贴近（最佳入场）' if dev<0.05 else '偏离过大'}",
        "pct": round(ds / 20 * 100),
    }

    # ═══ 维度3：量能评估（15分）═══════════════════════════
    # 缩量回调（量减价稳）最佳；放量突破次之；大量滞涨警告
    if   vol_ratio < 0.7  and chg5d >= -2:  vs = 15   # 健康缩量横盘
    elif vol_ratio < 0.8  and chg5d >= -4:  vs = 12
    elif vol_ratio > 2.0  and chg5d  > 3:   vs = 11   # 放量突破
    elif vol_ratio > 1.5  and chg5d  > 1:   vs = 9
    elif 0.8 <= vol_ratio <= 1.5:            vs = 7    # 量能正常
    elif vol_ratio > 3.0  and chg5d  < 0:   vs = 2    # 放量下跌，危险
    else:                                    vs = 4
    dims["volume"] = {
        "score": vs, "max": 15, "label": "量能评估",
        "detail": f"量比 {vol_ratio:.2f}x | 5日涨跌 {chg5d:+.1f}%",
        "pct": round(vs / 15 * 100),
    }

    # ═══ 维度4：支撑验证（10分）═══════════════════════════
    dist_ma20 = (price - ma20_v) / ma20_v if ma20_v > 0 else 0
    if   -0.02 <= dist_ma20 <= 0.03: ss = 10   # 贴近 MA20，支撑有效
    elif -0.05 <= dist_ma20 < -0.02: ss = 6    # 轻微跌破，需关注
    elif  0.03 <  dist_ma20 <= 0.08: ss = 7    # 小幅上方，趋势良好
    elif  0.08 <  dist_ma20 <= 0.15: ss = 5    # 较远，注意回调风险
    elif dist_ma20 > 0.15:           ss = 3    # 远离支撑，过度偏高
    else:                             ss = 0    # 深度跌破
    dims["support"] = {
        "score": ss, "max": 10, "label": "支撑验证",
        "detail": f"距MA20 {dist_ma20*100:+.1f}% | {'支撑有效' if ss>=8 else '注意支撑'}",
        "pct": round(ss / 10 * 100),
    }

    # ═══ 维度5：MACD确认（15分）═══════════════════════════
    ms = 0
    if dif_v > 0 and dea_v > 0: ms += 6    # 双线零轴上方
    elif dif_v > 0:              ms += 3    # DIF 零轴上方
    if dif_v > dea_v:            ms += 5   # DIF > DEA（金叉）
    if hist_v > 0 and hist_v > hist_p: ms += 4  # 柱量扩张
    elif hist_v > 0:             ms += 2
    dims["macd"] = {
        "score": ms, "max": 15, "label": "MACD确认",
        "detail": f"DIF={dif_v:.3f} DEA={dea_v:.3f} | {'金叉' if dif_v>dea_v else '死叉'}",
        "pct": round(ms / 15 * 100),
    }

    # ═══ 维度6：RSI极值（10分）═════════════════════════════
    if   rsi_v < 25:  rs_ = 10   # 极度超卖，最佳买入
    elif rsi_v < 35:  rs_ = 9
    elif rsi_v < 45:  rs_ = 7    # 温和区间
    elif rsi_v < 55:  rs_ = 5    # 中性
    elif rsi_v < 65:  rs_ = 4
    elif rsi_v < 75:  rs_ = 2    # 偏高，谨慎
    else:             rs_ = 0    # 超买，高风险
    dims["rsi"] = {
        "score": rs_, "max": 10, "label": "RSI极值",
        "detail": f"RSI(14)={rsi_v:.1f} | {'超卖区' if rsi_v<35 else ('中性' if rsi_v<65 else '超买区')}",
        "pct": round(rs_ / 10 * 100),
    }

    total = sum(d["score"] for d in dims.values())
    signal, color, signal_eng = get_signal(total)

    # ── 入场/止损/目标价 ─────────────────────────────────
    # 入场：贴近 MA5（回调入场原则）
    entry = round(ma5_v * 1.002, 2)
    # 止损：MA20 下方 2%（均线破位止损）
    stop  = round(ma20_v * 0.978, 2)
    # 目标1：基于风险比 2:1
    risk  = max(entry - stop, entry * 0.01)
    tgt1  = round(entry + risk * 2.0, 2)
    # 目标2：MA60 or 布林上轨
    bb_up = float((close.rolling(20).mean() + 2 * close.rolling(20).std()).iloc[-1])
    tgt2  = round(max(tgt1, bb_up), 2) if ma60_v is None else round(max(tgt1, ma60_v * 1.03), 2)

    # ── 自动生成催化剂 & 风险 ────────────────────────────
    catalysts, risks = [], []
    if ts >= 20:       catalysts.append("均线多头排列，中期趋势向上")
    if dif_v > dea_v:  catalysts.append("MACD金叉，动量转正")
    if vs >= 11:       catalysts.append("缩量回调/放量突破，量价配合良好")
    if rsi_v < 40:     catalysts.append(f"RSI({rsi_v:.0f}) 处于超卖区，反弹动力充足")
    if chg20d > 8:     catalysts.append(f"月度涨幅 {chg20d:.1f}%，强势标的")

    if price < ma20_v: risks.append("价格跌破MA20，中期趋势承压")
    if rsi_v > 70:     risks.append(f"RSI({rsi_v:.0f}) 超买，短期回调风险")
    if vol_ratio > 3 and chg5d < 0:
        risks.append("放量下跌，主力出货信号，谨慎操作")
    if dist_ma20 < -0.05:
        risks.append("大幅跌破MA20，趋势可能反转")
    if not risks:      risks.append("当前无重大技术风险，注意止损执行")

    return {
        "ok":          True,
        "total":       round(total, 1),
        "signal":      signal,
        "signal_color":color,
        "signal_eng":  signal_eng,
        "dimensions":  dims,
        "entry_price": entry,
        "stop_loss":   stop,
        "target1":     tgt1,
        "target2":     tgt2,
        "catalysts":   catalysts,
        "risks":       risks,
        "indicators": {
            "price":     round(price, 2),
            "ma5":       round(ma5_v, 2),
            "ma10":      round(ma10_v, 2),
            "ma20":      round(ma20_v, 2),
            "ma60":      round(ma60_v, 2) if ma60_v else None,
            "rsi14":     round(rsi_v, 1),
            "macd_dif":  round(dif_v, 4),
            "macd_dea":  round(dea_v, 4),
            "macd_hist": round(hist_v, 4),
            "vol_ratio": round(vol_ratio, 2),
            "chg5d":     round(chg5d, 2),
            "chg20d":    round(chg20d, 2),
        },
    }
