# -*- coding: utf-8 -*-
import errno
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _verified_test_calendar(monkeypatch):
    from market import trading_calendar

    monkeypatch.setattr(
        trading_calendar,
        "has_verified_calendar",
        lambda _year: True,
    )
    monkeypatch.setattr(
        trading_calendar,
        "is_trading_day",
        lambda value=None: (
            value is not None
            and value.weekday() < 5
        ),
    )


def _isolate_scheduler(monkeypatch, tmp_path):
    from analytics import auto_factor_scheduler as scheduler

    root = tmp_path / "relay_factor"
    monkeypatch.setattr(scheduler, "ROOT", root)
    monkeypatch.setattr(scheduler, "STATUS_PATH", root / "auto_scheduler_status.json")
    monkeypatch.setattr(scheduler, "LOCK_PATH", root / "auto_scheduler.lock")
    monkeypatch.setattr(
        scheduler,
        "RUNTIME_PATH",
        root / "auto_scheduler_runtime.json",
        raising=False,
    )
    return scheduler, root


def _structured_lock(
    *,
    pid=4321,
    process_start_token="start-1",
    nonce="nonce-1",
    created_at="2026-07-31 09:30:00",
):
    return {
        "pid": pid,
        "process_start_token": process_start_token,
        "nonce": nonce,
        "created_at": created_at,
    }


