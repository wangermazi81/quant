import subprocess
from pathlib import Path

import pytest


TARGET_SHA = "a" * 40
REMOTE_URL = "https://gitee.com/example/quant.git"
SSH_REMOTE_URL = "ssh://git@gitee.com/example/quant.git"
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


def test_deployment_tool_bundle_hash_normalizes_line_endings():
    from deploy import bootstrap_server_update

    lf_files = {
        path: content.replace(b"\r\n", b"\n")
        for path, content in REMOTE_FILES.items()
    }
    crlf_files = {
        path: content.replace(b"\n", b"\r\n")
        for path, content in REMOTE_FILES.items()
    }

    assert bootstrap_server_update.deployment_tool_bundle_sha256(
        lf_files
    ) == bootstrap_server_update.deployment_tool_bundle_sha256(
        crlf_files
    )


def _runner(
    app_dir: Path,
    calls: list,
    *,
    target: str = TARGET_SHA,
    remote_url: str = REMOTE_URL,
    expect_ssh: bool = False,
    pip_index_url: str | None = None,
):
    def fake_runner(command, **kwargs):
        command = list(command)
        calls.append((command, dict(kwargs)))
        if command[0] == "git":
            assert Path(kwargs["cwd"]).resolve() != app_dir.resolve()
        if "init" in command and "--bare" in command:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if "fetch" in command:
            assert remote_url in command
            assert "refs/heads/main" in command[-1]
            if expect_ssh:
                assert "-F /dev/null" in kwargs["env"]["GIT_SSH_COMMAND"]
                assert "StrictHostKeyChecking=yes" in (
                    kwargs["env"]["GIT_SSH_COMMAND"]
                )
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
            assert environment["DEPLOY_REMOTE_URL"] == remote_url
            assert environment["DEPLOY_BRANCH"] == "main"
            assert environment["DEPLOY_TARGET_SHA"] == TARGET_SHA
            assert (
                environment["DEPLOY_TOOL_BUNDLE_SHA256"]
                == TOOL_BUNDLE_SHA256
            )
            if expect_ssh:
                assert environment["DEPLOY_GIT_SSH_COMMAND"]
            if pip_index_url:
                assert (
                    environment["DEPLOY_PIP_INDEX_URL"]
                    == pip_index_url
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


def test_bootstrap_supports_pinned_ssh_deploy_key(tmp_path):
    from deploy import bootstrap_server_update

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    ssh_key = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts"
    ssh_key.write_text("private-key-fixture", encoding="utf-8")
    known_hosts.write_text("host-key-fixture", encoding="utf-8")
    ssh_key.chmod(0o600)
    known_hosts.chmod(0o600)
    calls = []

    result = bootstrap_server_update.bootstrap(
        app_dir=app_dir,
        remote_url=SSH_REMOTE_URL,
        branch="main",
        target_sha=TARGET_SHA,
        expected_bundle_sha256=TOOL_BUNDLE_SHA256,
        ssh_key_path=ssh_key,
        ssh_known_hosts_path=known_hosts,
        runner=_runner(
            app_dir,
            calls,
            remote_url=SSH_REMOTE_URL,
            expect_ssh=True,
        ),
    )

    assert result == 0
    fetch_environment = next(
        kwargs["env"]
        for command, kwargs in calls
        if "fetch" in command
    )
    assert str(ssh_key.resolve()) in fetch_environment["GIT_SSH_COMMAND"]
    assert str(known_hosts.resolve()) in (
        fetch_environment["GIT_SSH_COMMAND"]
    )


def test_bootstrap_rejects_ssh_remote_without_pinned_identity(tmp_path):
    from deploy import bootstrap_server_update

    app_dir = tmp_path / "app"
    app_dir.mkdir()

    with pytest.raises(ValueError, match="ssh_key_path"):
        bootstrap_server_update.bootstrap(
            app_dir=app_dir,
            remote_url=SSH_REMOTE_URL,
            branch="main",
            target_sha=TARGET_SHA,
            expected_bundle_sha256=TOOL_BUNDLE_SHA256,
            runner=lambda *_args, **_kwargs: None,
        )


def test_bootstrap_surfaces_remote_fetch_error(tmp_path):
    from deploy import bootstrap_server_update

    app_dir = tmp_path / "app"
    app_dir.mkdir()

    def runner(command, **_kwargs):
        if "fetch" in command:
            raise subprocess.CalledProcessError(
                128,
                command,
                stderr=b"private repository authentication failed",
            )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    with pytest.raises(
        RuntimeError,
        match="private repository authentication failed",
    ):
        bootstrap_server_update.bootstrap(
            app_dir=app_dir,
            remote_url=REMOTE_URL,
            branch="main",
            target_sha=TARGET_SHA,
            expected_bundle_sha256=TOOL_BUNDLE_SHA256,
            runner=runner,
        )


def test_bootstrap_passes_validated_pip_index_to_deployment(tmp_path):
    from deploy import bootstrap_server_update

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    pip_index_url = "https://mirrors.aliyun.com/pypi/simple/"
    calls = []

    assert bootstrap_server_update.bootstrap(
        app_dir=app_dir,
        remote_url=REMOTE_URL,
        branch="main",
        target_sha=TARGET_SHA,
        expected_bundle_sha256=TOOL_BUNDLE_SHA256,
        pip_index_url=pip_index_url,
        runner=_runner(
            app_dir,
            calls,
            pip_index_url=pip_index_url,
        ),
    ) == 0

    with pytest.raises(ValueError, match="HTTPS"):
        bootstrap_server_update.bootstrap(
            app_dir=app_dir,
            remote_url=REMOTE_URL,
            branch="main",
            target_sha=TARGET_SHA,
            expected_bundle_sha256=TOOL_BUNDLE_SHA256,
            pip_index_url="http://mirror.invalid/simple/",
            runner=lambda *_args, **_kwargs: None,
        )
