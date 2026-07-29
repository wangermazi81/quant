# -*- coding: utf-8 -*-
"""重请求准入控制 + SSE 连接配额。

要解决的是生产上反复发作的那个事故（「云端选股返回 0、进程态坏死、重启才好」）：

生产是 `workers=1 × threads=8` 的 gthread 模型，而
  - 两条 SSE 流（/api/paper/stream、/api/ai_trader/stream）是 `while True` 生成器，
    **每个连接永久占住一个 gthread**，一个浏览器标签就占 2 个；
  - 同时还有多条在请求内同步跑分钟级工作的路由（同步全市场选股、同步回测、
    请求内 build_panel 拉全市场、请求内抓 akshare 板块）。

于是 3-4 个标签 + 一次同步选股就能把 8 个线程吃干，后续所有请求（含 /api/health）
排队到 300s 超时 —— 外部表现就是 access.log 全 200/50 字节、选股返回 0、必须重启。

这里给两道闸，都不需要前端配合：
  1) `heavy_slot`：重路由限并发，占满时立刻返回 429 而不是排队占线程。
     宁可让这一个请求失败，也不能让它把整站拖死。
  2) `sse_slot`：SSE 连接配额 + 最长存活时间。到期主动结束流，
     浏览器 EventSource 会自动重连（两条流都在连接时重发 snapshot，重连即重新对齐状态），
     所以对前端完全透明，却能保证泄漏的连接最终一定会把线程还回来。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from functools import wraps

from flask import jsonify

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


# 重路由并发上限。默认 2：单人正常使用完全不受影响，
# 但杜绝「多个全市场扫描同时在跑」把线程池吃干。
HEAVY_LIMIT_DEFAULT = _env_int("THS_HEAVY_LIMIT", 2)
# SSE 每（用户×流）上限。给 2 是为了容忍重连瞬间的短暂重叠。
SSE_PER_USER_LIMIT = _env_int("THS_SSE_PER_USER", 2)
# SSE 全局上限。必须显著小于 gunicorn threads，给普通请求留出通道。
SSE_GLOBAL_LIMIT = _env_int("THS_SSE_GLOBAL", 12)
# SSE 单连接最长存活（秒）。到期结束流，由浏览器自动重连。
SSE_MAX_LIFETIME = _env_int("THS_SSE_MAX_LIFETIME", 1800)

_heavy_locks: dict[str, threading.BoundedSemaphore] = {}
_heavy_guard = threading.Lock()

_sse_counts: dict[tuple[str, int], int] = {}
_sse_total = 0
_sse_guard = threading.Lock()


def _heavy_sem(name: str, limit: int) -> threading.BoundedSemaphore:
    with _heavy_guard:
        sem = _heavy_locks.get(name)
        if sem is None:
            sem = threading.BoundedSemaphore(limit)
            _heavy_locks[name] = sem
        return sem


class HeavyLease:
    def __init__(self, sem: threading.BoundedSemaphore):
        self._sem = sem
        self._released = False
        self._guard = threading.Lock()

    def release(self) -> None:
        with self._guard:
            if self._released:
                return
            self._released = True
        self._sem.release()


def acquire_heavy(name: str, limit: int | None = None) -> HeavyLease:
    """非阻塞获取重任务名额，返回可由后台线程释放的租约。"""
    sem = _heavy_sem(name, limit or HEAVY_LIMIT_DEFAULT)
    if not sem.acquire(blocking=False):
        raise HeavyBusy(name)
    return HeavyLease(sem)


@contextmanager
def heavy_slot(name: str, limit: int | None = None):
    """占一个重请求名额；占不到抛 HeavyBusy。"""
    lease = acquire_heavy(name, limit)
    try:
        yield
    finally:
        lease.release()


class HeavyBusy(RuntimeError):
    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


def limit_heavy(name: str, limit: int | None = None, busy_msg: str | None = None):
    """装饰器版：占不到名额直接回 429，不排队、不占线程。"""
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            try:
                with heavy_slot(name, limit):
                    return f(*args, **kwargs)
            except HeavyBusy:
                logger.warning("[并发] %s 名额已满，拒绝新请求（保护线程池）", name)
                return jsonify({
                    "ok": False,
                    "error": "busy",
                    "msg": busy_msg or "当前有同类重任务正在运行，请稍后再试",
                }), 429
        return wrapper
    return deco


@contextmanager
def sse_slot(stream: str, uid: int):
    """占一个 SSE 名额。占不到时 yield None，调用方应立刻结束流。"""
    global _sse_total
    key = (stream, int(uid or 0))
    rejected = None
    with _sse_guard:
        if _sse_total >= SSE_GLOBAL_LIMIT:
            rejected = "global"
        elif _sse_counts.get(key, 0) >= SSE_PER_USER_LIMIT:
            rejected = "user"
        else:
            _sse_counts[key] = _sse_counts.get(key, 0) + 1
            _sse_total += 1
    if rejected:
        if rejected == "global":
            logger.warning("[SSE] 全局连接数已达上限 %s，拒绝 %s uid=%s",
                           SSE_GLOBAL_LIMIT, stream, uid)
        else:
            logger.info("[SSE] uid=%s 的 %s 连接数已达上限 %s，拒绝新连接",
                        uid, stream, SSE_PER_USER_LIMIT)
        yield None
        return
    try:
        yield True
    finally:
        with _sse_guard:
            left = _sse_counts.get(key, 1) - 1
            if left <= 0:
                _sse_counts.pop(key, None)
            else:
                _sse_counts[key] = left
            _sse_total = max(0, _sse_total - 1)


def sse_deadline() -> float:
    """本次 SSE 连接应当结束的时间戳。"""
    return time.time() + SSE_MAX_LIFETIME


def sse_stats() -> dict:
    with _sse_guard:
        return {
            "total": _sse_total,
            "global_limit": SSE_GLOBAL_LIMIT,
            "per_user_limit": SSE_PER_USER_LIMIT,
            "max_lifetime_sec": SSE_MAX_LIFETIME,
            "by_stream": {f"{s}:{u}": n for (s, u), n in _sse_counts.items()},
        }
