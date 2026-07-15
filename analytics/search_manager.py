# -*- coding: utf-8 -*-
"""
搜索管理器 v1.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
多平台搜索 + 金融数据 + 多账号轮换 + 五级降级链

支持平台：
  Tavily     — AI驱动搜索 / 网页内容提取（1000次/月/账号）
  SerpAPI    — 谷歌/百度搜索结果抓取（250次/月/账号 + 1h缓存免费）
  TwelveData — 全球金融行情（8次/分钟/账号，A股需Pro+）
  直接抓取    — 东方财富/新浪财经（本地降级）
  本地缓存    — 最终兜底

降级链（新闻）：  Tavily → SerpAPI → 直接抓取 → 旧缓存
降级链（行情）：  TwelveData → TDX → 本地CSV → 缓存
降级链（搜索）：  Tavily → SerpAPI → 缓存
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
# 配置加载
# ══════════════════════════════════════════════════════
_CONFIG_FILE = Path(__file__).parent.parent / "data" / "search_config.json"
_config: dict = {}
_config_lock = threading.Lock()


def _load_config() -> dict:
    global _config
    with _config_lock:
        if not _config and _CONFIG_FILE.exists():
            try:
                _config = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"[SM] 加载搜索配置失败: {e}")
                _config = {}
    return _config


def save_config(cfg: dict):
    global _config
    with _config_lock:
        _config = cfg
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_config() -> dict:
    return _load_config()


# ══════════════════════════════════════════════════════
# 内存缓存（含 TTL）
# ══════════════════════════════════════════════════════
_cache: dict = {}
_cache_lock = threading.Lock()


def _cache_set(key: str, data, ttl: int = 3600):
    with _cache_lock:
        _cache[key] = {"data": data, "ts": time.time(), "ttl": ttl}


def _cache_get(key: str, max_age: int = None):
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        age = time.time() - entry["ts"]
        limit = max_age if max_age is not None else entry["ttl"]
        if age > limit:
            return None
        return entry["data"]


def _cache_get_stale(key: str):
    """即使过期也返回（全部失败时的最后兜底）"""
    with _cache_lock:
        entry = _cache.get(key)
        return entry["data"] if entry else None


# ══════════════════════════════════════════════════════
# 多账号轮换器
# ══════════════════════════════════════════════════════
class AccountRotator:
    """
    账号轮换：选择剩余额度最多的账号。
    月度自动重置（根据 reset_day）。
    """
    def __init__(self, service: str):
        self.service = service
        self._lock = threading.Lock()

    def _keys(self) -> list:
        cfg = _load_config()
        return cfg.get(self.service, {}).get("keys", [])

    def _reset_if_needed(self, key_obj: dict):
        """如果到了重置日，清零 usage"""
        today = datetime.now()
        reset_day = _load_config().get(self.service, {}).get("reset_day", 1)
        last_reset_key = "_last_reset"
        last_reset = key_obj.get(last_reset_key, "")
        expected = f"{today.year}-{today.month:02d}-{reset_day:02d}"
        if today.day >= reset_day and last_reset != expected:
            key_obj["usage"] = 0
            key_obj["usage_today"] = 0
            key_obj[last_reset_key] = expected

    def pick(self) -> Optional[str]:
        """选择剩余额度最多的可用账号，返回 API Key 字符串"""
        keys = self._keys()
        if not keys:
            return None

        with self._lock:
            # 重置检查
            for k in keys:
                self._reset_if_needed(k)

            # TwelveData 用 usage_today + limit_daily
            if self.service == "twelvedata":
                available = [k for k in keys if k.get("usage_today", 0) < k.get("limit_daily", 800)]
                if not available:
                    return None
                best = min(available, key=lambda x: x.get("usage_today", 0))
            else:
                available = [k for k in keys if k.get("usage", 0) < k.get("limit", 9999)]
                if not available:
                    return None
                best = min(available, key=lambda x: x.get("usage", 0))

            return best["key"]

    def charge(self, api_key: str, amount: int = 1):
        """记录一次消耗"""
        keys = self._keys()
        with self._lock:
            for k in keys:
                if k["key"] == api_key:
                    if self.service == "twelvedata":
                        k["usage_today"] = k.get("usage_today", 0) + amount
                    else:
                        k["usage"] = k.get("usage", 0) + amount
                    break
        # 异步写回配置（不阻塞调用方）
        threading.Thread(target=self._persist, daemon=True).start()

    def _persist(self):
        try:
            cfg = _load_config()
            save_config(cfg)
        except Exception as e:
            logger.warning(f"[search] 搜索配置持久化失败（本次改动重启后丢失）: {e}")

    def stats(self) -> dict:
        keys = self._keys()
        result = []
        for k in keys:
            if self.service == "twelvedata":
                used = k.get("usage_today", 0)
                limit = k.get("limit_daily", 800)
            else:
                used = k.get("usage", 0)
                limit = k.get("limit", 1000)
            result.append({
                "label": k.get("label", "?"),
                "used": used,
                "limit": limit,
                "remaining": max(0, limit - used),
                "pct": round(used / limit * 100, 1) if limit > 0 else 0,
            })
        return {"service": self.service, "accounts": result}


# 全局轮换器
_rotators = {
    "tavily":     AccountRotator("tavily"),
    "serpapi":    AccountRotator("serpapi"),
    "twelvedata": AccountRotator("twelvedata"),
}

# ── 速率限制（令牌桶）────────────────────────────────
_rate_buckets: dict = {}
_rate_lock = threading.Lock()


def _rate_wait(service: str, rpm: int = 8):
    """每次调用前等待，确保不超过 rpm 请求/分钟"""
    interval = 60.0 / rpm
    with _rate_lock:
        last = _rate_buckets.get(service, 0)
        now = time.time()
        wait = interval - (now - last)
        if wait > 0:
            time.sleep(wait)
        _rate_buckets[service] = time.time()


# ══════════════════════════════════════════════════════
# HTTP 工具
# ══════════════════════════════════════════════════════
_REQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
_PROXY_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")


def _http_post(url: str, body: dict, headers: dict = None, timeout: int = 15) -> dict:
    """POST JSON，清除系统代理（国内接口不需要）"""
    proxy_bak = {k: os.environ.pop(k, None) for k in _PROXY_KEYS}
    try:
        h = dict(_REQ_HEADERS)
        h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    finally:
        for k, v in proxy_bak.items():
            if v is not None:
                os.environ[k] = v


def _http_get(url: str, params: dict = None, headers: dict = None, timeout: int = 15) -> dict:
    """GET 请求"""
    proxy_bak = {k: os.environ.pop(k, None) for k in _PROXY_KEYS}
    try:
        h = dict(_REQ_HEADERS)
        if headers:
            h.update(headers)
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    finally:
        for k, v in proxy_bak.items():
            if v is not None:
                os.environ[k] = v


# ══════════════════════════════════════════════════════
# Tavily 接口
# ══════════════════════════════════════════════════════
_TAVILY_SEARCH = "https://api.tavily.com/search"
_TAVILY_EXTRACT = "https://api.tavily.com/extract"


def _tavily_search(query: str, topic: str = "news", max_results: int = 8,
                   days: int = 7, include_domains: list = None) -> list:
    """
    Tavily 搜索。
    topic: "news" | "general" | "research"
    返回: list of {title, url, content, source, published_date}
    """
    api_key = _rotators["tavily"].pick()
    if not api_key:
        raise RuntimeError("Tavily 所有账号额度已耗尽")

    body = {
        "api_key": api_key,
        "query": query,
        "topic": topic,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
        "include_images": False,
        "include_raw_content": False,
        "days": days,
    }
    if include_domains:
        body["include_domains"] = include_domains

    r = _http_post(_TAVILY_SEARCH, body, timeout=20)
    _rotators["tavily"].charge(api_key, 1)

    results = r.get("results", [])
    normalized = []
    for item in results:
        normalized.append({
            "title":     item.get("title", ""),
            "url":       item.get("url", ""),
            "content":   item.get("content", "")[:300],
            "source":    item.get("url", "").split("/")[2] if item.get("url") else "",
            "time":      item.get("published_date", "")[:16] if item.get("published_date") else "",
            "from":      "tavily",
        })
    return normalized


def _tavily_extract(urls: list) -> list:
    """Tavily 网页内容提取"""
    api_key = _rotators["tavily"].pick()
    if not api_key:
        raise RuntimeError("Tavily 所有账号额度已耗尽")

    r = _http_post(_TAVILY_EXTRACT, {"api_key": api_key, "urls": urls[:5]}, timeout=25)
    _rotators["tavily"].charge(api_key, 1)

    results = r.get("results", [])
    return [
        {
            "url":     item.get("url", ""),
            "content": item.get("raw_content", item.get("content", ""))[:1000],
            "from":    "tavily_extract",
        }
        for item in results
    ]


# ══════════════════════════════════════════════════════
# SerpAPI 接口
# ══════════════════════════════════════════════════════
_SERPAPI_URL = "https://serpapi.com/search"


def _serpapi_search(query: str, engine: str = "google", num: int = 8,
                    news: bool = True, lang: str = "zh-cn") -> list:
    """
    SerpAPI 搜索。
    engine: "google" | "baidu" | "bing"
    news: 是否搜索新闻（Google tbm=nws）
    """
    api_key = _rotators["serpapi"].pick()
    if not api_key:
        raise RuntimeError("SerpAPI 所有账号额度已耗尽")

    params = {
        "api_key": api_key,
        "q":       query,
        "engine":  engine,
        "num":     num,
    }
    if engine == "google":
        if news:
            params["tbm"] = "nws"
        params["gl"] = "cn"
        params["hl"] = lang
    elif engine == "baidu":
        params["rn"] = num

    r = _http_get(_SERPAPI_URL, params, timeout=20)
    _rotators["serpapi"].charge(api_key, 1)

    # 解析结果
    results = []
    for key in ("news_results", "organic_results"):
        for item in r.get(key, []):
            title = item.get("title", "")
            if not title:
                continue
            results.append({
                "title":   title,
                "url":     item.get("link", ""),
                "content": item.get("snippet", "")[:300],
                "source":  item.get("source", ""),
                "time":    item.get("date", "")[:16],
                "from":    f"serpapi_{engine}",
            })
    return results[:num]


# ══════════════════════════════════════════════════════
# TwelveData 接口
# ══════════════════════════════════════════════════════
_TD_BASE = "https://api.twelvedata.com"


def _code_to_td_symbol(code: str) -> Optional[str]:
    """A股代码转 TwelveData symbol 格式"""
    if not code or len(code) != 6:
        return None
    if code.startswith(("6", "9")):
        return f"{code}:XSHG"   # 上交所
    elif code.startswith(("0", "3")):
        return f"{code}:XSHE"   # 深交所
    return None


def td_get_quote(code: str) -> dict:
    """
    TwelveData 实时报价。
    免费层对A股有限制，但可尝试。
    """
    symbol = _code_to_td_symbol(code)
    if not symbol:
        raise ValueError(f"无法转换股票代码: {code}")

    api_key = _rotators["twelvedata"].pick()
    if not api_key:
        raise RuntimeError("TwelveData 所有账号日额度已耗尽")

    _rate_wait("twelvedata", rpm=8)
    r = _http_get(f"{_TD_BASE}/quote", {"symbol": symbol, "apikey": api_key}, timeout=12)
    _rotators["twelvedata"].charge(api_key, 1)

    if r.get("status") == "error" or "code" in r and r.get("code") in (400, 401, 403, 404):
        raise RuntimeError(f"TwelveData 报价失败: {r.get('message', r)}")

    return {
        "code":         code,
        "symbol":       symbol,
        "name":         r.get("name", code),
        "price":        float(r.get("close", 0) or r.get("last_price", 0) or 0),
        "open":         float(r.get("open", 0) or 0),
        "high":         float(r.get("high", 0) or 0),
        "low":          float(r.get("low", 0) or 0),
        "volume":       int(r.get("volume", 0) or 0),
        "prev_close":   float(r.get("previous_close", 0) or 0),
        "change_pct":   float(r.get("percent_change", 0) or 0),
        "currency":     r.get("currency", "CNY"),
        "exchange":     r.get("exchange", ""),
        "timestamp":    r.get("timestamp", ""),
        "from":         "twelvedata",
    }


def td_get_kline(code: str, days: int = 60, interval: str = "1day") -> list:
    """
    TwelveData K线历史数据。
    interval: 1min/5min/15min/30min/1h/1day/1week/1month
    返回: list of {datetime, open, high, low, close, volume}
    """
    symbol = _code_to_td_symbol(code)
    if not symbol:
        raise ValueError(f"无法转换股票代码: {code}")

    api_key = _rotators["twelvedata"].pick()
    if not api_key:
        raise RuntimeError("TwelveData 所有账号日额度已耗尽")

    start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y-%m-%d")
    _rate_wait("twelvedata", rpm=8)
    r = _http_get(f"{_TD_BASE}/time_series", {
        "symbol":     symbol,
        "interval":   interval,
        "start_date": start,
        "outputsize": days,
        "apikey":     api_key,
        "timezone":   "Asia/Shanghai",
        "format":     "JSON",
    }, timeout=15)
    _rotators["twelvedata"].charge(api_key, 1)

    if r.get("status") == "error":
        raise RuntimeError(f"TwelveData K线失败: {r.get('message', r)}")

    bars = []
    for v in r.get("values", []):
        bars.append({
            "date":   v.get("datetime", "")[:10],
            "open":   float(v.get("open", 0)),
            "high":   float(v.get("high", 0)),
            "low":    float(v.get("low", 0)),
            "close":  float(v.get("close", 0)),
            "volume": int(float(v.get("volume", 0))),
        })
    # TwelveData 返回倒序，转正序
    bars.reverse()
    return bars


def td_symbol_search(query: str) -> list:
    """
    TwelveData 股票代码/名称搜索（免费可用）。
    返回: list of {symbol, name, exchange, type, currency}
    """
    api_key = _rotators["twelvedata"].pick()
    if not api_key:
        return []

    _rate_wait("twelvedata", rpm=8)
    try:
        r = _http_get(f"{_TD_BASE}/symbol_search", {
            "symbol":  query,
            "apikey":  api_key,
            "outputsize": 10,
        }, timeout=10)
        _rotators["twelvedata"].charge(api_key, 1)
        return [
            {
                "symbol":   d.get("symbol", ""),
                "name":     d.get("instrument_name", d.get("name", "")),
                "exchange": d.get("exchange", ""),
                "type":     d.get("instrument_type", d.get("type", "")),
                "currency": d.get("currency", ""),
                "country":  d.get("country", ""),
            }
            for d in r.get("data", [])
        ]
    except Exception as e:
        logger.warning(f"[SM] TwelveData symbol search 失败: {e}")
        return []


def td_get_technical(code: str, indicator: str = "rsi",
                     period: int = 14, interval: str = "1day") -> dict:
    """
    TwelveData 技术指标。
    indicator: rsi/macd/bbands/stoch/ema/sma/adx/atr/cci/mfi/obv/willr/...
    """
    symbol = _code_to_td_symbol(code)
    if not symbol:
        raise ValueError(f"无法转换股票代码: {code}")

    api_key = _rotators["twelvedata"].pick()
    if not api_key:
        raise RuntimeError("TwelveData 额度耗尽")

    _rate_wait("twelvedata", rpm=8)
    r = _http_get(f"{_TD_BASE}/{indicator}", {
        "symbol":   symbol,
        "interval": interval,
        "time_period": period,
        "apikey":   api_key,
        "outputsize": 1,
        "format":   "JSON",
    }, timeout=12)
    _rotators["twelvedata"].charge(api_key, 1)

    if r.get("status") == "error":
        raise RuntimeError(f"TwelveData {indicator} 失败: {r.get('message', r)}")
    return r


# ══════════════════════════════════════════════════════
# 五级降级 — 股票新闻搜索
# ══════════════════════════════════════════════════════
def search_stock_news(code: str, name: str = "", limit: int = 8,
                      force_refresh: bool = False) -> list:
    """
    获取个股近期新闻（五级降级链）。
    Level 1: Tavily (AI搜索，质量最高)
    Level 2: SerpAPI Google News
    Level 3: SerpAPI Baidu
    Level 4: 直接抓取（东方财富/新浪）
    Level 5: 本地旧缓存（兜底）

    返回: list of {title, url, content, source, time, from}
    """
    cache_key = f"news_{code}"
    if not force_refresh:
        cached = _cache_get(cache_key, max_age=1800)  # 30分钟缓存
        if cached:
            logger.debug(f"[SM] 新闻缓存命中: {code}")
            return cached

    query = f"{code} {name} 股票 新闻" if name else f"{code} 股票 新闻"

    # ── Level 1: Tavily ──────────────────────────────
    cfg = _load_config()
    if cfg.get("tavily", {}).get("enabled"):
        try:
            results = _tavily_search(query, topic="news", max_results=limit, days=7)
            if results:
                _cache_set(cache_key, results, ttl=1800)
                logger.info(f"[SM] 新闻-Tavily: {code} {len(results)}条")
                return results
        except Exception as e:
            logger.warning(f"[SM] Tavily新闻搜索失败: {e}")

    # ── Level 2: SerpAPI Google News ─────────────────
    if cfg.get("serpapi", {}).get("enabled"):
        try:
            results = _serpapi_search(query, engine="google", num=limit, news=True)
            if results:
                _cache_set(cache_key, results, ttl=1800)
                logger.info(f"[SM] 新闻-SerpAPI Google: {code} {len(results)}条")
                return results
        except Exception as e:
            logger.warning(f"[SM] SerpAPI Google新闻失败: {e}")

        # ── Level 3: SerpAPI Baidu ───────────────────
        try:
            query_baidu = f"{name or code} 股票新闻" if name else f"{code} 股票"
            results = _serpapi_search(query_baidu, engine="baidu", num=limit, news=False)
            if results:
                _cache_set(cache_key, results, ttl=1800)
                logger.info(f"[SM] 新闻-SerpAPI Baidu: {code} {len(results)}条")
                return results
        except Exception as e:
            logger.warning(f"[SM] SerpAPI Baidu新闻失败: {e}")

    # ── Level 4: 直接抓取 ────────────────────────────
    try:
        from market.news_fetcher import get_stock_news
        results_raw = get_stock_news(code, limit=limit)
        if results_raw:
            normalized = [
                {
                    "title":   n.get("title", ""),
                    "url":     n.get("url", ""),
                    "content": "",
                    "source":  n.get("source", ""),
                    "time":    n.get("time", ""),
                    "from":    n.get("from", "direct"),
                }
                for n in results_raw
            ]
            _cache_set(cache_key, normalized, ttl=1800)
            logger.info(f"[SM] 新闻-直接抓取: {code} {len(normalized)}条")
            return normalized
    except Exception as e:
        logger.warning(f"[SM] 直接抓取新闻失败: {e}")

    # ── Level 5: 旧缓存兜底 ──────────────────────────
    stale = _cache_get_stale(cache_key)
    if stale:
        logger.info(f"[SM] 新闻-旧缓存兜底: {code}")
        return stale

    logger.warning(f"[SM] 所有新闻源均不可用: {code}")
    return []


# ══════════════════════════════════════════════════════
# 四级降级 — 通用网页搜索
# ══════════════════════════════════════════════════════
def search_web(query: str, topic: str = "general", limit: int = 10,
               cache_ttl: int = 3600, force_refresh: bool = False) -> list:
    """
    通用网页搜索（四级降级链）。
    Level 1: Tavily (AI语义搜索)
    Level 2: SerpAPI Google
    Level 3: SerpAPI Baidu
    Level 4: 本地缓存

    topic: "general" | "news" | "research"
    返回: list of {title, url, content, source, time, from}
    """
    cache_key = f"web_{query[:50]}"
    if not force_refresh:
        cached = _cache_get(cache_key, max_age=cache_ttl)
        if cached:
            return cached

    cfg = _load_config()

    if cfg.get("tavily", {}).get("enabled"):
        try:
            results = _tavily_search(query, topic=topic, max_results=limit)
            if results:
                _cache_set(cache_key, results, ttl=cache_ttl)
                return results
        except Exception as e:
            logger.warning(f"[SM] Tavily搜索失败: {e}")

    if cfg.get("serpapi", {}).get("enabled"):
        try:
            results = _serpapi_search(query, engine="google", num=limit,
                                       news=(topic == "news"))
            if results:
                _cache_set(cache_key, results, ttl=cache_ttl)
                return results
        except Exception as e:
            logger.warning(f"[SM] SerpAPI Google搜索失败: {e}")

        try:
            results = _serpapi_search(query, engine="baidu", num=limit, news=False)
            if results:
                _cache_set(cache_key, results, ttl=cache_ttl)
                return results
        except Exception as e:
            logger.warning(f"[SM] SerpAPI Baidu搜索失败: {e}")

    stale = _cache_get_stale(cache_key)
    if stale:
        return stale

    return []


# ══════════════════════════════════════════════════════
# 统一数据源 — 股票K线（后台配置控制第一/第二数据源）
# ══════════════════════════════════════════════════════
def get_kline_with_fallback(code: str, days: int = 60) -> "pd.DataFrame":
    """
    获取A股K线：统一走 data_gateway，由后台数据源配置控制优先级。
    """
    import pandas as pd

    cache_key = f"kline_{code}_{days}"
    cached = _cache_get(cache_key, max_age=3600)  # 1小时缓存
    if cached is not None:
        return pd.DataFrame(cached)

    try:
        from analytics import data_gateway as dg
        df = dg.get_daily_kline(code, days=days, min_rows=10)
        if df is not None and not df.empty:
            out = df.tail(days).reset_index(drop=True)
            _cache_set(cache_key, out.to_dict("records"), ttl=3600)
            logger.info(f"[SM] K线-统一数据源: {code} {len(out)}条")
            return out
    except Exception as e:
        logger.warning(f"[SM] 统一数据源K线失败: {e}")

    return pd.DataFrame()


# ══════════════════════════════════════════════════════
# 股票行情搜索（组合查询）
# ══════════════════════════════════════════════════════
def search_stock_info(code: str, name: str = "") -> dict:
    """
    综合搜索个股信息（公司简介、最新动态、行业分析）。
    使用 Tavily 的 general 搜索，结合 TwelveData 报价。
    """
    result = {"code": code, "name": name, "news": [], "web": [], "quote": {}}

    # 新闻
    result["news"] = search_stock_news(code, name, limit=5)

    # 网页搜索（公司基本面信息）
    q = f"{name or code} 股票 公司介绍 业务 主营"
    result["web"] = search_web(q, topic="general", limit=5, cache_ttl=7200)

    # TwelveData 报价（可选）
    cfg = _load_config()
    if cfg.get("twelvedata", {}).get("enabled"):
        try:
            result["quote"] = td_get_quote(code)
        except Exception as e:
            logger.debug(f"[SM] TwelveData报价跳过: {e}")

    return result


# ══════════════════════════════════════════════════════
# 统计 & 状态
# ══════════════════════════════════════════════════════
def get_stats() -> dict:
    """获取所有搜索账号的用量统计"""
    return {
        "tavily":     _rotators["tavily"].stats(),
        "serpapi":    _rotators["serpapi"].stats(),
        "twelvedata": _rotators["twelvedata"].stats(),
        "cache_size": len(_cache),
    }


def get_service_status() -> dict:
    """快速检查各服务是否可用（有可用额度）"""
    cfg = _load_config()
    status = {}
    for svc in ("tavily", "serpapi", "twelvedata"):
        enabled = cfg.get(svc, {}).get("enabled", False)
        key = _rotators[svc].pick()
        status[svc] = {
            "enabled":   enabled,
            "available": bool(key),
            "key_count": len(cfg.get(svc, {}).get("keys", [])),
        }
    return status
