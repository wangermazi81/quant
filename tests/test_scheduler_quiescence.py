from pathlib import Path

import pytest

from deploy import check_scheduler_quiescence as quiescence
from deploy.check_scheduler_quiescence import ProcessInfo, find_unsafe_processes


APP_DIR = "/opt/quant"
WEB_CGROUP = "/system.slice/quant-platform.service"


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
