# -*- coding: utf-8 -*-
"""
AI 多智能体分析引擎 v2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
架构融合三个开源框架的精华：

  TradingAgents (TauricResearch)：
    • 分层智能体架构（分析师→研究员→风险→决策）
    • 多空辩证研究机制（bullish/bearish 两方辩论）

  AI-Hedge-Fund (virattt)：
    • 投资大师视角（价值派/成长派/趋势派/逆向派）
    • 结构化信号输出（STRONG_BUY / BUY / HOLD / WAIT / SELL）

  Daily-Stock-Analysis (ZhuLinsen)：
    • 六维评分体系（100分制，强烈买入≥75）
    • 决策仪表盘（分数+入场+止损+目标+催化剂+风险）
    • 市场背景注入（大盘趋势影响个股建议）

智能体流水线（6级）：
  Level 1: 量化评分师  — 6维技术评分（本地计算，无需LLM）
  Level 2: 技术分析师  — 形态/均线/指标综合判断
  Level 3: 多方研究员  — 看多论据
  Level 4: 空方研究员  — 看空论据与风险
  Level 5: 风险管理师  — 仓位/止损建议
  Level 6: 决策官      — 融合四大投资风格，给出最终结论
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import logging
import math
import os
import re
from pathlib import Path

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).parent / "data" / "ai_config.json"

DEFAULT_CONFIG = {
    "provider": "deepseek",
    "api_key":  "",
    "base_url": "https://api.deepseek.com/v1",
    "model":    "deepseek-chat",
    "enabled":  False,
}

PROVIDER_PRESETS = {
    "deepseek":  {"base_url": "https://api.deepseek.com/v1",             "model": "deepseek-chat"},
    "openai":    {"base_url": "https://api.openai.com/v1",               "model": "gpt-4o-mini"},
    "anthropic": {"base_url": "https://api.anthropic.com/v1",            "model": "claude-3-5-haiku-20241022"},
    "qwen":      {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "zhipu":     {"base_url": "https://open.bigmodel.cn/api/paas/v4",    "model": "glm-4-flash"},
    "nvidia":    {"base_url": "https://integrate.api.nvidia.com/v1",     "model": "nvidia/llama-3.1-nemotron-70b-instruct"},
    "custom":    {"base_url": "",                                         "model": ""},
}


def load_config() -> dict:
    """
    加载 AI 配置，优先级：
      1. 环境变量（OPENAI_API_KEY / DEEPSEEK_API_KEY / AI_API_KEY / AI_BASE_URL / AI_MODEL）
      2. data/ai_config.json（本地持久配置）
      3. DEFAULT_CONFIG 默认值
    """
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass

    # 环境变量覆盖（安全优先：env var > json file）
    env_key = (
        os.environ.get("AI_API_KEY") or
        os.environ.get("DEEPSEEK_API_KEY") or
        os.environ.get("OPENAI_API_KEY") or
        os.environ.get("ANTHROPIC_API_KEY") or
        ""
    )
    if env_key:
        cfg["api_key"] = env_key
    if os.environ.get("AI_BASE_URL"):
        cfg["base_url"] = os.environ["AI_BASE_URL"]
    if os.environ.get("AI_MODEL"):
        cfg["model"] = os.environ["AI_MODEL"]
    if os.environ.get("AI_PROVIDER"):
        cfg["provider"] = os.environ["AI_PROVIDER"]

    return cfg


def save_config(cfg: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ══════════════════════════════════════════════════════
# 技术指标计算（供 LLM 上下文使用）
# ══════════════════════════════════════════════════════
def _calc_indicators(df: pd.DataFrame) -> dict:
    close  = df["close"]
    volume = df["volume"]
    n = len(df)

    def sv(s, idx=-1):
        try:
            v = float(s.iloc[idx])
            return round(v, 4) if not math.isnan(v) else None
        except Exception:
            return None

    ma5   = close.rolling(5).mean()
    ma10  = close.rolling(10).mean()
    ma20  = close.rolling(20).mean()
    ma60  = close.rolling(60).mean() if n >= 60 else pd.Series([None]*n)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif   = ema12 - ema26
    dea   = dif.ewm(span=9, adjust=False).mean()
    macd  = (dif - dea) * 2

    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, float("nan"))
    rsi   = 100 - 100 / (1 + rs)

    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_up  = bb_mid + 2 * bb_std
    bb_dn  = bb_mid - 2 * bb_std

    avg_vol   = volume.rolling(20).mean()
    vol_ratio = volume.iloc[-1] / avg_vol.iloc[-1] if avg_vol.iloc[-1] > 0 else 1.0

    chg5d  = (close.iloc[-1] / close.iloc[-6]  - 1) * 100 if n >= 6  else 0
    chg20d = (close.iloc[-1] / close.iloc[-21] - 1) * 100 if n >= 21 else 0

    return {
        "price": sv(close), "ma5": sv(ma5), "ma10": sv(ma10),
        "ma20": sv(ma20), "ma60": sv(ma60),
        "macd_dif": sv(dif), "macd_dea": sv(dea), "macd_hist": sv(macd),
        "rsi14": sv(rsi),
        "bb_upper": sv(bb_up), "bb_mid": sv(bb_mid), "bb_lower": sv(bb_dn),
        "volume": int(volume.iloc[-1]),
        "vol_ratio": round(float(vol_ratio), 2),
        "chg_5d_pct": round(chg5d, 2),
        "chg_20d_pct": round(chg20d, 2),
        "high_52w": round(float(close.rolling(252).max().iloc[-1]), 2) if n >= 60 else None,
        "low_52w":  round(float(close.rolling(252).min().iloc[-1]), 2) if n >= 60 else None,
    }


def _price_history_text(df: pd.DataFrame, days: int = 15) -> str:
    rows = df.tail(days)[["date", "open", "close", "high", "low", "volume"]].copy()
    rows["date"] = rows["date"].astype(str).str[:10]
    lines = ["日期         开盘    收盘    最高    最低     成交量"]
    for _, r in rows.iterrows():
        lines.append(f"{r['date']}  {r['open']:.2f}  {r['close']:.2f}  "
                     f"{r['high']:.2f}  {r['low']:.2f}  {int(r['volume']):>10,}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════
# LLM 调用封装
# ══════════════════════════════════════════════════════
def _call_llm(cfg: dict, messages: list, temperature: float = 0.3,
              max_tokens: int = 1200, timeout: int = 180) -> str:
    """
    timeout 默认 180 秒。大模型（kimi-k2.6 等）生成长篇分析需要较长时间，
    60 秒往往不够，调高到 180 秒避免 "read operation timed out" 报错。
    """
    try:
        import urllib.request

        provider = cfg.get("provider", "deepseek")
        base_url = cfg.get("base_url", "").rstrip("/")
        api_key  = cfg.get("api_key", "")
        model    = cfg.get("model", "deepseek-chat")

        if provider == "anthropic":
            return _call_anthropic(cfg, messages, temperature, max_tokens, timeout)

        url  = f"{base_url}/chat/completions"
        body = json.dumps({
            "model": model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens,
        }).encode("utf-8")

        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="ignore")
                err_obj  = json.loads(err_body)
                api_msg  = err_obj.get("error", {})
                if isinstance(api_msg, dict):
                    api_msg = api_msg.get("message", str(api_msg))
                raise RuntimeError(f"HTTP {e.code} — {api_msg}")
            except (json.JSONDecodeError, AttributeError):
                raise RuntimeError(f"HTTP {e.code} {e.reason}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"网络错误: {e.reason}（请检查 Base URL 是否正确）")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"LLM 调用失败: {e}")


def _call_anthropic(cfg, messages, temperature, max_tokens, timeout: int = 180) -> str:
    import urllib.request
    system_msg, conv_msgs = "", []
    for m in messages:
        if m["role"] == "system":
            system_msg = m["content"]
        else:
            conv_msgs.append(m)
    body = json.dumps({
        "model": cfg.get("model", "claude-3-5-haiku-20241022"),
        "max_tokens": max_tokens, "temperature": temperature,
        "system": system_msg, "messages": conv_msgs,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json",
                 "x-api-key": cfg.get("api_key", ""),
                 "anthropic-version": "2023-06-01"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data["content"][0]["text"].strip()


# ══════════════════════════════════════════════════════
# 智能体系统提示词（六级流水线）
# ══════════════════════════════════════════════════════

_LANG = "\n\n【语言要求】所有输出必须使用中文，严禁使用英文或其他任何语言。"

SYSTEM_TECH = """你是一位专业A股技术分析师，精通K线形态、均线体系、量价关系。
基于提供的量化评分和K线数据，分析：
1. 当前价格形态（趋势/整理/突破/回调）
2. 关键支撑位和阻力位（精确到小数点后两位）
3. MACD/RSI/布林带综合信号
4. 成交量配合度（放量突破/缩量回调/量价背离）
5. 均线排列状态及趋势强度