def _write_structured_lock(scheduler, payload):
    scheduler.LOCK_PATH.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _start_scheduler_lock_process(root, mode):
    ready_path = root / f"child-{mode}.json"
    script = """
import json
import os
import sys
import time
from pathlib import Path
from analytics import auto_factor_scheduler as scheduler

root = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
mode = sys.argv[3]
scheduler.ROOT = root
scheduler.STATUS_PATH = root / "auto_scheduler_status.json"
scheduler.LOCK_PATH = root / "auto_scheduler.lock"
with scheduler._file_lock() as acquired:
    ready_path.write_text(
        json.dumps({"acquired": acquired, "pid": os.getpid()}),
        encoding="utf-8",
    )
    if not acquired:
        raise SystemExit(2)
    if mode == "crash":
        os._exit(17)
    time.sleep(60)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            script,
            str(root),
            str(ready_path),
            mode,
        ],
        cwd=Path(__file__).resolve().parent.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if ready_path.exists():
            return process, json.loads(
                ready_path.read_text(encoding="utf-8")
            )
        if process.poll() is not None:
            break
        time.sleep(0.05)
    _stdout, stderr = process.communicate(timeout=5)
    raise AssertionError(
        f"scheduler lock child did not become ready: {stderr}"
    )


def test_file_lock_writes_structured_process_identity(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(
        scheduler,
        "_process_start_token",
        lambda pid: f"start-{pid}",
        raising=False,
    )

    with scheduler._file_lock() as acquired:
        assert acquired is True
        payload = json.loads(
            scheduler.LOCK_PATH.read_text(encoding="utf-8")
        )

        assert set(payload) == {
            "pid",
            "process_start_token",
            "nonce",
            "created_at",
            "guard_protocol",
        }
        assert payload["pid"] == scheduler.os.getpid()
        assert payload["process_start_token"] == (
            f"start-{scheduler.os.getpid()}"
        )
        assert isinstance(payload["nonce"], str) and payload["nonce"]
        assert payload["guard_protocol"] == (
            "scheduler_kernel_guard_v1"
        )
        datetime.strptime(
            payload["created_at"],
            "%Y-%m-%d %H:%M:%S",
        )


def test_structured_lock_detects_reused_pid(monkeypatch, tmp_path):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    _write_structured_lock(
        scheduler,
        _structured_lock(process_start_token="old-process"),
    )
    monkeypatch.setattr(scheduler, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(
        scheduler,
        "_process_start_token",
        lambda pid: "new-process",
        raising=False,
    )

    assert scheduler._unlink_stale_lock() is True
    assert not scheduler.LOCK_PATH.exists()


def test_structured_lock_normalizes_string_pid_before_process_probe(
    monkeypatch,
    tmp_path,
):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    _write_structured_lock(
        scheduler,
        _structured_lock(pid="1234"),
    )
    observed = []
    monkeypatch.setattr(
        scheduler,
        "_pid_exists",
        lambda pid: observed.append(pid) or False,
    )

    assert scheduler._lock_is_stale() is True
    assert observed == [1234]


def test_live_structured_lock_is_preserved_even_when_old(
    monkeypatch,
    tmp_path,
):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    _write_structured_lock(scheduler, _structured_lock())
    lock_mtime = scheduler.LOCK_PATH.stat().st_mtime
    monkeypatch.setattr(
        scheduler.time,
        "time",
        lambda: lock_mtime + 10_000,
    )
    monkeypatch.setattr(scheduler, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(
        scheduler,
        "_process_start_token",
        lambda pid: "start-1",
        raising=False,
    )

    assert scheduler._unlink_stale_lock(max_age_seconds=1) is False
    assert scheduler.LOCK_PATH.exists()


def test_file_lock_does_not_release_replacement_lock(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(
        scheduler,
        "_process_start_token",
        lambda pid: "owner-start",
        raising=False,
    )
    replacement = _structured_lock(
        pid=scheduler.os.getpid(),
        process_start_token="owner-start",
        nonce="replacement-nonce",
    )

    with scheduler._file_lock() as acquired:
        assert acquired is True
        _write_structured_lock(scheduler, replacement)

    assert json.loads(
        scheduler.LOCK_PATH.read_text(encoding="utf-8")
    ) == replacement


def test_stale_cleanup_rechecks_lock_identity_before_unlink(
    monkeypatch,
    tmp_path,
):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    _write_structured_lock(
        scheduler,
        _structured_lock(pid=4321),
    )
    replacement = _structured_lock(
        pid=9876,
        nonce="replacement-nonce",
    )

    def replace_lock_while_checking_pid(pid):
        assert pid == 4321
        _write_structured_lock(scheduler, replacement)
        return False

    monkeypatch.setattr(
        scheduler,
        "_pid_exists",
        replace_lock_while_checking_pid,
    )

    assert scheduler._unlink_stale_lock() is False
    assert json.loads(
        scheduler.LOCK_PATH.read_text(encoding="utf-8")
    ) == replacement


def test_live_legacy_pid_lock_is_preserved_even_when_old(
    monkeypatch,
    tmp_path,
):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    scheduler.LOCK_PATH.write_text(
        "4321 2026-07-10 17:05:00",
        encoding="utf-8",
    )
    lock_mtime = scheduler.LOCK_PATH.stat().st_mtime
    monkeypatch.setattr(
        scheduler.time,
        "time",
        lambda: lock_mtime + 10_000,
    )
    monkeypatch.setattr(
        scheduler,
        "_pid_exists",
        lambda pid: pid == 4321,
    )

    assert scheduler._unlink_stale_lock(max_age_seconds=1) is False
    assert scheduler.LOCK_PATH.exists()


def test_linux_process_start_token_reads_proc_stat_field_22(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    fields_3_to_22 = [
        "S",
        *[str(field) for field in range(4, 22)],
        "987654321",
    ]

    def fake_read_text(path, encoding=None, errors=None):
        assert encoding == "utf-8"
        if path.as_posix() == "/proc/sys/kernel/random/boot_id":
            return "boot-id-1\n"
        assert path.as_posix() == "/proc/4321/stat"
        return (
            f"4321 (worker name (batch)) "
            f"{' '.join(fields_3_to_22)}\n"
        )

    monkeypatch.setattr(
        scheduler.Path,
        "read_text",
        fake_read_text,
    )

    assert (
        scheduler._linux_process_start_token(4321)
        == "boot-id-1:987654321"
    )


def test_windows_process_start_token_uses_creation_filetime(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    class FakeKernel32:
        def __init__(self):
            self.open_args = None
            self.closed_handle = None

        def OpenProcess(self, access, inherit_handle, pid):
            self.open_args = (access, inherit_handle, pid)
            return 123

        def GetProcessTimes(
            self,
            handle,
            creation,
            exit_time,
            kernel,
            user,
        ):
            assert handle == 123
            creation._obj.dwHighDateTime = 0x12345678
            creation._obj.dwLowDateTime = 0x9ABCDEF0
            for value in (exit_time, kernel, user):
                value._obj.dwHighDateTime = 0
                value._obj.dwLowDateTime = 0
            return True

        def CloseHandle(self, handle):
            self.closed_handle = handle
            return True

    kernel32 = FakeKernel32()

    token = scheduler._windows_process_start_token(
        2468,
        kernel32=kernel32,
    )

    assert token == "1311768467463790320"
    assert kernel32.open_args == (0x1000, False, 2468)
    assert kernel32.closed_handle == 123


def test_linux_process_start_epoch_uses_boot_time_and_clock_ticks(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(scheduler.sys, "platform", "linux")
    monkeypatch.setattr(
        scheduler,
        "_process_start_token",
        lambda pid: "250",
    )
    monkeypatch.setattr(
        scheduler.Path,
        "read_text",
        lambda path, **kwargs: "cpu 1 2 3\nbtime 1000\n",
    )
    monkeypatch.setattr(
        scheduler.os,
        "sysconf",
        lambda name: 100,
        raising=False,
    )

    assert scheduler._process_start_epoch(4321) == 1002.5


def test_legacy_linux_token_rejects_same_ticks_from_new_boot(
    monkeypatch,
    tmp_path,
):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    _write_structured_lock(
        scheduler,
        _structured_lock(
            process_start_token="250",
            created_at="2026-07-10 17:05:00",
        ),
    )
    monkeypatch.setattr(scheduler, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(
        scheduler,
        "_process_start_token",
        lambda pid: "new-boot-id:250",
    )
    monkeypatch.setattr(
        scheduler,
        "_process_start_epoch",
        lambda pid: datetime(
            2026,
            7,
            11,
            9,
            30,
        ).timestamp(),
    )

    assert scheduler._lock_is_stale() is True


def test_legacy_linux_token_without_boot_id_checks_process_epoch(
    monkeypatch,
    tmp_path,
):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    _write_structured_lock(
        scheduler,
        _structured_lock(
            process_start_token="250",
            created_at="2026-07-10 17:05:00",
        ),
    )
    monkeypatch.setattr(scheduler, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(
        scheduler,
        "_process_start_token",
        lambda pid: "250",
    )
    monkeypatch.setattr(
        scheduler,
        "_process_start_epoch",
        lambda pid: datetime(
            2026,
            7,
            11,
            9,
            30,
        ).timestamp(),
    )

    assert scheduler._lock_is_stale() is True


def test_windows_process_start_epoch_converts_filetime(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    unix_epoch = 123_456_789
    filetime = 116_444_736_000_000_000 + unix_epoch * 10_000_000
    monkeypatch.setattr(scheduler.sys, "platform", "win32")
    monkeypatch.setattr(
        scheduler,
        "_process_start_token",
        lambda pid: str(filetime),
    )

    assert scheduler._process_start_epoch(2468) == float(unix_epoch)


def test_file_lock_propagates_body_file_exists_error(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    with pytest.raises(FileExistsError, match="body failure"):
        with scheduler._file_lock() as acquired:
            assert acquired is True
            raise FileExistsError("body failure")


def test_recovery_holds_guard_while_clearing_stale_status(
    monkeypatch,
    tmp_path,
):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    lock = _structured_lock(pid=999999)
    lock["guard_protocol"] = scheduler.SCHEDULER_GUARD_PROTOCOL_VERSION
    _write_structured_lock(scheduler, lock)
    status = {
        "ok": True,
        "enabled": True,
        "running": True,
        "current_job": "factor_mining",
        "updated_at": "2026-07-10 17:05:00",
        "jobs": {},
        "history": [],
    }
    scheduler.STATUS_PATH.write_text(
        json.dumps(status),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        scheduler,
        "_pid_exists",
        lambda pid: False,
    )
    real_save = scheduler._save_status
    nested_acquisitions = []

    def save_while_probing_guard(value):
        with scheduler._file_lock() as acquired:
            nested_acquisitions.append(acquired)
        real_save(value)

    monkeypatch.setattr(
        scheduler,
        "_save_status",
        save_while_probing_guard,
    )

    recovered = scheduler._recover_stale_runtime_state(status)

    assert recovered["running"] is False
    assert nested_acquisitions == [False]


@pytest.mark.parametrize(
    "payload",
    [
        "4321 2026-07-10 17:05:00",
        "{",
        json.dumps(
            _structured_lock(
                pid=4321,
                process_start_token=None,
                created_at="2026-07-10 17:05:00",
            )
        ),
    ],
)
def test_legacy_or_incomplete_lock_is_not_reclaimed_during_normal_run(
    monkeypatch,
    tmp_path,
    payload,
):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    scheduler.LOCK_PATH.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(scheduler, "_pid_exists", lambda pid: False)

    assert scheduler._lock_is_stale() is False
    assert scheduler._unlink_stale_lock() is False
    assert scheduler.LOCK_PATH.exists()


def test_unknown_pid_state_never_reclaims_lock(monkeypatch, tmp_path):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    scheduler.LOCK_PATH.write_text(
        "4321 2026-07-10 17:05:00",
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler, "_pid_exists", lambda pid: None)

    assert scheduler._lock_is_stale() is False
    assert scheduler._unlink_stale_lock() is False
    assert scheduler.LOCK_PATH.exists()


def test_metadata_write_failure_releases_kernel_guard(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    real_publish = scheduler._publish_lock_metadata

    def fail_metadata_write(owner):
        raise OSError("metadata write failed")

    monkeypatch.setattr(
        scheduler,
        "_publish_lock_metadata",
        fail_metadata_write,
    )
    with pytest.raises(OSError, match="metadata write failed"):
        with scheduler._file_lock():
            pass

    monkeypatch.setattr(
        scheduler,
        "_publish_lock_metadata",
        real_publish,
    )
    with scheduler._file_lock() as acquired:
        assert acquired is True


def test_unsupported_hard_link_fails_closed_with_actionable_error(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    def reject_hard_link(source, destination):
        raise OSError(errno.EOPNOTSUPP, "not supported")

    monkeypatch.setattr(scheduler.os, "link", reject_hard_link)

    with pytest.raises(
        OSError,
        match="requires atomic hard-link support",
    ):
        with scheduler._file_lock():
            pass

    assert scheduler._scheduler_guard_path().exists()
    assert not scheduler.LOCK_PATH.exists()


def test_run_job_records_lock_capability_failure(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    def reject_hard_link(source, destination):
        raise OSError(errno.EOPNOTSUPP, "not supported")

    monkeypatch.setattr(scheduler.os, "link", reject_hard_link)
    monkeypatch.setattr(
        scheduler,
        "_today_text",
        lambda: "2026-07-31",
    )

    result = scheduler.run_job(
        "factor_mining",
        slot="17:10",
        force=True,
    )

    assert result["ok"] is False
    assert "hard-link support" in result["error"]
    status = scheduler.get_status()
    record = status["jobs"]["2026-07-31:factor_mining:1710"]
    assert record["ok"] is False
    assert "hard-link support" in record["result"]["error"]


def test_background_scheduler_fails_before_started_when_lock_probe_fails(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    started_threads = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            started_threads.append(True)

    def fail_probe():
        raise OSError("lock capability unavailable")

    monkeypatch.setattr(scheduler, "_started", False)
    monkeypatch.setattr(scheduler.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        scheduler,
        "_probe_lock_capability",
        fail_probe,
        raising=False,
    )

    with pytest.raises(
        OSError,
        match="lock capability unavailable",
    ):
        scheduler.start_background_scheduler()

    assert scheduler._started is False
    assert started_threads == []


def test_scheduler_refreshes_missing_authoritative_calendar(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from market import trading_calendar

    checks = iter([False, True])
    refreshes = []
    monkeypatch.setattr(
        trading_calendar,
        "has_verified_calendar",
        lambda year: next(checks),
    )
    monkeypatch.setattr(
        trading_calendar,
        "refresh_from_tushare",
        lambda year: refreshes.append(year)
        or {"ok": True, "year": year},
    )

    ready, reason = scheduler._ensure_calendar_readiness(
        datetime(2026, 7, 31, 9, 0)
    )

    assert ready is True
    assert reason == "ok"
    assert refreshes == [2026]


def test_background_scheduler_does_not_start_without_verified_calendar(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    started_threads = []

    class FakeEvent:
        @staticmethod
        def clear():
            return None

        @staticmethod
        def wait(_timeout):
            return False

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started_threads.append((args, kwargs))

    monkeypatch.setattr(scheduler, "_started", False)
    monkeypatch.setattr(
        scheduler,
        "_scheduler_ready_event",
        FakeEvent(),
    )
    monkeypatch.setattr(
        scheduler,
        "_probe_lock_capability",
        lambda: None,
    )
    monkeypatch.setattr(
        scheduler,
        "_ensure_calendar_readiness",
        lambda: (
            False,
            "authoritative_trade_calendar_unavailable_after_refresh",
        ),
        raising=False,
    )
    monkeypatch.setattr(scheduler.threading, "Thread", FakeThread)

    with pytest.raises(
        RuntimeError,
        match="authoritative_trade_calendar_unavailable_after_refresh",
    ):
        scheduler.start_background_scheduler()

    assert scheduler._started is False
    assert started_threads == []


def test_scheduler_first_cycle_rejects_invalid_config_before_ready(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    writes = []
    monkeypatch.setattr(
        scheduler,
        "get_config",
        lambda: {
            **scheduler.DEFAULT_CONFIG,
            "window_minutes": "not-an-integer",
        },
    )
    monkeypatch.setattr(
        scheduler,
        "_write_runtime_status",
        lambda **kwargs: writes.append(kwargs) or kwargs,
    )
    monkeypatch.setattr(
        scheduler.time,
        "sleep",
        lambda _seconds: pytest.fail(
            "invalid first-cycle config must terminate the thread"
        ),
    )
    scheduler._scheduler_ready_event.clear()

    scheduler.run_scheduler_loop(interval_seconds=5)

    assert scheduler._scheduler_ready_event.is_set()
    assert writes == [
        {
            "ready": False,
            "thread_alive": False,
            "calendar_ready": False,
            "calendar_reason": "scheduler_startup_error",
            "error": "window_minutes must be an integer",
        }
    ]


def test_scheduler_calendar_failure_is_published_as_not_ready(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    writes = []

    class StopLoop(Exception):
        pass

    monkeypatch.setattr(
        scheduler,
        "_validated_scheduler_config",
        lambda: dict(scheduler.DEFAULT_CONFIG),
    )
    monkeypatch.setattr(
        scheduler,
        "_calendar_readiness",
        lambda _now: (
            False,
            "authoritative_trade_calendar_unavailable",
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "_write_runtime_status",
        lambda **kwargs: writes.append(kwargs) or kwargs,
    )
    monkeypatch.setattr(
        scheduler.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(StopLoop()),
    )
    scheduler._scheduler_ready_event.clear()

    with pytest.raises(StopLoop):
        scheduler.run_scheduler_loop(interval_seconds=5)

    assert writes[0]["ready"] is False
    assert writes[0]["calendar_ready"] is False
    assert scheduler._scheduler_ready_event.is_set()


def test_runtime_status_binds_process_start_token(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(
        scheduler,
        "_process_start_token",
        lambda pid: f"boot-token:{pid}",
    )

    payload = scheduler._write_runtime_status(
        ready=True,
        thread_alive=True,
        calendar_ready=True,
        calendar_reason="ok",
    )

    assert payload["process_start_token"] == (
        f"boot-token:{os.getpid()}"
    )
    assert json.loads(
        scheduler.RUNTIME_PATH.read_text(encoding="utf-8")
    )["process_start_token"] == payload["process_start_token"]


def test_runtime_heartbeat_refreshes_while_job_thread_is_alive(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    class AliveThread:
        @staticmethod
        def is_alive():
            return True

    monkeypatch.setattr(scheduler, "_scheduler_thread", AliveThread())
    monkeypatch.setattr(
        scheduler,
        "_process_start_token",
        lambda _pid: "boot-token:123",
    )
    scheduler._write_runtime_status(
        ready=True,
        thread_alive=True,
        calendar_ready=True,
        calendar_reason="ok",
    )
    first = json.loads(
        scheduler.RUNTIME_PATH.read_text(encoding="utf-8")
    )
    first_progress = first.get("progress_at")
    monkeypatch.setattr(
        scheduler,
        "_runtime_now",
        lambda: "2026-07-31T17:10:30+08:00",
        raising=False,
    )

    refreshed = scheduler.refresh_runtime_heartbeat()

    assert refreshed["ready"] is True
    assert refreshed["thread_alive"] is True
    assert refreshed["heartbeat_at"] == "2026-07-31T17:10:30+08:00"
    assert refreshed.get("progress_at") == first_progress
    assert refreshed["started_at"] == first["started_at"]
    assert refreshed["process_start_token"] == "boot-token:123"


def test_runtime_heartbeat_does_not_forge_scheduler_progress(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    class AliveThread:
        @staticmethod
        def is_alive():
            return True

    monkeypatch.setattr(scheduler, "_scheduler_thread", AliveThread())
    monkeypatch.setattr(
        scheduler,
        "_last_scheduler_progress_at",
        "2026-07-31T16:00:00+08:00",
        raising=False,
    )
    scheduler._write_runtime_status(
        ready=True,
        thread_alive=True,
        calendar_ready=True,
        calendar_reason="ok",
    )
    monkeypatch.setattr(
        scheduler,
        "_runtime_now",
        lambda: "2026-07-31T17:10:30+08:00",
    )

    refreshed = scheduler.refresh_runtime_heartbeat()

    assert refreshed["heartbeat_at"] == "2026-07-31T17:10:30+08:00"
    assert refreshed["progress_at"] == "2026-07-31T16:00:00+08:00"


def test_runtime_heartbeat_reports_active_job_without_forging_progress(
    monkeypatch,
    tmp_path,
):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    scheduler.STATUS_PATH.write_text(
        json.dumps(
            {
                "running": True,
                "current_job": "factor_mining",
                "current_job_started_at": (
                    "2026-07-31T17:10:00+08:00"
                ),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        scheduler,
        "_timestamp_age_seconds",
        lambda value: (
            30.0
            if value == "2026-07-31T17:10:00+08:00"
            else 4200.0
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "_last_scheduler_progress_at",
        "2026-07-31T16:00:00+08:00",
    )

    payload = scheduler._write_runtime_status(
        ready=True,
        thread_alive=True,
        calendar_ready=True,
        calendar_reason="ok",
    )

    assert payload["job_running"] is True
    assert payload["current_job"] == "factor_mining"
    assert payload["job_started_at"] == (
        "2026-07-31T17:10:00+08:00"
    )
    assert payload["job_age_seconds"] == 30.0
    assert payload["progress_age_seconds"] == 4200.0


def test_windows_unsupported_hard_link_has_actionable_error(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    def reject_hard_link(source, destination):
        error = OSError(errno.EINVAL, "not supported")
        error.winerror = 50
        raise error

    monkeypatch.setattr(scheduler.os, "link", reject_hard_link)

    with pytest.raises(
        OSError,
        match="requires atomic hard-link support",
    ):
        with scheduler._file_lock():
            pass


def test_scheduler_guard_file_is_persistent(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    with scheduler._file_lock() as acquired:
        assert acquired is True
        guard_path = scheduler._scheduler_guard_path()
        assert guard_path.exists()

    assert guard_path.exists()
    assert not scheduler.LOCK_PATH.exists()


def test_file_lock_does_not_overwrite_legacy_claim_during_publish(
    monkeypatch,
    tmp_path,
):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    legacy_payload = (
        f"{scheduler.os.getpid()} 2026-07-30 09:30:00"
    )
    real_link = scheduler.os.link

    def legacy_claim_before_publish(source, destination):
        scheduler.LOCK_PATH.write_text(
            legacy_payload,
            encoding="utf-8",
        )
        return real_link(source, destination)

    monkeypatch.setattr(
        scheduler.os,
        "link",
        legacy_claim_before_publish,
    )

    with scheduler._file_lock() as acquired:
        assert acquired is False

    assert root.exists()
    assert scheduler.LOCK_PATH.read_text(
        encoding="utf-8"
    ) == legacy_payload


def test_current_guard_protocol_orphan_does_not_block_next_run(
    monkeypatch,
    tmp_path,
):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    monkeypatch.setattr(
        scheduler,
        "_process_start_token",
        lambda pid: "start-1",
    )
    orphan = _structured_lock(
        pid=scheduler.os.getpid(),
        process_start_token="start-1",
    )
    orphan["guard_protocol"] = "scheduler_kernel_guard_v1"
    _write_structured_lock(scheduler, orphan)

    with scheduler._file_lock() as acquired:
        assert acquired is True


def test_metadata_validation_read_failure_cleans_owned_inode(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    real_read = scheduler._read_lock_record
    monkeypatch.setattr(
        scheduler,
        "_read_lock_record",
        lambda: None,
    )

    with scheduler._file_lock() as acquired:
        assert acquired is False

    monkeypatch.setattr(
        scheduler,
        "_read_lock_record",
        real_read,
    )
    assert not scheduler.LOCK_PATH.exists()
    with scheduler._file_lock() as acquired:
        assert acquired is True


def test_transient_metadata_unlink_failure_is_recovered_on_next_run(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    real_unlink = scheduler.Path.unlink
    failed_once = False

    def fail_first_owner_unlink(path, *args, **kwargs):
        nonlocal failed_once
        if path == scheduler.LOCK_PATH and not failed_once:
            failed_once = True
            raise OSError("transient unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        scheduler.Path,
        "unlink",
        fail_first_owner_unlink,
    )

    with scheduler._file_lock() as acquired:
        assert acquired is True
    assert scheduler.LOCK_PATH.exists()

    with scheduler._file_lock() as acquired:
        assert acquired is True


def test_windows_pid_probe_treats_terminated_process_as_dead(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    class FakeKernel32:
        def __init__(self):
            self.closed = None

        def OpenProcess(self, access, inherit_handle, pid):
            return 321

        def WaitForSingleObject(self, handle, timeout_ms):
            assert handle == 321
            assert timeout_ms == 0
            return 0

        def CloseHandle(self, handle):
            self.closed = handle
            return True

    kernel32 = FakeKernel32()

    assert (
        scheduler._windows_pid_exists(
            4321,
            kernel32=kernel32,
        )
        is False
    )
    assert kernel32.closed == 321


def test_windows_pid_probe_treats_wait_timeout_as_running(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    class FakeKernel32:
        def OpenProcess(self, access, inherit_handle, pid):
            return 321

        def WaitForSingleObject(self, handle, timeout_ms):
            return 258

        def CloseHandle(self, handle):
            return True

    assert (
        scheduler._windows_pid_exists(
            4321,
            kernel32=FakeKernel32(),
        )
        is True
    )


def test_windows_pid_probe_rejects_dword_wraparound(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    class FakeKernel32:
        def __init__(self):
            self.open_calls = []

        def OpenProcess(self, access, inherit_handle, pid):
            self.open_calls.append(pid)
            return 321

        def WaitForSingleObject(self, handle, timeout_ms):
            return 258

        def CloseHandle(self, handle):
            return True

    kernel32 = FakeKernel32()

    assert (
        scheduler._windows_pid_exists(
            scheduler.os.getpid() + 2**32,
            kernel32=kernel32,
        )
        is False
    )
    assert kernel32.open_calls == []


def test_windows_start_token_rejects_dword_wraparound(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    class FakeKernel32:
        def __init__(self):
            self.open_calls = []

        def OpenProcess(self, access, inherit_handle, pid):
            self.open_calls.append(pid)
            return 321

        def GetProcessTimes(self, *args):
            return True

        def CloseHandle(self, handle):
            return True

    kernel32 = FakeKernel32()

    assert (
        scheduler._windows_process_start_token(
            scheduler.os.getpid() + 2**32,
            kernel32=kernel32,
        )
        is None
    )
    assert kernel32.open_calls == []


@pytest.mark.parametrize("error_type", [OverflowError, ValueError])
def test_linux_pid_probe_rejects_unrepresentable_pid(
    monkeypatch,
    tmp_path,
    error_type,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(
        scheduler,
        "_read_linux_process_stat",
        lambda pid: None,
    )

    def reject_pid(pid, signal):
        raise error_type("pid out of range")

    monkeypatch.setattr(scheduler.os, "kill", reject_pid)

    assert scheduler._linux_pid_exists(10**100) is False


def test_linux_pid_probe_rejects_zombie_process(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(
        scheduler.Path,
        "read_text",
        lambda path, **kwargs: (
            "4321 (worker name) Z 1 2 3 4 5 6 7 8 9 10 "
            "11 12 13 14 15 16 17 18 19 20\n"
        ),
    )

    assert scheduler._linux_pid_exists(4321) is False


def test_kernel_guard_excludes_real_subprocess(
    monkeypatch,
    tmp_path,
):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    process, ready = _start_scheduler_lock_process(root, "hold")
    try:
        assert ready["acquired"] is True
        with scheduler._file_lock() as acquired:
            assert acquired is False
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)

    deadline = time.monotonic() + 1.0
    while True:
        with scheduler._file_lock() as acquired:
            if acquired:
                break
        if time.monotonic() >= deadline:
            pytest.fail("kernel guard was not released after subprocess exit")
        time.sleep(0.01)


def test_kernel_guard_recovers_after_subprocess_os_exit(
    monkeypatch,
    tmp_path,
):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    process, ready = _start_scheduler_lock_process(root, "crash")

    assert ready["acquired"] is True
    assert process.wait(timeout=10) == 17
    with scheduler._file_lock() as acquired:
        assert acquired is True


@pytest.mark.skipif(os.name != "nt", reason="Windows os.kill semantics")
def test_pid_probe_does_not_terminate_the_target_process(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        assert scheduler._pid_exists(process.pid) is True
        time.sleep(0.1)
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def test_status_recovers_dead_pid_lock(monkeypatch, tmp_path):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    lock = _structured_lock(pid=999999)
    lock["guard_protocol"] = scheduler.SCHEDULER_GUARD_PROTOCOL_VERSION
    _write_structured_lock(scheduler, lock)
    scheduler.STATUS_PATH.write_text(
        json.dumps(
            {
                "ok": True,
                "enabled": True,
                "running": True,
                "current_job": "factor_mining",
                "updated_at": "2026-07-10 17:05:00",
                "jobs": {},
                "history": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler, "_pid_exists", lambda pid: False, raising=False)

    status = scheduler.get_status()

    assert status["running"] is False
    assert "current_job" not in status
    assert status["history"][0]["job"] == "factor_mining"
    assert status["history"][0]["ok"] is False
    assert status["history"][0]["error"] == "stale scheduler lock recovered"
    assert not scheduler.LOCK_PATH.exists()


def test_live_pid_lock_is_not_reclaimed_only_because_it_is_old(monkeypatch, tmp_path):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    scheduler.LOCK_PATH.write_text("12345 2026-07-10 17:05:00", encoding="utf-8")
    lock_mtime = scheduler.LOCK_PATH.stat().st_mtime
    monkeypatch.setattr(scheduler, "_pid_exists", lambda pid: pid == 12345)
    monkeypatch.setattr(
        scheduler.time,
        "time",
        lambda: lock_mtime + 7201,
    )

    assert scheduler._lock_is_stale(max_age_seconds=7200) is False
    assert scheduler._unlink_stale_lock(max_age_seconds=7200) is False
    assert scheduler.LOCK_PATH.exists()


def test_run_job_ignores_dead_pid_lock(monkeypatch, tmp_path):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    lock = _structured_lock(pid=999999)
    lock["guard_protocol"] = scheduler.SCHEDULER_GUARD_PROTOCOL_VERSION
    _write_structured_lock(scheduler, lock)
    scheduler.STATUS_PATH.write_text(
        json.dumps(
            {
                "ok": True,
                "enabled": True,
                "running": True,
                "current_job": "factor_mining",
                "updated_at": "2026-07-10 17:05:00",
                "jobs": {},
                "history": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler, "_pid_exists", lambda pid: False, raising=False)
    monkeypatch.setattr(scheduler, "_run_factor_mining", lambda date_text: {"ok": True, "candidate_count": 3})

    result = scheduler.run_job("factor_mining", slot="17:05", force=True)

    assert result == {"ok": True, "candidate_count": 3}
    status = scheduler.get_status()
    assert status["running"] is False
    assert status["jobs"]
    assert not scheduler.LOCK_PATH.exists()


def test_run_job_publishes_and_clears_active_job_start_time(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(
        scheduler,
        "_runtime_now",
        lambda: "2026-07-31T17:10:00+08:00",
    )
    observed = {}

    def run_factor_mining(_date_text):
        observed.update(
            json.loads(
                scheduler.STATUS_PATH.read_text(encoding="utf-8")
            )
        )
        return {"ok": True}

    monkeypatch.setattr(
        scheduler,
        "_run_factor_mining",
        run_factor_mining,
    )

    assert scheduler.run_job(
        "factor_mining",
        slot="17:10",
        force=True,
    ) == {"ok": True}

    assert observed["running"] is True
    assert observed["current_job"] == "factor_mining"
    assert observed["current_job_started_at"] == (
        "2026-07-31T17:10:00+08:00"
    )
    finished = json.loads(
        scheduler.STATUS_PATH.read_text(encoding="utf-8")
    )
    assert finished["running"] is False
    assert "current_job_started_at" not in finished


def test_run_job_records_execution_failure_before_releasing_file_lock(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(
        scheduler,
        "_today_text",
        lambda: "2026-07-31",
    )
    monkeypatch.setattr(
        scheduler,
        "_run_factor_mining",
        lambda _date: (_ for _ in ()).throw(
            RuntimeError("research failed")
        ),
    )
    real_save = scheduler._save_status
    completion_lock_states = []

    def capture_save(status):
        if status.get("jobs"):
            completion_lock_states.append(
                scheduler.LOCK_PATH.exists()
            )
        real_save(status)

    monkeypatch.setattr(scheduler, "_save_status", capture_save)

    result = scheduler.run_job(
        "factor_mining",
        slot="17:10",
        force=True,
    )

    assert result == {"ok": False, "error": "research failed"}
    assert completion_lock_states == [True]


def test_default_factor_mining_next_run_is_1710(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    next_runs = scheduler.next_runs(datetime(2026, 7, 10, 16, 59))

    assert scheduler.get_config()["factor_mining_time"] == "17:10"
    assert next_runs["factor_mining"] == "2026-07-10 17:10"


def test_default_factor_mining_history_can_cover_research_window_gate(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.relay_factor_mining import RESEARCH_VALIDATION_RULES

    mining = scheduler.get_config()["mining"]
    required_days = (
        60
        + int(mining["train_days"])
        + int(mining["test_days"])
        * int(RESEARCH_VALIDATION_RULES["minimum_oos_windows"])
        + int(mining["hold_days"])
        + 1
    )

    assert int(mining["days"]) >= required_days


def test_scheduler_uses_trade_calendar_and_finds_next_open_session(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from market import trading_calendar

    monkeypatch.setattr(
        trading_calendar,
        "is_trading_day",
        lambda value=None: getattr(value, "isoformat", lambda: "")() == "2026-07-13",
    )
    monkeypatch.setattr(
        trading_calendar,
        "has_verified_calendar",
        lambda _year: True,
    )

    assert scheduler._is_weekday(datetime(2026, 7, 10, 17, 10)) is False
    next_runs = scheduler.next_runs(datetime(2026, 7, 10, 16, 59))
    assert next_runs["factor_mining"] == "2026-07-13 17:10"


def test_scheduler_blocks_when_authoritative_calendar_is_unavailable(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from market import trading_calendar

    monkeypatch.setattr(
        trading_calendar,
        "has_verified_calendar",
        lambda _year: False,
    )
    monkeypatch.setattr(
        trading_calendar,
        "is_trading_day",
        lambda _value=None: True,
    )

    assert scheduler._is_weekday(
        datetime(2026, 7, 10, 17, 10)
    ) is False
    next_runs = scheduler.next_runs(
        datetime(2026, 7, 10, 16, 59)
    )
    assert next_runs == {
        "data_refresh": None,
        "factor_mining": None,
        "ai_decision": None,
    }


def test_data_refresh_reports_failure_when_factor_cache_is_incomplete(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics import data_manager as dm
    from analytics import lhb_engine
    from market import market_stats

    monkeypatch.setattr(dm, "refresh_name_cache", lambda: {"000001": "平安银行"})
    monkeypatch.setattr(
        dm,
        "refresh_factor_daily_cache",
        lambda **kwargs: {"ok": False, "latest_count": 2, "min_required": 800},
        raising=False,
    )
    monkeypatch.setattr(market_stats, "get_market_breadth", lambda date_text=None: {"ok": True})
    monkeypatch.setattr(market_stats, "get_market_turnover", lambda date_text=None: {"ok": True})
    monkeypatch.setattr(lhb_engine, "get_lhb_data", lambda date_text, force_refresh=False: {"ok": True, "total": 0})

    result = scheduler._run_data_refresh("2026-07-13")

    assert result["ok"] is False
    assert result["details"]["factor_cache"]["latest_count"] == 2


def test_data_refresh_updates_st_history_for_factor_universe(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics import data_manager as dm
    from analytics import lhb_engine
    from analytics import st_status_history
    from analytics.factors_zoo import scorer
    from market import market_stats

    monkeypatch.setattr(dm, "refresh_name_cache", lambda: {"000001": "平安银行"})
    refresh_kwargs = {}

    def fake_refresh_factor_daily_cache(**kwargs):
        refresh_kwargs.update(kwargs)
        return {"ok": True, "latest_count": 800, "min_required": 800}

    monkeypatch.setattr(
        dm,
        "refresh_factor_daily_cache",
        fake_refresh_factor_daily_cache,
        raising=False,
    )
    panel_kwargs = {}

    def fake_build_panel_from_cache(**kwargs):
        panel_kwargs.update(kwargs)
        return {"close": object()}, ["000001", "000002"]

    monkeypatch.setattr(
        scorer,
        "build_panel_from_cache",
        fake_build_panel_from_cache,
    )
    captured = {}

    def fake_refresh(codes, **kwargs):
        captured["codes"] = list(codes)
        return {"ok": True, "requested": 2, "usable": 2, "coverage": 1.0}

    monkeypatch.setattr(st_status_history, "refresh_cache", fake_refresh)
    monkeypatch.setattr(market_stats, "get_market_breadth", lambda date_text=None: {"ok": True})
    monkeypatch.setattr(market_stats, "get_market_turnover", lambda date_text=None: {"ok": True})
    monkeypatch.setattr(lhb_engine, "get_lhb_data", lambda date_text, force_refresh=False: {"ok": True, "total": 0})

    result = scheduler._run_data_refresh("2026-07-13")

    assert result["ok"] is True
    assert captured["codes"] == ["000001", "000002"]
    assert refresh_kwargs["refresh_security_master"] is True
    assert refresh_kwargs["min_required"] == 880
    assert panel_kwargs["n_stocks"] == 880
    assert panel_kwargs["require_security_master"] is True
    assert result["details"]["st_status"]["coverage"] == 1.0


def test_failed_job_is_cooled_down_before_retry(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    key = scheduler._job_key("factor_mining", "17:10", "2026-07-13")
    status = {
        "jobs": {
            key: {
                "ok": False,
                "finished_at": "2026-07-13 17:11:00",
            }
        }
    }

    assert scheduler._attempt_blocked(
        status,
        "factor_mining",
        "17:10",
        "2026-07-13",
        now=datetime(2026, 7, 13, 17, 15),
        retry_cooldown_minutes=10,
    ) is True
    assert scheduler._attempt_blocked(
        status,
        "factor_mining",
        "17:10",
        "2026-07-13",
        now=datetime(2026, 7, 13, 17, 22),
        retry_cooldown_minutes=10,
    ) is False


def test_scheduled_factor_mining_passes_trade_plan_to_research(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.factors_zoo import scorer
    from core import factor_watchlist

    captured = {}
    fresh_panel = {
        "close": pd.DataFrame(
            {"000001": [10.0]},
            index=pd.to_datetime(["2026-07-29"]),
        )
    }

    def fake_research(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "reason": "ok", "summary": {}, "latest_candidates": []}

    monkeypatch.setattr(scheduler, "get_config", lambda: {
        "mining": {
            "n_stocks": 100,
            "days": 90,
            "train_days": 40,
            "test_days": 10,
            "hold_days": 3,
            "stop_loss": -0.04,
            "take_profit": 0.08,
            "top_k": 5,
            "candidate_pool_size": 20,
        }
    })
    monkeypatch.setattr(scheduler.relay_factor_mining, "run_relay_research", fake_research)
    panel_kwargs = {}

    def build_panel(**kwargs):
        panel_kwargs.update(kwargs)
        return fresh_panel, ["000001"]

    monkeypatch.setattr(scorer, "build_panel_from_cache", build_panel)
    monkeypatch.setattr(
        factor_watchlist,
        "import_factor_candidates_to_watchlist",
        lambda *args, **kwargs: {"ok": True, "imported": 0, "total": 0},
    )

    result = scheduler._run_factor_mining("2026-07-29")

    assert result["ok"] is True
    assert captured["hold_days"] == 3
    assert captured["stop_loss"] == -0.04
    assert captured["take_profit"] == 0.08
    assert captured["feedback_user_id"] is None
    assert captured["load_live_feedback"] is False
    assert captured["panel"] is fresh_panel
    assert captured["n_stocks"] == 100
    assert panel_kwargs["n_stocks"] == 110
    assert panel_kwargs["require_security_master"] is True


def test_factor_mining_failure_is_retryable_when_prospective_cycle_raises(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.factors_zoo import scorer

    fresh_panel = {
        "close": pd.DataFrame(
            {"000001": [10.0]},
            index=pd.to_datetime(["2026-07-29"]),
        )
    }
    monkeypatch.setattr(
        scorer,
        "build_panel_from_cache",
        lambda **kwargs: (fresh_panel, ["000001"]),
    )
    monkeypatch.setattr(
        scheduler,
        "_run_prospective_cycle",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("prospective store unavailable")
        ),
    )
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "run_relay_research",
        lambda **kwargs: {
            "status": "ok",
            "data_status": "ok",
            "research_status": "failed",
            "research_assessment": {"deployment_eligible": False},
            "summary": {"latest_trade_date": "2026-07-29"},
            "latest_candidates": [],
        },
    )

    result = scheduler._run_factor_mining("2026-07-29")

    assert result["ok"] is False
    assert result["prospective"]["ok"] is False
    assert result["prospective"]["error"] == "prospective store unavailable"


def test_factor_mining_rejects_stale_cache_before_research(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.factors_zoo import scorer
    from core import factor_watchlist

    store = _short_window_store(tmp_path / "prospective.sqlite3")
    experiment = store.create_locked_experiment(
        "run-stale-cache",
        _frozen_manifest(required_sessions=1),
        locked_at="2026-07-28 17:10:00",
    )
    monkeypatch.setattr(scheduler, "_factor_experiment_store", lambda: store)
    stale_panel = {
        "close": pd.DataFrame(
            {"000001": [10.0]},
            index=pd.to_datetime(["2026-07-28"]),
        )
    }
    monkeypatch.setattr(scorer, "build_panel_from_cache", lambda **kwargs: (stale_panel, ["000001"]))
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "run_relay_research",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("research must not run")),
    )
    monkeypatch.setattr(
        factor_watchlist,
        "import_factor_candidates_to_watchlist",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("watchlist import must not run")),
    )

    result = scheduler._run_factor_mining("2026-07-29")

    assert result["ok"] is False
    assert result["data_status"] == "stale"
    assert result["expected_trade_date"] == "2026-07-29"
    assert result["latest_trade_date"] == "2026-07-28"
    assert result["error"] == (
        "factor cache is stale: expected 2026-07-29, got 2026-07-28"
    )
    assert result["prospective"]["recorded"] is True
    assert result["prospective"]["data_trusted"] is False
    session = store.list_sessions(experiment["experiment_id"])[0]
    assert session["signal_date"] == "2026-07-29"
    assert session["source_snapshot"]["failure_reason"] == "factor_cache_stale"


def test_full_after_close_skips_mining_when_refresh_fails(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    store = _short_window_store(tmp_path / "prospective.sqlite3")
    experiment = store.create_locked_experiment(
        "run-refresh-failed",
        _frozen_manifest(required_sessions=1),
        locked_at="2026-07-28 17:10:00",
    )
    refresh_result = {
        "ok": False,
        "details": {"factor_cache": {"ok": False, "error": "upstream unavailable"}},
    }
    monkeypatch.setattr(scheduler, "_today_text", lambda: "2026-07-29")
    monkeypatch.setattr(scheduler, "_factor_experiment_store", lambda: store)
    monkeypatch.setattr(scheduler, "_run_data_refresh", lambda date_text: refresh_result)
    monkeypatch.setattr(
        scheduler,
        "_run_factor_mining",
        lambda date_text: (_ for _ in ()).throw(AssertionError("mining must be skipped")),
    )

    result = scheduler.run_job("full_after_close", force=True)

    assert result["ok"] is False
    assert result["steps"]["data_refresh"] == refresh_result
    factor_step = result["steps"]["factor_mining"]
    assert factor_step["ok"] is False
    assert factor_step["skipped"] is True
    assert factor_step["reason"] == "data_refresh_failed"
    assert factor_step["prospective"]["recorded"] is True
    assert factor_step["prospective"]["data_trusted"] is False
    session = store.list_sessions(experiment["experiment_id"])[0]
    assert session["signal_date"] == "2026-07-29"
    assert session["source_snapshot"]["failure_reason"] == "data_refresh_failed"


def test_factor_mining_does_not_import_candidates_from_degraded_data(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.factors_zoo import scorer
    from core import factor_watchlist

    fresh_panel = {
        "close": pd.DataFrame(
            {"000001": [10.0]},
            index=pd.to_datetime(["2026-07-29"]),
        )
    }
    monkeypatch.setattr(scorer, "build_panel_from_cache", lambda **kwargs: (fresh_panel, ["000001"]))
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "run_relay_research",
        lambda **kwargs: {
            "status": "degraded",
            "data_status": "degraded",
            "reason": "turnover amount coverage too low",
            "research_status": "failed",
            "research_assessment": {"deployment_eligible": False},
            "summary": {"latest_trade_date": "2026-07-29"},
            "latest_candidates": [{"code": "000001"}],
        },
    )
    monkeypatch.setattr(
        factor_watchlist,
        "import_factor_candidates_to_watchlist",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("watchlist import must not run")),
    )

    result = scheduler._run_factor_mining("2026-07-29")

    assert result["ok"] is False
    assert result["data_status"] == "degraded"
    assert result["research_status"] == "failed"
    assert result["deployment_eligible"] is False
    assert result["candidate_count"] == 1
    assert result["watchlist"] == {
        "ok": False,
        "skipped": True,
        "reason": "data_status_degraded",
    }


def test_factor_mining_records_exploratory_research_without_importing_watchlist(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.factors_zoo import scorer
    from core import factor_watchlist

    fresh_panel = {
        "close": pd.DataFrame(
            {"000001": [10.0]},
            index=pd.to_datetime(["2026-07-29"]),
        )
    }
    candidate = {
        "code": "000001",
        "research_status": "failed",
        "deployment_eligible": False,
        "research_only": True,
        "position_pct": 0.0,
    }
    monkeypatch.setattr(scorer, "build_panel_from_cache", lambda **kwargs: (fresh_panel, ["000001"]))
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "run_relay_research",
        lambda **kwargs: {
            "status": "ok",
            "data_status": "ok",
            "reason": "ok",
            "research_status": "failed",
            "research_assessment": {
                "research_status": "failed",
                "deployment_eligible": False,
                "failure_reasons": ["non_positive_oos_return"],
            },
            "summary": {"latest_trade_date": "2026-07-29"},
            "latest_candidates": [candidate],
        },
    )
    monkeypatch.setattr(
        factor_watchlist,
        "import_factor_candidates_to_watchlist",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("exploratory candidates must not enter the trading watchlist")
        ),
    )

    result = scheduler._run_factor_mining("2026-07-29")

    assert result["ok"] is True
    assert result["data_status"] == "ok"
    assert result["research_status"] == "failed"
    assert result["deployment_eligible"] is False
    assert result["watchlist"] == {
        "ok": False,
        "skipped": True,
        "reason": "exploratory_research_not_deployable",
    }


def _frozen_manifest(required_sessions=20):
    from analytics import relay_factor_mining

    return {
        "schema_version": 3,
        "strategy_key": "relay_factor_mining_v2",
        "protocol_binding": relay_factor_mining.build_current_frozen_protocol_binding(),
        "model": {
            "model_version": "relay_factor_mining_v3_nested_ridge",
            "fallback": False,
            "validation_accepted": True,
            "validation_stable": True,
            "model_spec_protocol_version": (
                relay_factor_mining.MODEL_SPEC_PROTOCOL_VERSION
            ),
            "model_spec_calibrated_through": (
                relay_factor_mining.MODEL_SPEC_CALIBRATED_THROUGH
            ),
            "selected_factors": ["ret_5"],
            "coefficients": {"ret_5": 1.0},
            "feature_means": {"ret_5": 0.5},
            "feature_scales": {"ret_5": 0.25},
            "selected_market_gate": {"enabled": False},
        },
        "config": {
            "hold_days": 2,
            "stop_loss": -0.035,
            "take_profit": 0.07,
            "top_k": 1,
            "candidate_pool_size": 3,
            "include_market_signals": False,
        },
        "mining_rules": {},
        "model_specification": {
            "protocol_version": relay_factor_mining.MODEL_SPEC_PROTOCOL_VERSION,
            "calibrated_through": (
                relay_factor_mining.MODEL_SPEC_CALIBRATED_THROUGH
            ),
            "parameters": dict(relay_factor_mining.FIXED_MODEL_PARAMETERS),
        },
        "acceptance_rules": {
            "protocol_version": "prospective_60_session_bootstrap_v2",
            "required_sessions": required_sessions,
            "minimum_total_return_pct": 0.0,
            "maximum_drawdown_pct": -10.0,
            "minimum_executed_trades": 1,
            "minimum_bootstrap_lower_return_pct": None,
            "maximum_protocol_violations": 0,
            "preregistered_at": (
                relay_factor_mining.MODEL_SPEC_CALIBRATED_THROUGH
            ),
            "calibrated_through": (
                relay_factor_mining.MODEL_SPEC_CALIBRATED_THROUGH
            ),
        },
    }


def _short_window_store(db_path):
    from analytics.relay_factor_experiment import RelayFactorExperimentStore

    class ShortWindowStore(RelayFactorExperimentStore):
        @staticmethod
        def _require_verified_protocol(experiment) -> None:
            return None

        def _latest_completed_session_date(self):
            return self._test_completed_session_date

        def record_verified_shadow_session(
            self,
            experiment_id,
            signal_date,
            *,
            panel,
        ):
            self._test_completed_session_date = str(signal_date)[:10]
            return super().record_verified_shadow_session(
                experiment_id,
                signal_date,
                panel=panel,
            )

        def create_locked_experiment(self, *args, **kwargs):
            locked_at = kwargs.pop("locked_at", None)
            if locked_at is not None:
                parsed = datetime.fromisoformat(
                    str(locked_at).replace("Z", "+00:00")
                )
                self._clock = lambda: parsed
            experiment = super().create_locked_experiment(*args, **kwargs)
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE experiments
                    SET protocol_verified = 1
                    WHERE experiment_id = ?
                    """,
                    (experiment["experiment_id"],),
                )
            return self.get_experiment(experiment["experiment_id"])

    return ShortWindowStore(db_path)


