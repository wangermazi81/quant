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
import time as _time
from pathlib import Path

import pandas as pd
import numpy as np


def _json_safe(obj):
    """递归把 dict / list 中的 NaN / Inf 替换为 None。

    背景：pandas rolling/ewm 在数据不足时返回 NaN（如 close.rolling(252) 对 120 行数据
    返回 NaN）。Python json.dumps 默认输出字面量 NaN，但浏览器 JSON.parse 不允许
    （SyntaxError: Unexpected token 'N'），导致前端 fetch 抛错、UI 显示"分析失败"。

    所有对外返回的 dict 都应过这个函数。
    """
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    # numpy 浮点也要处理（np.float64 不是 Python float 的子类）
    if isinstance(obj, np.floating):
        v = float(obj)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    if isinstance(obj, np.integer):
        return int(obj)
    return obj

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
# 决策稳定性缓存（同一股票10分钟内重复分析时对比前次结论）
# ══════════════════════════════════════════════════════
_DECISION_CACHE: dict = {}   # code -> {"ts": float, "action": str, "confidence": int}
_DECISION_TTL = 600           # 10分钟


def _update_decision_cache(code: str, action: str, confidence: int):
    _DECISION_CACHE[code] = {"ts": _time.time(), "action": action, "confidence": confidence}


def _check_decision_stability(code: str, new_action: str, new_confidence: int) -> dict:
    """
    与缓存中的上次决策对比，检测不稳定性。
    返回: {"stable": bool, "prev_action": str, "warn": str}
    """
    prev = _DECISION_CACHE.get(code)
    if not prev or (_time.time() - prev["ts"]) > _DECISION_TTL:
        return {"stable": True, "prev_action": None, "warn": ""}

    pa = prev.get("action", "")
    # 定义相反方向
    bullish = {"强烈买入", "买入"}
    bearish = {"卖出", "减仓"}
    if pa and new_action:
        pa_bull = pa in bullish
        na_bull = new_action in bullish
        pa_bear = pa in bearish
        na_bear = new_action in bearish
        if (pa_bull and na_bear) or (pa_bear and na_bull):
            age = int(_time.time() - prev["ts"])
            warn = (f"⚠ 决策稳定性警告：{age}秒前分析结论为【{pa}】，"
                    f"本次为【{new_action}】，方向相反，请结合实际行情谨慎操作")
            return {"stable": False, "prev_action": pa, "warn": warn}

    return {"stable": True, "prev_action": pa, "warn": ""}


# ══════════════════════════════════════════════════════
# 健壮 JSON 解析（多种回退策略）
# ══════════════════════════════════════════════════════
def _safe_parse_json(text: str) -> dict:
    """
    从 LLM 输出文本中提取 JSON 对象，支持多种格式：
    - ```json {...} ```
    - { ... }（裸 JSON）
    - 不完整 JSON（尝试自动修复）
    """
    if not text:
        return {}

    # 1. 尝试 ```json ... ``` 代码块
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass

    # 2. 尝试任意 { ... } 块（贪婪，最长匹配）
    m = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass

    # 3. 宽松提取（允许尾部截断 — 补全缺失的 }）
    start = text.find("{")
    if start >= 0:
        snippet = text[start:]
        # 尝试逐步截短，找到可解析的前缀
        for end in range(len(snippet), max(0, len(snippet)-50), -1):
            try:
                candidate = snippet[:end]
                # 补充缺失的 }
                opens  = candidate.count("{") - candidate.count("}")
                if opens > 0:
                    candidate += "}" * opens
                return json.loads(candidate)
            except Exception:
                continue

    return {}

CONFIG_FILE = Path(__file__).parent.parent / "data" / "ai_config.json"

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
    "xiaomimimo":{"base_url": "https://token-plan-cn.xiaomimimo.com/v1", "model": "mimo-v2.5-pro"},
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
    #
    # 修复：跳过 .env.example 风格的占位符。
    # 历史问题：server_init.sh 自动 `cp .env.example .env` 时会带入
    #   AI_API_KEY=nvapi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 这种 placeholder，
    # 旧逻辑见非空就覆盖，导致网页里配的真 key 被假 key 顶掉，调用 API 必失败。
    # 现在：env 值含 'xxxxx' / 'your-' / '请替换' 等占位符标记时当未设置处理。
    env_key = (
        os.environ.get("AI_API_KEY") or
        os.environ.get("DEEPSEEK_API_KEY") or
        os.environ.get("OPENAI_API_KEY") or
        os.environ.get("ANTHROPIC_API_KEY") or
        ""
    )
    # 仅当 env_key 是真实有效值（非占位符）时，整套 env 配置才覆盖 json 配置
    if env_key and not _is_env_placeholder(env_key):
        cfg["api_key"] = env_key
        if os.environ.get("AI_BASE_URL") and not _is_env_placeholder(os.environ["AI_BASE_URL"]):
            cfg["base_url"] = os.environ["AI_BASE_URL"]
        if os.environ.get("AI_MODEL") and not _is_env_placeholder(os.environ["AI_MODEL"]):
            cfg["model"] = os.environ["AI_MODEL"]
        if os.environ.get("AI_PROVIDER") and not _is_env_placeholder(os.environ["AI_PROVIDER"]):
            cfg["provider"] = os.environ["AI_PROVIDER"]

    return cfg


