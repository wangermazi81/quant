# -*- coding: utf-8 -*-
"""LLM 调用客户端（A-6）。

从 analytics/ai_analyst.py 抽出来的原因：`_call_llm` 这个**下划线私有函数**
事实上已经是平台级公共 API —— research_center、review_engine、ai_trader、
web/ai 四个模块都在直接调它，却没有任何契约保护。想换供应商、加流式输出，
就得在一个 73KB 的"分析引擎"文件里动刀，还要同时照顾 5 个消费方。

这里只搬**传输与协议层**（供应商预设、模型探测、OpenAI/Anthropic 两种协议、
代理降级、重试、token 用量归一化），不碰任何分析编排逻辑。
ai_analyst 保留同名别名，所有既有调用点与打桩测试原样可用。

保留原有注释：里面记录的 SSL EOF 降级、空响应重试、429/502 退避
都是实测踩出来的坑，不是可有可无的说明。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# 记住上次成功的链路（直连 or 系统代理），下次优先走它
_prefer_direct = False


PROVIDER_PRESETS = {
    # ── 国产直连 ──────────────────────────────────────────────
    "deepseek":   {"name": "DeepSeek 深度求索", "base_url": "https://api.deepseek.com/v1",             "model": "deepseek-chat"},
    "qwen":       {"name": "通义千问（阿里）",   "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "zhipu":      {"name": "智谱 GLM",          "base_url": "https://open.bigmodel.cn/api/paas/v4",    "model": "glm-4-flash"},
    "moonshot":   {"name": "Kimi（月之暗面）",  "base_url": "https://api.moonshot.cn/v1",              "model": "kimi-latest"},
    "doubao":     {"name": "豆包（火山方舟）",  "base_url": "https://ark.cn-beijing.volces.com/api/v3","model": ""},
    "siliconflow":{"name": "硅基流动",          "base_url": "https://api.siliconflow.cn/v1",           "model": "deepseek-ai/DeepSeek-V3"},
    "hunyuan":    {"name": "腾讯混元",          "base_url": "https://api.hunyuan.cloud.tencent.com/v1","model": "hunyuan-turbo"},
    "minimax":    {"name": "MiniMax",          "base_url": "https://api.minimaxi.com/v1",             "model": "MiniMax-Text-01"},
    "baichuan":   {"name": "百川智能",          "base_url": "https://api.baichuan-ai.com/v1",          "model": "Baichuan4-Air"},
    "yi":         {"name": "零一万物",          "base_url": "https://api.lingyiwanwu.com/v1",          "model": "yi-lightning"},
    "stepfun":    {"name": "阶跃星辰",          "base_url": "https://api.stepfun.com/v1",              "model": "step-2-mini"},
    "spark":      {"name": "讯飞星火",          "base_url": "https://spark-api-open.xf-yun.com/v1",    "model": "generalv3.5"},
    "xiaomimimo": {"name": "小米 MiMo",        "base_url": "https://token-plan-cn.xiaomimimo.com/v1", "model": "mimo-v2.5-pro"},
    # ── 海外直连 ──────────────────────────────────────────────
    "openai":     {"name": "OpenAI",           "base_url": "https://api.openai.com/v1",               "model": "gpt-4o-mini"},
    "anthropic":  {"name": "Anthropic Claude", "base_url": "https://api.anthropic.com/v1",            "model": "claude-3-5-haiku-20241022"},
    "gemini":     {"name": "Google Gemini",    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.0-flash"},
    "xai":        {"name": "xAI Grok",         "base_url": "https://api.x.ai/v1",                     "model": "grok-3-mini"},
    "groq":       {"name": "Groq",             "base_url": "https://api.groq.com/openai/v1",          "model": "llama-3.3-70b-versatile"},
    "nvidia":     {"name": "NVIDIA NIM",       "base_url": "https://integrate.api.nvidia.com/v1",     "model": "nvidia/llama-3.1-nemotron-70b-instruct"},
    # ── 聚合 / 中转 / 本地 ────────────────────────────────────
    "openrouter": {"name": "OpenRouter 聚合",  "base_url": "https://openrouter.ai/api/v1",            "model": "deepseek/deepseek-chat"},
    "ollama":     {"name": "Ollama 本地",      "base_url": "http://127.0.0.1:11434/v1",               "model": ""},
    "custom":     {"name": "自定义中转站（OneAPI/NewAPI等）", "base_url": "", "model": ""},
}

# key 前缀 → 服务商猜测（自动识别填充用）。
# 注意：裸 "sk-" 太普遍（OpenAI/DeepSeek/Moonshot/各中转站都用），不能据此判断。
_KEY_PREFIX_MAP = [
    ("sk-or-",  "openrouter"),
    ("sk-ant-", "anthropic"),
    ("gsk_",    "groq"),
    ("AIza",    "gemini"),
    ("xai-",    "xai"),
    ("nvapi-",  "nvidia"),
]


def guess_provider_by_key(api_key: str) -> str:
    """按 key 前缀猜服务商，猜不出返回空串。"""
    k = (api_key or "").strip()
    for prefix, provider in _KEY_PREFIX_MAP:
        if k.startswith(prefix):
            return provider
    return ""


def _urlopen_smart(req, timeout):
    """系统代理失败时自动降级直连（并记住成功链路）。

    本机常开 v2ray\\clash 系统代理（注册表 127.0.0.1:10809），国内 LLM 服务
    （小米MiMo/DeepSeek 等）经海外出口会被服务端掐 TLS → urllib 报
    `SSL: UNEXPECTED_EOF_WHILE_READING`；绕过代理直连反而通（2026-06-10 实测）。
    HTTP 错误码说明链路是通的，直接抛出不换路径。
    """
    global _prefer_direct
    import urllib.request
    import urllib.error
    last = None
    order = [True, False] if _prefer_direct else [False, True]
    for is_direct in order:
        try:
            opener = (urllib.request.build_opener(urllib.request.ProxyHandler({}))
                      if is_direct else urllib.request.build_opener())
            # ProxyHandler 会原地改写 Request.host 指向代理 → 换链路必须用全新 Request，
            # 否则第二次"直连"实际仍发往代理（修 SSL EOF 降级不生效的坑）
            fresh = urllib.request.Request(
                req.full_url, data=req.data,
                headers=dict(req.header_items()), method=req.get_method())
            resp = opener.open(fresh, timeout=timeout)
            _prefer_direct = is_direct
            return resp
        except urllib.error.HTTPError as e:
            # 4xx（鉴权/参数等）= 请求本身的问题，换链路也没用 → 直接抛。
            # 502/503/504/429 = 网关/过载/上游错误，可能与当前链路相关（境外中转站
            # 直连常被 GFW 掐 TLS 或网关降级，而走系统代理 200）→ 记下并尝试另一条链路。
            if e.code in (429, 502, 503, 504):
                last = e
                continue
            raise
        except Exception as e:
            last = e
    raise last


def _normalize_usage(usage: dict | None) -> dict:
    """Normalize provider token usage to OpenAI-compatible fields."""
    usage = usage or {}
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    total = usage.get("total_tokens") or (int(prompt) + int(completion))
    return {
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "total_tokens": int(total),
    }


def _llm_result(content: str, data: dict | None, provider: str, model: str, return_usage: bool):
    if not return_usage:
        return content
    return {
        "content": content,
        "usage": _normalize_usage((data or {}).get("usage")),
        "provider": provider,
        "model": model,
        "estimated": False,
    }


def detect_models(base_url: str, api_key: str, timeout: int = 20) -> dict:
    """调 OpenAI 兼容接口 GET {base_url}/models 列出该 key 可用的全部模型。

    OneAPI/NewAPI 等中转站、以及绝大多数官方 OpenAI 兼容端点都支持。
    Anthropic 官方 API 用 x-api-key 头，单独处理。
    返回 {"ok": bool, "models": [str], "error": str}
    """
    import urllib.request
    base_url = (base_url or "").rstrip("/")
    if not base_url:
        return {"ok": False, "error": "缺少 Base URL", "models": []}
    if not api_key:
        return {"ok": False, "error": "缺少 API Key", "models": []}

    is_anthropic = "anthropic" in base_url or api_key.startswith("sk-ant-")
    if is_anthropic:
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    else:
        headers = {"Authorization": f"Bearer {api_key}"}
    headers["Accept"] = "application/json"

    req = urllib.request.Request(f"{base_url}/models", headers=headers, method="GET")
    try:
        with _urlopen_smart(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        import urllib.error
        if isinstance(e, urllib.error.HTTPError):
            try:
                detail = e.read().decode("utf-8", errors="ignore")[:200]
            except Exception:
                detail = str(e.reason)
            return {"ok": False, "error": f"HTTP {e.code} — {detail}", "models": []}
        return {"ok": False, "error": f"请求失败: {e}", "models": []}

    items = data.get("data") or data.get("models") or []
    ids = []
    for it in items:
        mid = (it.get("id") or it.get("name") or "") if isinstance(it, dict) else str(it)
        if mid:
            ids.append(str(mid))
    # 过滤非对话模型（embedding/语音/图像/重排等），中转站列表动辄几百个
    _skip = ("embedding", "embed-", "whisper", "tts", "dall-e", "stable-", "flux",
             "rerank", "bge-", "moderation", "audio", "image", "vision-only",
             "text-davinci", "babbage", "ada")
    chat_ids = [m for m in ids if not any(s in m.lower() for s in _skip)]
    chat_ids = sorted(set(chat_ids or ids))[:500]
    return {"ok": True, "models": chat_ids, "total": len(ids)}


def call_llm(cfg: dict, messages: list, temperature: float = 0.3,
             max_tokens: int = 1200, timeout: int = 180,
             return_usage: bool = False):
    """
    timeout 默认 180 秒。大模型（kimi-k2.6 等）生成长篇分析需要较长时间，
    60 秒往往不够，调高到 180 秒避免 "read operation timed out" 报错。
    """
    try:
        import urllib.request
        import urllib.error

        provider = cfg.get("provider", "deepseek")
        base_url = cfg.get("base_url", "").rstrip("/")
        api_key  = cfg.get("api_key", "")
        model    = cfg.get("model", "deepseek-chat")

        if provider == "anthropic":
            return call_anthropic(cfg, messages, temperature, max_tokens, timeout, return_usage=return_usage)

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
        import time as _time
        _content = ""
        _data = {}
        # ★ 空响应重试：部分国产模型(小米MiMo等)在限流/偶发时会返回 200 但 content 为空，
        #   旧代码直接返回空串 → AI 分析某一级(多方/风控等)整段空白、看着"不完整"。
        #   200-空 重试一次（HTTP/网络错误仍立即抛出，不重试）。
        for _attempt in range(3):
            try:
                with _urlopen_smart(req, timeout=timeout) as resp:
                    _data = json.loads(resp.read().decode("utf-8"))
                    _content = (_data["choices"][0]["message"]["content"] or "").strip()
            except urllib.error.HTTPError as e:
                try:
                    err_body = e.read().decode("utf-8", errors="ignore")
                except Exception:
                    err_body = ""
                try:
                    err_obj = json.loads(err_body)
                    api_msg = err_obj.get("error", {})
                    if isinstance(api_msg, dict):
                        api_msg = api_msg.get("message", str(api_msg))
                    elif not api_msg:
                        api_msg = err_body  # 直接显示原始body
                except (json.JSONDecodeError, ValueError):
                    api_msg = err_body or e.reason
                # 429/502/503/504 = 中转站/网关瞬时不可用（境外中转常秒级抖动）→ 退避重试；
                # 其余(401鉴权/400参数/404模型不存在等)是确定性错误，立即抛。
                if e.code in (429, 502, 503, 504) and _attempt < 2:
                    _time.sleep(2.0 * (_attempt + 1))
                    continue
                raise RuntimeError(f"HTTP {e.code} — {api_msg}")
            except urllib.error.URLError as e:
                # 网络层错误（含 SSL/TLS 被掐断 UNEXPECTED_EOF）→ 退避重试，最后一次才抛。
                if _attempt < 2:
                    _time.sleep(2.0 * (_attempt + 1))
                    continue
                raise RuntimeError(f"网络错误: {e.reason}（请检查 Base URL / 网络 / 代理）")
            if _content:
                return _llm_result(_content, _data, provider, model, return_usage)
            if _attempt < 2:
                _time.sleep(2.0 * (_attempt + 1))   # 空响应退避重试：2s、4s
        return _llm_result(_content, _data, provider, model, return_usage)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"LLM 调用失败: {e}")


def call_anthropic(cfg, messages, temperature, max_tokens, timeout: int = 180, return_usage: bool = False):
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
        content = data["content"][0]["text"].strip()
        return _llm_result(content, data, "anthropic", cfg.get("model", "claude-3-5-haiku-20241022"), return_usage)
