# -*- coding: utf-8 -*-
"""重请求准入 / SSE 配额 的回归测试（P-2）。

守的是生产上反复发作的那个事故：`workers=1 × threads=8` 下，两条 SSE 流
每个连接永久占一个 gthread（一个浏览器标签占 2 个），叠加同步全市场选股/回测，
3-4 个标签就能把线程池吃干 → 所有请求排队到 300s 超时 → 「选股返回 0、重启才好」。
"""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from web import _concurrency as cc


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """每个用例用独立的信号量表与计数器，避免互相干扰。"""
    monkeypatch.setattr(cc, "_heavy_locks", {})
    monkeypatch.setattr(cc, "_sse_counts", {})
    monkeypatch.setattr(cc, "_sse_total", 0)
    yield


class TestHeavySlot:
    def test_second_concurrent_request_is_rejected(self):
        """占满即拒，不排队——排队就是占着线程等，正是要避免的行为。"""
        with cc.heavy_slot("scan", limit=1):
            with pytest.raises(cc.HeavyBusy):
                with cc.heavy_slot("scan", limit=1):
                    pass

    def test_slot_is_released_after_use(self):
        with cc.heavy_slot("scan", limit=1):
            pass
        with cc.heavy_slot("scan", limit=1):     # 应能再次获得
            pass

    def test_slot_is_released_even_on_exception(self):
        """被测代码抛异常也必须归还名额，否则一次失败就永久少一个槽位。"""
        with pytest.raises(ValueError):
            with cc.heavy_slot("scan", limit=1):
                raise ValueError("boom")
        with cc.heavy_slot("scan", limit=1):
            pass

    def test_different_names_do_not_share_slots(self):
        with cc.heavy_slot("scan", limit=1):
            with cc.heavy_slot("backtest", limit=1):     # 不同名互不影响
                pass

    def test_acquired_lease_can_be_released_by_worker_thread(self):
        lease = cc.acquire_heavy("scan", limit=1)
        with pytest.raises(cc.HeavyBusy):
            cc.acquire_heavy("scan", limit=1)

        worker = threading.Thread(target=lease.release)
        worker.start()
        worker.join(1)

        assert not worker.is_alive()
        again = cc.acquire_heavy("scan", limit=1)
        again.release()


class TestSseSlot:
    def test_per_user_limit_rejects_extra_connection(self, monkeypatch):
        monkeypatch.setattr(cc, "SSE_PER_USER_LIMIT", 1)
        monkeypatch.setattr(cc, "SSE_GLOBAL_LIMIT", 99)
        with cc.sse_slot("paper", 7) as first:
            assert first is True
            with cc.sse_slot("paper", 7) as second:
                assert second is None, "同一用户同一流超额时必须拒绝"

    def test_global_limit_rejects_across_users(self, monkeypatch):
        monkeypatch.setattr(cc, "SSE_PER_USER_LIMIT", 9)
        monkeypatch.setattr(cc, "SSE_GLOBAL_LIMIT", 1)
        with cc.sse_slot("paper", 1) as first:
            assert first is True
            with cc.sse_slot("paper", 2) as second:
                assert second is None, "全局上限必须跨用户生效，否则多用户仍能吃干线程池"

    def test_slot_released_on_exit(self, monkeypatch):
        monkeypatch.setattr(cc, "SSE_PER_USER_LIMIT", 1)
        monkeypatch.setattr(cc, "SSE_GLOBAL_LIMIT", 99)
        with cc.sse_slot("paper", 7):
            pass
        with cc.sse_slot("paper", 7) as again:
            assert again is True

    def test_counts_are_isolated_per_stream(self, monkeypatch):
        monkeypatch.setattr(cc, "SSE_PER_USER_LIMIT", 1)
        monkeypatch.setattr(cc, "SSE_GLOBAL_LIMIT", 99)
        with cc.sse_slot("paper", 7) as a:
            assert a is True
            with cc.sse_slot("ai_trader", 7) as b:
                assert b is True, "不同流应各有配额"

    def test_stats_reports_active_and_limits(self, monkeypatch):
        monkeypatch.setattr(cc, "SSE_GLOBAL_LIMIT", 12)
        with cc.sse_slot("paper", 3):
            st = cc.sse_stats()
            assert st["total"] == 1
            assert st["global_limit"] == 12

    def test_rejected_context_does_not_hold_stats_lock(self, monkeypatch):
        monkeypatch.setattr(cc, "SSE_PER_USER_LIMIT", 1)
        monkeypatch.setattr(cc, "SSE_GLOBAL_LIMIT", 99)
        rejected_entered = threading.Event()
        release_rejected = threading.Event()
        stats_done = threading.Event()

        def hold_rejected_context():
            with cc.sse_slot("paper", 7) as granted:
                assert granted is None
                rejected_entered.set()
                release_rejected.wait(2)

        def read_stats():
            cc.sse_stats()
            stats_done.set()

        with cc.sse_slot("paper", 7):
            rejected_thread = threading.Thread(target=hold_rejected_context)
            rejected_thread.start()
            assert rejected_entered.wait(1)
            stats_thread = threading.Thread(target=read_stats)
            stats_thread.start()
            try:
                assert stats_done.wait(0.3), "拒绝路径仍持有 _sse_guard，统计接口被锁死"
            finally:
                release_rejected.set()
                rejected_thread.join(2)
                stats_thread.join(2)

    def test_deadline_is_in_the_future(self):
        import time
        assert cc.sse_deadline() > time.time()


def test_sse_max_lifetime_is_bounded():
    """必须有上限：无上限的 SSE 连接一旦泄漏就永久扣掉一个线程。"""
    assert 0 < cc.SSE_MAX_LIFETIME <= 24 * 3600


def test_global_sse_limit_leaves_room_for_normal_requests():
    """SSE 全局上限必须显著小于 gunicorn 的 threads，否则普通请求没有通道。"""
    import re
    conf = (Path(__file__).resolve().parents[1] / "deploy" / "gunicorn.conf.py").read_text(encoding="utf-8")
    m = re.search(r"^threads\s*=\s*(\d+)", conf, re.M)
    assert m, "gunicorn.conf.py 里找不到 threads 配置"
    threads = int(m.group(1))
    assert cc.SSE_GLOBAL_LIMIT < threads / 2, (
        f"SSE 上限 {cc.SSE_GLOBAL_LIMIT} 相对 threads={threads} 过高，"
        f"长连接会把普通请求挤掉")