def _is_env_placeholder(s: str) -> bool:
    """
    检测字符串是否为 .env.example 风格的占位符。
    例如：
      'nvapi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'  → True（含连续 xxxxx）
      'your-api-key-here'                       → True
      '请替换为真实 key'                          → True
      'sk-proj-abc123def456...'                  → False（看着像真 key）
    """
    if not isinstance(s, str):
        return True
    s_low = s.strip().lower()
    if not s_low or len(s_low) < 8:
        return True
    markers = (
        'xxxxx',          # 连续 5 个 x 是 example 文件常用占位
        'your-', 'your_',
        'placeholder',
        '请替换', '请填',
        '<your',
        'example.com',
        'changeme', 'change-me',
        'todo', 'fixme',
    )
    return any(m in s_low for m in markers)


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
        # 部分国产 API（小米MiMo等）不接受 temperature=0，强制最小值 0.01
        safe_temp = max(0.01, float(temperature))
        # 部分 API 要求 max_tokens 至少 16
        safe_tokens = max(16, int(max_tokens))
        body = json.dumps({
            "model": model, "messages": messages,
            "temperature": safe_temp, "max_tokens": safe_tokens,
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
                try:
                    err_obj = json.loads(err_body)
                    api_msg = err_obj.get("error", {})
                    if isinstance(api_msg, dict):
                        api_msg = api_msg.get("message", str(api_msg))
                    elif not api_msg:
                        api_msg = err_body  # 直接显示原始body
                except json.JSONDecodeError:
                    api_msg = err_body or e.reason
                raise RuntimeError(f"HTTP {e.code} — {api_msg}")
            except RuntimeError:
                raise
            except Exception:
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
def run_analysis(code: str, name: str, df: pd.DataFrame,
                 config_override: dict = None) -> dict:
    """
    运行六级多智能体分析流水线。
    Level 1 (量化评分) 始终运行，无需 LLM。
    Level 2-6 需要 AI 配置。
    config_override: 传入则覆盖全局 AI 配置（用于用户自己的 API Key）。
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
    cfg = config_override if config_override else load_config()
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

        # ── 新闻情绪获取（优先 search_manager，含名称以提升搜索质量）──
        news_data = {}
        news_ctx_text = ""
        try:
            import news_fetcher as _nf
            # 传入股票名称，让 Tavily/SerpAPI 搜索更精准
            news_list = _nf.get_stock_news(code, limit=6, name=name)
            sentiment = _nf._simple_sentiment(news_list)
            context   = _nf.news_to_context_text(news_list, code)
            news_data = {"news": news_list, "sentiment": sentiment, "context_text": context}
            news_ctx_text = context
            if news_ctx_text:
                sent = sentiment
                logger.info(f"[AI] 新闻情绪 {code}: {sent.get('label','?')} score={sent.get('score',0)} ({len(news_list)}条)")
        except Exception as _ne:
            logger.debug(f"[AI] 新闻获取跳过: {_ne}")

        # 将新闻情绪注入 stock_ctx
        if news_ctx_text:
            sent = news_data.get("sentiment", {})
            news_section = f"\n{news_ctx_text}\n【新闻情绪】{sent.get('label','中性')} (得分:{sent.get('score',0):+.2f})"
            stock_ctx = stock_ctx.rstrip("═" * 39).rstrip() + news_section + "\n" + "═" * 39

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

        # ── 健壮 JSON 解析（多回退策略）───────────────
        summary = _safe_parse_json(final_report)

        # JSON 校验：确保关键字段类型正确
        if summary:
            for num_field in ("confidence", "target1", "target2", "stop_loss"):
                v = summary.get(num_field)
                if v is not None:
                    try:
                        summary[num_field] = float(v)
                    except (ValueError, TypeError):
                        summary.pop(num_field, None)
            # action 字段标准化
            action_raw = str(summary.get("action", "")).strip()
            valid_actions = {"强烈买入", "买入", "观望", "减仓", "卖出"}
            if action_raw and action_raw not in valid_actions:
                # 模糊匹配
                for va in valid_actions:
                    if va in action_raw or action_raw in va:
                        summary["action"] = va
                        break
                else:
                    logger.debug(f"[AI] 未识别的 action 值: {action_raw}")

        # 如果 summary 中有止损，更新评分结果
        if summary.get("stop_loss"):
            score_result["stop_loss"] = summary["stop_loss"]
        if summary.get("target1"):
            score_result["target1"] = summary["target1"]
        if summary.get("target2"):
            score_result["target2"] = summary["target2"]

        # ── 决策稳定性检查 ───────────────────────────
        final_action = summary.get("action", "")
        final_conf   = int(summary.get("confidence", 50))
        stability = _check_decision_stability(code, final_action, final_conf)
        _update_decision_cache(code, final_action, final_conf)

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
            "news":           news_data.get("news", []),
            "news_sentiment": news_data.get("sentiment", {}),
            "stability":      stability,
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

SYSTEM_STRATEGY_GEN = """你是一位资深A股量化策略工程师。请严格按照以下规范输出完整的 Python 策略文件。

【输出格式要求 - 极其重要】
- 只输出纯 Python 代码，禁止加任何说明文字、禁止使用 markdown 代码块（不要写```python）
- 第一行必须是 META 注释，之后紧跟完整的 scan() 函数实现
- 不要写"好的"、"以下是代码"等任何自然语言

【完整代码模板 - 必须严格遵守此结构】

# META: {"name": "策略名称", "desc": "策略描述", "author": "AI生成", "builtin": false}
import pandas as pd
import numpy as np

def scan(codes: list, get_kline, get_quotes) -> list:
    \"\"\"
    策略扫描函数
    codes: 股票代码列表
    get_kline(code, days) -> DataFrame[date,open,close,high,low,volume]
    get_quotes(codes) -> DataFrame[代码,名称,最新价,涨跌幅,昨收,成交量,量比]
    返回: list of {code, name, price, change, score, reason}
    \"\"\"
    results = []
    for code in codes:
        try:
            df = get_kline(code, 60)
            if df is None or len(df) < 20:
                continue
            df = df.sort_values('date').reset_index(drop=True)
            close = df['close']
            volume = df['volume']

            # === 在此实现策略逻辑 ===

            # 示例：均线条件
            ma5  = close.rolling(5).mean().iloc[-1]
            ma20 = close.rolling(20).mean().iloc[-1]
            cur_price = close.iloc[-1]

            if cur_price <= 0 or ma5 <= 0 or ma20 <= 0:
                continue

            # 计算得分
            score = round((cur_price / ma5 - 1) * 100, 2)
            reason = f"MA5={ma5:.2f} MA20={ma20:.2f}"

            results.append({
                'code': code, 'name': code,
                'price': cur_price,
                'change': round((close.iloc[-1]/close.iloc[-2]-1)*100, 2) if len(close)>1 else 0,
                'score': score, 'reason': reason
            })
        except Exception:
            continue

    # 按得分降序，最多返回 50 只
    results.sort(key=lambda x: x.get('score', 0), reverse=True)
    return results[:50]

【策略规范】
1. get_quotes 列名：代码/名称/最新价/涨跌幅/昨收/成交量/量比（用中文列名访问）
2. get_kline 返回列：date/open/close/high/low/volume
3. 昨收列：df_q['昨收'] 而不是 last_close
4. 成交量列：df_q['成交量'] 而不是 vol
5. 只用 pandas/numpy/math，禁止 import 其他库
6. 必须有 try/except 防止单只股票报错中断整体扫描
7. 返回列表按 score 降序，最多 50 项

现在请根据用户需求输出完整代码（从 # META 行开始，直到 return results[:50] 结束）："""


def _extract_code(raw: str, meta_key: str, default_meta: str) -> str:
    """从 LLM 输出中提取干净的 Python 代码"""
    text = raw.strip()
    # 去除 markdown 代码块
    text = re.sub(r"^```(?:python)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text.strip())
    # 去除开头的自然语言（到第一个 # 或 import/def 之前）
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('#') or stripped.startswith('import') or stripped.startswith('def '):
            start = i
            break
    text = "\n".join(lines[start:]).strip()
    # 补 META 注释
    if meta_key not in text:
        text = default_meta + "\n\n" + text
    return text


def generate_strategy(description: str, config_override: dict = None) -> dict:
    """根据自然语言描述，用 AI 生成选股策略 Python 代码（含验证+重试）"""
    cfg = config_override if config_override else load_config()
    if not cfg.get("enabled") or not cfg.get("api_key"):
        return {"ok": False, "error": "AI功能未启用，请在「AI设置」中填写API Key并启用"}

    messages = [
        {"role": "system", "content": SYSTEM_STRATEGY_GEN},
        {"role": "user",   "content":
            f"请根据以下A股选股需求，生成完整的 Python 策略代码（从 # META 行开始，不要写任何解释）：\n\n{description}"},
    ]

    last_err = ""
    for attempt in range(2):   # 最多重试 1 次
        try:
            raw = _call_llm(cfg, messages, temperature=0.2, max_tokens=3500, timeout=180)
            meta_default = (
                f'# META: {{"name": "AI策略", "desc": "{description[:40]}", '
                f'"author": "AI生成", "builtin": false}}'
            )
            code = _extract_code(raw, "# META:", meta_default)

            # 验证：必须包含 def scan
            if "def scan" not in code:
                last_err = f"第{attempt+1}次生成的代码缺少 scan() 函数（{len(code)} 字符），正在重试..."
                logger.warning(f"[AI策略] {last_err}")
                # 重试时在消息里加提示
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content":
                    "你的输出缺少 def scan 函数，请重新输出完整代码，"
                    "必须包含 def scan(codes, get_kline, get_quotes) 函数实现。"})
                continue

            return {"ok": True, "code": code, "length": len(code)}
        except Exception as e:
            last_err = str(e)
            logger.error(f"[AI策略] 生成失败(尝试{attempt+1}): {e}")

    return {"ok": False, "error": f"策略生成失败：{last_err}"}


