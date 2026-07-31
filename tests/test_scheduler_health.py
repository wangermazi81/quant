import json
import os
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _stable_process_start_token(monkeypatch):
    from deploy import check_scheduler_health

    monkeypatch.setattr(
        check_scheduler_health,
        "_process_start_token",
        lambda _pid: "test-process-start",
        raising=False,
    )


def _runtime_payload(**changes):
    payload = {
        "ready": True,
        "pid": os.getpid(),
        "process_start_token": "test-process-start",
        "thread_alive": True,
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "progress_at": datetime.now(timezone.utc).isoformat(),
        "calendar_ready": True,
        "calendar_reason": "ok",
    }
    payload.update(changes)
    return payload


def test_scheduler_health_accepts_fresh_ready_runtime(tmp_path):
    from deploy import check_scheduler_health

    path = tmp_path / "scheduler-runtime.json"
    path.write_text(
        json.dumps(_runtime_payload()),
        encoding="utf-8",
    )

    result = check_scheduler_health.check_runtime(
        path,
        expected_pid=os.getpid(),
        max_heartbeat_age_seconds=30,
    )

    assert result["ok"] is True
    assert result["pid"] == os.getpid()


def test_scheduler_health_uses_active_job_deadline_instead_of_loop_progress(
    tmp_path,
):
    from deploy import check_scheduler_health

    started_at = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    path = tmp_path / "scheduler-runtime.json"
    path.write_text(
        json.dumps(
            _runtime_payload(
                progress_at=started_at,
                job_running=True,
                current_job="factor_mining",
                job_started_at=started_at,
            )
        ),
        encoding="utf-8",
    )

    result = check_scheduler_health.check_runtime(
        path,
        expected_pid=os.getpid(),
        max_progress_age_seconds=60,
        max_job_age_seconds=21600,
    )

    assert result["ok"] is True
    assert result["job_running"] is True
    assert result["current_job"] == "factor_mining"


def test_scheduler_health_rejects_active_job_past_deadline(tmp_path):
    from deploy import check_scheduler_health

    path = tmp_path / "scheduler-runtime.json"
    path.write_text(
        json.dumps(
            _runtime_payload(
                job_running=True,
                current_job="factor_mining",
                job_started_at=(
                    datetime.now(timezone.utc)
                    - timedelta(hours=7)
                ).isoformat(),
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="job is stale"):
        check_scheduler_health.check_runtime(
            path,
            expected_pid=os.getpid(),
            max_job_age_seconds=21600,
        )


def test_scheduler_health_uses_windows_handle_probe(monkeypatch):
    from deploy import check_scheduler_health

    observed = []
    monkeypatch.setattr(
        check_scheduler_health,
        "_IS_WINDOWS",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        check_scheduler_health,
        "_windows_pid_exists",
        lambda pid: observed.append(pid) or True,
        raising=False,
    )
    monkeypatch.setattr(
        check_scheduler_health.os,
        "kill",
        lambda *_args: pytest.fail("Windows must not use os.kill(pid, 0)"),
    )

    assert check_scheduler_health._pid_exists(4321) is True
    assert observed == [4321]


@pytest.mark.parametrize(
    "changes",
    [
        {"ready": False},
        {"thread_alive": False},
        {"pid": 999999},
        {"process_start_token": None},
        {"process_start_token": "reused-pid-start"},
        {"calendar_ready": False},
        {
            "heartbeat_at": (
                datetime.now(timezone.utc) - timedelta(minutes=10)
            ).isoformat()
        },
        {
            "progress_at": (
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat()
        },
    ],
)
def test_scheduler_health_rejects_false_or_stale_runtime(
    tmp_path,
    changes,
):
    from deploy import check_scheduler_health

    path = tmp_path / "scheduler-runtime.json"
    path.write_text(
        json.dumps(_runtime_payload(**changes)),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError):
        check_scheduler_health.check_runtime(
            path,
            expected_pid=os.getpid(),
            max_heartbeat_age_seconds=30,
            max_progress_age_seconds=60,
        )


def test_scheduler_health_rejects_linux_zombie(monkeypatch):
    from deploy import check_scheduler_health

    monkeypatch.setattr(
        check_scheduler_health,
        "_IS_WINDOWS",
        False,
    )
    monkeypatch.setattr(
        check_scheduler_health,
        "_read_linux_process_stat",
        lambda _pid: ("Z", "12345"),
        raising=False,
    )

    assert check_scheduler_health._pid_exists(4321) is False
    assert check_scheduler_health._linux_process_start_token(4321) is None


def test_scheduler_health_linux_token_binds_boot_and_starttime(
    monkeypatch,
):
    from deploy import check_scheduler_health

    monkeypatch.setattr(
        check_scheduler_health,
        "_read_linux_process_stat",
        lambda _pid: ("S", "987654"),
        raising=False,
    )
    monkeypatch.setattr(
        check_scheduler_health,
        "_linux_boot_id",
        lambda: "boot-id-1",
        raising=False,
    )

    assert (
        check_scheduler_health._linux_process_start_token(4321)
        == "boot-id-1:987654"
    )


def test_scheduler_health_windows_token_uses_creation_time(
    monkeypatch,
):
    from deploy import check_scheduler_health

    class FakeKernel32:
        def OpenProcess(self, access, inherit_handle, pid):
            assert (access, inherit_handle, pid) == (
                0x1000,
                False,
                2468,
            )
            return 123

        @staticmethod
        def GetProcessTimes(
            handle,
            creation,
            exit_time,
            kernel,
            user,
        ):
            assert handle == 123
            creation._obj.dwHighDateTime = 0x12345678
            creation._obj.dwLowDateTime = 0x9ABCDEF0
            return True

        @staticmethod
        def CloseHandle(handle):
            assert handle == 123
            return True

    assert check_scheduler_health._windows_process_start_token(
        2468,
        kernel32=FakeKernel32(),
    ) == "1311768467463790320"