要求：客观、数据驱动、避免主观臆断，300字以内。""" + _LANG

SYSTEM_BULL = """你是一位A股多方研究员，职责是发现并论证股票的看多理由。
基于技术分析报告，梳理所有支持买入的证据：
1. 技术面利好（均线/量价/形态）
2. 相对强度（与大盘/同行业对比）
3. 潜在催化剂和突破动力
4. 最佳入场时机和价位

要求：聚焦积极因素，逻辑清晰，200字以内。""" + _LANG

SYSTEM_BEAR = """你是一位A股空方研究员，职责是发现并论证股票的看空风险。
基于技术分析报告，梳理所有需要警惕的风险：
1. 技术面隐患（均线压制/量价背离/形态风险）
2. 止损破位条件
3. 最坏情形（下方支撑、可能跌幅）
4. 什么情况下应该放弃该标的

要求：严格客观揭示风险，不回避矛盾，200字以内。""" + _LANG

SYSTEM_RISK = """你是一位A股风险管理专家，专注仓位管理和本金保护。
综合技术分析、多空研究，给出：
1. 风险等级：低/中/高/极高（必须明确）
2. 精确止损位（支撑跌破点）
3. 建议仓位：
   - 极高风险：不买（空仓）
   - 高风险：轻仓5-15%
   - 中风险：半仓20-35%
   - 低风险：正常仓位35-50%
