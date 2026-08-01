# -*- coding: utf-8 -*-
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


def _extract_shell_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}", start) + 2
    return script[start:end]


def _bash_executable() -> str:
    bundled = Path(r"D:\软件用\Git\bin\bash.exe")
    if bundled.exists():
        return str(bundled)
    discovered = shutil.which("bash")
    if discovered:
        return discovered
    pytest.skip("bash is unavailable")


def _bash_path(path: Path) -> str:
    resolved = path.resolve()
    if resolved.drive:
        drive = resolved.drive.rstrip(":").lower()
        return f"/{drive}{resolved.as_posix()[2:]}"
    return resolved.as_posix()


def test_server_update_syncs_nginx_anti_crawl_templates():
    script = Path("deploy/server_update.sh").read_text(encoding="utf-8")

    assert "sync_nginx_anti_crawl" in script
    assert "anti_crawl_http.conf" in script
    assert "anti_crawl_server_snippet.conf" in script
    assert "/etc/nginx/snippets/jwalpha-anti-crawl-server.conf" in script
    assert "systemctl reload nginx" in script


def test_server_update_installs_and_restarts_data_scheduler():
    script = Path("deploy/server_update.sh").read_text(encoding="utf-8")

    assert "ensure_data_scheduler_service" in script
    assert "data_scheduler_service_exists" in script
    assert "deploy/quant-scheduler.service" in script
    assert "systemctl enable quant-scheduler" in script
    assert "systemctl enable --now quant-scheduler" not in script
    assert "systemctl restart quant-scheduler" in script
    assert 'systemctl cat quant-scheduler.service' in script
    assert 'list-unit-files 2>/dev/null | grep -q "^quant-scheduler.service"' not in script
    assert "systemctl enable quant-scheduler >/dev/null" in script
    assert "systemctl enable quant-scheduler >/dev/null 2>&1 || true" not in script
    assert "check_scheduler_health.py" in script
    assert "--cleanup-lock" in script


def test_deploy_splits_web_trading_scheduler_from_data_scheduler():
    update_script = Path("deploy/server_update.sh").read_text(encoding="utf-8")
    init_script = Path("deploy/server_init.sh").read_text(encoding="utf-8")

    assert "ensure_platform_scheduler_split" in update_script
    assert "/etc/systemd/system/$SERVICE.service.d/scheduler-split.conf" in update_script
    assert "Environment=THS_EMBED_SCHEDULER=0" in update_script
    assert "Environment=THS_EMBED_SCHEDULER=0" in init_script

    quiesce_body = update_script.split(
        "quiesce_legacy_schedulers() {",
        1,
    )[1].split("\n}", 1)[0]
    verify_body = update_script.split(
        "verify_scheduler_quiescence() {",
        1,
    )[1].split("\n}", 1)[0]
    assert (
        quiesce_body.index("systemctl stop quant-scheduler")
        < quiesce_body.index("ensure_platform_scheduler_split")
        < quiesce_body.index('systemctl restart "$SERVICE"')
    )

    update_flow = update_script.split(
        'echo "[0/7] Verify remote revision and local state"',
        1,
    )[1]
    assert (
        update_flow.index("trusted_git fetch --prune --no-tags")
        < update_flow.index("quiesce_legacy_schedulers")
        < update_flow.index("systemctl restart quant-scheduler")
    )
    assert (
        quiesce_body.index('systemctl restart "$SERVICE"')
        < quiesce_body.index("verify_scheduler_quiescence")
    )
    assert (
        quiesce_body.index("verify_scheduler_quiescence")
        < quiesce_body.index('secure_runtime_secrets "$RUN_USER"')
    )
    assert (
        'QUIESCENCE_HELPER="${QUIESCENCE_HELPER:-'
        '$APP_DIR/deploy/check_scheduler_quiescence.py}"'
        in update_script
    )
    assert "QUIESCENCE_HELPER" in verify_body
    updated_branch = update_script.split(
        'echo "[6/7] Restart services"',
        1,
    )[1]
    assert (
        updated_branch.index('systemctl restart "$SERVICE"')
        < updated_branch.index("verify_scheduler_quiescence")
        < updated_branch.index("systemctl restart quant-scheduler")
        < updated_branch.index("verify_scheduler_health")
    )
    same_commit_branch = update_script.split(
        'if [ "$LOCAL" = "$REMOTE" ]; then',
        1,
    )[1].split("\nfi", 1)[0]
    assert "exit 0" not in same_commit_branch