def _shadow_snapshot(signal_date, manifest=None):
    manifest = manifest or {}
    snapshot = {
        "ok": True,
        "data_status": "ok",
        "reason": "ok",
        "signal_date": signal_date,
        "market_gate": {"enabled": False, "pass": True},
        "data_quality": {
            "latest_real_amount_coverage": 1.0,
            "latest_st_status_coverage": 1.0,
            "latest_market_data_trusted": True,
        },
        "candidates": [
            {
                "ts_code": "000001.SZ",
                "code": "000001",
                "trade_date": signal_date,
                "candidate_rank": 1,
                "experiment_id": manifest.get("experiment_id", ""),
                "model_instance_id": manifest.get("model_instance_id", ""),
                "source_run_id": manifest.get("source_run_id", ""),
                "shadow_target_position_pct": 0.1,
                "position_pct": 0.0,
                "research_status": "locked_shadow",
                "deployment_eligible": False,
                "research_only": True,
                "gate_pass": True,
            }
        ],
    }
    if int(manifest.get("schema_version") or 0) >= 4:
        snapshot.update(
            {
                "control_candidates": [
                    {
                        "ts_code": "000002.SZ",
                        "code": "000002",
                        "trade_date": signal_date,
                        "candidate_rank": 1,
                        "experiment_id": manifest.get(
                            "experiment_id",
                            "",
                        ),
                        "model_instance_id": manifest.get(
                            "model_instance_id",
                            "",
                        ),
                        "source_run_id": manifest.get(
                            "source_run_id",
                            "",
                        ),
                        "shadow_target_position_pct": 0.1,
                        "position_pct": 0.0,
                        "research_status": "locked_shadow",
                        "deployment_eligible": False,
                        "research_only": True,
                        "gate_pass": True,
                    }
                ],
                "control_policy_version": (
                    "same_pit_universe_sha256_equal_weight_v1"
                ),
                "control_universe_codes_sha256": "a" * 64,
            }
        )
    return snapshot


