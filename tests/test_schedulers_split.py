# -*- coding: utf-8 -*-
"""调度器分组回归测试（工单5）。

保护的不变量：
1. 交易组（run_scheduler/run_vibe_scheduler）与数据组（新闻/复盘/概念/AI因子
   挖掘）分别由 start_user_trading_schedulers / start_data_schedulers 启动。
2. start_background_services = 两组全启（单进程模式，与拆分前行为一致）。
3. 各启动函数幂等（重复调用不重复起线程）。
"""
import ast
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests._module_stubs import stub_project_module  # noqa: E402


def _fresh_schedulers(monkeypatch):
    """重置幂等标志 + 记录线程启动目标（不真正起线程）。"""
    from core import schedulers

    monkeypatch.setattr(schedulers, "_trading_started", False)
    monkeypatch.setattr(schedulers, "_data_started", False)
    monkeypatch.setattr(schedulers, "_data_threads", {})
    monkeypatch.setattr(schedulers, "_data_progress", {})

    started = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None, args=()):
            self._label = name or getattr(target, "__name__", str(target))

        def start(self):
            started.append(self._label)

        @staticmethod
        def is_alive():
            return True

    monkeypatch.setattr(schedulers.threading, "Thread", FakeThread)
    monkeypatch.setattr(schedulers, "_restore_all_user_states", lambda: started.append("restore_states"))
    monkeypatch.setattr(schedulers.um, "expire_stale_signals", lambda *a, **k: 0)

    # 数据组的四个模块全部打桩（不真跑新闻/复盘/概念/因子挖掘）
    monkeypatch.setitem(sys.modules, "news_center",
                        types.SimpleNamespace(run_news_scheduler=lambda: None))
    def start_concept_scheduler(*_args, **_kwargs):
        thread = FakeThread(name="concept")
        thread.start()
        return thread

    monkeypatch.setitem(
        sys.modules,
        "research_center",
        types.SimpleNamespace(
            run_concept_scheduler=start_concept_scheduler
        ),
    )
    stub_project_module(monkeypatch, "review_engine",
                        types.SimpleNamespace(run_review_scheduler=lambda **_kwargs: None))
    stub_project_module(monkeypatch, "auto_factor_scheduler",
                        types.SimpleNamespace(start_background_scheduler=lambda: started.append("auto_factor") or True))
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
    threads = schedulers.start_data_schedulers()
    assert "news-center" in started
    assert "review-engine" in started
    assert "concept" in started
    assert "auto_factor" in started
    assert "run_scheduler" not in started
    assert "vibe-scheduler" not in started
    assert "restore_states" not in started
    assert set(threads) == {
        "news-center",
        "review-engine",
        "concept-daily",
    }


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


def test_data_scheduler_startup_fails_closed_when_factor_scheduler_fails(
    monkeypatch,
):
    schedulers, started = _fresh_schedulers(monkeypatch)

    def fail_factor_scheduler():
        raise OSError("factor scheduler lock probe failed")

    stub_project_module(
        monkeypatch,
        "auto_factor_scheduler",
        types.SimpleNamespace(
            start_background_scheduler=fail_factor_scheduler
        ),
    )

    with pytest.raises(
        OSError,
        match="factor scheduler lock probe failed",
    ):
        schedulers.start_data_schedulers()

    assert schedulers._data_started is False
    assert "news-center" not in started
    assert "review-engine" not in started
    assert "concept" not in started


def test_scheduler_process_bypasses_blocking_atexit_handlers():
    source = (Path(__file__).parent.parent / "scheduler_main.py").read_text(encoding="utf-8")

    assert "exit_code = main()" in source
    assert "os._exit(exit_code)" in source


def test_scheduler_process_requires_successful_database_migration():
    source = (
        Path(__file__).parent.parent / "scheduler_main.py"
    ).read_text(encoding="utf-8")
    module = ast.parse(source)
    main_function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )

    direct_calls = [
        statement.value.func
        for statement in main_function.body
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
    ]

    assert any(
        isinstance(function, ast.Attribute)
        and function.attr == "migrate_users_db"
        for function in direct_calls
    )


