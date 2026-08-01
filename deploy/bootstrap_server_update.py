#!/usr/bin/env python3
"""Stage pinned deployment tools without trusting the old checkout."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import shlex
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit


DEPLOY_TOOL_PATHS = (
    "deploy/check_scheduler_health.py",
    "deploy/check_scheduler_quiescence.py",
    "deploy/harden_runtime_permissions.py",
    "deploy/server_update.sh",
)
_BRANCH_RE = re.compile(
    r"^(?!-)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]+$"
)
_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
Runner = Callable[..., subprocess.CompletedProcess]


def deployment_tool_bundle_sha256(
    source_by_path: Mapping[str, bytes],
) -> str:
    if set(source_by_path) != set(DEPLOY_TOOL_PATHS):
        raise ValueError("deployment tool bundle is incomplete")
    digest = hashlib.sha256()
    for relative_path in sorted(source_by_path):
        content = bytes(source_by_path[relative_path]).replace(
            b"\r\n",
            b"\n",
        ).replace(b"\r", b"\n")
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def build_local_tool_bundle_sha256(
    repository_root: str | Path,
) -> str:
    root = Path(repository_root)
    return deployment_tool_bundle_sha256(
        {
            relative_path: (root / relative_path).read_bytes()
            for relative_path in DEPLOY_TOOL_PATHS
        }
    )


def _validate_remote_url(remote_url: str) -> str:
    value = str(remote_url or "").strip()
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("remote_url contains an invalid port") from error
    valid_https = bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )
    valid_ssh = bool(
        parsed.scheme == "ssh"
        and parsed.hostname
        and parsed.username == "git"
        and parsed.password is None
        and port in {None, 22}
        and parsed.path not in {"", "/"}
    )
    if (
        not (valid_https or valid_ssh)
        or parsed.query
        or parsed.fragment
        or any(char in value for char in "\r\n\0")
    ):
        raise ValueError(
            "remote_url must be credential-free HTTPS or "
            "ssh://git@host/path"
        )
    return value


def _validated_ssh_file(
    value: str | Path | None,
    *,
    field: str,
    private: bool,
) -> Path:
    if value is None:
        raise ValueError(f"{field} is required for an SSH remote")
    path = Path(os.path.abspath(os.fspath(value)))
    if not Path(value).is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    try:
        details = os.lstat(path)
    except OSError as error:
        raise RuntimeError(f"{field} is unavailable: {error}") from error
    mode = stat.S_IMODE(details.st_mode)
    posix_permissions_invalid = bool(
        os.name == "posix"
        and (
            details.st_uid != os.geteuid()
            or mode & (0o077 if private else 0o022)
        )
    )
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or posix_permissions_invalid
    ):
        raise RuntimeError(
            f"{field} must be owner-controlled regular file"
        )
    return path


def _git_ssh_command(
    *,
    ssh_key_path: str | Path | None,
    ssh_known_hosts_path: str | Path | None,
) -> str:
    key = _validated_ssh_file(
        ssh_key_path,
        field="ssh_key_path",
        private=True,
    )
    known_hosts = _validated_ssh_file(
        ssh_known_hosts_path,
        field="ssh_known_hosts_path",
        private=False,
    )
    arguments = [
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-i",
        str(key),
    ]
    return " ".join(shlex.quote(value) for value in arguments)


def _trusted_git_environment(
    staging: Path,
    *,
    git_ssh_command: str | None = None,
) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get(
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        ),
        "HOME": str(staging),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    if git_ssh_command:
        environment["GIT_SSH_COMMAND"] = git_ssh_command
    return environment


def _git_command(
    git_dir: Path,
    *arguments: str,
) -> list[str]:
    return [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.file.allow=never",
        "--git-dir",
        str(git_dir),
        *arguments,
    ]


def _command_stdout_ascii(result: subprocess.CompletedProcess) -> str:
    content = result.stdout
    if isinstance(content, bytes):
        return content.decode("ascii", errors="strict").strip()
    return str(content or "").strip()


def _remote_file(
    *,
    git_dir: Path,
    target_sha: str,
    relative_path: str,
    runner: Runner,
    cwd: Path,
    environment: Mapping[str, str],
) -> bytes:
    result = runner(
        _git_command(
            git_dir,
            "show",
            f"{target_sha}:{relative_path}",
        ),
        cwd=cwd,
        check=True,
        capture_output=True,
        env=dict(environment),
    )
    content = result.stdout
    if isinstance(content, str):
        return content.encode("utf-8")
    return bytes(content or b"")


def _deployment_environment(
    *,
    app_dir: Path,
    remote_url: str,
    branch: str,
    target_sha: str,
    bundle_sha256: str,
    staged_paths: Mapping[str, Path],
    git_ssh_command: str | None = None,
) -> dict[str, str]:
    environment = {
        "PATH": (
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "APP_DIR": str(app_dir),
        "DEPLOY_PYTHON": "/usr/bin/python3",
        "QUIESCENCE_HELPER": str(
            staged_paths["deploy/check_scheduler_quiescence.py"]
        ),
        "HEALTH_HELPER": str(
            staged_paths["deploy/check_scheduler_health.py"]
        ),
        "PERMISSIONS_HELPER": str(
            staged_paths["deploy/harden_runtime_permissions.py"]
        ),
        "SCHEDULER_MIGRATION_BOOTSTRAPPED": "1",
        "DEPLOY_REMOTE_URL": remote_url,
        "DEPLOY_BRANCH": branch,
        "DEPLOY_TARGET_SHA": target_sha,
        "DEPLOY_TOOL_BUNDLE_SHA256": bundle_sha256,
    }
    if git_ssh_command:
        environment["DEPLOY_GIT_SSH_COMMAND"] = git_ssh_command
    return environment


def bootstrap(
    *,
    app_dir: str | Path,
    remote_url: str,
    branch: str,
    target_sha: str,
    expected_bundle_sha256: str,
    ssh_key_path: str | Path | None = None,
    ssh_known_hosts_path: str | Path | None = None,
    runner: Runner = subprocess.run,
) -> int:
    root = Path(os.path.abspath(os.fspath(app_dir)))
    try:
        root_details = os.lstat(root)
    except OSError as error:
        raise RuntimeError(f"app_dir is unavailable: {error}") from error
    if stat.S_ISLNK(root_details.st_mode) or not stat.S_ISDIR(
        root_details.st_mode
    ):
        raise RuntimeError("app_dir must be a real directory")

    trusted_remote = _validate_remote_url(remote_url)
    remote_scheme = urlsplit(trusted_remote).scheme
    if remote_scheme == "ssh":
        git_ssh_command = _git_ssh_command(
            ssh_key_path=ssh_key_path,
            ssh_known_hosts_path=ssh_known_hosts_path,
        )
    else:
        if ssh_key_path is not None or ssh_known_hosts_path is not None:
            raise ValueError(
                "SSH identity paths require an SSH remote"
            )
        git_ssh_command = None
    trusted_branch = str(branch or "").strip()
    trusted_target = str(target_sha or "").strip().lower()
    trusted_bundle = str(expected_bundle_sha256 or "").strip().lower()
    if (
        not _BRANCH_RE.fullmatch(trusted_branch)
        or trusted_branch.startswith("/")
        or trusted_branch.endswith("/")
        or "//" in trusted_branch
    ):
        raise ValueError("branch contains unsupported characters")
    if not _OBJECT_ID_RE.fullmatch(trusted_target):
        raise ValueError("target_sha must be a full Git object ID")
    if not _SHA256_RE.fullmatch(trusted_bundle):
        raise ValueError(
            "expected_bundle_sha256 must be a lowercase SHA-256"
        )

    with tempfile.TemporaryDirectory(
        prefix="jwalpha-server-update-"
    ) as temp_dir:
        staging = Path(temp_dir)
        git_dir = staging / "remote.git"
        git_environment = _trusted_git_environment(
            staging,
            git_ssh_command=git_ssh_command,
        )
        runner(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "init",
                "--bare",
                str(git_dir),
            ],
            cwd=staging,
            check=True,
            capture_output=True,
            env=git_environment,
        )
        remote_ref = f"refs/remotes/deploy/{trusted_branch}"
        fetch_command = _git_command(
            git_dir,
            "fetch",
            "--depth=1",
            "--no-tags",
            trusted_remote,
            f"+refs/heads/{trusted_branch}:{remote_ref}",
        )
        try:
            runner(
                fetch_command,
                cwd=staging,
                check=True,
                capture_output=True,
                env=git_environment,
            )
        except subprocess.CalledProcessError as error:
            detail = error.stderr
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            detail = str(detail or "").strip()[:500]
            raise RuntimeError(
                "remote fetch failed"
                + (f": {detail}" if detail else "")
            ) from error
        resolved = _command_stdout_ascii(
            runner(
                _git_command(
                    git_dir,
                    "rev-parse",
                    "--verify",
                    f"{remote_ref}^{{commit}}",
                ),
                cwd=staging,
                check=True,
                capture_output=True,
                env=git_environment,
            )
        ).lower()
        if not hmac.compare_digest(resolved, trusted_target):
            raise RuntimeError(
                "remote branch moved or does not match target_sha"
            )

        source_by_path = {
            relative_path: _remote_file(
                git_dir=git_dir,
                target_sha=trusted_target,
                relative_path=relative_path,
                runner=runner,
                cwd=staging,
                environment=git_environment,
            )
            for relative_path in DEPLOY_TOOL_PATHS
        }
        actual_bundle = deployment_tool_bundle_sha256(source_by_path)
        if not hmac.compare_digest(actual_bundle, trusted_bundle):
            raise RuntimeError(
                "deployment tool bundle SHA-256 mismatch"
            )

        staged_paths: dict[str, Path] = {}
        for relative_path, content in source_by_path.items():
            staged_path = staging / Path(relative_path).name
            staged_path.write_bytes(content)
            staged_path.chmod(
                0o700
                if relative_path == "deploy/server_update.sh"
                else 0o600
            )
            staged_paths[relative_path] = staged_path
        result = runner(
            [
                "bash",
                str(staged_paths["deploy/server_update.sh"]),
            ],
            cwd=root,
            check=False,
            env=_deployment_environment(
                app_dir=root,
                remote_url=trusted_remote,
                branch=trusted_branch,
                target_sha=trusted_target,
                bundle_sha256=trusted_bundle,
                staged_paths=staged_paths,
                git_ssh_command=git_ssh_command,
            ),
        )
        return int(result.returncode)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", default="/opt/quant")
    parser.add_argument("--remote-url", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--tool-bundle-sha256", required=True)
    parser.add_argument("--ssh-key-path")
    parser.add_argument("--ssh-known-hosts-path")
    args = parser.parse_args()
    return bootstrap(
        app_dir=args.app_dir,
        remote_url=args.remote_url,
        branch=args.branch,
        target_sha=args.target_sha,
        expected_bundle_sha256=args.tool_bundle_sha256,
        ssh_key_path=args.ssh_key_path,
        ssh_known_hosts_path=args.ssh_known_hosts_path,
    )


if __name__ == "__main__":
    raise SystemExit(main())
