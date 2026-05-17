# -*- coding: utf-8 -*-
"""
市场环境探测模块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
参考 daily_stock_analysis 的市场背景分析思路，结合
TradingAgents 的市场环境注入机制。

通过分析上证指数/深证成指/创业板的近期走势，
判断当前市场处于：
  牛市 (≥70分) → 积极做多
  震荡 (45-69分) → 波段操作
  熊市 (≤44分) → 轻仓防守

市场背景会注入到所有 AI 分析中，提升个股分析准确度。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import logging
import time
from functools import lru_cache
import pandas as pd

logger = logging.getLogger(__name__)

# 大盘指数代码
_INDEX_MAP = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
}

# 指数市场编号（pytdx: SH=1, SZ=0）
# 注意：000001 是上证指数(SH=1)，不能用 _market() 自动判断（会误判为SZ=0）
_INDEX_MARKET_PAIRS = [
    (1, "000001"),  # 上证指数 → 上交所
    (0, "399001"),  # 深证成指 → 深交所
    (0, "399006"),  # 创业板指 → 深交所（创业板）
]
_INDEX_MARKET = {code: mkt for mkt, code in _INDEX_MARKET_PAIRS}

# 简单缓存：5分钟内不重复请求
_cache = {"data": None, "ts": 0}
_CACHE_TTL = 300  # 5 分钟


def _get_index_bars(code: str, days: int = 60):
    """通过 TDX 获取指数日K线（用 get_index_kline 指定正确市场编号）"""
    try:
        import ths_quote as tq
        client = tq.get_client()
        if not client or not client._ok:
            return None
        market = _INDEX_MARKET.get(code, 0)
        bars = client.get_index_kline(code, market, days)
        if bars:
            return pd.DataFrame(bars)
    except Exception as e:
        logger.debug(f"指数K线 {code} 失败: {e}")
    return None


def _score_index(df) -> int:
    """对单个指数打趋势强度分（0-40）"""
    if df is None or len(df) < 22:
        return 20  # 无数据时返回中性

    close = df["close"]
    n = len(close)
    score = 0

    ma5  = float(close.rolling(5).mean().iloc[-1])
    ma20 = float(close.rolling(20).mean().iloc[-1])
    cur  = float(close.iloc[-1])

    if cur > ma5:   score += 8
    if ma5 > ma20:  score += 12
    if cur > ma20:  score += 8

    ret_20 = (cur - float(close.iloc[-21])) / float(close.iloc[-21]) * 100 if n >= 21 else 0
    if   ret_20 > 10:  score += 12
    elif ret_20 > 5:   score += 9
    elif ret_20 > 0:   score += 5
    elif ret_20 < -10: score -= 10
    elif ret_20 < -5:  score -= 5

    return max(0, min(40, score))


def get_market_regime(force_refresh: bool = False) -> dict:
    """
    分析当前市场环境（5分钟缓存）。

    返回：
        trend:    "牛市" | "震荡" | "熊市"
        strength: 0-100  综合强度得分
        context:  字符串，一句话描述
        advice:   操作建议
        indices:  各指数当日行情
        position_guidance: 建议仓位
    """
    now = time.time()
    if not force_refresh and _cache["data"] and (now - _cache["ts"]) < _CACHE_TTL:
        return _cache["data"]

    result = {
        "trend":    "震荡",
        "strength": 50,
        "context":  "市场数据暂不可用",
        "advice":   "谨慎操作，适度观望",
        "indices":  {},
        "position_guidance": "半仓（30-50%）",
        "sh_ret20": 0,
    }

    try:
        import ths_quote as tq
        client = tq.get_client()
        if not client or not client._ok:
            _cache.update({"data": result, "ts": now})
            return result

        # ── 实时行情（三大指数，用正确市场编号）─────────
        qs = client.get_index_quotes(_INDEX_MARKET_PAIRS)
        indices = {}
        if qs:
            for q in qs:
                code = q.get("code", "")
                name = _INDEX_MAP.get(code, "")
                if name:
                    indices[name] = {
                        "code":   code,
                        "price":  round(q.get("price", 0), 2),
                        "change": round(q.get("change_pct", 0), 2),
                        "vol":    q.get("vol", 0),
                    }
        result["indices"] = indices

        # ── K 线趋势分析（上证为主指标）────────────────
        sh_df = _get_index_bars("000001", 60)
        sz_df = _get_index_bars("399001", 40)
        cy_df = _get_index_bars("399006", 40)

        sh_score = _score_index(sh_df) if sh_df is not None else 20
        sz_score = _score_index(sz_df) if sz_df is not None else 20
        cy_score = _score_index(cy_df) if cy_df is not None else 20

        # 三指数综合（上证权重 0.5，深证 0.3，创业 0.2）
        combined = sh_score * 0.5 + sz_score * 0.3 + cy_score * 0.2
        # 映射到 0-100
        strength = int(combined / 40 * 100)
        strength = max(0, min(100, strength))

        # ── 20 日收益率（上证）────────────────────────
        sh_ret20 = 0.0
        if sh_df is not None and len(sh_df) >= 21:
            close = sh_df["close"]
            sh_ret20 = round((float(close.iloc[-1]) - float(close.iloc[-21]))
                             / float(close.iloc[-21]) * 100, 2)
        result["sh_ret20"] = sh_ret20

        # 实时涨跌加权修正
        changes = [v.get("change", 0) for v in indices.values()]
        avg_chg = sum(changes) / len(changes) if changes else 0
        if   avg_chg > 1:  strength = min(100, strength + 5)
        elif avg_chg < -1: strength = max(0,   strength - 5)

        # ── 市场判断 ────────────────────────────────
        if strength >= 70:
            trend = "牛市"
            advice = "积极做多，可配置优质成长股与动量标的"
            position_guidance = "重仓（60-80%）"
        elif strength >= 55:
            trend = "震荡偏多"
            advice = "波段操作为主，精选高弹性标的，严格止损"
            position_guidance = "半仓（40-60%）"
        elif strength >= 45:
            trend = "震荡"
            advice = "谨慎参与，以防守为主，减少频繁操作"
            position_guidance = "轻仓（20-40%）"
        elif strength >= 30:
            trend = "震荡偏弱"
            advice = "大幅减仓，以现金为主，等待趋势明朗"
            position_guidance = "极轻仓（10-20%）"
        else:
            trend = "熊市"
            advice = "空仓观望，坚决止损，等待市场底部确认"
            position_guidance = "空仓（0-10%）"

        # ── 一句话描述 ────────────────────────────────
        sh_name = "上证" if "上证指数" in indices else ""
        sh_chg  = indices.get("上证指数", {}).get("change", 0)
        ctx_parts = [
            f"大盘强度 {strength}/100",
            f"上证 {sh_ret20:+.1f}%（近20日）" if sh_df is not None else "",
            f"今日 {avg_chg:+.2f}%",
        ]
        context = " | ".join(p for p in ctx_parts if p)

        result.update({
            "trend":             trend,
            "strength":          strength,
            "context":           context,
            "advice":            advice,
            "position_guidance": position_guidance,
        })

    except Exception as e:
        logger.warning(f"市场环境分析异常: {e}")

    _cache.update({"data": result, "ts": now})
    return result


def get_market_summary_text() -> str:
    """返回适合注入 LLM 提示词的市场背景摘要文本"""
    ctx = get_market_regime()
    indices_text = ""
    for name, info in ctx.get("indices", {}).items():
        chg = info.get("change", 0)
        arrow = "↑" if chg > 0 else ("↓" if chg < 0 else "→")
        indices_text += f"  {name}: {info.get('price', '-')} {arrow}{abs(chg):.2f}%\n"

    return f"""【当前市场环境】
市场趋势：{ctx['trend']}（强度 {ctx['strength']}/100）
市场概况：{ctx['context']}
仓位建议：{ctx['position_guidance']}
操作策略：{ctx['advice']}
三大指数（今日）：
{indices_text if indices_text else '  暂无数据'}"""