def test_first_time_init_installs_independent_data_scheduler():
    init_script = Path("deploy/server_init.sh").read_text(encoding="utf-8")
    update_script = Path("deploy/server_update.sh").read_text(encoding="utf-8")

    assert "deploy/quant-scheduler.service" in init_script
    assert "/etc/systemd/system/quant-scheduler.service" in init_script
    assert "systemctl enable quant-scheduler" in init_script
    assert "systemctl restart quant-scheduler" in init_script
    assert 's|/opt/quant|$APP_DIR|g' in init_script
    assert 's|/opt/quant|$APP_DIR|g' in update_script


def test_server_update_rejects_dirty_diverged_or_inexact_checkout():
    script = Path("deploy/server_update.sh").read_text(encoding="utf-8")

    assert "SCHEDULER_MIGRATION_BOOTSTRAPPED" in script
    assert "DEPLOY_REMOTE_URL" in script
    assert "DEPLOY_GIT_SSH_COMMAND" in script
    assert "DEPLOY_PIP_INDEX_URL" in script
    assert "PIP_DEFAULT_TIMEOUT=120" in script
    assert "PIP_RETRIES=5" in script
    assert '"PIP_NO_CACHE_DIR=1"' in script
    assert "PIP_CACHE_DIR=" not in script
    assert "ssh://git@" in script
    assert "DEPLOY_BRANCH" in script
    assert "DEPLOY_TOOL_BUNDLE_SHA256" in script
    assert 'REMOTE_TARGET="$DEPLOY_TARGET_SHA"' in script
    assert "trusted_git fetch" in script
    assert 'trusted_git rev-parse "$REMOTE_REF"' in script
    assert "trusted_git diff --quiet --" in script
    assert "trusted_git diff --cached --quiet --" in script
    assert "trusted_git ls-files --others --exclude-standard" in script
    assert (
        'trusted_git merge-base --is-ancestor "$LOCAL" "$REMOTE_TARGET"'
        in script
    )
    assert 'trusted_git merge --ff-only "$REMOTE_TARGET"' in script
    assert 'if [ "$REMOTE" != "$REMOTE_TARGET" ]; then' in script
    assert "git config --global" not in script
    assert "git fetch origin" not in script
    assert "git pull --ff-only" not in script


def test_server_update_resets_local_git_config_with_staged_helper():
    script = Path("deploy/server_update.sh").read_text(encoding="utf-8")

    assert "reset_git_configuration" in script
    reset_body = _extract_shell_function(
        script,
        "reset_git_configuration",
    )
    assert '"$PERMISSIONS_HELPER"' in reset_body
    assert "--git-config-only" in reset_body
    assert '--remote-url "$DEPLOY_REMOTE_URL"' in reset_body
    assert '--branch "$DEPLOY_BRANCH"' in reset_body


def test_server_update_restores_scheduler_after_failed_quiesced_update():
    script = Path("deploy/server_update.sh").read_text(encoding="utf-8")

    assert "restore_services_on_failure" in script
    assert "trap restore_services_on_failure EXIT" in script
    assert 'if [ "$SCHEDULER_WAS_ACTIVE" = "1" ]; then' in script
    assert "SCHEDULER_UNIT_BACKUP" in script
    assert "SCHEDULER_SHOULD_RUN" in script
    assert "systemctl is-enabled --quiet quant-scheduler" in script
    assert 'systemctl restart "$SERVICE"' in script
    assert "systemctl restart quant-scheduler" in script
    assert "DEPLOY_COMPLETED=1" in script


def test_server_update_fails_when_scheduler_unit_or_permissions_fail():
    update_script = Path("deploy/server_update.sh").read_text(
        encoding="utf-8"
    )
    init_script = Path("deploy/server_init.sh").read_text(
        encoding="utf-8"
    )
    ensure_body = update_script.split(
        "ensure_data_scheduler_service() {",
        1,
    )[1].split("\n}", 1)[0]
    secrets_body = update_script.split(
        "secure_runtime_secrets() {",
        1,
    )[1].split("\n}", 1)[0]
    init_secrets_body = init_script.split(
        "secure_runtime_secrets() {",
        1,
    )[1].split("\n}", 1)[0]

    assert "ERROR: quant-scheduler install requires root" in ensure_body
    assert "ERROR: scheduler unit source is missing" in ensure_body
    assert "return 1" in ensure_body
    assert "|| true" not in secrets_body
    assert "|| true" not in init_secrets_body