def test_prospective_cycle_records_one_immutable_shadow_session(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from core import factor_watchlist

    store = _short_window_store(tmp_path / "prospective.sqlite3")
    experiment = store.create_locked_experiment(
        "run-shadow",
        _frozen_manifest(),
        locked_at="2026-07-01 17:10:00",
    )
    monkeypatch.setattr(scheduler, "_factor_experiment_store", lambda: store)
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda panel, manifest: _shadow_snapshot("2026-07-02", manifest),
    )
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "settle_frozen_shadow_candidate",
        lambda panel, candidate, manifest: {
            "status": "pending",
            "reason": "awaiting_exit_data",
        },
    )
    monkeypatch.setattr(
        factor_watchlist,
        "import_factor_candidates_to_watchlist",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("locked shadow candidates must not enter watchlist")
        ),
    )
    monkeypatch.setattr(
        store,
        "record_frozen_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("scheduler must use verified shadow persistence")
        ),
    )
    monkeypatch.setattr(
        store,
        "settle_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("scheduler must use verified settlement")
        ),
    )

    first = scheduler._run_prospective_cycle("2026-07-02", panel={})
    second = scheduler._run_prospective_cycle("2026-07-02", panel={})

    assert first["ok"] is True
    assert first["shadow"]["recorded"] is True
    assert second["shadow"]["idempotent"] is True
    progress = store.get_progress(experiment["experiment_id"])
    assert progress["observed_sessions"] == 1
    assert progress["settled_sessions"] == 0
    candidate = store.list_candidates(experiment["experiment_id"])[0]
    assert candidate["selected"] is True
    assert candidate["target_position_pct"] == 0.1
    assert candidate["snapshot"]["research_only"] is True