SYSTEM_FACTOR_GEN = """你是一位资深A股量化因子工程师。请严格按照以下规范输出完整的 Python 因子文件。

【输出格式要求 - 极其重要】
- 只输出纯 Python 代码，禁止加任何说明文字，禁止使用 markdown 代码块（不要写```python）
- 第一行必须是 FACTOR 注释，之后紧跟完整的 scan() 函数实现

【完整代码模板】

# FACTOR: {"name": "因子名称", "desc": "描述", "category": "动量", "period": 20, "builtin": false}
import pandas as pd
import numpy as np

def scan(codes: list, get_kline, get_quotes) -> list:
    \"\"\"
    因子扫描函数
    get_kline(code, days) -> DataFrame[date,open,close,high,low,volume]
    get_quotes(codes) -> DataFrame[代码,名称,最新价,涨跌幅,昨收,成交量,量比]
    返回: list of {code, name, price, change, score, reason}  按 score 降序 top50
    \"\"\"
    results = []
    for code in codes:
        try:
            df = get_kline(code, 30)
            if df is None or len(df) < 20:
                continue
            df = df.sort_values('date').reset_index(drop=True)
            close = df['close']
            cur_price = close.iloc[-1]
            if cur_price < 3:
                continue  # 过滤低价股

            # === 在此实现因子计算逻辑 ===
            score = 0.0
            reason = ""

            results.append({
                'code': code, 'name': code,
                'price': cur_price,
                'change': round((close.iloc[-1]/close.iloc[-2]-1)*100, 2) if len(close)>1 else 0,
                'score': score, 'reason': reason
            })
        except Exception:
            continue
    results.sort(key=lambda x: x.get('score', 0), reverse=True)
    return results[:50]

【规范要点】
1. get_quotes 列名用中文：代码/名称/最新价/涨跌幅/昨收/成交量/量比
2. 只用 pandas/numpy/math，禁止引入其他库
3. 每只股票必须 try/except 隔离
4. category 取值：动量/反转/波动/量价/趋势/质量/综合

现在请根据用户需求输出完整代码（从 # FACTOR 行开始）："""