def test_database_migration_failure_aborts_deployment():
    init_script = Path("deploy/server_init.sh").read_text(
        encoding="utf-8"
    )
    update_script = Path("deploy/server_update.sh").read_text(
        encoding="utf-8"
    )
    init_migration = init_script.split(
        'echo "[7/9] Run database migration"',
        1,
    )[1].split('echo "[8/9] Configure systemd service"', 1)[0]
    update_migration = update_script.split(
        'echo "[4/7] Run database migration"',
        1,
    )[1].split('echo "[5/7] Install scheduler services"', 1)[0]

    assert 'sudo -u "$RUN_USER"' in init_migration
    assert 'sudo -u "$RUN_USER"' in update_migration
    assert "|| true" not in init_migration
    assert "|| echo" not in update_migration


def test_deploy_helpers_use_root_owned_system_python_and_private_code():
    update_script = Path("deploy/server_update.sh").read_text(
        encoding="utf-8"
    )
    init_script = Path("deploy/server_init.sh").read_text(
        encoding="utf-8"
    )

    assert 'DEPLOY_PYTHON="${DEPLOY_PYTHON:-/usr/bin/python3}"' in (
        update_script
    )
    assert '"$DEPLOY_PYTHON" -I' in update_script
    assert '"$APP_DIR/venv/bin/python" \\\n        "$QUIESCENCE_HELPER"' not in (
        update_script
    )
    assert "secure_application_ownership" in update_script
    assert 'chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"' not in (
        update_script
    )
    assert "/usr/bin/python3 -I" in init_script


def test_server_init_protects_secrets_before_optional_early_exit():
    script = Path("deploy/server_init.sh").read_text(encoding="utf-8")

    assert "umask 077" in script
    assert "RUN_USER=" in script
    assert "useradd --system" in script
    assert "User=$RUN_USER" in script
    assert 'read -r -p "Have private files' not in script
    assert "secure_application_ownership" in script


def test_server_init_never_reuses_checkout_or_executes_legacy_venv():
    script = Path("deploy/server_init.sh").read_text(encoding="utf-8")

    assert "is already a git repository; skip clone" not in script
    assert "source venv/bin/activate" not in script
    assert 'sudo -u "$RUN_USER" env -i' in script
    assert '"$DEPLOY_PYTHON" -I -m venv --copies' in script
    assert '"$APP_DIR/venv/bin/python" -I -m pip install' in script


def test_server_init_secure_cookie_requires_explicit_https_readiness():
    script = Path("deploy/server_init.sh").read_text(encoding="utf-8")

    assert "PUBLIC_HTTPS_READY" in script
    assert (
        'SESSION_COOKIE_SECURE=$PUBLIC_HTTPS_READY'
        in script
    )


def test_server_update_uses_an_exclusive_deployment_lock():
    script = Path("deploy/server_update.sh").read_text(encoding="utf-8")

    assert "jwalpha-deploy.lock" in script
    assert "/usr/bin/flock -n" in script


def test_scheduler_service_is_private_and_loads_release_environment():
    unit = Path("deploy/quant-scheduler.service").read_text(
        encoding="utf-8"
    )
    init_script = Path("deploy/server_init.sh").read_text(
        encoding="utf-8"
    )
    update_script = Path("deploy/server_update.sh").read_text(
        encoding="utf-8"
    )

    assert "EnvironmentFile=/opt/quant/.env" in unit
    assert "UMask=0077" in unit
    assert "--wait-seconds 70" in unit
    assert "TimeoutStartSec=90" in unit
    assert "UMask=0077" in init_script
    assert "UMask=0077" in update_script


def test_server_update_unconditionally_verifies_scheduler_main_pid():
    script = Path("deploy/server_update.sh").read_text(encoding="utf-8")
    health_body = script.split(
        "verify_scheduler_health() {",
        1,
    )[1].split("\n}", 1)[0]
    restart_flow = script.split(
        'echo "[6/7] Restart services"',
        1,
    )[1]

    assert 'if ! [[ "$scheduler_pid" =~ ^[1-9][0-9]*$ ]]; then' in (
        health_body
    )
    assert (
        restart_flow.index("systemctl restart quant-scheduler")
        < restart_flow.index("verify_scheduler_health")
    )


