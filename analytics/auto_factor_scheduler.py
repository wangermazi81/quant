# -*- coding: utf-8 -*-
"""Root-admin auto research scheduler for factor mining and AI trade decisions.

The scheduler runs inside the existing web service process. It never exposes
strategy source code and always uses the root admin user's own AI trading config
for model calls.
"""
from __future__ import annotations

import errno
import json
import logging
import math
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from core import user_manager as um
from analytics import relay_factor_mining
from analytics import relay_factor_experiment
from analytics import short_factor_job
from analytics import ai_trader as ai_trader_engine

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
ROOT = BASE_DIR / "data" / "research" / "relay_factor"
STATUS_PATH = ROOT / "auto_scheduler_status.json"
LOCK_PATH = ROOT / "auto_scheduler.lock"
RUNTIME_PATH = ROOT / "auto_scheduler_runtime.json"
SCHEDULER_GUARD_PROTOCOL_VERSION = "scheduler_kernel_guard_v1"

DEFAULT_CONFIG = {
    "enabled": True,
    "data_refresh_times": ["15:35", "16:10"],
    "factor_mining_time": "17:10",
    "short_factor_discovery_time": "17:10",
    "ai_decision_times": ["09:15", "09:25"],
    "window_minutes": 90,
    "data_refresh_window_minutes": 20,
    "retry_cooldown_minutes": 10,
    "data_refresh_max_attempts_per_slot": 1,
    "short_factor_max_attempts_per_day": 3,
    "short_factor_catch_up_deadline": "23:30",
    "max_candidates": 6,
    "execute_paper": False,
    "mining": {
        "n_stocks": 800,
        "days": 320,
        "train_days": 60,
        "test_days": 10,
        "hold_days": 2,
        "stop_loss": -0.035,
        "take_profit": 0.07,
        "top_k": 10,
        "candidate_pool_size": 100,
    },
}

_thread_lock = threading.Lock()
_started = False
_scheduler_thread: threading.Thread | None = None
_scheduler_ready_event = threading.Event()
_runtime_lock = threading.RLock()
_last_scheduler_progress_at: str | None = None
_heartbeat_thread: threading.Thread | None = None


def _scheduler_guard_path() -> Path:
    return LOCK_PATH.with_name(f"{LOCK_PATH.name}.guard")


def _try_acquire_kernel_guard(handle) -> str | None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {
                errno.EACCES,
                errno.EAGAIN,
                errno.EDEADLK,
            } or getattr(exc, "winerror", None) in {33, 36}:
                return None
            raise
        return "windows"

    if sys.platform.startswith("linux"):
        import fcntl

        try:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return None
            raise
        return "linux"

    raise RuntimeError(
        f"unsupported scheduler lock platform: {sys.platform}"
    )


def _release_kernel_guard(handle, lock_kind: str) -> None:
    if lock_kind == "windows":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    if lock_kind != "linux":
        raise RuntimeError(
            f"unsupported scheduler lock kind: {lock_kind}"
        )

    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _scheduler_guard():
    ROOT.mkdir(parents=True, exist_ok=True)
    handle = _scheduler_guard_path().open("a+b")
    lock_kind = _try_acquire_kernel_guard(handle)
    if lock_kind is None:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            _release_kernel_guard(handle, lock_kind)
        finally:
            handle.close()


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today_text() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _calendar_readiness(
    now: datetime | None = None,
) -> tuple[bool, str]:
    from market import trading_calendar

    current = now or datetime.now()
    try:
        verified = trading_calendar.has_verified_calendar(
            current.year
        )
    except Exception:
        verified = False
    if not verified:
        return (
            False,
            "authoritative_trade_calendar_unavailable",
        )
    return True, "ok"


def _ensure_calendar_readiness(
    now: datetime | None = None,
) -> tuple[bool, str]:
    from market import trading_calendar

    current = now or datetime.now()
    ready, reason = _calendar_readiness(current)
    if ready:
        return ready, reason
    try:
        trading_calendar.refresh_from_tushare(current.year)
    except Exception:
        return False, "authoritative_trade_calendar_refresh_error"
    ready, _reason = _calendar_readiness(current)
    if not ready:
        return (
            False,
            "authoritative_trade_calendar_unavailable_after_refresh",
        )
    return True, "ok"


def _write_runtime_status(
    *,
    ready: bool,
    thread_alive: bool,
    calendar_ready: bool,
    calendar_reason: str,
    error: str | None = None,
) -> dict[str, Any]:
    with _runtime_lock:
        pid = os.getpid()
        process_start_token = _process_start_token(pid)
        previous = _read_json(RUNTIME_PATH, {})
        if (
            not isinstance(previous, dict)
            or previous.get("pid") != pid
            or not _process_tokens_match(
                _normalize_process_start_token(
                    previous.get("process_start_token")
                ),
                _normalize_process_start_token(process_start_token),
            )
        ):
            previous = {}
        now = _runtime_now()
        progress_at = (
            _last_scheduler_progress_at
            or previous.get("progress_at")
        )
        scheduler_status = _read_json(STATUS_PATH, {})
        if not isinstance(scheduler_status, dict):
            scheduler_status = {}
        job_running = scheduler_status.get("running") is True
        current_job = (
            str(scheduler_status.get("current_job") or "")
            if job_running
            else ""
        )
        job_started_at = (
            scheduler_status.get("current_job_started_at")
            if job_running
            else None
        )
        payload = {
            "ready": bool(ready),
            "pid": pid,
            "process_start_token": process_start_token,
            "thread_alive": bool(thread_alive),
            "started_at": previous.get("started_at") or now,
            "heartbeat_at": now,
            "progress_at": progress_at,
            "progress_age_seconds": _timestamp_age_seconds(
                progress_at
            ),
            "job_running": job_running,
            "current_job": current_job or None,
            "job_started_at": job_started_at,
            "job_age_seconds": _timestamp_age_seconds(
                job_started_at
            ),
            "calendar_ready": bool(calendar_ready),
            "calendar_reason": str(calendar_reason or "unknown"),
            "error": str(error or "")[:300],
        }
        _write_json(RUNTIME_PATH, payload)
        return payload


