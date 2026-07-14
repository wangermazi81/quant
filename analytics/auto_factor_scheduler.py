# -*- coding: utf-8 -*-
"""Root-admin auto research scheduler for factor mining and AI trade decisions.

The scheduler runs inside the existing web service process. It never exposes
strategy source code and always uses the root admin user's own AI trading config
for model calls.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core import user_manager as um
from analytics import relay_factor_mining
from analytics import ai_trader as ai_trader_engine

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
ROOT = BASE_DIR / "data" / "research" / "relay_factor"
STATUS_PATH = ROOT / "auto_scheduler_status.json"
LOCK_PATH = ROOT / "auto_scheduler.lock"

DEFAULT_CONFIG = {
    "enabled": True,
    "data_refresh_times": ["15:35", "16:10"],
    "factor_mining_time": "17:10",
    "ai_decision_times": ["09:15", "09:25"],
    "window_minutes": 90,
    "retry_cooldown_minutes": 10,
    "max_candidates": 6,
    "execute_paper": False,
    "mining": {
        "n_stocks": 800,
        "days": 180,
        "train_days": 60,
        "test_days": 10,
        "hold_days": 2,
        "top_k": 10,
        "candidate_pool_size": 100,
    },
}

_thread_lock = threading.Lock()
_started = False


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


def _lock_pid() -> int | None:
    try:
        text = LOCK_PATH.read_text(encoding="utf-8").strip()
        first = text.split()[0] if text else ""
        return int(first)
    except Exception:
        return None


def _pid_exists(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_is_stale(max_age_seconds: int = 7200) -> bool:
    if not LOCK_PATH.exists():
        return False
    try:
        if time.time() - LOCK_PATH.stat().st_mtime > max_age_seconds:
            return True
    except Exception:
        return False
    pid = _lock_pid()
    return pid is not None and not _pid_exists(pid)


def _unlink_stale_lock(max_age_seconds: int = 7200) -> bool:
    if not _lock_is_stale(max_age_seconds=max_age_seconds):
        return False
    try:
        LOCK_PATH.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _recover_stale_runtime_state(status: dict) -> dict:
    stale_lock_removed = _unlink_stale_lock()
    if not status.get("running"):
        return status
    if LOCK_PATH.exists() and not stale_lock_removed:
        return status
    job = str(status.get("current_job") or "unknown")
    status["running"] = False
    status.pop("current_job", None)
    status.setdefault("jobs", {})
    _append_history(
        status,
        {
            "job": job,
            "ok": False,
            "error": "stale scheduler lock recovered",
        },
    )
    _save_status(status)
    return status


@contextmanager
def _file_lock(max_age_seconds: int = 7200):
    ROOT.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        try:
            _unlink_stale_lock(max_age_seconds=max_age_seconds)
        except Exception:
            pass
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {_now_text()}".encode("utf-8"))
        yield True
    except FileExistsError:
        yield False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
            try:
                LOCK_PATH.unlink(missing_ok=True)
            except Exception:
                pass


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


def _is_weekday(now: datetime | None = None) -> bool:
    return (now or datetime.now()).weekday() < 5


def next_runs(now: datetime | None = None) -> dict:
    cfg = get_config()
    now = now or datetime.now()
    dates = [now.date(), (now + timedelta(days=1)).date()]
    out: dict[str, str | None] = {"data_refresh": None, "factor_mining": None, "ai_decision": None}
    schedule = {
        "data_refresh": cfg.get("data_refresh_times") or [],
        "factor_mining": [cfg.get("factor_mining_time") or DEFAULT_CONFIG["factor_mining_time"]],
        "ai_decision": cfg.get("ai_decision_times") or [],
    }
    for name, times in schedule.items():
        for d in dates:
            if d.weekday() >= 5:
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
        details["factor_cache"] = dm.refresh_factor_daily_cache(
            days=int(mining_cfg.get("days", 180)),
            min_required=int(mining_cfg.get("n_stocks", 800)),
        )
        factor_cache_ok = bool(details["factor_cache"].get("ok"))
    except Exception as exc:
        details["name_cache_error"] = str(exc)[:180]
        details["factor_cache"] = {"ok": False, "error": str(exc)[:180]}

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


def _run_factor_mining(date_text: str) -> dict:
    from web import admin as admin_api

    cfg = get_config().get("mining", {})
    run_id = datetime.now().strftime("auto-%Y%m%d-%H%M%S")
    out_dir = ROOT / run_id
    report = relay_factor_mining.run_relay_research(
        out_dir=out_dir,
        n_stocks=int(cfg.get("n_stocks", 800)),
        days=int(cfg.get("days", 180)),
        train_days=int(cfg.get("train_days", 60)),
        test_days=int(cfg.get("test_days", 10)),
        hold_days=int(cfg.get("hold_days", 2)),
        top_k=int(cfg.get("top_k", 10)),
        candidate_pool_size=int(cfg.get("candidate_pool_size", 100)),
        include_market_signals=True,
    )
    candidates = report.get("latest_candidates", []) if isinstance(report, dict) else []
    watchlist = admin_api._import_factor_candidates_to_watchlist(
        candidates,
        run_id=run_id,
        strategy_name="AI自动因子挖掘候选",
    )
    return {
        "ok": True,
        "run_id": run_id,
        "status": report.get("status") if isinstance(report, dict) else None,
        "reason": report.get("reason") if isinstance(report, dict) else "",
        "latest_trade_date": ((report.get("summary") or {}).get("latest_trade_date") if isinstance(report, dict) else None),
        "candidate_count": len(candidates),
        "watchlist": watchlist,
    }


def _run_ai_decision(date_text: str) -> dict:
    from web import admin as admin_api

    uid = _root_admin_uid()
    if not uid:
        return {"ok": False, "error": "root admin user not found"}
    cfg = get_config()
    max_candidates = int(cfg.get("max_candidates", 6) or 6)
    candidates = admin_api._factor_watchlist_candidates(max_candidates=max_candidates, latest_only=True)
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
    admin_api._save_ai_trade_decision(payload)
    admin_api._apply_ai_trade_decision_to_watchlist(payload)
    return {
        "ok": True,
        "selected_count": result.get("selected_count", 0),
        "buy_count": sum(1 for x in result.get("results", []) if str(x.get("action") or "").upper() == "BUY"),
        "risk_ok_count": sum(1 for x in result.get("results", []) if x.get("risk_ok")),
        "token_usage": result.get("token_usage"),
        "broker_mode": result.get("broker_mode"),
        "execute_paper": result.get("execute_paper"),
    }


def run_job(job: str, slot: str | None = None, force: bool = False) -> dict:
    job = str(job or "").strip()
    if job not in {"data_refresh", "factor_mining", "ai_decision", "full_after_close"}:
        return {"ok": False, "error": "unknown auto scheduler job"}
    if not force and not get_config().get("enabled", True):
        return {"ok": False, "error": "auto scheduler disabled"}
    with _thread_lock:
        with _file_lock() as acquired:
            if not acquired:
                return {"ok": False, "error": "auto scheduler is already running"}
            status = get_status()
            status["running"] = True
            status["current_job"] = job
            _save_status(status)
            date_text = _today_text()
            key = _job_key(job, slot, date_text)
            try:
                if job == "data_refresh":
                    result = _run_data_refresh(date_text)
                elif job == "factor_mining":
                    result = _run_factor_mining(date_text)
                elif job == "ai_decision":
                    result = _run_ai_decision(date_text)
                else:
                    result = {"ok": True, "steps": {}}
                    result["steps"]["data_refresh"] = _run_data_refresh(date_text)
                    result["steps"]["factor_mining"] = _run_factor_mining(date_text)
                status = get_status()
                status["running"] = False
                status.pop("current_job", None)
                status.setdefault("jobs", {})[key] = {
                    "job": job,
                    "slot": slot,
                    "date": date_text,
                    "ok": bool(result.get("ok")),
                    "finished_at": _now_text(),
                    "result": result,
                }
                _append_history(status, {"job": job, "slot": slot, "ok": bool(result.get("ok")), "result": result})
                _save_status(status)
                return result
            except Exception as exc:
                logger.exception("[auto_factor_scheduler] job %s failed", job)
                result = {"ok": False, "error": str(exc)[:300]}
                status = get_status()
                status["running"] = False
                status.pop("current_job", None)
                status.setdefault("jobs", {})[key] = {
                    "job": job,
                    "slot": slot,
                    "date": date_text,
                    "ok": False,
                    "finished_at": _now_text(),
                    "result": result,
                }
                _append_history(status, {"job": job, "slot": slot, "ok": False, "error": result["error"]})
                _save_status(status)
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
) -> bool:
    rec = status.get("jobs", {}).get(_job_key(job, slot, date_text))
    if not rec:
        return False
    if rec.get("ok"):
        return True
    finished = rec.get("finished_at")
    try:
        finished_at = datetime.strptime(str(finished), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False
    retry_at = finished_at + timedelta(minutes=max(1, int(retry_cooldown_minutes)))
    return (now or datetime.now()) < retry_at


def run_scheduler_loop(interval_seconds: int = 30) -> None:
    logger.info("[auto_factor_scheduler] started")
    while True:
        try:
            cfg = get_config()
            now = datetime.now()
            if cfg.get("enabled", True) and _is_weekday(now):
                status = get_status()
                date_text = now.strftime("%Y-%m-%d")
                window = int(cfg.get("window_minutes", 90) or 90)
                retry_cooldown = int(cfg.get("retry_cooldown_minutes", 10) or 10)
                for slot in cfg.get("data_refresh_times") or []:
                    slot = str(slot)
                    if _in_window(now, slot, window) and not _attempt_blocked(
                        status, "data_refresh", slot, date_text, now=now, retry_cooldown_minutes=retry_cooldown
                    ):
                        run_job("data_refresh", slot=slot)
                        status = get_status()
                slot = str(cfg.get("factor_mining_time") or DEFAULT_CONFIG["factor_mining_time"])
                if _in_window(now, slot, window) and not _attempt_blocked(
                    status, "factor_mining", slot, date_text, now=now, retry_cooldown_minutes=retry_cooldown
                ):
                    run_job("factor_mining", slot=slot)
                    status = get_status()
                for slot in cfg.get("ai_decision_times") or []:
                    slot = str(slot)
                    if _in_window(now, slot, 20) and not _attempt_blocked(
                        status, "ai_decision", slot, date_text, now=now, retry_cooldown_minutes=retry_cooldown
                    ):
                        run_job("ai_decision", slot=slot)
                        status = get_status()
        except Exception as exc:
            logger.warning(f"[auto_factor_scheduler] loop error: {exc}")
        time.sleep(max(5, int(interval_seconds)))


def start_background_scheduler() -> bool:
    global _started
    with _thread_lock:
        if _started:
            return False
        _started = True
        threading.Thread(target=run_scheduler_loop, daemon=True, name="auto-factor-scheduler").start()
        return True