def test_prospective_cycle_freezes_untrusted_shadow_day_and_fails_fixed_window(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    store = _short_window_store(tmp_path / "prospective.sqlite3")
    experiment = store.create_locked_experiment(
        "run-untrusted-shadow",
        _frozen_manifest(required_sessions=1),
        locked_at="2026-07-01 17:10:00",
    )
    monkeypatch.setattr(scheduler, "_factor_experiment_store", lambda: store)
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda panel, manifest: {
            "ok": False,
            "data_status": "degraded",
            "reason": "turnover_amount_coverage_too_low",
            "signal_date": "2026-07-02",
            "data_quality": {"latest_market_data_trusted": False},
            "candidates": [],
        },
    )

    result = scheduler._run_prospective_cycle("2026-07-02", panel={})

    assert result["shadow"]["recorded"] is True
    assert result["shadow"]["data_trusted"] is False
    session = store.list_sessions(experiment["experiment_id"])[0]
    assert session["source_snapshot"]["failure_reason"] == (
        "turnover_amount_coverage_too_low"
    )
    assert store.get_progress(experiment["experiment_id"])["observed_sessions"] == 1
    assert store.get_experiment(experiment["experiment_id"])["status"] == "failed"


def test_prospective_cycle_backfills_missed_fixed_window_sessions_as_untrusted(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    store = _short_window_store(tmp_path / "prospective.sqlite3")
    experiment = store.create_locked_experiment(
        "run-missed-window",
        _frozen_manifest(required_sessions=3),
        locked_at="2026-07-01 17:10:00",
    )
    monkeypatch.setattr(scheduler, "_factor_experiment_store", lambda: store)
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda panel, manifest: _shadow_snapshot("2026-07-07", manifest),
    )
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "settle_frozen_shadow_candidate",
        lambda panel, candidate, manifest: {
            "status": "pending",
            "reason": "awaiting_exit_data",
        },
    )

    result = scheduler._run_prospective_cycle("2026-07-07", panel={})

    sessions = store.list_sessions(experiment["experiment_id"])
    assert [item["signal_date"] for item in sessions] == [
        "2026-07-02",
        "2026-07-03",
        "2026-07-06",
    ]
    assert all(item["data_trusted"] is False for item in sessions)
    assert all(
        item["source_snapshot"]["failure_reason"]
        == "scheduler_missed_fixed_window_session"
        for item in sessions
    )
    assert result["reconciliation"]["recorded_dates"] == [
        "2026-07-02",
        "2026-07-03",
        "2026-07-06",
    ]
    assert store.get_experiment(experiment["experiment_id"])["status"] == "failed"


