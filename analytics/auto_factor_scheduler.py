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
from analytics import relay_factor_experiment
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
    from market import trading_calendar

    return trading_calendar.is_trading_day((now or datetime.now()).date())


def next_runs(now: datetime | None = None) -> dict:
    from market import trading_calendar

    cfg = get_config()
    now = now or datetime.now()
    dates = [(now + timedelta(days=offset)).date() for offset in range(14)]
    out: dict[str, str | None] = {"data_refresh": None, "factor_mining": None, "ai_decision": None}
    schedule = {
        "data_refresh": cfg.get("data_refresh_times") or [],
        "factor_mining": [cfg.get("factor_mining_time") or DEFAULT_CONFIG["factor_mining_time"]],
        "ai_decision": cfg.get("ai_decision_times") or [],
    }
    for name, times in schedule.items():
        for d in dates:
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
        details["factor_cache"] = dm.refresh_factor_daily_cache(
            days=int(mining_cfg.get("days", 320)),
            min_required=int(mining_cfg.get("n_stocks", 800)),
        )
        factor_cache_ok = bool(details["factor_cache"].get("ok"))
    except Exception as exc:
        details["name_cache_error"] = str(exc)[:180]
        details["factor_cache"] = {"ok": False, "error": str(exc)[:180]}

    if factor_cache_ok:
        try:
            from analytics import st_status_history
            from analytics.factors_zoo import scorer

            mining_cfg = get_config().get("mining", {})
            _panel, factor_codes = scorer.build_panel_from_cache(
                n_stocks=int(mining_cfg.get("n_stocks", 800)),
                days=int(mining_cfg.get("days", 320)),
                min_history=min(int(mining_cfg.get("days", 320)), 60),
                require_latest=True,
                point_in_time=True,
            )
            details["st_status"] = st_status_history.refresh_cache(factor_codes)
            factor_cache_ok = bool(details["st_status"].get("ok"))
        except Exception as exc:
            details["st_status"] = {"ok": False, "error": str(exc)[:180]}
            factor_cache_ok = False

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
        store.record_frozen_snapshot(
            experiment_id,
            signal_date,
            data_complete=False,
            data_trusted=False,
            source_snapshot={
                "data_complete": False,
                "market_data_trusted": False,
                "failure_reason": "scheduler_missed_fixed_window_session",
                "reconciled_on": through_date,
                "model_instance_id": experiment["model_instance_id"],
                "protocol_violation_count": 0,
            },
            candidates=[],
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
        frozen = store.record_frozen_snapshot(
            experiment_id,
            date_text,
            data_complete=False,
            data_trusted=False,
            source_snapshot={
                "data_complete": False,
                "market_data_trusted": False,
                "failure_reason": str(reason),
                "failure_details": details,
                "model_instance_id": experiment["model_instance_id"],
                "protocol_violation_count": 0,
            },
            candidates=[],
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
    experiment_id = str(experiment["experiment_id"])
    manifest = experiment["manifest"]
    settled = 0
    pending = 0
    for stored in store.list_candidates(experiment_id):
        if not stored.get("selected") or stored.get("settlement_status") == "settled":
            continue
        candidate = dict(stored.get("snapshot") or {})
        candidate.setdefault("ts_code", stored.get("code"))
        candidate.setdefault("trade_date", stored.get("signal_date"))
        result = relay_factor_mining.settle_frozen_shadow_candidate(
            panel,
            candidate,
            manifest,
        )
        if result.get("status") != "settled":
            pending += 1
            continue
        block_reasons = result.get("block_reasons") or []
        store.settle_candidate(
            experiment_id,
            stored["signal_date"],
            stored["code"],
            entry_date=result.get("entry_date"),
            exit_date=result.get("exit_date"),
            entry_price=result.get("entry_price"),
            exit_price=result.get("exit_price"),
            exit_reason=result.get("exit_reason"),
            realized_return_pct=float(result.get("realized_return_pct") or 0.0),
            blocked=bool(result.get("execution_blocked")),
            block_reason=",".join(str(item) for item in block_reasons),
            valuation_path=result.get("valuation_path") or [],
        )
        settled += 1
    return {"settled": settled, "pending": pending}


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

    snapshot = relay_factor_mining.build_frozen_shadow_snapshot(
        panel,
        experiment["manifest"],
    )
    if (
        not snapshot.get("ok")
        or snapshot.get("data_status") != "ok"
        or str(snapshot.get("signal_date") or "") != date_text
    ):
        source_snapshot = {
            "data_complete": False,
            "market_data_trusted": False,
            "data_quality": snapshot.get("data_quality") or {},
            "failure_reason": (
                snapshot.get("reason") or "shadow_snapshot_not_trusted"
            ),
            "snapshot_signal_date": snapshot.get("signal_date"),
            "data_status": snapshot.get("data_status"),
            "model_instance_id": experiment["model_instance_id"],
            "protocol_violation_count": 0,
        }
        try:
            session_result = store.record_frozen_snapshot(
                experiment_id,
                date_text,
                data_complete=False,
                data_trusted=False,
                source_snapshot=source_snapshot,
                candidates=[],
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
        return {
            **session_result,
            "reason": source_snapshot["failure_reason"],
            "data_status": snapshot.get("data_status"),
        }

    source_snapshot = {
        "data_complete": True,
        "market_data_trusted": True,
        "data_quality": snapshot.get("data_quality") or {},
        "market_gate": snapshot.get("market_gate") or {},
        "candidate_count": len(snapshot.get("candidates") or []),
        "model_instance_id": experiment["model_instance_id"],
        "protocol_violation_count": 0,
    }
    top_k = max(
        1,
        int((experiment["manifest"].get("config") or {}).get("top_k") or 1),
    )
    frozen_candidates = []
    for candidate in snapshot.get("candidates") or []:
        row = dict(candidate)
        rank = int(row.get("candidate_rank") or 0)
        row.update(
            {
                "code": str(row.get("ts_code") or row.get("code") or ""),
                "experiment_id": experiment_id,
                "model_instance_id": experiment["model_instance_id"],
                "source_run_id": experiment["source_run_id"],
                "selected": 0 < rank <= top_k,
                "target_position_pct": float(
                    row.get("shadow_target_position_pct") or 0.0
                ),
            }
        )
        frozen_candidates.append(row)
    try:
        session_result = store.record_frozen_snapshot(
            experiment_id,
            date_text,
            data_complete=True,
            data_trusted=True,
            source_snapshot=source_snapshot,
            candidates=frozen_candidates,
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
    immediate = _settle_pending_shadow_candidates(
        store,
        experiment,
        panel,
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
    return result


def _run_factor_mining(date_text: str) -> dict:
    # 直连领域模块，不再反向 import web 层（A-2）
    from analytics import data_manager as dm
    from analytics.factors_zoo import scorer

    cfg = get_config().get("mining", {})
    run_id = datetime.now().strftime("auto-%Y%m%d-%H%M%S")
    out_dir = ROOT / run_id
    requested_at = datetime.strptime(date_text, "%Y-%m-%d").replace(hour=17, minute=10)
    expected_trade_date = dm.expected_completed_trade_date(requested_at)
    panel, _factor_codes = scorer.build_panel_from_cache(
        n_stocks=int(cfg.get("n_stocks", 800)),
        days=int(cfg.get("days", 320)),
        min_history=min(int(cfg.get("days", 320)), 60),
        require_latest=True,
        point_in_time=True,
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
        n_stocks=int(cfg.get("n_stocks", 800)),
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
                    refresh_result = _run_data_refresh(date_text)
                    result = {"ok": False, "steps": {"data_refresh": refresh_result}}
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
