# -*- coding: utf-8 -*-
"""调度器分组回归测试（工单5）。

保护的不变量：
1. 交易组（run_scheduler/run_vibe_scheduler）与数据组（新闻/复盘/概念/AI因子
   挖掘）分别由 start_user_trading_schedulers / start_data_schedulers 启动。
2. start_background_services = 两组全启（单进程模式，与拆分前行为一致）。
3. 各启动函数幂等（重复调用不重复起线程）。
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests._module_stubs import stub_project_module  # noqa: E402


def _fresh_schedulers(monkeypatch):
    """重置幂等标志 + 记录线程启动目标（不真正起线程）。"""
    from core import schedulers

    monkeypatch.setattr(schedulers, "_trading_started", False)
    monkeypatch.setattr(schedulers, "_data_started", False)

    started = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None, args=()):
            self._label = name or getattr(target, "__name__", str(target))

        def start(self):
            started.append(self._label)

    monkeypatch.setattr(schedulers.threading, "Thread", FakeThread)
    monkeypatch.setattr(schedulers, "_restore_all_user_states", lambda: started.append("restore_states"))
    monkeypatch.setattr(schedulers.um, "expire_stale_signals", lambda *a, **k: 0)

    # 数据组的四个模块全部打桩（不真跑新闻/复盘/概念/因子挖掘）
    monkeypatch.setitem(sys.modules, "news_center",
                        types.SimpleNamespace(run_news_scheduler=lambda: None))
    monkeypatch.setitem(sys.modules, "research_center",
                        types.SimpleNamespace(run_concept_scheduler=lambda: started.append("concept")))
    stub_project_module(monkeypatch, "review_engine",
                        types.SimpleNamespace(run_review_scheduler=lambda: None))
    stub_project_module(monkeypatch, "auto_factor_scheduler",
                        types.SimpleNamespace(start_background_scheduler=lambda: started.append("auto_factor")))
    return schedulers, started


def test_trading_group_starts_scan_and_vibe_only(monkeypatch):
    schedulers, started = _fresh_schedulers(monkeypatch)
    schedulers.start_user_trading_schedulers()
    assert "restore_states" in started
    assert "run_scheduler" in started
    assert "vibe-scheduler" in started
    assert "news-center" not in started
    assert "review-engine" not in started
    assert "concept" not in started
    assert "auto_factor" not in started


def test_data_group_starts_four_data_schedulers_only(monkeypatch):
    schedulers, started = _fresh_schedulers(monkeypatch)
    schedulers.start_data_schedulers()
    assert "news-center" in started
    assert "review-engine" in started
    assert "concept" in started
    assert "auto_factor" in started
    assert "run_scheduler" not in started
    assert "vibe-scheduler" not in started
    assert "restore_states" not in started


def test_background_services_starts_both_groups(monkeypatch):
    schedulers, started = _fresh_schedulers(monkeypatch)
    schedulers.start_background_services()
    for label in ("run_scheduler", "vibe-scheduler", "news-center",
                  "review-engine", "concept", "auto_factor"):
        assert label in started, f"缺少 {label}"


def test_start_functions_are_idempotent(monkeypatch):
    schedulers, started = _fresh_schedulers(monkeypatch)
    schedulers.start_user_trading_schedulers()
    schedulers.start_data_schedulers()
    n_first = len(started)
    schedulers.start_user_trading_schedulers()
    schedulers.start_data_schedulers()
    schedulers.start_background_services()
    assert len(started) == n_first, "重复调用不应重复启动线程"