def test_web_process_requires_successful_database_migration():
    source = (
        Path(__file__).parent.parent / "main.py"
    ).read_text(encoding="utf-8")
    module = ast.parse(source)
    init_function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_init_runtime"
    )
    migration_calls = [
        node
        for node in ast.walk(init_function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr
        in {"migrate_paper_trading_db", "migrate_users_db"}
    ]

    assert migration_calls
    for call in migration_calls:
        assert not any(
            isinstance(parent, ast.Try)
            and call in list(ast.walk(parent))
            for parent in ast.walk(init_function)
        )


def test_scheduler_supervisor_exits_when_factor_thread_dies():
    import scheduler_main

    class StopEvent:
        @staticmethod
        def wait(_timeout):
            return False

    class FactorScheduler:
        @staticmethod
        def refresh_runtime_heartbeat():
            return {
                "ready": False,
                "thread_alive": False,
            }

    assert scheduler_main._supervise_factor_scheduler(
        FactorScheduler(),
        stop_event=StopEvent(),
        interval_seconds=0,
        grace_seconds=60,
    ) == 1


def test_scheduler_supervisor_exits_after_persistent_not_ready():
    import scheduler_main

    class StopEvent:
        @staticmethod
        def wait(_timeout):
            return False

    class FactorScheduler:
        @staticmethod
        def refresh_runtime_heartbeat():
            return {
                "ready": False,
                "thread_alive": True,
            }

    clock = iter([100.0, 106.0])

    assert scheduler_main._supervise_factor_scheduler(
        FactorScheduler(),
        stop_event=StopEvent(),
        interval_seconds=0,
        grace_seconds=5,
        monotonic=lambda: next(clock),
    ) == 1


def test_scheduler_supervisor_allows_transient_not_ready():
    import scheduler_main

    class StopEvent:
        calls = 0

        def wait(self, _timeout):
            self.calls += 1
            return self.calls >= 3

    states = iter(
        [
            {"ready": False, "thread_alive": True},
            {"ready": True, "thread_alive": True},
        ]
    )

    class FactorScheduler:
        @staticmethod
        def refresh_runtime_heartbeat():
            return next(states)

    assert scheduler_main._supervise_factor_scheduler(
        FactorScheduler(),
        stop_event=StopEvent(),
        interval_seconds=0,
        grace_seconds=5,
        monotonic=iter([100.0, 101.0]).__next__,
    ) == 0


def test_scheduler_supervisor_allows_stale_loop_progress_during_active_job():
    import scheduler_main

    class StopEvent:
        calls = 0

        def wait(self, _timeout):
            self.calls += 1
            return self.calls >= 2

    class FactorScheduler:
        @staticmethod
        def refresh_runtime_heartbeat():
            return {
                "ready": True,
                "thread_alive": True,
                "progress_age_seconds": 7200,
                "job_running": True,
                "job_age_seconds": 7200,
            }

    assert scheduler_main._supervise_factor_scheduler(
        FactorScheduler(),
        stop_event=StopEvent(),
        interval_seconds=0,
        max_factor_progress_age_seconds=3600,
        max_factor_job_age_seconds=21600,
    ) == 0


def test_scheduler_supervisor_exits_when_active_job_exceeds_deadline():
    import scheduler_main

    class StopEvent:
        @staticmethod
        def wait(_timeout):
            return False

    class FactorScheduler:
        @staticmethod
        def refresh_runtime_heartbeat():
            return {
                "ready": True,
                "thread_alive": True,
                "progress_age_seconds": 1,
                "job_running": True,
                "job_age_seconds": 21601,
            }

    assert scheduler_main._supervise_factor_scheduler(
        FactorScheduler(),
        stop_event=StopEvent(),
        interval_seconds=0,
        max_factor_progress_age_seconds=3600,
        max_factor_job_age_seconds=21600,
    ) == 1


def test_scheduler_supervisor_exits_when_auxiliary_thread_dies():
    import scheduler_main

    class StopEvent:
        @staticmethod
        def wait(_timeout):
            return False

    class FactorScheduler:
        @staticmethod
        def refresh_runtime_heartbeat():
            return {
                "ready": True,
                "thread_alive": True,
                "progress_age_seconds": 1,
            }

    assert scheduler_main._supervise_factor_scheduler(
        FactorScheduler(),
        data_runtime=lambda: {
            "news-center": {
                "thread_alive": False,
                "progress_age_seconds": 1,
            }
        },
        stop_event=StopEvent(),
        interval_seconds=0,
    ) == 1


def test_scheduler_supervisor_exits_when_auxiliary_progress_stalls():
    import scheduler_main

    class StopEvent:
        @staticmethod
        def wait(_timeout):
            return False

    class FactorScheduler:
        @staticmethod
        def refresh_runtime_heartbeat():
            return {
                "ready": True,
                "thread_alive": True,
                "progress_age_seconds": 1,
            }

    assert scheduler_main._supervise_factor_scheduler(
        FactorScheduler(),
        data_runtime=lambda: {
            "review-engine": {
                "thread_alive": True,
                "progress_age_seconds": 901,
            }
        },
        max_data_progress_age_seconds=900,
        stop_event=StopEvent(),
        interval_seconds=0,
    ) == 1


def test_market_close_keeps_enabled_strategies_for_next_trading_day(monkeypatch):
    from core import schedulers

    state = {
        "running": True,
        "queue": [
            {"code": "000001", "status": "pending"},
            {"code": "000002", "status": "done"},
        ],
    }
    cancelled = []
    monkeypatch.setattr(schedulers, "user_states", {901: state})
    monkeypatch.setattr(schedulers, "get_user_state", lambda uid: state)
    monkeypatch.setattr(schedulers, "_snap_all_paper_equity", lambda: None)
    monkeypatch.setattr(
        schedulers.um,
        "cancel_pending_signals",
        lambda uid: cancelled.append(uid) or 1,
    )
    monkeypatch.setattr(schedulers, "_add_log", lambda *args, **kwargs: None)

    schedulers._on_market_close()

    assert state["running"] is True
    assert state["queue"] == [{"code": "000002", "status": "done"}]
    assert "_persist_state" not in schedulers._on_market_close.__code__.co_names
    assert cancelled == [901]


def test_scheduler_does_not_dispatch_scans_outside_trading_hours(monkeypatch):
    from core import schedulers

    class StopLoop(Exception):
        pass

    started = []
    monkeypatch.setattr(
        schedulers,
        "user_states",
        {902: {"running": True, "active_strategy": "custom.py"}},
    )
    monkeypatch.setattr(schedulers, "is_trading_time", lambda: False)
    monkeypatch.setattr(
        schedulers.threading,
        "Thread",
        lambda *args, **kwargs: started.append((args, kwargs)),
    )
    monkeypatch.setattr(
        schedulers.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(StopLoop()),
    )

    with pytest.raises(StopLoop):
        schedulers.run_scheduler()

    assert started == []