def test_prospective_cycle_imports_only_latest_validated_frozen_model(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from core import factor_watchlist

    store = _short_window_store(tmp_path / "prospective.sqlite3")
    manifest = _frozen_manifest(required_sessions=1)
    manifest["schema_version"] = 4
    manifest["acceptance_rules"] = {
        "protocol_version": "prospective_60_session_paired_control_v3",
        "required_sessions": 1,
        "minimum_total_return_pct": 0.0,
        "minimum_excess_return_pct": 0.0,
        "maximum_drawdown_pct": -10.0,
        "minimum_executed_trades": 1,
        "minimum_bootstrap_lower_return_pct": None,
        "minimum_excess_bootstrap_lower_return_pct": None,
        "maximum_protocol_violations": 0,
        "preregistered_at": (
            "2026-07-31T23:38:02+08:00"
        ),
        "calibrated_through": "2026-07-30",
    }
    experiment = store.create_locked_experiment(
        "run-validated",
        manifest,
        locked_at="2026-07-01 17:10:00",
    )
    experiment_id = experiment["experiment_id"]
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda panel, manifest: _shadow_snapshot("2026-07-02", manifest),
    )
    def settle_candidate(panel, candidate, manifest):
        is_control = candidate.get("prospective_track") == "control"
        realized_return = -0.36 if is_control else 9.64
        exit_price = 10.0 if is_control else 11.0
        return {
            "status": "settled",
            "execution_blocked": False,
            "block_reasons": [],
            "entry_date": "2026-07-03",
            "exit_date": "2026-07-06",
            "entry_price": 10.0,
            "exit_price": exit_price,
            "exit_reason": (
                "holding_period" if is_control else "take_profit"
            ),
            "realized_return_pct": realized_return,
            "valuation_path": [
                {"date": "2026-07-03", "return_pct": -0.36},
                {
                    "date": "2026-07-06",
                    "return_pct": realized_return,
                },
            ],
        }

    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "settle_frozen_shadow_candidate",
        settle_candidate,
    )
    store.record_verified_shadow_session(
        experiment_id,
        "2026-07-02",
        panel={},
    )
    store.settle_verified_candidates(experiment_id, panel={})
    assert store.evaluate_once(experiment_id)["status"] == "validated"

    monkeypatch.setattr(scheduler, "_factor_experiment_store", lambda: store)
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda panel, manifest: _shadow_snapshot("2026-07-07", manifest),
    )
    captured = {}

    def fake_import(candidates, **kwargs):
        captured["candidates"] = list(candidates)
        captured["kwargs"] = kwargs
        return {"ok": True, "imported": len(candidates)}

    monkeypatch.setattr(
        factor_watchlist,
        "import_factor_candidates_to_watchlist",
        fake_import,
    )

    result = scheduler._run_prospective_cycle("2026-07-07", panel={})

    assert result["ok"] is True
    assert result["production"]["imported"] == 1
    candidate = captured["candidates"][0]
    assert candidate["experiment_id"] == experiment_id
    assert candidate["model_instance_id"] == experiment["model_instance_id"]
    assert candidate["research_status"] == "validated"
    assert candidate["deployment_eligible"] is True
    assert candidate["research_only"] is False
    assert candidate["position_pct"] == 0.1


