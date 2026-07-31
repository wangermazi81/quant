#!/usr/bin/env python3
"""Harden application ownership without following attacker-controlled links."""

from __future__ import annotations

import argparse
import errno
import os
import re
import secrets
import stat
from pathlib import Path
from urllib.parse import urlsplit


O_CLOEXEC = getattr(os, "O_CLOEXEC", 0x80000)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0x10000)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0x20000)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0x800)
_BRANCH_RE = re.compile(
    r"^(?!-)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]+$"
)
RUNTIME_SECRET_PATHS = (
    "data/.secret_key",
    "data/ai_config.json",
    "data/ai_trader_config.json",
    "data/ai_research.db",
    "data/ai_research.db-wal",
    "data/ai_research.db-shm",
    "data/users.db",
    "data/users.db-wal",
    "data/users.db-shm",
    "data/research/relay_factor/auto_scheduler.lock",
    "data/research/relay_factor/auto_scheduler.lock.guard",
    "data/search_config.json",
    "data/notify_config.json",
    "tools/bridge_config.json",
    "tools/mobile_ths_api_profile.json",
    "tools/mobile_ths_profile.json",
)
_CODE_RUNTIME_PATHS = frozenset(
    path for path in RUNTIME_SECRET_PATHS if not path.startswith("data/")
)


def _open_error(name: str, error: OSError) -> RuntimeError:
    if error.errno == errno.ELOOP:
        return RuntimeError(f"symbolic link is not allowed: {name}")
    return RuntimeError(f"cannot open {name}: {error}")


def _secure_regular_at(
    parent_fd: int,
    name: str,
    *,
    uid: int,
    gid: int,
    mode: int,
    expected_dev: int | None = None,
) -> None:
    flags = os.O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        raise _open_error(name, error) from error
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode):
            raise RuntimeError(f"regular file required: {name}")
        if details.st_nlink != 1:
            raise RuntimeError(f"hard-linked file is not allowed: {name}")
        if (
            expected_dev is not None
            and details.st_dev != expected_dev
        ):
            raise RuntimeError(f"cross-device file is not allowed: {name}")
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _open_directory_at(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | O_CLOEXEC | O_DIRECTORY | O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        raise _open_error(name, error) from error
    details = os.fstat(fd)
    if not stat.S_ISDIR(details.st_mode):
        os.close(fd)
        raise RuntimeError(f"directory required: {name}")
    return fd


def _regular_mode(details: os.stat_result, private: bool) -> int:
    if private:
        return 0o700 if details.st_mode & 0o111 else 0o600
    return 0o750 if details.st_mode & 0o111 else 0o640


def _secure_directory_contents(
    directory_fd: int,
    *,
    uid: int,
    gid: int,
    directory_mode: int,
    private: bool,
    expected_dev: int,
    relative_prefix: str = "",
    skip_paths: frozenset[str] = frozenset(),
) -> None:
    for name in sorted(os.listdir(directory_fd)):
        relative_path = (
            f"{relative_prefix}/{name}"
            if relative_prefix
            else name
        )
        if relative_path in skip_paths:
            continue
        try:
            details = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise RuntimeError(f"cannot inspect {name}: {error}") from error
        if stat.S_ISLNK(details.st_mode):
            raise RuntimeError(f"symbolic link is not allowed: {name}")
        if stat.S_ISDIR(details.st_mode):
            child_fd = _open_directory_at(directory_fd, name)
            try:
                child_details = os.fstat(child_fd)
                if child_details.st_dev != expected_dev:
                    raise RuntimeError(
                        f"cross-device directory is not allowed: {name}"
                    )
                _secure_directory_contents(
                    child_fd,
                    uid=uid,
                    gid=gid,
                    directory_mode=directory_mode,
                    private=private,
                    expected_dev=expected_dev,
                    relative_prefix=relative_path,
                    skip_paths=skip_paths,
                )
                os.fchown(child_fd, uid, gid)
                os.fchmod(child_fd, directory_mode)
            finally:
                os.close(child_fd)
            continue
        if stat.S_ISREG(details.st_mode):
            _secure_regular_at(
                directory_fd,
                name,
                uid=uid,
                gid=gid,
                mode=_regular_mode(details, private),
                expected_dev=expected_dev,
            )
            continue
        raise RuntimeError(f"unsupported filesystem entry: {name}")


def _secure_tree_at(
    parent_fd: int,
    name: str,
    *,
    uid: int,
    gid: int,
    directory_mode: int,
    private: bool,
    expected_dev: int,
    relative_prefix: str = "",
    skip_paths: frozenset[str] = frozenset(),
) -> None:
    directory_fd = _open_directory_at(parent_fd, name)
    try:
        details = os.fstat(directory_fd)
        if details.st_dev != expected_dev:
            raise RuntimeError(
                f"cross-device directory is not allowed: {name}"
            )
        _secure_directory_contents(
            directory_fd,
            uid=uid,
            gid=gid,
            directory_mode=directory_mode,
            private=private,
            expected_dev=expected_dev,
            relative_prefix=relative_prefix or name,
            skip_paths=skip_paths,
        )
        os.fchown(directory_fd, uid, gid)
        os.fchmod(directory_fd, directory_mode)
    finally:
        os.close(directory_fd)


def _ensure_directory_at(
    parent_fd: int,
    name: str,
    *,
    mode: int,
) -> None:
    try:
        os.mkdir(name, mode=mode, dir_fd=parent_fd)
    except FileExistsError:
        return
    except OSError as error:
        raise RuntimeError(f"cannot create {name}: {error}") from error


def _open_relative_parent(
    root_fd: int,
    relative_path: str,
    *,
    expected_dev: int,
) -> tuple[int, str]:
    parts = Path(relative_path).parts
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or Path(relative_path).is_absolute()
    ):
        raise ValueError(f"invalid relative path: {relative_path}")
    current_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = _open_directory_at(current_fd, part)
            details = os.fstat(next_fd)
            if details.st_dev != expected_dev:
                os.close(next_fd)
                raise RuntimeError(
                    f"cross-device directory is not allowed: {part}"
                )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, parts[-1]
    except Exception:
        os.close(current_fd)
        raise


