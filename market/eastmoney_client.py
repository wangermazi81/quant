# -*- coding: utf-8 -*-
"""东方财富接口的唯一客户端（A-7）。

此前 data_manager / market_stats / web.market / research_center / lhb_engine /
news_fetcher 各写一套：同样的 User-Agent + Referer、同样的镜像主机列表、
同样的「失败就换下一个域名」重试、同样的 no_proxy 包裹。
东财接口一变（换域名、加校验头、改字段）就要改 4-6 处，漏一处就是偶发失效。

这里只收敛**传输层**（请求头 / 镜像列表 / 重试 / 代理规避），
各调用方的字段映射与降级策略保持原样 —— 那些语义各不相同，
强行统一反而会悄悄改变取数行为。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 东财对 UA/Referer 有反爬校验，全项目共用这一份
EM_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
}

# ── 镜像主机组（东财同一接口有多个可用域名，挨个试）────────────────
UUST_URLS = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get",
    "https://48.push2.eastmoney.com/api/qt/ulist.np/get",
)
STOCK_GET_URLS = (
    "https://push2.eastmoney.com/api/qt/stock/get",
)
CLIST_URLS = (
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://17.push2.eastmoney.com/api/qt/clist/get",
)
INDEX_KLINE_URLS = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://push2delay.eastmoney.com/api/qt/stock/kline/get",
)
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _no_proxy_ctx():
    """东财行情主机走海外出口会被随机掐，必须绕开系统/注册表代理。"""
    try:
        from research_center import no_proxy

        return no_proxy()
    except Exception:
        from contextlib import nullcontext

        return nullcontext()


def get_json(urls, params: dict, timeout: int = 10, headers: dict | None = None):
    """按顺序试各镜像，返回第一个成功解析出的 dict。全失败抛 RuntimeError。

    与原 market_stats._eastmoney_get_json 行为一致（它是被验证过的实现，
    这里只是把它提升为全项目共用）。
    """
    import requests

    if isinstance(urls, str):
        urls = (urls,)
    errors = []
    for url in urls:
        try:
            with _no_proxy_ctx():
                response = requests.get(url, params=params,
                                        headers=headers or EM_HEADERS, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                return payload
            raise ValueError("non-dict eastmoney payload")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("; ".join(errors))


def try_get_json(urls, params: dict, timeout: int = 10, headers: dict | None = None):
    """get_json 的不抛版本：全失败返回 None（供 best-effort 调用方用）。"""
    try:
        return get_json(urls, params, timeout=timeout, headers=headers)
    except Exception as exc:
        logger.debug(f"[eastmoney] 请求失败: {exc}")
        return None
