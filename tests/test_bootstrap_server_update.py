import subprocess
from pathlib import Path

import pytest


TARGET_SHA = "a" * 40
REMOTE_URL = "https://gitee.com/example/quant.git"
TOOL_BUNDLE_SHA256 = (
    "3e932d182bfcae80e8fdcc7246692706"
    "67fb7d7ae3f6a055ccb44563631ada7d"
)
REMOTE_FILES = {
    "deploy/check_scheduler_health.py": b"health\n",
    "deploy/check_scheduler_quiescence.py": b"quiescence\n",
    "deploy/harden_runtime_permissions.py": b"permissions\n",
    "deploy/server_update.sh": b"update\n",
}


def _runner(app_dir: Path, calls: list, *, target: str = TARGET_SHA):
    def fake_runner(command, **kwargs):
        command = list(command)
        calls.append((command, dict(kwargs)))
        if command[0] == "git":
            assert Path(kwargs["cwd"]).resolve() != app_dir.resolve()
        if "init" in command and "--bare" in command:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if "fetch" in command:
            assert REMOTE_URL in command
            assert "refs/heads/main" in command[-1]
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if "rev-parse" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                f"{target}\n".encode("ascii"),
                b"",
            )
        if "show" in command:
            remote_path = command[-1]
            commit, relative_path = remote_path.split(":", 1)
            assert commit == TARGET_SHA
            return subprocess.CompletedProcess(
                command,
                0,
                REMOTE_FILES[relative_path],
                b"",
            )
        if command[0] == "bash":
            environment = kwargs["env"]
            assert environment["SCHEDULER_MIGRATION_BOOTSTRAPPED"] == "1"
            assert environment["DEPLOY_REMOTE_URL"] == REMOTE_URL
            assert environment["DEPLOY_BRANCH"] == "main"
            assert environment["DEPLOY_TARGET_SHA"] == TARGET_SHA
            assert (
                environment["DEPLOY_TOOL_BUNDLE_SHA256"]
                == TOOL_BUNDLE_SHA256
            )
            for variable, relative_path in {
                "QUIESCENCE_HELPER": (
                    "deploy/check_scheduler_quiescence.py"
                ),
                "HEALTH_HELPER": "deploy/check_scheduler_health.py",
                "PERMISSIONS_HELPER": (
                    "deploy/harden_runtime_permissions.py"
                ),
            }.items():
                assert Path(environment[variable]).read_bytes() == (
                    REMOTE_FILES[relative_path]
                )
            assert Path(command[1]).read_bytes() == (
                REMOTE_FILES["deploy/server_update.sh"]
            )
            return subprocess.CompletedProcess(command, 0, b"", b"")
        raise AssertionError(command)

    return fake_runner


def test_bootstrap_fetches_pinned_tools_outside_old_checkout(tmp_path):
    from deploy import bootstrap_server_update

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    calls = []

    result = bootstrap_server_update.bootstrap(
        app_dir=app_dir,
        remote_url=REMOTE_URL,
        branch="main",
        target_sha=TARGET_SHA,
        expected_bundle_sha256=TOOL_BUNDLE_SHA256,
        runner=_runner(app_dir, calls),
    )

    assert result == 0
    assert sum("show" in call[0] for call in calls) == len(REMOTE_FILES)
    assert calls[-1][0][0] == "bash"


def test_bootstrap_rejects_tool_bundle_mismatch_before_execution(
    tmp_path,
):
    from deploy import bootstrap_server_update

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    calls = []

    with pytest.raises(RuntimeError, match="bundle SHA-256 mismatch"):
        bootstrap_server_update.bootstrap(
            app_dir=app_dir,
            remote_url=REMOTE_URL,
            branch="main",
            target_sha=TARGET_SHA,
            expected_bundle_sha256="0" * 64,
            runner=_runner(app_dir, calls),
        )

    assert not any(call[0][0] == "bash" for call in calls)


def test_bootstrap_rejects_moved_branch_and_non_https_remote(tmp_path):
    from deploy import bootstrap_server_update

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    with pytest.raises(ValueError, match="HTTPS"):
        bootstrap_server_update.bootstrap(
            app_dir=app_dir,
            remote_url="file:///tmp/quant.git",
            branch="main",
            target_sha=TARGET_SHA,
            expected_bundle_sha256=TOOL_BUNDLE_SHA256,
            runner=lambda *_args, **_kwargs: None,
        )

    calls = []
    with pytest.raises(RuntimeError, match="branch moved"):
        bootstrap_server_update.bootstrap(
            app_dir=app_dir,
            remote_url=REMOTE_URL,
            branch="main",
            target_sha=TARGET_SHA,
            expected_bundle_sha256=TOOL_BUNDLE_SHA256,
            runner=_runner(app_dir, calls, target="b" * 40),
        )
    assert not any(call[0][0] == "bash" for call in calls)
