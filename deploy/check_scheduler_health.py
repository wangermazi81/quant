#!/usr/bin/env python3
"""Validate the independent scheduler's cross-process readiness record."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_IS_WINDOWS = os.name == "nt"


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("scheduler heartbeat is missing")
    try:
        parsed = datetime.fromisoformat(
            value.strip().replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise RuntimeError("scheduler heartbeat is invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("scheduler heartbeat timezone is missing")
    return parsed.astimezone(timezone.utc)


def _windows_pid_exists(pid: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            return int(kernel32.WaitForSingleObject(handle, 0)) == 258
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _windows_process_start_token(
    pid: int,
    *,
    kernel32=None,
) -> str | None:
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or not 0 < pid <= 0xFFFFFFFF
    ):
        return None
    try:
        import ctypes

        class FileTime(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32),
            ]

        if kernel32 is None:
            kernel32 = ctypes.WinDLL(
                "kernel32",
                use_last_error=True,
            )
            kernel32.OpenProcess.argtypes = [
                ctypes.c_uint32,
                ctypes.c_int,
                ctypes.c_uint32,
            ]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            pointer = ctypes.POINTER(FileTime)
            kernel32.GetProcessTimes.argtypes = [
                ctypes.c_void_p,
                pointer,
                pointer,
                pointer,
                pointer,
            ]
            kernel32.GetProcessTimes.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int

        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            creation = FileTime()
            exit_time = FileTime()
            kernel = FileTime()
            user = FileTime()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return str(
                (int(creation.dwHighDateTime) << 32)
                | int(creation.dwLowDateTime)
            )
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _read_linux_process_stat(pid: int) -> tuple[str, str] | None:
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text(
            encoding="utf-8"
        )
        closing_paren = text.rfind(")")
        if closing_paren < 0:
            return None
        fields_3_onward = text[closing_paren + 1 :].split()
        if len(fields_3_onward) < 20:
            return None
        return fields_3_onward[0], fields_3_onward[19]
    except (OSError, UnicodeError, ValueError):
        return None


def _linux_boot_id() -> str | None:
    try:
        value = Path(
            "/proc/sys/kernel/random/boot_id"
        ).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def _linux_process_start_token(pid: int) -> str | None:
    process_stat = _read_linux_process_stat(pid)
    if (
        process_stat is None
        or process_stat[0].upper() in {"Z", "X"}
    ):
        return None
    boot_id = _linux_boot_id()
    if boot_id is None:
        return None
    return f"{boot_id}:{process_stat[1]}"


def _process_start_token(pid: int) -> str | None:
    if _IS_WINDOWS:
        return _windows_process_start_token(pid)
    if sys.platform.startswith("linux"):
        return _linux_process_start_token(pid)
    return None


def _pid_exists(pid: int) -> bool:
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or not 0 < pid <= 0xFFFFFFFF
    ):
        return False
    if pid == os.getpid():
        return True
    if _IS_WINDOWS:
        return _windows_pid_exists(pid)
    if sys.platform.startswith("linux"):
        process_stat = _read_linux_process_stat(pid)
        if process_stat is not None:
            return process_stat[0].upper() not in {"Z", "X"}
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False
    return True


def check_runtime(
    path: str | Path,
    *,
    expected_pid: int | None = None,
    max_heartbeat_age_seconds: int = 120,
    max_progress_age_seconds: int = 3600,
    max_job_age_seconds: int = 21600,
) -> dict[str, Any]:
    runtime_path = Path(path)
    try:
        payload = json.loads(
            runtime_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "scheduler runtime status is unavailable"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("scheduler runtime status is invalid")
    if payload.get("ready") is not True:
        raise RuntimeError("scheduler is not ready")
    if payload.get("thread_alive") is not True:
        raise RuntimeError("factor scheduler thread is not alive")
    pid = payload.get("pid")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
    ):
        raise RuntimeError("scheduler pid is invalid")
    if expected_pid is not None and pid != int(expected_pid):
        raise RuntimeError("scheduler pid does not match service MainPID")
    if not _pid_exists(pid):
        raise RuntimeError("scheduler process is not alive")
    expected_process_token = payload.get("process_start_token")
    current_process_token = _process_start_token(pid)
    if (
        not isinstance(expected_process_token, str)
        or not expected_process_token.strip()
        or not isinstance(current_process_token, str)
        or not current_process_token
        or not hmac.compare_digest(
            expected_process_token.strip(),
            current_process_token,
        )
    ):
        raise RuntimeError(
            "scheduler process start identity does not match"
        )
    if payload.get("calendar_ready") is not True:
        raise RuntimeError("scheduler trade calendar is not ready")
    heartbeat = _parse_timestamp(payload.get("heartbeat_at"))
    age = (
        datetime.now(timezone.utc) - heartbeat
    ).total_seconds()
    maximum_age = max(1, int(max_heartbeat_age_seconds))
    if age < -30 or age > maximum_age:
        raise RuntimeError("scheduler heartbeat is stale")
    progress = _parse_timestamp(payload.get("progress_at"))
    progress_age = (
        datetime.now(timezone.utc) - progress
    ).total_seconds()
    job_running = payload.get("job_running") is True
    current_job = str(payload.get("current_job") or "")
    if job_running:
        if not current_job:
            raise RuntimeError("scheduler active job is invalid")
        job_started = _parse_timestamp(
            payload.get("job_started_at")
        )
        job_age = (
            datetime.now(timezone.utc) - job_started
        ).total_seconds()
        maximum_job_age = max(1, int(max_job_age_seconds))
        if job_age < -30 or job_age > maximum_job_age:
            raise RuntimeError("scheduler active job is stale")
    else:
        maximum_progress_age = max(
            1,
            int(max_progress_age_seconds),
        )
        if (
            progress_age < -30
            or progress_age > maximum_progress_age
        ):
            raise RuntimeError("scheduler progress is stale")
    return {
        "ok": True,
        "pid": pid,
        "process_start_token": expected_process_token,
        "heartbeat_at": payload["heartbeat_at"],
        "progress_at": payload["progress_at"],
        "job_running": job_running,
        "current_job": current_job or None,
        "job_started_at": payload.get("job_started_at"),
        "calendar_ready": payload.get("calendar_ready") is True,
        "calendar_reason": str(
            payload.get("calendar_reason") or "unknown"
        ),
    }


def wait_for_runtime(
    path: str | Path,
    *,
    expected_pid: int | None = None,
    wait_seconds: int = 0,
    max_heartbeat_age_seconds: int = 120,
    max_progress_age_seconds: int = 3600,
    max_job_age_seconds: int = 21600,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0, int(wait_seconds))
    last_error: RuntimeError | None = None
    while True:
        try:
            return check_runtime(
                path,
                expected_pid=expected_pid,
                max_heartbeat_age_seconds=(
                    max_heartbeat_age_seconds
                ),
                max_progress_age_seconds=max_progress_age_seconds,
                max_job_age_seconds=max_job_age_seconds,
            )
        except RuntimeError as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            raise last_error
        time.sleep(0.25)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", default="/opt/quant")
    parser.add_argument("--runtime-path")
    parser.add_argument("--expected-pid", type=int)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument(
        "--max-heartbeat-age-seconds",
        type=int,
        default=120,
    )
    parser.add_argument(
        "--max-progress-age-seconds",
        type=int,
        default=3600,
    )
    parser.add_argument(
        "--max-job-age-seconds",
        type=int,
        default=21600,
    )
    args = parser.parse_args()
    runtime_path = (
        Path(args.runtime_path)
        if args.runtime_path
        else Path(args.app_dir)
        / "data"
        / "research"
        / "relay_factor"
        / "auto_scheduler_runtime.json"
    )
    try:
        result = wait_for_runtime(
            runtime_path,
            expected_pid=args.expected_pid,
            wait_seconds=args.wait_seconds,
            max_heartbeat_age_seconds=(
                args.max_heartbeat_age_seconds
            ),
            max_progress_age_seconds=args.max_progress_age_seconds,
            max_job_age_seconds=args.max_job_age_seconds,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
