#!/usr/bin/env python3
"""Fail closed when an old scheduler can race a lock-protocol migration."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    cmdline: tuple[str, ...]
    cwd: str
    cgroups: tuple[str, ...]
    environment: Mapping[str, str]


@dataclass(frozen=True)
class UnsafeProcess:
    pid: int
    reason: str
    command: str


def _is_within(path: str, parent: str) -> bool:
    if not path:
        return False
    try:
        return os.path.commonpath(
            (os.path.realpath(path), os.path.realpath(parent))
        ) == os.path.realpath(parent)
    except (OSError, ValueError):
        return False


def _module_invocation(tokens: Sequence[str], module: str) -> bool:
    return any(
        token == "-m" and index + 1 < len(tokens)
        and tokens[index + 1] == module
        for index, token in enumerate(tokens)
    )


def _has_script(tokens: Sequence[str], filename: str) -> bool:
    return any(Path(token).name == filename for token in tokens)


def _has_app_script(
    process: ProcessInfo,
    app_dir: str,
    filename: str,
) -> bool:
    for token in process.cmdline:
        if Path(token).name != filename:
            continue
        candidate = (
            token
            if token.startswith("/")
            else str(Path(process.cwd) / token)
        )
        if _is_within(candidate, app_dir):
            return True
    return False


def _is_platform_process(process: ProcessInfo, app_dir: str) -> bool:
    tokens = process.cmdline
    if _has_app_script(process, app_dir, "main.py") or _has_app_script(
        process,
        app_dir,
        "launcher.py",
    ):
        return True
    if _module_invocation(tokens, "main"):
        return True
    return _is_within(process.cwd, app_dir) and any(
        "gunicorn" in Path(token).name.lower() for token in tokens
    ) and any(token == "main:app" for token in tokens)


def _is_standalone_scheduler(process: ProcessInfo) -> bool:
    return _has_script(
        process.cmdline,
        "scheduler_main.py",
    ) or _module_invocation(process.cmdline, "scheduler_main")


def find_unsafe_processes(
    processes: Iterable[ProcessInfo],
    *,
    app_dir: str,
    web_cgroup: str,
) -> list[UnsafeProcess]:
    issues: list[UnsafeProcess] = []
    for process in processes:
        command = " ".join(process.cmdline)
        if _is_standalone_scheduler(process):
            issues.append(
                UnsafeProcess(
                    process.pid,
                    "standalone_scheduler_still_running",
                    command,
                )
            )
            continue
        if not _is_platform_process(process, app_dir):
            continue
        if web_cgroup not in process.cgroups:
            issues.append(
                UnsafeProcess(
                    process.pid,
                    "platform_process_outside_systemd_cgroup",
                    command,
                )
            )
            continue
        if process.environment.get("THS_EMBED_SCHEDULER") != "0":
            issues.append(
                UnsafeProcess(
                    process.pid,
                    "platform_scheduler_split_not_applied",
                    command,
                )
            )
    return issues


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _decode_nul_fields(payload: bytes) -> tuple[str, ...]:
    return tuple(
        value.decode("utf-8", errors="replace")
        for value in payload.split(b"\0")
        if value
    )


def _read_environment(path: Path) -> dict[str, str]:
    environment: dict[str, str] = {}
    for item in _decode_nul_fields(_read_bytes(path)):
        key, separator, value = item.partition("=")
        if separator:
            environment[key] = value
    return environment


def read_processes(proc_root: str | Path = "/proc") -> list[ProcessInfo]:
    root = Path(proc_root)
    processes: list[ProcessInfo] = []
    for directory in root.iterdir():
        if not directory.name.isdigit():
            continue
        pid = int(directory.name)
        try:
            cmdline = _decode_nul_fields(
                _read_bytes(directory / "cmdline")
            )
            if not cmdline:
                continue
            cwd = os.readlink(directory / "cwd")
            cgroups = tuple(
                line.split(":", 2)[2]
                for line in (directory / "cgroup").read_text(
                    encoding="utf-8",
                    errors="replace",
                ).splitlines()
                if line.count(":") >= 2
            )
            environment = _read_environment(directory / "environ")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"cannot read process metadata for pid {pid}: {exc}"
            ) from exc
        processes.append(
            ProcessInfo(
                pid=pid,
                cmdline=cmdline,
                cwd=cwd,
                cgroups=cgroups,
                environment=environment,
            )
        )
    return processes


def systemd_control_group(service: str) -> str:
    completed = subprocess.run(
        [
            "systemctl",
            "show",
            service,
            "-p",
            "ControlGroup",
            "--value",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    control_group = completed.stdout.strip()
    if not control_group or control_group == "/":
        raise RuntimeError(
            f"systemd control group unavailable for {service}"
        )
    return control_group


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--web-service", required=True)
    parser.add_argument("--proc-root", default="/proc")
    args = parser.parse_args(argv)
    try:
        web_cgroup = systemd_control_group(args.web_service)
        processes = read_processes(args.proc_root)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"ERROR: scheduler quiescence check failed: {exc}", file=sys.stderr)
        return 2

    issues = find_unsafe_processes(
        processes,
        app_dir=args.app_dir,
        web_cgroup=web_cgroup,
    )
    if not issues:
        print("Scheduler quiescence verified.")
        return 0
    for issue in issues:
        print(
            f"ERROR: pid={issue.pid} reason={issue.reason} "
            f"command={issue.command!r}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