def _runtime_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _timestamp_age_seconds(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(
            value.strip().replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return max(
        0.0,
        (
            datetime.now().astimezone()
            - parsed.astimezone()
        ).total_seconds(),
    )


def _mark_scheduler_progress() -> str:
    global _last_scheduler_progress_at
    with _runtime_lock:
        _last_scheduler_progress_at = _runtime_now()
        return _last_scheduler_progress_at


def refresh_runtime_heartbeat() -> dict[str, Any]:
    with _runtime_lock:
        previous = _read_json(RUNTIME_PATH, {})
        if not isinstance(previous, dict):
            previous = {}
        thread = _scheduler_thread
        thread_alive = bool(
            thread is not None and thread.is_alive()
        )
        return _write_runtime_status(
            ready=bool(previous.get("ready") is True and thread_alive),
            thread_alive=thread_alive,
            calendar_ready=previous.get("calendar_ready") is True,
            calendar_reason=str(
                previous.get("calendar_reason") or "unknown"
            ),
            error=(
                str(previous.get("error") or "")
                if thread_alive
                else "factor scheduler thread exited"
            ),
        )


def _heartbeat_loop(interval_seconds: int = 10) -> None:
    while True:
        runtime = refresh_runtime_heartbeat()
        if runtime.get("thread_alive") is not True:
            return
        time.sleep(max(1, int(interval_seconds)))


def _schedule_time(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be HH:MM")
    text = value.strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise ValueError(f"{field} must be HH:MM") from exc
    if parsed.strftime("%H:%M") != text:
        raise ValueError(f"{field} must be HH:MM")
    return text


def _config_int(
    value: Any,
    field: str,
    *,
    minimum: int = 1,
    maximum: int = 20_000,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} is out of range")
    return int(value)


def _config_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _validated_scheduler_config() -> dict[str, Any]:
    raw = get_config()
    if not isinstance(raw, dict):
        raise ValueError("scheduler config must be an object")
    enabled = raw.get("enabled")
    execute_paper = raw.get("execute_paper")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be boolean")
    if not isinstance(execute_paper, bool):
        raise ValueError("execute_paper must be boolean")

    normalized = dict(raw)
    for field in ("data_refresh_times", "ai_decision_times"):
        values = raw.get(field)
        if not isinstance(values, list):
            raise ValueError(f"{field} must be a list")
        normalized[field] = [
            _schedule_time(value, f"{field}[{index}]")
            for index, value in enumerate(values)
        ]
    normalized["factor_mining_time"] = _schedule_time(
        raw.get("factor_mining_time"),
        "factor_mining_time",
    )
    normalized["short_factor_discovery_time"] = _schedule_time(
        raw.get("short_factor_discovery_time"),
        "short_factor_discovery_time",
    )
    normalized["window_minutes"] = _config_int(
        raw.get("window_minutes"),
        "window_minutes",
        maximum=1_440,
    )
    normalized["data_refresh_window_minutes"] = _config_int(
        raw.get("data_refresh_window_minutes"),
        "data_refresh_window_minutes",
        maximum=1_440,
    )
    normalized["retry_cooldown_minutes"] = _config_int(
        raw.get("retry_cooldown_minutes"),
        "retry_cooldown_minutes",
        maximum=1_440,
    )
    normalized["data_refresh_max_attempts_per_slot"] = _config_int(
        raw.get("data_refresh_max_attempts_per_slot"),
        "data_refresh_max_attempts_per_slot",
        maximum=20,
    )
    normalized["short_factor_max_attempts_per_day"] = _config_int(
        raw.get("short_factor_max_attempts_per_day"),
        "short_factor_max_attempts_per_day",
        maximum=20,
    )
    normalized["short_factor_catch_up_deadline"] = _schedule_time(
        raw.get("short_factor_catch_up_deadline"),
        "short_factor_catch_up_deadline",
    )
    normalized["max_candidates"] = _config_int(
        raw.get("max_candidates"),
        "max_candidates",
    )

    mining = raw.get("mining")
    if not isinstance(mining, dict):
        raise ValueError("mining must be an object")
    normalized_mining = dict(mining)
    for field in (
        "n_stocks",
        "days",
        "train_days",
        "test_days",
        "hold_days",
        "top_k",
        "candidate_pool_size",
    ):
        normalized_mining[field] = _config_int(
            mining.get(field),
            f"mining.{field}",
        )
    for field in ("stop_loss", "take_profit"):
        normalized_mining[field] = _config_float(
            mining.get(field),
            f"mining.{field}",
        )
    if (
        normalized_mining["candidate_pool_size"]
        < normalized_mining["top_k"]
    ):
        raise ValueError(
            "mining.candidate_pool_size must be at least mining.top_k"
        )
    normalized["mining"] = normalized_mining
    return normalized


def get_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    custom = _read_json(ROOT / "auto_scheduler_config.json", {})
    if isinstance(custom, dict):
        cfg.update({k: v for k, v in custom.items() if k != "mining"})
        mining = dict(DEFAULT_CONFIG["mining"])
        if isinstance(custom.get("mining"), dict):
            mining.update(custom["mining"])
        cfg["mining"] = mining
    return cfg


def _base_status() -> dict:
    return {
        "ok": True,
        "enabled": bool(get_config().get("enabled", True)),
        "running": False,
        "updated_at": None,
        "jobs": {},
        "history": [],
    }


def get_status() -> dict:
    status = _read_json(STATUS_PATH, _base_status())
    if not isinstance(status, dict):
        status = _base_status()
    status.setdefault("ok", True)
    status.setdefault("jobs", {})
    status.setdefault("history", [])
    status = _recover_stale_runtime_state(status)
    status["enabled"] = bool(get_config().get("enabled", True))
    status["next_runs"] = next_runs()
    return status


def _save_status(status: dict) -> None:
    status["updated_at"] = _now_text()
    _write_json(STATUS_PATH, status)


def _append_history(status: dict, event: dict) -> None:
    history = status.setdefault("history", [])
    history.insert(0, {"time": _now_text(), **event})
    del history[50:]


def _read_linux_process_stat(
    pid: int,
) -> tuple[str, str] | None:
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
    except Exception:
        return None


def _linux_process_start_token(pid: int) -> str | None:
    process_stat = _read_linux_process_stat(pid)
    if process_stat is None:
        return None
    start_ticks = process_stat[1]
    try:
        boot_id = Path(
            "/proc/sys/kernel/random/boot_id"
        ).read_text(encoding="utf-8").strip()
    except Exception:
        boot_id = ""
    return (
        f"{boot_id}:{start_ticks}"
        if boot_id
        else start_ticks
    )


def _windows_process_start_token(
    pid: int,
    kernel32=None,
) -> str | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid <= 0xFFFFFFFF:
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
            file_time_pointer = ctypes.POINTER(FileTime)
            kernel32.GetProcessTimes.argtypes = [
                ctypes.c_void_p,
                file_time_pointer,
                file_time_pointer,
                file_time_pointer,
                file_time_pointer,
            ]
            kernel32.GetProcessTimes.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int

        handle = kernel32.OpenProcess(
            0x1000,
            False,
            int(pid),
        )
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


def _process_start_token(pid: int | None) -> str | None:
    if not pid or pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        return _linux_process_start_token(pid)
    if os.name == "nt":
        return _windows_process_start_token(pid)
    return None


def _process_start_epoch(pid: int | None) -> float | None:
    if not pid or pid <= 0:
        return None
    token = _process_start_token(pid)
    try:
        raw_value = int(str(token).rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        return None

    if sys.platform.startswith("linux"):
        try:
            clock_ticks = float(os.sysconf("SC_CLK_TCK"))
            if clock_ticks <= 0:
                return None
            boot_time = None
            for line in Path("/proc/stat").read_text(
                encoding="utf-8"
            ).splitlines():
                key, _, value = line.partition(" ")
                if key == "btime":
                    boot_time = float(value.strip())
                    break
            if boot_time is None:
                return None
            return boot_time + raw_value / clock_ticks
        except Exception:
            return None

    if sys.platform.startswith("win"):
        return raw_value / 10_000_000 - 11_644_473_600
    return None


def _normalize_process_start_token(value: Any) -> str | None:
    if value is None:
        return None
    token = str(value).strip()
    return token or None


def _process_tokens_match(
    expected: str | None,
    current: str | None,
) -> bool:
    if expected is None or current is None:
        return False
    if expected == current:
        return True
    return (
        ":" not in expected
        and current.rsplit(":", 1)[-1] == expected
    )


def _structured_lock_identity(
    lock: dict[str, Any] | None,
) -> tuple[int, str | None, str] | None:
    if not isinstance(lock, dict):
        return None
    if "process_start_token" not in lock:
        return None
    try:
        pid = int(lock.get("pid"))
    except (TypeError, ValueError):
        return None
    nonce = lock.get("nonce")
    process_start_token = _normalize_process_start_token(
        lock.get("process_start_token")
    )
    if (
        pid <= 0
        or process_start_token is None
        or not isinstance(nonce, str)
        or not nonce
    ):
        return None
    return (
        pid,
        process_start_token,
        nonce,
    )


def _read_lock_record() -> dict[str, Any] | None:
    try:
        text = LOCK_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return None

    try:
        payload = json.loads(text)
    except Exception:
        payload = None
    identity = _structured_lock_identity(payload)
    if isinstance(payload, dict) and identity is not None:
        return {
            **payload,
            "pid": identity[0],
            "process_start_token": identity[1],
            "nonce": identity[2],
            "_format": "json",
            "_raw": text,
        }

    try:
        first = text.split()[0] if text else ""
        pid = int(first)
    except (TypeError, ValueError, IndexError):
        pid = None
    if pid and pid > 0:
        record = {
            "pid": pid,
            "_format": "legacy",
            "_raw": text,
        }
        created_at = text[len(first) :].strip()
        if created_at:
            record["created_at"] = created_at
        return record
    return {
        "_format": "unknown",
        "_raw": text,
    }


def _same_lock(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> bool:
    left_identity = _structured_lock_identity(left)
    right_identity = _structured_lock_identity(right)
    if left_identity is not None or right_identity is not None:
        return (
            left_identity is not None
            and left_identity == right_identity
        )
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return bool(
        left.get("_format") == right.get("_format")
        and left.get("_raw") == right.get("_raw")
    )


def _lock_pid() -> int | None:
    record = _read_lock_record()
    try:
        return int(record["pid"]) if record else None
    except (KeyError, TypeError, ValueError):
        return None


def _windows_pid_exists(
    pid: int,
    *,
    kernel32=None,
) -> bool | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid <= 0xFFFFFFFF:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        if kernel32 is None:
            kernel32 = ctypes.WinDLL(
                "kernel32",
                use_last_error=True,
            )
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

        handle = kernel32.OpenProcess(0x00100000, False, int(pid))
        if not handle:
            error_code = ctypes.get_last_error()
            if error_code == 87:
                return False
            if error_code == 5:
                return True
            return None
        try:
            wait_result = int(
                kernel32.WaitForSingleObject(handle, 0)
            )
            if wait_result == 0:
                return False
            if wait_result == 258:
                return True
            return None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _linux_pid_exists(pid: int) -> bool | None:
    process_stat = _read_linux_process_stat(pid)
    if process_stat is not None:
        if process_stat[0].upper() in {"Z", "X"}:
            return False
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError):
        return False
    except OSError:
        return None
    return True


def _pid_exists(pid: int | None) -> bool | None:
    if not pid or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_pid_exists(pid)
    if sys.platform.startswith("linux"):
        return _linux_pid_exists(pid)
    return None


def _lock_created_epoch(record: dict[str, Any]) -> float | None:
    created_at = record.get("created_at")
    if isinstance(created_at, str) and created_at.strip():
        try:
            return datetime.fromisoformat(created_at.strip()).timestamp()
        except ValueError:
            pass
    try:
        return LOCK_PATH.stat().st_mtime
    except OSError:
        return None


def _process_started_after_lock(
    pid: int | None,
    record: dict[str, Any],
) -> bool:
    process_started_at = _process_start_epoch(pid)
    lock_created_at = _lock_created_epoch(record)
    if process_started_at is None or lock_created_at is None:
        return False
    return process_started_at > lock_created_at + 1.0


def _lock_is_stale(
    max_age_seconds: int = 7200,
    record: dict[str, Any] | None = None,
) -> bool:
    if record is None:
        record = _read_lock_record()
    if record is None:
        return False
    if record.get("_format") in {"legacy", "unknown"}:
        return False

    pid = record.get("pid")
    pid_exists = _pid_exists(pid)
    if pid_exists is False:
        return True
    if pid_exists is not True:
        return False

    if record.get("_format") == "legacy":
        return _process_started_after_lock(pid, record)
    if _structured_lock_identity(record) is not None:
        expected_token = _normalize_process_start_token(
            record.get("process_start_token")
        )
        current_token = _process_start_token(pid)
        normalized_current = _normalize_process_start_token(current_token)
        if expected_token is not None and normalized_current is not None:
            tokens_match = _process_tokens_match(
                expected_token,
                normalized_current,
            )
            if not tokens_match:
                return True
            if ":" not in expected_token:
                return _process_started_after_lock(pid, record)
            return False
        return _process_started_after_lock(pid, record)

    try:
        return time.time() - LOCK_PATH.stat().st_mtime > max_age_seconds
    except Exception:
        return False


def _unlink_stale_lock_locked(max_age_seconds: int = 7200) -> bool:
    observed = _read_lock_record()
    current_guard_orphan = bool(
        isinstance(observed, dict)
        and observed.get("guard_protocol")
        == SCHEDULER_GUARD_PROTOCOL_VERSION
    )
    if (
        not current_guard_orphan
        and (
            not isinstance(observed, dict)
            or observed.get("_format") != "json"
            or not _lock_is_stale(
                max_age_seconds=max_age_seconds,
                record=observed,
            )
        )
    ):
        return False
    if not _same_lock(observed, _read_lock_record()):
        return False
    try:
        LOCK_PATH.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _unlink_stale_lock(max_age_seconds: int = 7200) -> bool:
    with _scheduler_guard() as acquired:
        if not acquired:
            return False
        return _unlink_stale_lock_locked(
            max_age_seconds=max_age_seconds
        )


def _recover_stale_runtime_state(status: dict) -> dict:
    with _scheduler_guard() as acquired:
        if not acquired:
            return status
        latest = _read_json(STATUS_PATH, None)
        if isinstance(latest, dict):
            status = latest
        stale_lock_removed = _unlink_stale_lock_locked()
        if not status.get("running"):
            return status
        if LOCK_PATH.exists() and not stale_lock_removed:
            return status
        job = str(status.get("current_job") or "unknown")
        slot = str(status.get("current_job_slot") or "").strip() or None
        date_text = (
            str(status.get("current_job_date") or "").strip()
            or str(status.get("current_job_started_at") or "")[:10]
            or _today_text()
        )
        key = _job_key(job, slot, date_text)
        result = {
            "ok": False,
            "error": "stale scheduler lock recovered",
        }
        jobs = status.setdefault("jobs", {})
        jobs[key] = _completed_job_record(
            job=job,
            slot=slot,
            date_text=date_text,
            result=result,
            previous=jobs.get(key),
        )
        status["running"] = False
        status.pop("current_job", None)
        status.pop("current_job_slot", None)
        status.pop("current_job_date", None)
        status.pop("current_job_started_at", None)
        _append_history(
            status,
            {
                "job": job,
                "slot": slot,
                "ok": False,
                "error": result["error"],
            },
        )
        _save_status(status)
        return status


def _lock_file_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return int(stat_result.st_dev), int(stat_result.st_ino)


def _atomic_hard_link(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except FileExistsError:
        raise
    except OSError as exc:
        unsupported_errnos = {
            value
            for value in (
                getattr(errno, "EACCES", None),
                getattr(errno, "EPERM", None),
                getattr(errno, "EXDEV", None),
                getattr(errno, "ENOTSUP", None),
                getattr(errno, "EOPNOTSUPP", None),
            )
            if value is not None
        }
        if (
            exc.errno in unsupported_errnos
            or getattr(exc, "winerror", None) in {1, 50}
        ):
            raise OSError(
                exc.errno,
                "scheduler lock requires atomic hard-link support "
                f"and permission in {destination.parent}",
            ) from exc
        raise


def _probe_lock_capability() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    source = ROOT / f".scheduler-lock-probe-{nonce}.source"
    destination = ROOT / f".scheduler-lock-probe-{nonce}.link"
    try:
        with _scheduler_guard() as acquired:
            if not acquired:
                raise RuntimeError(
                    "scheduler kernel guard is held by another process"
                )
            source.write_bytes(b"probe")
            _atomic_hard_link(source, destination)
            if _lock_file_identity(source) != _lock_file_identity(
                destination
            ):
                raise OSError(
                    "scheduler hard-link probe identity mismatch"
                )
    finally:
        for path in (destination, source):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _publish_lock_metadata(
    owner: dict[str, Any],
) -> tuple[int, int]:
    nonce = str(owner["nonce"])
    prepared = LOCK_PATH.with_name(
        f".{LOCK_PATH.name}.{nonce}.owner"
    )
    staging = prepared.with_suffix(prepared.suffix + ".tmp")
    try:
        _write_json(prepared, owner)
        identity = _lock_file_identity(prepared)
        if identity is None:
            raise OSError("scheduler lock metadata is unavailable")
        _atomic_hard_link(prepared, LOCK_PATH)
        return identity
    finally:
        for path in (prepared, staging):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _unlink_owned_lock(
    identity: tuple[int, int] | None,
    owner: dict[str, Any],
) -> bool:
    if identity is None or _lock_file_identity(LOCK_PATH) != identity:
        return False
    current = _read_lock_record()
    if current is not None and not _same_lock(owner, current):
        return False
    try:
        LOCK_PATH.unlink(missing_ok=True)
        return True
    except OSError:
        return False


@contextmanager
def _file_lock(max_age_seconds: int = 7200):
    with _scheduler_guard() as acquired:
        if not acquired:
            yield False
            return
        _unlink_stale_lock_locked(
            max_age_seconds=max_age_seconds
        )
        if LOCK_PATH.exists():
            yield False
            return
        pid = os.getpid()
        process_start_token = _process_start_token(pid)
        if process_start_token is None:
            raise RuntimeError(
                "scheduler process start identity is unavailable"
            )
        owner = {
            "pid": pid,
            "process_start_token": process_start_token,
            "nonce": uuid.uuid4().hex,
            "created_at": _now_text(),
            "guard_protocol": SCHEDULER_GUARD_PROTOCOL_VERSION,
        }
        try:
            owner_identity = _publish_lock_metadata(owner)
        except FileExistsError:
            yield False
            return
        try:
            if not _same_lock(owner, _read_lock_record()):
                yield False
                return
            yield True
        finally:
            _unlink_owned_lock(owner_identity, owner)


def _root_admin_uid() -> int | None:
    admins = um.list_admin_users().get("users", [])
    for user in admins:
        if um.is_root_admin_user(user):
            try:
                return int(user["id"])
            except Exception:
                return None
    return None


def _job_key(job: str, slot: str | None = None, date_text: str | None = None) -> str:
    bits = [date_text or _today_text(), job]
    if slot:
        bits.append(slot.replace(":", ""))
    return ":".join(bits)


def _parse_time(date_text: str, hhmm: str) -> datetime | None:
    try:
        return datetime.strptime(f"{date_text} {hhmm}", "%Y-%m-%d %H:%M")
    except Exception:
        return None


def _in_window(now: datetime, hhmm: str, minutes: int) -> bool:
    start = _parse_time(now.strftime("%Y-%m-%d"), hhmm)
    return bool(start and start <= now <= start + timedelta(minutes=max(1, minutes)))


def _in_catch_up_window(
    now: datetime,
    hhmm: str,
    deadline_hhmm: str,
) -> bool:
    date_text = now.strftime("%Y-%m-%d")
    start = _parse_time(date_text, hhmm)
    deadline = _parse_time(date_text, deadline_hhmm)
    return bool(start and deadline and start <= now <= deadline)


def _is_weekday(now: datetime | None = None) -> bool:
    from market import trading_calendar

    current = now or datetime.now()
    calendar_ready, _reason = _calendar_readiness(current)
    return bool(
        calendar_ready
        and trading_calendar.is_trading_day(current.date())
    )


def next_runs(now: datetime | None = None) -> dict:
    from market import trading_calendar

    cfg = get_config()
    now = now or datetime.now()
    dates = [(now + timedelta(days=offset)).date() for offset in range(14)]
    out: dict[str, str | None] = {
        "data_refresh": None,
        "factor_mining": None,
        "short_factor_discovery": None,
        "ai_decision": None,
    }
    schedule = {
        "data_refresh": cfg.get("data_refresh_times") or [],
        "factor_mining": [cfg.get("factor_mining_time") or DEFAULT_CONFIG["factor_mining_time"]],
        "short_factor_discovery": [
            cfg.get("short_factor_discovery_time")
            or DEFAULT_CONFIG["short_factor_discovery_time"]
        ],
        "ai_decision": cfg.get("ai_decision_times") or [],
    }
    for name, times in schedule.items():
        for d in dates:
            try:
                calendar_ready = (
                    trading_calendar.has_verified_calendar(d.year)
                )
            except Exception:
                calendar_ready = False
            if not calendar_ready:
                continue
            if not trading_calendar.is_trading_day(d):
                continue
            for hhmm in times:
                dt = _parse_time(d.strftime("%Y-%m-%d"), str(hhmm))
                if dt and dt >= now:
                    out[name] = dt.strftime("%Y-%m-%d %H:%M")
                    break
            if out[name]:
                break
    return out


def _run_data_refresh(date_text: str) -> dict:
    details: dict[str, Any] = {"date": date_text}
    factor_cache_ok = False
    try:
        from analytics import data_manager as dm

        names = dm.refresh_name_cache()
        details["name_cache_count"] = len(names or {})
        mining_cfg = get_config().get("mining", {})
        target_stocks = int(mining_cfg.get("n_stocks", 800))
        candidate_stocks = relay_factor_mining.research_candidate_pool_size(
            target_stocks
        )
        days = int(mining_cfg.get("days", 320))
        cache_freshness = dm.factor_cache_freshness(
            min_required=candidate_stocks,
        )
        details["factor_cache_freshness"] = cache_freshness

        if cache_freshness.get("security_master_fresh"):
            details["security_master"] = {
                "ok": True,
                "already_current": True,
                "security_master_trade_date": cache_freshness.get(
                    "security_master_trade_date"
                ),
                "expected_trade_date": cache_freshness.get(
                    "expected_trade_date"
                ),
            }
        else:
            details["security_master"] = (
                dm.refresh_factor_security_master_history(
                    days=days,
                    n_stocks=candidate_stocks,
                    defer_latest_active_bar=True,
                )
            )

        security_master_ok = bool(details["security_master"].get("ok"))
        if security_master_ok:
            details["factor_cache"] = dm.refresh_factor_daily_cache(
                days=days,
                min_required=candidate_stocks,
                refresh_security_master=False,
            )
        else:
            details["factor_cache"] = {
                "ok": False,
                "error": "point-in-time security master refresh failed",
            }
        factor_cache_ok = security_master_ok and bool(
            details["factor_cache"].get("ok")
        )
    except Exception as exc:
        details["name_cache_error"] = str(exc)[:180]
        details.setdefault(
            "factor_cache",
            {"ok": False, "error": str(exc)[:180]},
        )

    if factor_cache_ok:
        try:
            from analytics import st_status_history
            from analytics.factors_zoo import scorer

            factor_codes = scorer.factor_universe_codes_from_cache(
                n_stocks=candidate_stocks,
                days=days,
                min_history=min(days, 60),
                require_latest=True,
                require_security_master=True,
            )
            details["st_status"] = st_status_history.refresh_cache(factor_codes)
        except Exception as exc:
            details["st_status"] = {"ok": False, "error": str(exc)[:180]}

    try:
        from market import market_stats

        details["breadth"] = market_stats.get_market_breadth(date_text=date_text)
        details["turnover"] = market_stats.get_market_turnover(date_text=date_text)
    except Exception as exc:
        details["market_stats_error"] = str(exc)[:180]

    try:
        from analytics import lhb_engine

        lhb = lhb_engine.get_lhb_data(date_text, force_refresh=True)
        details["lhb"] = {
            "ok": bool(lhb.get("ok")),
            "total": lhb.get("total", 0),
            "status": lhb.get("status"),
            "error": lhb.get("error"),
        }
    except Exception as exc:
        details["lhb_error"] = str(exc)[:180]
    return {"ok": factor_cache_ok, "details": details}


def _factor_experiment_store() -> relay_factor_experiment.RelayFactorExperimentStore:
    return relay_factor_experiment.RelayFactorExperimentStore(
        ROOT / "prospective.sqlite3"
    )


def _reconcile_missed_fixed_sessions(
    store: relay_factor_experiment.RelayFactorExperimentStore,
    experiment: dict[str, Any],
    through_date: str,
) -> dict[str, Any]:
    experiment_id = str(experiment["experiment_id"])
    expected_dates = store.expected_session_dates(experiment_id)
    existing_dates = {
        str(item.get("signal_date") or "")
        for item in store.list_sessions(experiment_id)
    }
    missing_overdue = [
        signal_date
        for signal_date in expected_dates
        if signal_date < through_date and signal_date not in existing_dates
    ]
    recorded_dates = []
    for signal_date in missing_overdue:
        store.record_verified_failure_session(
            experiment_id,
            signal_date,
            reason="scheduler_missed_fixed_window_session",
            details={
                "reconciled_on": through_date,
            },
        )
        recorded_dates.append(signal_date)
    return {
        "expected_session_dates": expected_dates,
        "missing_overdue_dates": missing_overdue,
        "recorded_dates": recorded_dates,
        "evaluation": store.evaluate_once(experiment_id),
    }


def _record_failed_prospective_session(
    date_text: str,
    *,
    reason: str,
    details: Any = None,
) -> dict[str, Any]:
    from analytics import data_manager as dm

    requested_at = datetime.strptime(date_text, "%Y-%m-%d").replace(
        hour=17,
        minute=10,
    )
    expected_trade_date = dm.expected_completed_trade_date(requested_at)
    if expected_trade_date != date_text:
        return {
            "recorded": False,
            "skipped": True,
            "reason": "not_a_completed_trading_session",
            "expected_trade_date": expected_trade_date,
        }
    store = _factor_experiment_store()
    experiment = store.get_active_experiment()
    if experiment is None:
        return {
            "recorded": False,
            "skipped": True,
            "reason": "no_active_locked_experiment",
        }
    experiment_id = str(experiment["experiment_id"])
    reconciliation = _reconcile_missed_fixed_sessions(
        store,
        experiment,
        date_text,
    )
    current = store.get_experiment(experiment_id)
    if current["status"] != "locked_shadow":
        return {
            "recorded": False,
            "skipped": True,
            "reason": f"experiment_{current['status']}",
            "reconciliation": reconciliation,
            "evaluation": reconciliation["evaluation"],
        }
    if date_text not in reconciliation["expected_session_dates"]:
        return {
            "recorded": False,
            "skipped": True,
            "reason": "outside_fixed_prospective_window",
            "reconciliation": reconciliation,
            "evaluation": reconciliation["evaluation"],
        }
    existing = next(
        (
            item
            for item in store.list_sessions(experiment_id)
            if item.get("signal_date") == date_text
            and item.get("candidates_recorded")
        ),
        None,
    )
    if existing is not None:
        evaluation = store.evaluate_once(experiment_id)
        return {
            "recorded": True,
            "idempotent": True,
            "preserved_existing": True,
            "signal_date": date_text,
            "data_complete": bool(existing.get("data_complete")),
            "data_trusted": bool(existing.get("data_trusted")),
            "reconciliation": reconciliation,
            "evaluation": evaluation,
        }
    try:
        frozen = store.record_verified_failure_session(
            experiment_id,
            date_text,
            reason=str(reason),
            details=details,
        )
    except ValueError as exc:
        if "after locked_at" in str(exc):
            return {
                "recorded": False,
                "skipped": True,
                "reason": "waiting_for_first_post_lock_session",
            }
        raise
    return {
        **frozen,
        "failure_reason": str(reason),
        "reconciliation": reconciliation,
        "evaluation": store.evaluate_once(experiment_id),
    }


def _settle_pending_shadow_candidates(
    store: relay_factor_experiment.RelayFactorExperimentStore,
    experiment: dict[str, Any],
    panel: dict[str, Any],
) -> dict[str, Any]:
    return store.settle_verified_candidates(
        str(experiment["experiment_id"]),
        panel=panel,
    )


def _record_locked_shadow_session(
    store: relay_factor_experiment.RelayFactorExperimentStore,
    experiment: dict[str, Any],
    date_text: str,
    panel: dict[str, Any],
) -> dict[str, Any]:
    experiment_id = str(experiment["experiment_id"])
    if date_text not in store.expected_session_dates(experiment_id):
        return {
            "recorded": False,
            "skipped": True,
            "signal_date": date_text,
            "reason": "outside_fixed_prospective_window",
        }
    existing = next(
        (
            item
            for item in store.list_sessions(experiment_id)
            if item.get("signal_date") == date_text
        ),
        None,
    )
    if existing and existing.get("candidates_recorded"):
        return {
            "recorded": True,
            "idempotent": True,
            "signal_date": date_text,
        }

    try:
        session_result = store.record_verified_shadow_session(
            experiment_id,
            date_text,
            panel=panel,
        )
    except ValueError as exc:
        if "after locked_at" in str(exc):
            return {
                "recorded": False,
                "idempotent": False,
                "signal_date": date_text,
                "reason": "waiting_for_first_post_lock_session",
            }
        raise
    immediate = store.settle_verified_candidates(
        experiment_id,
        panel=panel,
    )
    return {
        **session_result,
        "immediate_settlement": immediate,
    }


def _validated_production_candidates(
    experiment: dict[str, Any],
    panel: dict[str, Any],
) -> dict[str, Any]:
    from core import factor_watchlist

    snapshot = relay_factor_mining.build_frozen_shadow_snapshot(
        panel,
        experiment["manifest"],
    )
    if not snapshot.get("ok") or snapshot.get("data_status") != "ok":
        result = {
            "ok": False,
            "skipped": True,
            "reason": snapshot.get("reason") or "validated_snapshot_not_trusted",
        }
        signal_date = str(snapshot.get("signal_date") or "")[:10]
        if signal_date:
            model = experiment["manifest"].get("model") or {}
            scope_result = factor_watchlist.record_factor_watchlist_scope(
                trade_date=signal_date,
                source_run_id=experiment["source_run_id"],
                strategy_key=experiment["manifest"].get("strategy_key"),
                model_version=model.get("model_version"),
                experiment_id=experiment["experiment_id"],
                model_instance_id=experiment["model_instance_id"],
                candidate_count=0,
            )
            result.update(
                {
                    "imported": 0,
                    "latest_scope": scope_result["latest_scope"],
                }
            )
        return result
    top_k = max(
        1,
        int((experiment["manifest"].get("config") or {}).get("top_k") or 1),
    )
    candidates = []
    for candidate in snapshot.get("candidates") or []:
        rank = int(candidate.get("candidate_rank") or 0)
        position_pct = float(
            candidate.get("shadow_target_position_pct") or 0.0
        )
        if not (0 < rank <= top_k and position_pct > 0.0):
            continue
        row = dict(candidate)
        row.update(
            {
                "experiment_id": experiment["experiment_id"],
                "model_instance_id": experiment["model_instance_id"],
                "source_run_id": experiment["source_run_id"],
                "validation_accepted": True,
                "research_status": "validated",
                "deployment_eligible": True,
                "research_only": False,
                "position_pct": position_pct,
            }
        )
        factor_explain = row.get("factor_explain")
        if isinstance(factor_explain, dict):
            factor_explain.setdefault("trading_plan", {})[
                "position_pct"
            ] = position_pct
        candidates.append(row)
    if not candidates:
        model = experiment["manifest"].get("model") or {}
        scope_result = factor_watchlist.record_factor_watchlist_scope(
            trade_date=snapshot.get("signal_date"),
            source_run_id=experiment["source_run_id"],
            strategy_key=experiment["manifest"].get("strategy_key"),
            model_version=model.get("model_version"),
            experiment_id=experiment["experiment_id"],
            model_instance_id=experiment["model_instance_id"],
            candidate_count=0,
        )
        return {
            "ok": True,
            "skipped": True,
            "reason": "validated_model_has_no_executable_candidates",
            "imported": 0,
            "latest_scope": scope_result["latest_scope"],
        }
    return factor_watchlist.import_factor_candidates_to_watchlist(
        candidates,
        run_id=experiment["source_run_id"],
        strategy_name="冻结前瞻验证因子候选",
    )


def _run_prospective_cycle(
    date_text: str,
    *,
    panel: dict[str, Any],
) -> dict[str, Any]:
    store = _factor_experiment_store()
    result: dict[str, Any] = {"ok": True}
    active = store.get_active_experiment()
    if active is not None:
        result["settlement"] = _settle_pending_shadow_candidates(
            store,
            active,
            panel,
        )
        result["reconciliation"] = _reconcile_missed_fixed_sessions(
            store,
            active,
            date_text,
        )
        result["evaluation_before_signal"] = result["reconciliation"][
            "evaluation"
        ]
        current = store.get_experiment(active["experiment_id"])
        if current["status"] == "locked_shadow":
            result["shadow"] = _record_locked_shadow_session(
                store,
                current,
                date_text,
                panel,
            )
            result["evaluation_after_signal"] = store.evaluate_once(
                current["experiment_id"]
            )
        else:
            result["shadow"] = {
                "recorded": False,
                "skipped": True,
                "reason": f"experiment_{current['status']}",
            }
    else:
        result["shadow"] = {
            "recorded": False,
            "skipped": True,
            "reason": "no_active_locked_experiment",
        }

    validated = store.get_latest_validated_experiment()
    if validated is None:
        result["production"] = {
            "ok": True,
            "skipped": True,
            "reason": "no_validated_experiment",
        }
    else:
        result["production"] = _validated_production_candidates(
            validated,
            panel,
        )
    result["ok"] = bool(result["production"].get("ok"))
    return result


def _run_factor_mining(date_text: str) -> dict:
    # 直连领域模块，不再反向 import web 层（A-2）
    from analytics import data_manager as dm
    from analytics.factors_zoo import scorer

    cfg = get_config().get("mining", {})
    target_stocks = int(cfg.get("n_stocks", 800))
    candidate_stocks = relay_factor_mining.research_candidate_pool_size(
        target_stocks
    )
    run_id = datetime.now().strftime("auto-%Y%m%d-%H%M%S")
    out_dir = ROOT / run_id
    requested_at = datetime.strptime(date_text, "%Y-%m-%d").replace(hour=17, minute=10)
    expected_trade_date = dm.expected_completed_trade_date(requested_at)
    panel, _factor_codes = scorer.build_panel_from_cache(
        n_stocks=candidate_stocks,
        days=int(cfg.get("days", 320)),
        min_history=min(int(cfg.get("days", 320)), 60),
        require_latest=True,
        point_in_time=True,
        require_security_master=True,
    )
    close_frame = panel.get("close") if isinstance(panel, dict) else None
    latest_index = getattr(close_frame, "index", [])
    latest_value = max(latest_index) if len(latest_index) else None
    latest_trade_date = (
        latest_value.strftime("%Y-%m-%d")
        if hasattr(latest_value, "strftime")
        else str(latest_value or "")[:10]
    )
    if latest_trade_date != expected_trade_date:
        prospective = _record_failed_prospective_session(
            date_text,
            reason="factor_cache_stale",
            details={
                "expected_trade_date": expected_trade_date,
                "latest_trade_date": latest_trade_date or None,
            },
        )
        return {
            "ok": False,
            "data_status": "stale",
            "error": (
                f"factor cache is stale: expected {expected_trade_date}, "
                f"got {latest_trade_date or 'none'}"
            ),
            "run_id": run_id,
            "expected_trade_date": expected_trade_date,
            "latest_trade_date": latest_trade_date or None,
            "candidate_count": 0,
            "prospective": prospective,
        }
    try:
        prospective = _run_prospective_cycle(
            date_text,
            panel=panel,
        )
    except Exception as exc:
        logger.exception("prospective factor cycle failed")
        prospective = {"ok": False, "error": str(exc)[:240]}
    try:
        feedback_user_id = _root_admin_uid()
    except Exception as exc:
        logger.warning("factor mining feedback identity unavailable: %s", exc)
        feedback_user_id = None
    report = relay_factor_mining.run_relay_research(
        out_dir=out_dir,
        panel=panel,
        n_stocks=target_stocks,
        days=int(cfg.get("days", 320)),
        train_days=int(cfg.get("train_days", 60)),
        test_days=int(cfg.get("test_days", 10)),
        hold_days=int(cfg.get("hold_days", 2)),
        stop_loss=float(cfg.get("stop_loss", -0.035)),
        take_profit=float(cfg.get("take_profit", 0.07)),
        top_k=int(cfg.get("top_k", 10)),
        candidate_pool_size=int(cfg.get("candidate_pool_size", 100)),
        include_market_signals=True,
        feedback_user_id=feedback_user_id,
        load_live_feedback=feedback_user_id is not None,
    )
    candidates = report.get("latest_candidates", []) if isinstance(report, dict) else []
    data_status = (
        str(report.get("data_status") or report.get("status") or "degraded")
        if isinstance(report, dict)
        else "degraded"
    )
    research_status = (
        str(report.get("research_status") or "failed")
        if isinstance(report, dict)
        else "failed"
    )
    research_assessment = (
        report.get("research_assessment") or {}
        if isinstance(report, dict)
        else {}
    )
    deployment_eligible = bool(research_assessment.get("deployment_eligible"))
    if data_status != "ok":
        watchlist = {
            "ok": False,
            "skipped": True,
            "reason": f"data_status_{data_status}",
        }
    else:
        watchlist = {
            "ok": False,
            "skipped": True,
            "reason": "exploratory_research_not_deployable",
        }
    return {
        "ok": data_status == "ok" and bool(prospective.get("ok")),
        "run_id": run_id,
        "status": report.get("status") if isinstance(report, dict) else None,
        "data_status": data_status,
        "research_status": research_status,
        "deployment_eligible": deployment_eligible,
        "reason": report.get("reason") if isinstance(report, dict) else "",
        "latest_trade_date": ((report.get("summary") or {}).get("latest_trade_date") if isinstance(report, dict) else None),
        "candidate_count": len(candidates),
        "watchlist": watchlist,
        "prospective": prospective,
    }


def _run_short_factor_discovery(_date_text: str) -> dict:
    result = short_factor_job.run_discovery_job()
    if not isinstance(result, dict):
        return {
            "ok": False,
            "error": "short factor discovery returned invalid result",
        }
    return result


def _run_ai_decision(date_text: str) -> dict:
    from core import factor_watchlist as admin_api

    uid = _root_admin_uid()
    if not uid:
        return {"ok": False, "error": "root admin user not found"}
    cfg = get_config()
    max_candidates = int(cfg.get("max_candidates", 6) or 6)
    candidates = admin_api.factor_watchlist_candidates(max_candidates=max_candidates, latest_only=True)
    if not candidates:
        return {"ok": False, "error": "no factor watchlist candidates"}
    result = ai_trader_engine.get_engine(uid).decide_watchlist_candidates(
        candidates,
        max_candidates=max_candidates,
        execute_paper=bool(cfg.get("execute_paper", False)),
    )
    if not result.get("ok"):
        return result
    payload = {**result, "candidate_source": "factor_watchlist", "latest_only": True, "auto": True}
    admin_api.save_ai_trade_decision(payload)
    admin_api.apply_ai_trade_decision_to_watchlist(payload)
    return {
        "ok": True,
        "selected_count": result.get("selected_count", 0),
        "buy_count": sum(1 for x in result.get("results", []) if str(x.get("action") or "").upper() == "BUY"),
        "risk_ok_count": sum(1 for x in result.get("results", []) if x.get("risk_ok")),
        "token_usage": result.get("token_usage"),
        "broker_mode": result.get("broker_mode"),
        "execute_paper": result.get("execute_paper"),
    }


def _recorded_attempt_count(record: Any) -> int:
    if not isinstance(record, dict):
        return 0
    value = record.get("attempt_count")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return int(value)
    return 1


def _job_attempt_limit(
    job: str,
    cfg: Mapping[str, Any] | None = None,
) -> int | None:
    resolved = cfg if isinstance(cfg, Mapping) else get_config()
    if job == "data_refresh":
        field = "data_refresh_max_attempts_per_slot"
    elif job == "short_factor_discovery":
        field = "short_factor_max_attempts_per_day"
    else:
        return None
    try:
        value = int(resolved.get(field, DEFAULT_CONFIG[field]))
    except (TypeError, ValueError):
        value = int(DEFAULT_CONFIG[field])
    return max(1, value)


def _parse_job_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed


def _completed_job_record(
    *,
    job: str,
    slot: str | None,
    date_text: str,
    result: Mapping[str, Any],
    previous: Any = None,
    cfg: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    finished_at = _now_text()
    attempt_count = _recorded_attempt_count(previous) + 1
    ok = bool(result.get("ok"))
    limit = _job_attempt_limit(job, cfg)
    retry_cooldown = int(
        (cfg or get_config()).get(
            "retry_cooldown_minutes",
            DEFAULT_CONFIG["retry_cooldown_minutes"],
        )
        or DEFAULT_CONFIG["retry_cooldown_minutes"]
    )
    next_retry_at: str | None = None
    if ok:
        blocked_by = "already_succeeded"
    elif limit is not None and attempt_count >= limit:
        blocked_by = "max_attempts"
    else:
        blocked_by = "retry_cooldown"
        finished = _parse_job_timestamp(finished_at)
        if finished is not None:
            next_retry_at = (
                finished
                + timedelta(minutes=max(1, retry_cooldown))
            ).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "job": job,
        "slot": slot,
        "date": date_text,
        "ok": ok,
        "attempt_count": attempt_count,
        "last_attempt_at": finished_at,
        "next_retry_at": next_retry_at,
        "blocked_by": blocked_by,
        "finished_at": finished_at,
        "result": dict(result),
    }


def _alert_exhausted_short_factor(record: Mapping[str, Any]) -> None:
    if (
        record.get("job") != "short_factor_discovery"
        or record.get("ok") is True
        or record.get("blocked_by") != "max_attempts"
    ):
        return
    try:
        from core import notification_manager

        result = record.get("result")
        error = (
            str(result.get("error") or "unknown error")[:300]
            if isinstance(result, Mapping)
            else "unknown error"
        )
        notification_manager.send_notification(
            "见微 Alpha 短因子重试耗尽",
            "job=short_factor_discovery\n"
            f"date={record.get('date')}\n"
            f"slot={record.get('slot')}\n"
            f"attempt_count={record.get('attempt_count')}\n"
            f"error={error}",
        )
    except Exception as exc:
        logger.warning(
            "[auto_factor_scheduler] exhausted-attempt alert failed: %s",
            exc,
        )


def run_job(job: str, slot: str | None = None, force: bool = False) -> dict:
    job = str(job or "").strip()
    if job not in {
        "data_refresh",
        "factor_mining",
        "short_factor_discovery",
        "ai_decision",
        "full_after_close",
    }:
        return {"ok": False, "error": "unknown auto scheduler job"}
    if not force and not get_config().get("enabled", True):
        return {"ok": False, "error": "auto scheduler disabled"}
    with _thread_lock:
        date_text = _today_text()
        key = _job_key(job, slot, date_text)
        try:
            with _file_lock() as acquired:
                if not acquired:
                    return {
                        "ok": False,
                        "error": "auto scheduler is already running",
                    }
                status = get_status()
                status["running"] = True
                status["current_job"] = job
                status["current_job_slot"] = slot
                status["current_job_date"] = date_text
                status["current_job_started_at"] = _runtime_now()
                _save_status(status)
                try:
                    if job == "data_refresh":
                        result = _run_data_refresh(date_text)
                    elif job == "factor_mining":
                        result = _run_factor_mining(date_text)
                    elif job == "short_factor_discovery":
                        result = _run_short_factor_discovery(date_text)
                    elif job == "ai_decision":
                        result = _run_ai_decision(date_text)
                    else:
                        refresh_result = _run_data_refresh(date_text)
                        result = {
                            "ok": False,
                            "steps": {"data_refresh": refresh_result},
                        }
                        if refresh_result.get("ok"):
                            mining_result = _run_factor_mining(date_text)
                            result["steps"]["factor_mining"] = mining_result
                            result["ok"] = bool(mining_result.get("ok"))
                        else:
                            result["steps"]["factor_mining"] = {
                                "ok": False,
                                "skipped": True,
                                "reason": "data_refresh_failed",
                                "prospective": _record_failed_prospective_session(
                                    date_text,
                                    reason="data_refresh_failed",
                                    details=refresh_result.get("details"),
                                ),
                            }
                except Exception as exc:
                    logger.exception(
                        "[auto_factor_scheduler] job %s failed",
                        job,
                    )
                    result = {
                        "ok": False,
                        "error": str(exc)[:300],
                    }
                status = get_status()
                status["running"] = False
                status.pop("current_job", None)
                status.pop("current_job_slot", None)
                status.pop("current_job_date", None)
                status.pop("current_job_started_at", None)
                jobs = status.setdefault("jobs", {})
                record = _completed_job_record(
                    job=job,
                    slot=slot,
                    date_text=date_text,
                    result=result,
                    previous=jobs.get(key),
                )
                jobs[key] = record
                _append_history(status, {"job": job, "slot": slot, "ok": bool(result.get("ok")), "result": result})
                _save_status(status)
                _alert_exhausted_short_factor(record)
                return result
        except Exception as exc:
            logger.exception("[auto_factor_scheduler] job %s failed", job)
            result = {"ok": False, "error": str(exc)[:300]}
            status = get_status()
            status["running"] = False
            status.pop("current_job", None)
            status.pop("current_job_slot", None)
            status.pop("current_job_date", None)
            status.pop("current_job_started_at", None)
            jobs = status.setdefault("jobs", {})
            record = _completed_job_record(
                job=job,
                slot=slot,
                date_text=date_text,
                result=result,
                previous=jobs.get(key),
            )
            jobs[key] = record
            _append_history(status, {"job": job, "slot": slot, "ok": False, "error": result["error"]})
            _save_status(status)
            _alert_exhausted_short_factor(record)
            return result


def _already_ran(status: dict, job: str, slot: str, date_text: str) -> bool:
    rec = status.get("jobs", {}).get(_job_key(job, slot, date_text))
    return bool(rec and rec.get("ok"))


def _attempt_blocked(
    status: dict,
    job: str,
    slot: str,
    date_text: str,
    now: datetime | None = None,
    retry_cooldown_minutes: int = 10,
    max_attempts: int | None = None,
) -> bool:
    rec = status.get("jobs", {}).get(_job_key(job, slot, date_text))
    if not rec:
        return False
    if rec.get("ok"):
        return True
    if (
        max_attempts is not None
        and _recorded_attempt_count(rec) >= max(1, int(max_attempts))
    ):
        return True
    retry_at = _parse_job_timestamp(rec.get("next_retry_at"))
    if retry_at is None:
        finished_at = _parse_job_timestamp(rec.get("finished_at"))
        if finished_at is None:
            return False
        retry_at = finished_at + timedelta(
            minutes=max(1, int(retry_cooldown_minutes))
        )
    return (now or datetime.now()) < retry_at


def _run_scheduler_cycle(
    now: datetime,
    cfg: Mapping[str, Any],
) -> list[dict[str, Any]]:
    status = get_status()
    date_text = now.strftime("%Y-%m-%d")
    window = int(cfg.get("window_minutes", 90) or 90)
    data_window = int(
        cfg.get(
            "data_refresh_window_minutes",
            DEFAULT_CONFIG["data_refresh_window_minutes"],
        )
        or DEFAULT_CONFIG["data_refresh_window_minutes"]
    )
    retry_cooldown = int(
        cfg.get("retry_cooldown_minutes", 10) or 10
    )
    executed: list[dict[str, Any]] = []

    def run_due(
        job: str,
        slot: str,
        *,
        due: bool,
        max_attempts: int | None = None,
    ) -> None:
        nonlocal status
        if not due or _attempt_blocked(
            status,
            job,
            slot,
            date_text,
            now=now,
            retry_cooldown_minutes=retry_cooldown,
            max_attempts=max_attempts,
        ):
            return
        _mark_scheduler_progress()
        result = run_job(job, slot=slot)
        executed.append({"job": job, "slot": slot, "result": result})
        _mark_scheduler_progress()
        status = get_status()

    short_slot = str(
        cfg.get("short_factor_discovery_time")
        or DEFAULT_CONFIG["short_factor_discovery_time"]
    )
    catch_up_deadline = str(
        cfg.get("short_factor_catch_up_deadline")
        or DEFAULT_CONFIG["short_factor_catch_up_deadline"]
    )
    run_due(
        "short_factor_discovery",
        short_slot,
        due=_in_catch_up_window(
            now,
            short_slot,
            catch_up_deadline,
        ),
        max_attempts=int(
            cfg.get(
                "short_factor_max_attempts_per_day",
                DEFAULT_CONFIG["short_factor_max_attempts_per_day"],
            )
            or DEFAULT_CONFIG["short_factor_max_attempts_per_day"]
        ),
    )

    factor_slot = str(
        cfg.get("factor_mining_time")
        or DEFAULT_CONFIG["factor_mining_time"]
    )
    run_due(
        "factor_mining",
        factor_slot,
        due=_in_window(now, factor_slot, window),
    )

    data_max_attempts = int(
        cfg.get(
            "data_refresh_max_attempts_per_slot",
            DEFAULT_CONFIG["data_refresh_max_attempts_per_slot"],
        )
        or DEFAULT_CONFIG["data_refresh_max_attempts_per_slot"]
    )
    for value in cfg.get("data_refresh_times") or []:
        data_slot = str(value)
        run_due(
            "data_refresh",
            data_slot,
            due=_in_window(now, data_slot, data_window),
            max_attempts=data_max_attempts,
        )

    for value in cfg.get("ai_decision_times") or []:
        ai_slot = str(value)
        run_due(
            "ai_decision",
            ai_slot,
            due=_in_window(now, ai_slot, 20),
        )
    return executed


def run_scheduler_loop(interval_seconds: int = 30) -> None:
    logger.info("[auto_factor_scheduler] started")
    first_cycle = True
    while True:
        _mark_scheduler_progress()
        try:
            cfg = _validated_scheduler_config()
            now = datetime.now()
            calendar_ready, calendar_reason = (
                _calendar_readiness(now)
            )
            _write_runtime_status(
                ready=calendar_ready,
                thread_alive=True,
                calendar_ready=calendar_ready,
                calendar_reason=calendar_reason,
            )
            if first_cycle:
                _scheduler_ready_event.set()
                first_cycle = False
            if (
                cfg.get("enabled", True)
                and calendar_ready
                and _is_weekday(now)
            ):
                _run_scheduler_cycle(now, cfg)
        except Exception as exc:
            logger.warning(f"[auto_factor_scheduler] loop error: {exc}")
            if first_cycle:
                try:
                    _write_runtime_status(
                        ready=False,
                        thread_alive=False,
                        calendar_ready=False,
                        calendar_reason="scheduler_startup_error",
                        error=str(exc),
                    )
                finally:
                    _scheduler_ready_event.set()
                return
            _write_runtime_status(
                ready=False,
                thread_alive=True,
                calendar_ready=False,
                calendar_reason="scheduler_loop_error",
                error=str(exc),
            )
            if not _scheduler_ready_event.is_set():
                _scheduler_ready_event.set()
        _mark_scheduler_progress()
        time.sleep(max(5, int(interval_seconds)))


def start_background_scheduler() -> bool:
    global _started, _scheduler_thread, _heartbeat_thread
    global _last_scheduler_progress_at
    with _thread_lock:
        if _started:
            if (
                _scheduler_thread is not None
                and _scheduler_thread.is_alive()
            ):
                return False
            _started = False
        _probe_lock_capability()
        calendar_ready, calendar_reason = (
            _ensure_calendar_readiness()
        )
        if not calendar_ready:
            raise RuntimeError(calendar_reason)
        _scheduler_ready_event.clear()
        _last_scheduler_progress_at = None
        thread = threading.Thread(
            target=run_scheduler_loop,
            daemon=True,
            name="auto-factor-scheduler",
        )
        _scheduler_thread = thread
        thread.start()
        if not _scheduler_ready_event.wait(timeout=5):
            _scheduler_thread = None
            raise RuntimeError(
                "auto factor scheduler readiness timed out"
            )
        runtime = _read_json(RUNTIME_PATH, {})
        if (
            not thread.is_alive()
            or not isinstance(runtime, dict)
            or runtime.get("ready") is not True
            or runtime.get("pid") != os.getpid()
            or not _process_tokens_match(
                _normalize_process_start_token(
                    runtime.get("process_start_token")
                ),
                _normalize_process_start_token(
                    _process_start_token(os.getpid())
                ),
            )
        ):
            _scheduler_thread = None
            raise RuntimeError(
                "auto factor scheduler failed readiness check"
            )
        _started = True
        heartbeat = threading.Thread(
            target=_heartbeat_loop,
            daemon=True,
            name="auto-factor-heartbeat",
        )
        _heartbeat_thread = heartbeat
        heartbeat.start()
        return True