4. 最大亏损容忍度分析

要求：保守优先，明确数字，150字以内。""" + _LANG

SYSTEM_DECISION = """你是一位融合四大投资风格的首席交易决策官。

四大投资视角（参考全球顶级投资大师思想）：
【价值派（格雷厄姆/巴菲特）】安全边际、低估值、长期持有
【成长派（彼得·林奇）】业绩加速、PEG合理、赛道景气
【趋势派（斯坦利·德鲁肯米勒）】顺势而为、动量确认、市场共识
【逆向派（塔勒布/邓普顿）】极端恐惧/贪婪、尾部风险、逆向布局

请综合六维评分、技术分析、多空研究、风险评估和市场背景，给出：
1. 各投资视角的判断（各一句话）
2. 综合交易建议：【强烈买入/买入/观望/减仓/卖出】（必须明确）
3. 置信度：0-100分
4. 目标价位（T1近期目标、T2中期目标）
5. 止损执行价
6. 操作检查清单（3-5条）
7. 核心风险提示

最后必须输出 JSON 摘要（```json 包裹）：
```json
{
  "action": "买入",
  "confidence": 72,
  "target1": 12.50,
  "target2": 13.80,
  "stop_loss": 10.20,
  "position": "半仓30%",
  "summary": "一句话核心理由",
  "checklist": ["检查1", "检查2", "检查3"],
  "catalysts": ["催化剂1"],
  "risks": ["风险1"]
}
```""" + _LANG


# ══════════════════════════════════════════════════════
# 主分析函数
# ══════════════════════════════════════════════════════
def run_analysis(code: str, name: str, df: pd.DataFrame) -> dict:
    """
    运行六级多智能体分析流水线。
    Level 1 (量化评分) 始终运行，无需 LLM。
    Level 2-6 需要 AI 配置。
    """
    # ── Level 1: 量化评分（始终运行）─────────────────
    from stock_scorer import score_stock
    score_result = score_stock(df)

    # ── 市场背景获取 ────────────────────────────────
    market_ctx_text = ""
    market_ctx_data = {}
    try:
        from market_context import get_market_regime, get_market_summary_text
        market_ctx_data = get_market_regime()
        market_ctx_text = get_market_summary_text()
    except Exception:
        pass

    # 无 LLM 时仅返回评分结果
    cfg = load_config()
    if not cfg.get("enabled") or not cfg.get("api_key"):
        return {
            "ok":             True,
            "code":           code,
            "name":           name,
            "score":          score_result,
            "market_context": market_ctx_data,
            "ai_enabled":     False,
            "message":        "量化评分已完成。启用AI可获得深度多智能体分析。",
        }

    if df is None or df.empty or len(df) < 30:
        return {"ok": False, "error": f"{code} 数据不足（需≥30条），请先下载历史数据"}

    try:
        ind  = _calc_indicators(df)
        hist = _price_history_text(df, days=15)

        # ── 构建股票上下文（含六维评分）───────────────
        score_text = ""
        if score_result.get("ok"):
            s = score_result
            dims = s.get("dimensions", {})
            score_text = f"""
【六维量化评分】总分: {s['total']}/100  信号: {s['signal']}
  趋势评估: {dims.get('trend',{}).get('score',0)}/{dims.get('trend',{}).get('max',30)}分  {dims.get('trend',{}).get('detail','')}
  偏离分析: {dims.get('deviation',{}).get('score',0)}/{dims.get('deviation',{}).get('max',20)}分  {dims.get('deviation',{}).get('detail','')}
  量能评估: {dims.get('volume',{}).get('score',0)}/{dims.get('volume',{}).get('max',15)}分  {dims.get('volume',{}).get('detail','')}
  支撑验证: {dims.get('support',{}).get('score',0)}/{dims.get('support',{}).get('max',10)}分  {dims.get('support',{}).get('detail','')}
  MACD确认: {dims.get('macd',{}).get('score',0)}/{dims.get('macd',{}).get('max',15)}分  {dims.get('macd',{}).get('detail','')}
  RSI极值:  {dims.get('rsi',{}).get('score',0)}/{dims.get('rsi',{}).get('max',10)}分  {dims.get('rsi',{}).get('detail','')}
建议入场: {s.get('entry_price')}  止损: {s.get('stop_loss')}  目标T1: {s.get('target1')}  T2: {s.get('target2')}"""

        stock_ctx = f"""
═══════════════════════════════════════
股票：{code} {name}
当前价：{ind['price']}  |  5日涨跌：{ind['chg_5d_pct']}%  |  20日涨跌：{ind['chg_20d_pct']}%
═══════════════════════════════════════
【技术指标】
均线：MA5={ind['ma5']}  MA10={ind['ma10']}  MA20={ind['ma20']}  MA60={ind['ma60']}
MACD：DIF={ind['macd_dif']}  DEA={ind['macd_dea']}  柱={ind['macd_hist']}
RSI14：{ind['rsi14']}   布林带：上={ind['bb_upper']}  中={ind['bb_mid']}  下={ind['bb_lower']}
成交量比：{ind['vol_ratio']}x（相对20日均量）
52周高低：最高={ind['high_52w']}  最低={ind['low_52w']}
{score_text}
{market_ctx_text}
【近15日K线】
{hist}
═══════════════════════════════════════"""

        logs = []

        # ── Level 2: 技术分析师 ─────────────────────
        logs.append({"agent": "📊 技术分析师", "status": "running"})
        tech_report = _call_llm(cfg, [
            {"role": "system", "content": SYSTEM_TECH},
            {"role": "user",   "content": f"请对以下股票进行技术面分析（用中文回答）：\n{stock_ctx}"},
        ], max_tokens=600)
        logs[-1].update({"status": "done", "output": tech_report})

        # ── Level 3: 多方研究员 ─────────────────────
        logs.append({"agent": "📈 多方研究员", "status": "running"})
        bull_report = _call_llm(cfg, [
            {"role": "system", "content": SYSTEM_BULL},
            {"role": "user",   "content": f"股票信息：\n{stock_ctx}\n\n技术分析：\n{tech_report}\n\n请用中文论证看多理由。"},
        ], max_tokens=400)
        logs[-1].update({"status": "done", "output": bull_report})

        # ── Level 4: 空方研究员 ─────────────────────
        logs.append({"agent": "📉 空方研究员", "status": "running"})
        bear_report = _call_llm(cfg, [
            {"role": "system", "content": SYSTEM_BEAR},
            {"role": "user",   "content": f"股票信息：\n{stock_ctx}\n\n技术分析：\n{tech_report}\n\n请用中文论证看空风险。"},
        ], max_tokens=400)
        logs[-1].update({"status": "done", "output": bear_report})

        # ── Level 5: 风险管理师 ─────────────────────
        logs.append({"agent": "🛡 风险管理师", "status": "running"})
        risk_report = _call_llm(cfg, [
            {"role": "system", "content": SYSTEM_RISK},
            {"role": "user",   "content": (
                f"股票信息：\n{stock_ctx}\n\n"
                f"多方观点：\n{bull_report}\n\n"
                f"空方观点：\n{bear_report}\n\n"
                "请用中文给出风险评估和仓位建议。"
            )},
        ], max_tokens=300)
        logs[-1].update({"status": "done", "output": risk_report})

        # ── Level 6: 首席决策官 ─────────────────────
        logs.append({"agent": "🎯 首席决策官", "status": "running"})
        final_report = _call_llm(cfg, [
            {"role": "system", "content": SYSTEM_DECISION},
            {"role": "user",   "content": (
                f"股票：{code} {name}\n"
                f"量化评分：{score_result.get('total', '-')}/100  信号：{score_result.get('signal', '-')}\n\n"
                f"技术分析：\n{tech_report}\n\n"
                f"多方研究：\n{bull_report}\n\n"
                f"空方研究：\n{bear_report}\n\n"
                f"风险评估：\n{risk_report}\n\n"
                f"市场背景：{market_ctx_data.get('trend', '-')} 强度{market_ctx_data.get('strength', '-')}/100\n\n"
                "请用中文给出最终综合决策，JSON摘要中的字符串值也必须是中文。"
            )},
        ], temperature=0.2, max_tokens=1000)
        logs[-1].update({"status": "done", "output": final_report})

        # ── 解析 JSON 摘要 ───────────────────────────
        summary = {}
        try:
            m = re.search(r"```json\s*(\{.*?\})\s*```", final_report, re.DOTALL)
            if m:
                summary = json.loads(m.group(1))
        except Exception:
            pass

        # 如果 summary 中有止损，更新评分结果
        if summary.get("stop_loss"):
            score_result["stop_loss"] = summary["stop_loss"]
        if summary.get("target1"):
            score_result["target1"] = summary["target1"]
        if summary.get("target2"):
            score_result["target2"] = summary["target2"]

        return {
            "ok":             True,
            "code":           code,
            "name":           name,
            "ai_enabled":     True,
            "score":          score_result,
            "market_context": market_ctx_data,
            "indicators":     ind,
            "tech_report":    tech_report,
            "bull_report":    bull_report,
            "bear_report":    bear_report,
            "risk_report":    risk_report,
            "final_report":   final_report,
            "summary":        summary,
            "agents":         logs,
        }

    except Exception as e:
        err_msg = str(e)
        logger.error(f"AI分析失败 {code}: {err_msg}")
        # LLM 失败时仍返回量化评分，同时告知用户具体错误原因
        return {
            "ok":             True,
            "code":           code,
            "name":           name,
            "ai_enabled":     True,   # 已启用但调用失败，前端显示错误而非"未启用"
            "ai_error":       err_msg,
            "score":          score_result,
            "market_context": market_ctx_data,
            "message":        f"AI接口调用失败：{err_msg}",
        }


# ══════════════════════════════════════════════════════
# AI 策略生成（原功能保留，提示词增强）
# ══════════════════════════════════════════════════════

SYSTEM_STRATEGY_GEN = """你是一位资深A股量化策略工程师，融合了以下三个框架的最佳实践：
1. TradingAgents: 分层信号过滤，先大盘后个股
2. AI-Hedge-Fund: 多因子综合，控制回撤
3. Daily-Stock-Analysis: 六维评分纪律（趋势+偏离+量能+支撑+MACD+RSI）

【代码规范】
1. 文件第一行：# META: {"name": "策略名称", "desc": "描述", "author": "AI生成", "builtin": false}
2. 必须实现 scan(codes, get_kline, get_quotes) → list[dict]
   - codes: 股票代码列表
   - get_kline(code, days) → DataFrame [date/open/close/high/low/volume]
   - get_quotes(codes) → DataFrame [代码/名称/最新价/涨跌幅/昨收/量比]
   - 返回每项：{code, name, price, change, score, reason}
3. 只用 pandas/numpy，不引入外部库
4. 代码清晰有注释，包含入场纪律（偏离MA5不超过5%）和风险控制

直接输出 Python 代码，不加任何解释或 markdown 包裹。"""


def generate_strategy(description: str) -> dict:
    cfg = load_config()
    if not cfg.get("enabled") or not cfg.get("api_key"):
        return {"ok": False, "error": "AI功能未启用，请在「AI设置」中填写API Key并启用"}

    try:
        code = _call_llm(cfg, [
            {"role": "system", "content": SYSTEM_STRATEGY_GEN},
            {"role": "user",   "content": f"请根据以下描述生成A股选股策略：\n\n{description}"},
        ], temperature=0.4, max_tokens=2000)

        code = re.sub(r"^```(?:python)?\s*\n?", "", code.strip())
        code = re.sub(r"\n?```\s*$", "", code.strip())

        if "# META:" not in code:
            code = ('# META: {"name": "AI策略", "desc": "' + description[:40]
                    + '", "author": "AI生成", "builtin": false}\n\n' + code)

        return {"ok": True, "code": code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


SYSTEM_FACTOR_GEN = """你是一位资深A股量化因子工程师，专注于 AlphaLens 风格的单因子研究。

【因子文件规范】
1. 文件第一行：# FACTOR: {"name": "因子名称", "desc": "描述", "category": "动量|反转|波动|量价|趋势|质量|综合", "period": N, "builtin": false}
2. 必须实现 scan(codes, get_kline, get_quotes) → list[dict]
   - codes: 股票代码列表
   - get_kline(code, days) → DataFrame [date/open/close/high/low/volume]
   - get_quotes(codes) → DataFrame [代码/名称/最新价/涨跌幅/昨收/成交量/量比]
   - 返回每项：{code, name, price, change, score, reason}
3. 设计原则：
   - 因子逻辑清晰、单一（不要把多个因子混在一起）
   - score 越高表示信号越强（做多方向）
   - 合理使用 get_quotes(codes) 批量获取行情（性能关键：建议传入 [code] 单个查询，因子管理器已有缓存）
   - 设置合理的过滤条件（价格>3元、流动性、非ST）
   - 结果限制 top 50，按 score 降序
4. 只用 pandas/numpy/math，不引入外部库
5. 代码清晰有注释

直接输出 Python 代码，不加任何解释或 markdown 包裹。"""


def generate_factor(description: str) -> dict:
    """根据自然语言描述，用 AI 生成因子 Python 代码"""
    cfg = load_config()
    if not cfg.get("enabled") or not cfg.get("api_key"):
        return {"ok": False, "error": "AI功能未启用，请在「AI设置」中填写API Key并启用"}

    try:
        code = _call_llm(cfg, [
            {"role": "system", "content": SYSTEM_FACTOR_GEN},
            {"role": "user",   "content": f"请根据以下描述生成A股选股因子：\n\n{description}"},
        ], temperature=0.4, max_tokens=2000)

        code = re.sub(r"^```(?:python)?\s*\n?", "", code.strip())
        code = re.sub(r"\n?```\s*$", "", code.strip())

        if "# FACTOR:" not in code:
            code = ('# FACTOR: {"name": "AI因子", "desc": "' + description[:40]
                    + '", "category": "综合", "period": 20, "builtin": false}\n\n' + code)

        return {"ok": True, "code": code}
    except Exception as e:
        return {"ok": False, "error": str(e)}
