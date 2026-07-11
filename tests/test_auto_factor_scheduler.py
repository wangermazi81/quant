# -*- coding: utf-8 -*-
import json
from datetime import datetime
from pathlib import Path


def _isolate_scheduler(monkeypatch, tmp_path):
    from analytics import auto_factor_scheduler as scheduler

    root = tmp_path / "relay_factor"
    monkeypatch.setattr(scheduler, "ROOT", root)
    monkeypatch.setattr(scheduler, "STATUS_PATH", root / "auto_scheduler_status.json")
    monkeypatch.setattr(scheduler, "LOCK_PATH", root / "auto_scheduler.lock")
    return scheduler, root


def test_status_recovers_dead_pid_lock(monkeypatch, tmp_path):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    scheduler.LOCK_PATH.write_text("999999 2026-07-10 17:05:00", encoding="utf-8")
    scheduler.STATUS_PATH.write_text(
        json.dumps(
            {
                "ok": True,
                "enabled": True,
                "running": True,
                "current_job": "factor_mining",
                "updated_at": "2026-07-10 17:05:00",
                "jobs": {},
                "history": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler, "_pid_exists", lambda pid: False, raising=False)

    status = scheduler.get_status()

    assert status["running"] is False
    assert "current_job" not in status
    assert status["history"][0]["job"] == "factor_mining"
    assert status["history"][0]["ok"] is False
    assert status["history"][0]["error"] == "stale scheduler lock recovered"
    assert not scheduler.LOCK_PATH.exists()


def test_run_job_ignores_dead_pid_lock(monkeypatch, tmp_path):
    scheduler, root = _isolate_scheduler(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    scheduler.LOCK_PATH.write_text("999999 2026-07-10 17:05:00", encoding="utf-8")
    scheduler.STATUS_PATH.write_text(
        json.dumps(
            {
                "ok": True,
                "enabled": True,
                "running": True,
                "current_job": "factor_mining",
                "updated_at": "2026-07-10 17:05:00",
                "jobs": {},
                "history": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler, "_pid_exists", lambda pid: False, raising=False)
    monkeypatch.setattr(scheduler, "_run_factor_mining", lambda date_text: {"ok": True, "candidate_count": 3})

    result = scheduler.run_job("factor_mining", slot="17:05", force=True)

    assert result == {"ok": True, "candidate_count": 3}
    status = scheduler.get_status()
    assert status["running"] is False
    assert status["jobs"]
    assert not scheduler.LOCK_PATH.exists()


def test_default_factor_mining_next_run_is_1710(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)

    next_runs = scheduler.next_runs(datetime(2026, 7, 10, 16, 59))

    assert scheduler.get_config()["factor_mining_time"] == "17:10"
    assert next_runs["factor_mining"] == "2026-07-10 17:10"
