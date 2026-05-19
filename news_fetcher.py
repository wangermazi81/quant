# -*- coding: utf-8 -*-
"""
股票新闻情绪抓取器
多源级联：东方财富 → 新浪财经 → 百度财经
"""

import json
import logging
import re
import urllib.request
import urllib.parse
from datetime import datetime

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_TIMEOUT = 8


def _fetch(url: str, extra_headers: dict = None) -> str:
    headers = dict(_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    # 临时清除代理（国内接口不需要代理）
    import os
    proxy_bak = {k: os.environ.pop(k, None)
                 for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                           "all_proxy", "ALL_PROXY")}
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    finally:
        for k, v in proxy_bak.items():
            if v is not None:
                os.environ[k] = v


def _get_em_news(code: str, limit: int = 5) -> list:
    """东方财富个股新闻"""
    # 判断市场
    mkt = "0" if code.startswith(("6", "9")) else "1"
    url = (
        f"https://np-listapi.eastmoney.com/comm/wap/getListInfo"
        f"?cb=&client=wap&type=1&code={mkt}.{code}&pageSize={limit}"
        f"&orderType=1&keyword=&emSort=1&_={int(datetime.now().timestamp()*1000)}"
    )
    try:
        text = _fetch(url)
        # 可能是 JSONP 或纯 JSON
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            return []
        data = json.loads(m.group(0))
        items = (data.get("data", {}) or {}).get("list", []) or []
        result = []
        for it in items[:limit]:
            title = it.get("title", "") or it.get("Title", "")
            source = it.get("mediaName", "") or it.get("source", "")
            pub_time = it.get("publishDate", "") or it.get("date", "")
            url_link = it.get("url", "") or it.get("articleUrl", "")
            if title:
                result.append({
                    "title": title,
                    "source": source,
                    "time": str(pub_time)[:16],
                    "url": url_link,
                    "from": "eastmoney",
                })
        return result
    except Exception as e:
        logger.debug(f"[News] 东方财富抓取失败 {code}: {e}")
        return []


def _get_em_news_v2(code: str, limit: int = 5) -> list:
    """东方财富 v2 接口（备用）"""
    mkt = "1" if code.startswith(("6", "9")) else "0"
    url = (
        f"https://np-anotice-stock.eastmoney.com/api/security/ann"
        f"?sr=-1&page_size={limit}&page_index=1&ann_type=A&client_source=web"
        f"&stock_list={mkt}%2C{code}"
    )
    try:
        text = _fetch(url)
        data = json.loads(text)
        items = (data.get("data", {}) or {}).get("list", []) or []
        result = []
        for it in items[:limit]:
            title = it.get("title", "")
            if title:
                result.append({
                    "title": title,
                    "source": "东财公告",
                    "time": str(it.get("notice_date", ""))[:16],
                    "url": "",
                    "from": "eastmoney_ann",
                })
        return result
    except Exception as e:
        logger.debug(f"[News] 东方财富v2抓取失败 {code}: {e}")
        return []


def _get_sina_news(code: str, limit: int = 5) -> list:
    """新浪财经个股新闻"""
    # 新浪用 sz/sh 前缀
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    url = (
        f"https://finance.sina.com.cn/realstock/company/{prefix}{code}/nc.shtml"
    )
    try:
        text = _fetch(url, {"Accept": "text/html,*/*"})
        # 从 HTML 中提取新闻列表
        titles = re.findall(r'<li[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([^<]{5,80})</a>', text)
        result = []
        for href, title in titles[:limit]:
            if title.strip() and "http" in href:
                result.append({
                    "title": title.strip(),
                    "source": "新浪财经",
                    "time": "",
                    "url": href,
                    "from": "sina",
                })
        return result
    except Exception as e:
        logger.debug(f"[News] 新浪财经抓取失败 {code}: {e}")
        return []


def get_stock_news(code: str, limit: int = 5, name: str = "") -> list:
    """
    获取个股近期新闻（优先走 search_manager 五级降级链）。
    返回：list of {"title": str, "source": str, "time": str, "url": str}
    """
    # 优先使用 search_manager（Tavily→SerpAPI→直接抓取）
    try:
        from search_manager import search_stock_news as _sm_news
        results = _sm_news(code, name=name, limit=limit)
        if results:
            # 统一格式
            normalized = []
            seen = set()
            for r in results:
                title = r.get("title", "")
                if not title or title[:15] in seen:
                    continue
                seen.add(title[:15])
                normalized.append({
                    "title":  title,
                    "source": r.get("source", r.get("from", "")),
                    "time":   r.get("time", ""),
                    "url":    r.get("url", ""),
                    "from":   r.get("from", "search"),
                })
            if normalized:
                return normalized[:limit]
    except Exception as e:
        logger.debug(f"[News] search_manager 调用失败，降级到直接抓取: {e}")

    # 降级：直接抓取（东方财富/新浪）
    news = []
    news = _get_em_news(code, limit)
    if not news:
        news = _get_em_news_v2(code, limit)
    if not news:
        news = _get_sina_news(code, limit)

    seen = set()
    unique = []
    for n in news:
        key = n["title"][:20]
        if key not in seen:
            seen.add(key)
            unique.append(n)

    return unique[:limit]


def news_to_context_text(news_list: list, code: str = "") -> str:
    """将新闻列表转换为 LLM 上下文文本"""
    if not news_list:
        return ""
    lines = [f"【近期新闻（{code}）】"]
    for i, n in enumerate(news_list, 1):
        t = n.get("time", "")
        s = n.get("source", "")
        title = n.get("title", "")
        meta = f" [{s} {t}]" if (s or t) else ""
        lines.append(f"{i}. {title}{meta}")
    return "\n".join(lines)


def _simple_sentiment(news_list: list) -> dict:
    """
    基于关键词的简单情绪分析（无需LLM）。
    返回: {"score": -1~1, "label": "利空/中性/利好", "keywords": []}
    """
    positive_kw = ["涨停", "大涨", "突破", "利好", "增持", "买入", "业绩增长",
                   "高增", "中标", "获批", "签约", "战略合作", "回购", "分红",
                   "超预期", "强势", "上调", "升级", "解禁", "上市"]
    negative_kw = ["跌停", "大跌", "暴跌", "利空", "减持", "卖出", "亏损",
                   "业绩下滑", "处罚", "立案", "问询", "违规", "诉讼", "债务",
                   "低于预期", "下调", "退市", "清仓", "解除", "终止"]

    pos_score = 0
    neg_score = 0
    matched_kw = []

    for n in news_list:
        title = n.get("title", "")
        for kw in positive_kw:
            if kw in title:
                pos_score += 1
                matched_kw.append(f"+{kw}")
        for kw in negative_kw:
            if kw in title:
                neg_score += 1
                matched_kw.append(f"-{kw}")

    total = len(news_list) or 1
    score = round((pos_score - neg_score) / total, 2)
    score = max(-1.0, min(1.0, score))

    if score > 0.2:
        label = "利好"
    elif score < -0.2:
        label = "利空"
    else:
        label = "中性"

    return {
        "score": score,
        "label": label,
        "keywords": list(set(matched_kw))[:8],
        "pos_count": pos_score,
        "neg_count": neg_score,
    }


def get_news_with_sentiment(code: str, limit: int = 5) -> dict:
    """
    获取新闻 + 情绪分析。
    返回: {"news": [...], "sentiment": {...}, "context_text": str}
    """
    news = get_stock_news(code, limit)
    sentiment = _simple_sentiment(news)
    context = news_to_context_text(news, code)
    return {
        "news": news,
        "sentiment": sentiment,
        "context_text": context,
    }
