from contextlib import contextmanager
import os
from pathlib import Path

import pytest

from deploy import check_scheduler_quiescence as quiescence
from deploy.check_scheduler_quiescence import ProcessInfo, find_unsafe_processes


APP_DIR = "/opt/quant"
WEB_CGROUP = "/system.slice/quant-platform.service"


def test_guard_open_uses_nofollow_and_private_creation(
    tmp_path,
    monkeypatch,
):
    backing_path = tmp_path / "backing"
    real_open = os.open
    backing_fd = real_open(
        backing_path,
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    opened = {}
    nofollow = 1 << 28

    def capture_open(path, flags, mode):
        opened.update(
            {
                "path": os.fspath(path),
                "flags": flags,
                "mode": mode,
            }
        )
        return os.dup(backing_fd)

    monkeypatch.setattr(quiescence.sys, "platform", "linux")
    monkeypatch.setattr(
        quiescence.os,
        "O_NOFOLLOW",
        nofollow,
        raising=False,
    )
    monkeypatch.setattr(quiescence.os, "open", capture_open)
    try:
        with quiescence._open_scheduler_guard(
            tmp_path / "guard"
        ):
            pass
    finally:
        os.close(backing_fd)

    assert opened["path"] == os.fspath(tmp_path / "guard")
    assert opened["flags"] & nofollow
    assert opened["mode"] == 0o600


def test_guard_open_rejects_non_regular_file(
    tmp_path,
    monkeypatch,
):
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(quiescence.sys, "platform", "linux")
    monkeypatch.setattr(
        quiescence.os,
        "O_NOFOLLOW",
        1 << 28,
        raising=False,
    )
    monkeypatch.setattr(
        quiescence.os,
        "open",
        lambda _path, _flags, _mode: read_fd,
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="regular file",
        ):
            quiescence._open_scheduler_guard(tmp_path / "guard")
    finally:
        os.close(write_fd)

    with pytest.raises(OSError):
        os.close(read_fd)


def test_manual_scheduler_process_blocks_protocol_migration():
    processes = [
        ProcessInfo(
            pid=101,
            cmdline=("python", "scheduler_main.py"),
            cwd=APP_DIR,
            cgroups=("/user.slice/user-0.slice/session-1.scope",),
            environment={},
        )
    ]

    issues = find_unsafe_processes(
        processes,
        app_dir=APP_DIR,
        web_cgroup=WEB_CGROUP,
    )

    assert [(issue.pid, issue.reason) for issue in issues] == [
        (101, "standalone_scheduler_still_running")
    ]


def test_manual_web_process_blocks_protocol_migration():
    processes = [
        ProcessInfo(
            pid=102,
            cmdline=("python", "main.py"),
            cwd=APP_DIR,
            cgroups=("/user.slice/user-0.slice/session-1.scope",),
            environment={},
        )
    ]

    issues = find_unsafe_processes(
        processes,
        app_dir=APP_DIR,
        web_cgroup=WEB_CGROUP,
    )

    assert [(issue.pid, issue.reason) for issue in issues] == [
        (102, "platform_process_outside_systemd_cgroup")
    ]


def test_module_web_process_blocks_protocol_migration():
    processes = [
        ProcessInfo(
            pid=105,
            cmdline=("python", "-m", "main"),
            cwd=APP_DIR,
            cgroups=("/user.slice/user-0.slice/session-1.scope",),
            environment={},
        )
    ]

    issues = find_unsafe_processes(
        processes,
        app_dir=APP_DIR,
        web_cgroup=WEB_CGROUP,
    )

    assert [(issue.pid, issue.reason) for issue in issues] == [
        (105, "platform_process_outside_systemd_cgroup")
    ]


def test_module_web_process_blocks_outside_app_with_pythonpath():
    processes = [
        ProcessInfo(
            pid=107,
            cmdline=("python", "-m", "main"),
            cwd="/tmp",
            cgroups=("/user.slice/user-0.slice/session-1.scope",),
            environment={"PYTHONPATH": APP_DIR},
        )
    ]

    issues = find_unsafe_processes(
        processes,
        app_dir=APP_DIR,
        web_cgroup=WEB_CGROUP,
    )

    assert [(issue.pid, issue.reason) for issue in issues] == [
        (107, "platform_process_outside_systemd_cgroup")
    ]


def test_absolute_web_script_blocks_even_when_cwd_is_outside_app():
    processes = [
        ProcessInfo(
            pid=106,
            cmdline=("python", f"{APP_DIR}/main.py"),
            cwd="/tmp",
            cgroups=("/user.slice/user-0.slice/session-1.scope",),
            environment={},
        )
    ]

    issues = find_unsafe_processes(
        processes,
        app_dir=APP_DIR,
        web_cgroup=WEB_CGROUP,
    )

    assert [(issue.pid, issue.reason) for issue in issues] == [
        (106, "platform_process_outside_systemd_cgroup")
    ]


def test_split_web_service_process_is_allowed():
    processes = [
        ProcessInfo(
            pid=103,
            cmdline=("gunicorn", "main:app"),
            cwd=APP_DIR,
            cgroups=(WEB_CGROUP,),
            environment={"THS_EMBED_SCHEDULER": "0"},
        )
    ]

    assert (
        find_unsafe_processes(
            processes,
            app_dir=APP_DIR,
            web_cgroup=WEB_CGROUP,
        )
        == []
    )


def test_web_service_without_split_environment_blocks_migration():
    processes = [
        ProcessInfo(
            pid=104,
            cmdline=("gunicorn", "main:app"),
            cwd=APP_DIR,
            cgroups=(WEB_CGROUP,),
            environment={},
        )
    ]

    issues = find_unsafe_processes(
        processes,
        app_dir=APP_DIR,
        web_cgroup=WEB_CGROUP,
    )

    assert [(issue.pid, issue.reason) for issue in issues] == [
        (104, "platform_scheduler_split_not_applied")
    ]


def test_proc_permission_error_fails_closed_for_candidate_process(
    tmp_path,
    monkeypatch,
):
    process_dir = tmp_path / "4242"
    process_dir.mkdir()
    (process_dir / "cmdline").write_bytes(
        b"python\0/opt/quant/main.py\0"
    )
    real_readlink = quiescence.os.readlink

    def deny_candidate_cwd(path):
        if Path(path) == process_dir / "cwd":
            raise PermissionError("denied")
        return real_readlink(path)

    monkeypatch.setattr(quiescence.os, "readlink", deny_candidate_cwd)

    with pytest.raises(RuntimeError, match="pid 4242"):
        quiescence.read_processes(tmp_path)


def test_quiescence_explicitly_cleans_old_lock_after_process_check(
    tmp_path,
    monkeypatch,
):
    lock_path = (
        tmp_path
        / "data"
        / "research"
        / "relay_factor"
        / "auto_scheduler.lock"
    )
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("{half-written", encoding="utf-8")
    monkeypatch.setattr(
        quiescence,
        "systemd_control_group",
        lambda _service: WEB_CGROUP,
    )
    monkeypatch.setattr(
        quiescence,
        "read_processes",
        lambda _proc_root: [],
    )

    result = quiescence.main(
        [
            "--app-dir",
            str(tmp_path),
            "--web-service",
            "quant-platform",
            "--cleanup-lock",
        ]
    )

    assert result == 0
    assert not lock_path.exists()


def test_cleanup_holds_kernel_guard_across_final_scan_and_unlink(
    tmp_path,
    monkeypatch,
):
    state = {"guarded": False}

    @contextmanager
    def fake_guard(_app_dir):
        state["guarded"] = True
        try:
            yield
        finally:
            state["guarded"] = False

    def guarded_scan(_proc_root):
        assert state["guarded"] is True
        return []

    def guarded_cleanup(_app_dir):
        assert state["guarded"] is True
        return tmp_path / "auto_scheduler.lock"

    monkeypatch.setattr(
        quiescence,
        "_exclusive_scheduler_guard",
        fake_guard,
        raising=False,
    )
    monkeypatch.setattr(
        quiescence,
        "systemd_control_group",
        lambda _service: WEB_CGROUP,
    )
    monkeypatch.setattr(
        quiescence,
        "read_processes",
        guarded_scan,
    )
    monkeypatch.setattr(
        quiescence,
        "cleanup_scheduler_lock",
        guarded_cleanup,
    )

    assert quiescence.main(
        [
            "--app-dir",
            str(tmp_path),
            "--web-service",
            "quant-platform",
            "--cleanup-lock",
        ]
    ) == 0
    assert state["guarded"] is False