def _secure_optional_regular(
    root_fd: int,
    relative_path: str,
    *,
    uid: int,
    gid: int,
    mode: int,
    expected_dev: int,
) -> None:
    try:
        parent_fd, name = _open_relative_parent(
            root_fd,
            relative_path,
            expected_dev=expected_dev,
        )
    except RuntimeError as error:
        if isinstance(error.__cause__, FileNotFoundError):
            return
        raise
    try:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        _secure_regular_at(
            parent_fd,
            name,
            uid=uid,
            gid=gid,
            mode=mode,
            expected_dev=expected_dev,
        )
    finally:
        os.close(parent_fd)


def _account_ids(run_user: str) -> tuple[int, int]:
    if os.name != "posix":
        raise RuntimeError("permission hardening requires a POSIX host")
    import pwd

    if os.geteuid() != 0:
        raise RuntimeError("permission hardening requires root privileges")
    if not run_user or run_user == "root":
        raise RuntimeError("run_user must be an existing non-root account")
    try:
        account = pwd.getpwnam(run_user)
    except KeyError as error:
        raise RuntimeError(f"unknown run_user: {run_user}") from error
    return account.pw_uid, account.pw_gid


def _git_config_payload(remote_url: str, branch: str) -> bytes:
    trusted_url = str(remote_url or "").strip()
    trusted_branch = str(branch or "").strip()
    parsed = urlsplit(trusted_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char in trusted_url for char in "\r\n\0")
    ):
        raise ValueError(
            "remote_url must be an HTTPS URL without credentials"
        )
    if (
        not _BRANCH_RE.fullmatch(trusted_branch)
        or trusted_branch.startswith("/")
        or trusted_branch.endswith("/")
        or "//" in trusted_branch
    ):
        raise ValueError("branch contains unsupported characters")
    return (
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tfilemode = true\n"
        "\tbare = false\n"
        "\tlogallrefupdates = true\n"
        "\thooksPath = /dev/null\n"
        '[remote "origin"]\n'
        f"\turl = {trusted_url}\n"
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
        f'[branch "{trusted_branch}"]\n'
        "\tremote = origin\n"
        f"\tmerge = refs/heads/{trusted_branch}\n"
    ).encode("utf-8")