def test_prospective_cycle_propagates_validated_production_failure(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    validated = {"experiment_id": "validated-experiment"}

    class Store:
        @staticmethod
        def get_active_experiment():
            return None

        @staticmethod
        def get_latest_validated_experiment():
            return validated

    production_failure = {
        "ok": False,
        "skipped": True,
        "reason": "validated_snapshot_not_trusted",
    }
    monkeypatch.setattr(scheduler, "_factor_experiment_store", Store)
    monkeypatch.setattr(
        scheduler,
        "_validated_production_candidates",
        lambda experiment, panel: production_failure,
    )

    result = scheduler._run_prospective_cycle("2026-07-29", panel={})

    assert result["production"] == production_failure
    assert result["ok"] is False


def test_validated_zero_candidate_day_advances_watchlist_scope_without_fallback(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    import app_state
    from core import factor_watchlist

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    factor_watchlist.import_factor_candidates_to_watchlist(
        [
            {
                **_shadow_snapshot("2026-07-28")["candidates"][0],
                "strategy_key": "relay_factor_mining_v2",
                "model_version": "relay_factor_mining_v3_nested_ridge",
                "experiment_id": "experiment-validated",
                "model_instance_id": "model-validated",
                "validation_accepted": True,
                "research_status": "validated",
                "deployment_eligible": True,
                "research_only": False,
                "live_readiness_status": "ready",
                "position_pct": 0.1,
            }
        ],
        run_id="run-validated",
    )
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda panel, manifest: {
            "ok": True,
            "data_status": "ok",
            "reason": "no_candidates",
            "signal_date": "2026-07-29",
            "candidates": [],
        },
    )
    experiment = {
        "experiment_id": "experiment-validated",
        "model_instance_id": "model-validated",
        "source_run_id": "run-validated",
        "manifest": _frozen_manifest(required_sessions=1),
    }

    result = scheduler._validated_production_candidates(experiment, panel={})

    assert result["ok"] is True
    assert result["imported"] == 0
    assert result["latest_scope"]["trade_date"] == "2026-07-29"
    assert result["latest_scope"]["candidate_count"] == 0
    assert factor_watchlist.factor_watchlist_candidates(
        max_candidates=6,
        latest_only=True,
    ) == []


def test_validated_untrusted_snapshot_advances_empty_watchlist_scope(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    import app_state
    from core import factor_watchlist

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    factor_watchlist.import_factor_candidates_to_watchlist(
        [
            {
                **_shadow_snapshot("2026-07-28")["candidates"][0],
                "strategy_key": "relay_factor_mining_v2",
                "model_version": "relay_factor_mining_v3_nested_ridge",
                "experiment_id": "experiment-validated",
                "model_instance_id": "model-validated",
                "validation_accepted": True,
                "research_status": "validated",
                "deployment_eligible": True,
                "research_only": False,
                "live_readiness_status": "ready",
                "position_pct": 0.1,
            }
        ],
        run_id="run-validated",
    )
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda panel, manifest: {
            "ok": False,
            "data_status": "degraded",
            "reason": "market_data_untrusted",
            "signal_date": "2026-07-29",
            "candidates": [],
        },
    )
    experiment = {
        "experiment_id": "experiment-validated",
        "model_instance_id": "model-validated",
        "source_run_id": "run-validated",
        "manifest": _frozen_manifest(required_sessions=1),
    }

    result = scheduler._validated_production_candidates(experiment, panel={})

    assert result["ok"] is False
    assert result["reason"] == "market_data_untrusted"
    assert result["latest_scope"]["trade_date"] == "2026-07-29"
    assert result["latest_scope"]["candidate_count"] == 0