def generate_factor(description: str, config_override: dict = None) -> dict:
    """根据自然语言描述，用 AI 生成因子 Python 代码（含验证+重试）"""
    cfg = config_override if config_override else load_config()
    if not cfg.get("enabled") or not cfg.get("api_key"):
        return {"ok": False, "error": "AI功能未启用，请在「AI设置」中填写API Key并启用"}

    messages = [
        {"role": "system", "content": SYSTEM_FACTOR_GEN},
        {"role": "user",   "content":
            f"请根据以下需求生成完整的 Python 因子代码（从 # FACTOR 行开始，不要写任何解释）：\n\n{description}"},
    ]

    last_err = ""
    for attempt in range(2):
        try:
            raw = _call_llm(cfg, messages, temperature=0.2, max_tokens=3500, timeout=180)
            meta_default = (
                f'# FACTOR: {{"name": "AI因子", "desc": "{description[:40]}", '
                f'"category": "综合", "period": 20, "builtin": false}}'
            )
            code = _extract_code(raw, "# FACTOR:", meta_default)

            if "def scan" not in code:
                last_err = f"第{attempt+1}次生成的代码缺少 scan() 函数，正在重试..."
                logger.warning(f"[AI因子] {last_err}")
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content":
                    "你的输出缺少 def scan 函数，请重新输出完整代码，"
                    "必须包含 def scan(codes, get_kline, get_quotes) 函数实现。"})
                continue

            return {"ok": True, "code": code, "length": len(code)}
        except Exception as e:
            last_err = str(e)
            logger.error(f"[AI因子] 生成失败(尝试{attempt+1}): {e}")

    return {"ok": False, "error": f"因子生成失败：{last_err}"}