def _write_all(fd: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise RuntimeError("short write while replacing Git config")
        remaining = remaining[written:]


def reset_git_config(
    app_dir: str | Path,
    *,
    remote_url: str,
    branch: str,
) -> None:
    payload = _git_config_payload(remote_url, branch)
    root_path = Path(os.path.abspath(os.fspath(app_dir)))
    root_fd = os.open(
        str(root_path),
        os.O_RDONLY | O_CLOEXEC | O_DIRECTORY | O_NOFOLLOW,
    )
    temp_name = (
        f"config.deploy-{os.getpid()}-{secrets.token_hex(8)}"
    )
    git_fd = -1
    config_fd = -1
    try:
        root_details = os.fstat(root_fd)
        git_fd = _open_directory_at(root_fd, ".git")
        git_details = os.fstat(git_fd)
        if (
            git_details.st_dev != root_details.st_dev
            or git_details.st_uid != 0
            or stat.S_IMODE(git_details.st_mode) & 0o022
        ):
            raise RuntimeError(
                ".git must be root-owned and not group/world writable"
            )
        config_fd = os.open(
            temp_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | O_CLOEXEC
            | O_NOFOLLOW,
            0o600,
            dir_fd=git_fd,
        )
        _write_all(config_fd, payload)
        os.fchown(config_fd, 0, 0)
        os.fchmod(config_fd, 0o600)
        os.fsync(config_fd)
        os.close(config_fd)
        config_fd = -1
        os.replace(
            temp_name,
            "config",
            src_dir_fd=git_fd,
            dst_dir_fd=git_fd,
        )
        os.fsync(git_fd)
    finally:
        if config_fd >= 0:
            os.close(config_fd)
        if git_fd >= 0:
            try:
                os.unlink(temp_name, dir_fd=git_fd)
            except FileNotFoundError:
                pass
            os.close(git_fd)
        os.close(root_fd)


def harden_application(
    app_dir: str | Path,
    *,
    runtime_uid: int,
    runtime_gid: int,
    runtime_only: bool = False,
    include_venv: bool = False,
) -> None:
    root_path = Path(os.path.abspath(os.fspath(app_dir)))
    flags = os.O_RDONLY | O_CLOEXEC | O_DIRECTORY | O_NOFOLLOW
    try:
        root_fd = os.open(str(root_path), flags)
    except OSError as error:
        raise _open_error(str(root_path), error) from error
    try:
        root_details = os.fstat(root_fd)
        root_dev = root_details.st_dev
        for runtime_name in ("data", "logs"):
            _ensure_directory_at(root_fd, runtime_name, mode=0o700)
            runtime_fd = _open_directory_at(root_fd, runtime_name)
            try:
                runtime_details = os.fstat(runtime_fd)
                if runtime_details.st_dev != root_dev:
                    raise RuntimeError(
                        "cross-device runtime directory is not allowed: "
                        f"{runtime_name}"
                    )
                os.fchown(runtime_fd, runtime_uid, runtime_gid)
                os.fchmod(runtime_fd, 0o700)
            finally:
                os.close(runtime_fd)
        _secure_optional_regular(
            root_fd,
            ".env",
            uid=0,
            gid=runtime_gid,
            mode=0o640,
            expected_dev=root_dev,
        )
        for relative_path in RUNTIME_SECRET_PATHS:
            _secure_optional_regular(
                root_fd,
                relative_path,
                uid=runtime_uid,
                gid=runtime_gid,
                mode=0o600,
                expected_dev=root_dev,
            )
        if runtime_only:
            return

        excluded = {"data", "logs", ".env"}
        if not include_venv:
            excluded.add("venv")
        for name in sorted(os.listdir(root_fd)):
            if name in excluded:
                continue
            details = os.stat(
                name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(details.st_mode):
                raise RuntimeError(
                    f"symbolic link is not allowed in application code: {name}"
                )
            if stat.S_ISDIR(details.st_mode):
                is_git = name == ".git"
                _secure_tree_at(
                    root_fd,
                    name,
                    uid=0,
                    gid=0 if is_git else runtime_gid,
                    directory_mode=0o700 if is_git else 0o750,
                    private=is_git,
                    expected_dev=root_dev,
                    relative_prefix=name,
                    skip_paths=(
                        frozenset()
                        if is_git
                        else _CODE_RUNTIME_PATHS
                    ),
                )
            elif stat.S_ISREG(details.st_mode):
                _secure_regular_at(
                    root_fd,
                    name,
                    uid=0,
                    gid=runtime_gid,
                    mode=_regular_mode(details, False),
                    expected_dev=root_dev,
                )
            else:
                raise RuntimeError(
                    f"unsupported application entry: {name}"
                )
        os.fchown(root_fd, 0, runtime_gid)
        os.fchmod(root_fd, 0o750)
    finally:
        os.close(root_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--run-user", required=True)
    parser.add_argument("--runtime-only", action="store_true")
    parser.add_argument("--include-venv", action="store_true")
    parser.add_argument("--git-config-only", action="store_true")
    parser.add_argument("--remote-url")
    parser.add_argument("--branch")
    args = parser.parse_args()
    runtime_uid, runtime_gid = _account_ids(args.run_user)
    if args.git_config_only:
        if args.runtime_only or args.include_venv:
            parser.error(
                "--git-config-only cannot be combined with hardening modes"
            )
        if not args.remote_url or not args.branch:
            parser.error(
                "--git-config-only requires --remote-url and --branch"
            )
        reset_git_config(
            args.app_dir,
            remote_url=args.remote_url,
            branch=args.branch,
        )
        return 0
    harden_application(
        args.app_dir,
        runtime_uid=runtime_uid,
        runtime_gid=runtime_gid,
        runtime_only=args.runtime_only,
        include_venv=args.include_venv,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