@pytest.mark.parametrize(
    "script_path",
    [
        Path("deploy/server_init.sh"),
        Path("deploy/server_update.sh"),
    ],
)
def test_application_ownership_delegates_to_fd_hardener(
    tmp_path,
    script_path,
):
    script = script_path.read_text(encoding="utf-8")
    function_source = _extract_shell_function(
        script,
        "secure_application_ownership",
    )
    helper = tmp_path / "hardener.py"
    helper.write_text("# test helper\n", encoding="utf-8")
    call_log = tmp_path / "hardener.log"
    runner = tmp_path / "ownership-test.sh"
    runner.write_text(
        "\n".join(
            [
                "#!/bin/bash",
                "set -euo pipefail",
                (
                    'fake_python() { printf "%s\\n" "$*" '
                    '>> "$DEPLOY_TEST_HARDENER_LOG"; }'
                ),
                (
                    'id() { if [ "${1:-}" = "-u" ]; then '
                    'printf "0\\n"; fi; return 0; }'
                ),
                "APP_DIR=/opt/test-app",
                "DEPLOY_PYTHON=fake_python",
                (
                    "PERMISSIONS_HELPER="
                    f"{shlex.quote(_bash_path(helper))}"
                ),
                function_source,
                "secure_application_ownership quant",
            ]
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    environment = dict(os.environ)
    environment["DEPLOY_TEST_HARDENER_LOG"] = _bash_path(call_log)

    completed = subprocess.run(
        [_bash_executable(), runner.as_posix()],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    invocation = call_log.read_text(encoding="utf-8")
    assert (
        f"-I {_bash_path(helper)} "
        "--app-dir /opt/test-app --run-user quant"
        in invocation
    )


@pytest.mark.parametrize(
    "script_path",
    [
        Path("deploy/server_init.sh"),
        Path("deploy/server_update.sh"),
    ],
)
def test_runtime_secret_hardening_delegates_to_fd_hardener(
    tmp_path,
    script_path,
):
    script = script_path.read_text(encoding="utf-8")
    helper = tmp_path / "hardener.py"
    helper.write_text("# test helper\n", encoding="utf-8")
    call_log = tmp_path / "hardener.log"
    runner = tmp_path / "runtime-hardening-test.sh"
    runner.write_text(
        "\n".join(
            [
                "#!/bin/bash",
                "set -euo pipefail",
                (
                    'fake_python() { printf "%s\\n" "$*" '
                    '>> "$DEPLOY_TEST_HARDENER_LOG"; }'
                ),
                "APP_DIR=/opt/test-app",
                "DEPLOY_PYTHON=fake_python",
                (
                    "PERMISSIONS_HELPER="
                    f"{shlex.quote(_bash_path(helper))}"
                ),
                _extract_shell_function(
                    script,
                    "secure_runtime_secrets",
                ),
                "secure_runtime_secrets quant",
            ]
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    environment = dict(os.environ)
    environment["DEPLOY_TEST_HARDENER_LOG"] = _bash_path(call_log)

    completed = subprocess.run(
        [_bash_executable(), runner.as_posix()],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    invocation = call_log.read_text(encoding="utf-8")
    assert (
        f"-I {_bash_path(helper)} "
        "--app-dir /opt/test-app --run-user quant --runtime-only"
        in invocation
    )


def test_failed_update_removes_new_scheduler_enablement(tmp_path):
    script = Path("deploy/server_update.sh").read_text(
        encoding="utf-8"
    )
    systemctl_log = tmp_path / "systemctl.log"
    runner = tmp_path / "restore-test.sh"
    runner.write_text(
        "\n".join(
                [
                    "#!/bin/bash",
                    "set -uo pipefail",
                    (
                        'systemctl() { printf "%s\\n" "$*" '
                        '>> "$DEPLOY_TEST_SYSTEMCTL_LOG"; }'
                    ),
                    "DEPLOY_COMPLETED=0",
                "QUIESCE_STARTED=1",
                "SCHEDULER_WAS_ACTIVE=0",
                "SCHEDULER_SHOULD_RUN=0",
                "SCHEDULER_UNIT_WAS_PRESENT=0",
                "SCHEDULER_UNIT_BACKUP=''",
                "PLATFORM_SPLIT_WAS_PRESENT=0",
                    "PLATFORM_SPLIT_BACKUP=''",
                    "SERVICE=test-web",
                    (
                        "SCHEDULER_UNIT_PATH="
                        f"{shlex.quote(_bash_path(tmp_path / 'scheduler.service'))}"
                    ),
                    (
                        "PLATFORM_SPLIT_OVERRIDE="
                        f"{shlex.quote(_bash_path(tmp_path / 'split.conf'))}"
                    ),
                _extract_shell_function(
                    script,
                    "restore_services_on_failure",
                ),
                "false",
                "restore_services_on_failure",
            ]
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    environment = dict(os.environ)
    environment["DEPLOY_TEST_SYSTEMCTL_LOG"] = (
        _bash_path(systemctl_log)
    )

    completed = subprocess.run(
        [_bash_executable(), runner.as_posix()],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1
    calls = systemctl_log.read_text(encoding="utf-8").splitlines()
    assert "disable quant-scheduler" in calls
    assert "stop quant-scheduler" in calls


def test_permission_hardening_uses_fd_based_helper_not_path_chown():
    update_script = Path("deploy/server_update.sh").read_text(
        encoding="utf-8"
    )
    init_script = Path("deploy/server_init.sh").read_text(
        encoding="utf-8"
    )

    for script in (update_script, init_script):
        assert "harden_runtime_permissions.py" in script
        assert 'chown "$owner:$owner" "$file"' not in script
        assert 'chmod 600 "$file"' not in script
        assert 'chown "root:$owner" ".env"' not in script
        assert 'chmod 640 ".env"' not in script


def test_up_to_date_deployment_still_runs_dependencies_and_migrations():
    script = Path("deploy/server_update.sh").read_text(
        encoding="utf-8"
    )
    flow = script.split(
        'echo "[1/7] Fast-forward to verified remote revision"',
        1,
    )[1]

    same_commit_branch = flow.split(
        'if [ "$LOCAL" = "$REMOTE" ]; then',
        1,
    )[1].split("\nfi", 1)[0]
    assert "exit 0" not in same_commit_branch
    assert "prepare_release_venv" in flow
    assert '"$RELEASE_VENV/bin/python" -I -m pip install' in script
    assert 'core/db_migration.py' in flow


def test_failed_post_update_deploy_stays_stopped_for_safe_retry():
    script = Path("deploy/server_update.sh").read_text(
        encoding="utf-8"
    )
    restore_body = _extract_shell_function(
        script,
        "restore_services_on_failure",
    )

    assert "CODE_UPDATED" in restore_body
    assert "leaving services stopped for a safe retry" in restore_body


def test_update_builds_fresh_release_venv_without_executing_legacy_venv():
    script = Path("deploy/server_update.sh").read_text(
        encoding="utf-8"
    )
    flow = script.split(
        'echo "[1/7] Fast-forward to verified remote revision"',
        1,
    )[1]

    assert "prepare_release_venv" in flow
    assert 'sudo -u "$RUN_USER" env -i' in script
    assert '"$DEPLOY_PYTHON" -I -m venv --copies' in script
    assert 'sudo -u "$RUN_USER" "$RELEASE_VENV/bin/python"' in flow
    assert '"$APP_DIR/venv/bin/python" -m pip' not in flow
    assert '"$APP_DIR/venv/bin/python" core/db_migration.py' not in flow


def test_release_venv_is_bound_to_both_systemd_services():
    script = Path("deploy/server_update.sh").read_text(
        encoding="utf-8"
    )

    assert "ensure_release_venv_override" in script
    assert "release-venv.conf" in script
    assert "ExecStart=$RELEASE_VENV/bin/gunicorn" in script
    assert 's|$APP_DIR/venv|$RELEASE_VENV|g' in script


def test_release_venv_directory_is_ignored_by_deployment_checkout():
    completed = subprocess.run(
        [
            "git",
            "check-ignore",
            "--quiet",
            ".venv-releases/release-test/bin/python",
        ],
        check=False,
    )

    assert completed.returncode == 0
