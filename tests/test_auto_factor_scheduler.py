# -*- coding: utf-8 -*-
import json
from datetime import datetime

import pandas as pd


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


def test_default_factor_mining_history_can_cover_research_window_gate(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.relay_factor_mining import RESEARCH_VALIDATION_RULES

    mining = scheduler.get_config()["mining"]
    required_days = (
        60
        + int(mining["train_days"])
        + int(mining["test_days"])
        * int(RESEARCH_VALIDATION_RULES["minimum_oos_windows"])
        + int(mining["hold_days"])
        + 1
    )

    assert int(mining["days"]) >= required_days


def test_scheduler_uses_trade_calendar_and_finds_next_open_session(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from market import trading_calendar

    monkeypatch.setattr(
        trading_calendar,
        "is_trading_day",
        lambda value=None: getattr(value, "isoformat", lambda: "")() == "2026-07-13",
    )

    assert scheduler._is_weekday(datetime(2026, 7, 10, 17, 10)) is False
    next_runs = scheduler.next_runs(datetime(2026, 7, 10, 16, 59))
    assert next_runs["factor_mining"] == "2026-07-13 17:10"


def test_data_refresh_reports_failure_when_factor_cache_is_incomplete(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics import data_manager as dm
    from analytics import lhb_engine
    from market import market_stats

    monkeypatch.setattr(dm, "refresh_name_cache", lambda: {"000001": "平安银行"})
    monkeypatch.setattr(
        dm,
        "refresh_factor_daily_cache",
        lambda **kwargs: {"ok": False, "latest_count": 2, "min_required": 800},
        raising=False,
    )
    monkeypatch.setattr(market_stats, "get_market_breadth", lambda date_text=None: {"ok": True})
    monkeypatch.setattr(market_stats, "get_market_turnover", lambda date_text=None: {"ok": True})
    monkeypatch.setattr(lhb_engine, "get_lhb_data", lambda date_text, force_refresh=False: {"ok": True, "total": 0})

    result = scheduler._run_data_refresh("2026-07-13")

    assert result["ok"] is False
    assert result["details"]["factor_cache"]["latest_count"] == 2


def test_data_refresh_updates_st_history_for_factor_universe(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics import data_manager as dm
    from analytics import lhb_engine
    from analytics import st_status_history
    from analytics.factors_zoo import scorer
    from market import market_stats

    monkeypatch.setattr(dm, "refresh_name_cache", lambda: {"000001": "平安银行"})
    monkeypatch.setattr(
        dm,
        "refresh_factor_daily_cache",
        lambda **kwargs: {"ok": True, "latest_count": 800, "min_required": 800},
        raising=False,
    )
    monkeypatch.setattr(
        scorer,
        "build_panel_from_cache",
        lambda **kwargs: ({"close": object()}, ["000001", "000002"]),
    )
    captured = {}

    def fake_refresh(codes, **kwargs):
        captured["codes"] = list(codes)
        return {"ok": True, "requested": 2, "usable": 2, "coverage": 1.0}

    monkeypatch.setattr(st_status_history, "refresh_cache", fake_refresh)
    monkeypatch.setattr(market_stats, "get_market_breadth", lambda date_text=None: {"ok": True})
    monkeypatch.setattr(market_stats, "get_market_turnover", lambda date_text=None: {"ok": True})
    monkeypatch.setattr(lhb_engine, "get_lhb_data", lambda date_text, force_refresh=False: {"ok": True, "total": 0})

    result = scheduler._run_data_refresh("2026-07-13")

    assert result["ok"] is True
    assert captured["codes"] == ["000001", "000002"]
    assert result["details"]["st_status"]["coverage"] == 1.0


def test_failed_job_is_cooled_down_before_retry(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    key = scheduler._job_key("factor_mining", "17:10", "2026-07-13")
    status = {
        "jobs": {
            key: {
                "ok": False,
                "finished_at": "2026-07-13 17:11:00",
            }
        }
    }

    assert scheduler._attempt_blocked(
        status,
        "factor_mining",
        "17:10",
        "2026-07-13",
        now=datetime(2026, 7, 13, 17, 15),
        retry_cooldown_minutes=10,
    ) is True
    assert scheduler._attempt_blocked(
        status,
        "factor_mining",
        "17:10",
        "2026-07-13",
        now=datetime(2026, 7, 13, 17, 22),
        retry_cooldown_minutes=10,
    ) is False


def test_scheduled_factor_mining_passes_trade_plan_to_research(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.factors_zoo import scorer
    from core import factor_watchlist

    captured = {}
    fresh_panel = {
        "close": pd.DataFrame(
            {"000001": [10.0]},
            index=pd.to_datetime(["2026-07-29"]),
        )
    }

    def fake_research(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "reason": "ok", "summary": {}, "latest_candidates": []}

    monkeypatch.setattr(scheduler, "get_config", lambda: {
        "mining": {
            "n_stocks": 100,
            "days": 90,
            "train_days": 40,
            "test_days": 10,
            "hold_days": 3,
            "stop_loss": -0.04,
            "take_profit": 0.08,
            "top_k": 5,
            "candidate_pool_size": 20,
        }
    })
    monkeypatch.setattr(scheduler.relay_factor_mining, "run_relay_research", fake_research)
    monkeypatch.setattr(scorer, "build_panel_from_cache", lambda **kwargs: (fresh_panel, ["000001"]))
    monkeypatch.setattr(
        factor_watchlist,
        "import_factor_candidates_to_watchlist",
        lambda *args, **kwargs: {"ok": True, "imported": 0, "total": 0},
    )

    result = scheduler._run_factor_mining("2026-07-29")

    assert result["ok"] is True
    assert captured["hold_days"] == 3
    assert captured["stop_loss"] == -0.04
    assert captured["take_profit"] == 0.08
    assert captured["feedback_user_id"] is None
    assert captured["panel"] is fresh_panel


def test_factor_mining_failure_is_retryable_when_prospective_cycle_raises(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.factors_zoo import scorer

    fresh_panel = {
        "close": pd.DataFrame(
            {"000001": [10.0]},
            index=pd.to_datetime(["2026-07-29"]),
        )
    }
    monkeypatch.setattr(
        scorer,
        "build_panel_from_cache",
        lambda **kwargs: (fresh_panel, ["000001"]),
    )
    monkeypatch.setattr(
        scheduler,
        "_run_prospective_cycle",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("prospective store unavailable")
        ),
    )
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "run_relay_research",
        lambda **kwargs: {
            "status": "ok",
            "data_status": "ok",
            "research_status": "failed",
            "research_assessment": {"deployment_eligible": False},
            "summary": {"latest_trade_date": "2026-07-29"},
            "latest_candidates": [],
        },
    )

    result = scheduler._run_factor_mining("2026-07-29")

    assert result["ok"] is False
    assert result["prospective"]["ok"] is False
    assert result["prospective"]["error"] == "prospective store unavailable"


def test_factor_mining_rejects_stale_cache_before_research(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.relay_factor_experiment import RelayFactorExperimentStore
    from analytics.factors_zoo import scorer
    from core import factor_watchlist

    store = RelayFactorExperimentStore(tmp_path / "prospective.sqlite3")
    experiment = store.create_locked_experiment(
        "run-stale-cache",
        _frozen_manifest(required_sessions=1),
        locked_at="2026-07-28 17:10:00",
    )
    monkeypatch.setattr(scheduler, "_factor_experiment_store", lambda: store)
    stale_panel = {
        "close": pd.DataFrame(
            {"000001": [10.0]},
            index=pd.to_datetime(["2026-07-28"]),
        )
    }
    monkeypatch.setattr(scorer, "build_panel_from_cache", lambda **kwargs: (stale_panel, ["000001"]))
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "run_relay_research",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("research must not run")),
    )
    monkeypatch.setattr(
        factor_watchlist,
        "import_factor_candidates_to_watchlist",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("watchlist import must not run")),
    )

    result = scheduler._run_factor_mining("2026-07-29")

    assert result["ok"] is False
    assert result["data_status"] == "stale"
    assert result["expected_trade_date"] == "2026-07-29"
    assert result["latest_trade_date"] == "2026-07-28"
    assert result["error"] == (
        "factor cache is stale: expected 2026-07-29, got 2026-07-28"
    )
    assert result["prospective"]["recorded"] is True
    assert result["prospective"]["data_trusted"] is False
    session = store.list_sessions(experiment["experiment_id"])[0]
    assert session["signal_date"] == "2026-07-29"
    assert session["source_snapshot"]["failure_reason"] == "factor_cache_stale"


def test_full_after_close_skips_mining_when_refresh_fails(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.relay_factor_experiment import RelayFactorExperimentStore

    store = RelayFactorExperimentStore(tmp_path / "prospective.sqlite3")
    experiment = store.create_locked_experiment(
        "run-refresh-failed",
        _frozen_manifest(required_sessions=1),
        locked_at="2026-07-28 17:10:00",
    )
    refresh_result = {
        "ok": False,
        "details": {"factor_cache": {"ok": False, "error": "upstream unavailable"}},
    }
    monkeypatch.setattr(scheduler, "_today_text", lambda: "2026-07-29")
    monkeypatch.setattr(scheduler, "_factor_experiment_store", lambda: store)
    monkeypatch.setattr(scheduler, "_run_data_refresh", lambda date_text: refresh_result)
    monkeypatch.setattr(
        scheduler,
        "_run_factor_mining",
        lambda date_text: (_ for _ in ()).throw(AssertionError("mining must be skipped")),
    )

    result = scheduler.run_job("full_after_close", force=True)

    assert result["ok"] is False
    assert result["steps"]["data_refresh"] == refresh_result
    factor_step = result["steps"]["factor_mining"]
    assert factor_step["ok"] is False
    assert factor_step["skipped"] is True
    assert factor_step["reason"] == "data_refresh_failed"
    assert factor_step["prospective"]["recorded"] is True
    assert factor_step["prospective"]["data_trusted"] is False
    session = store.list_sessions(experiment["experiment_id"])[0]
    assert session["signal_date"] == "2026-07-29"
    assert session["source_snapshot"]["failure_reason"] == "data_refresh_failed"


def test_factor_mining_does_not_import_candidates_from_degraded_data(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.factors_zoo import scorer
    from core import factor_watchlist

    fresh_panel = {
        "close": pd.DataFrame(
            {"000001": [10.0]},
            index=pd.to_datetime(["2026-07-29"]),
        )
    }
    monkeypatch.setattr(scorer, "build_panel_from_cache", lambda **kwargs: (fresh_panel, ["000001"]))
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "run_relay_research",
        lambda **kwargs: {
            "status": "degraded",
            "data_status": "degraded",
            "reason": "turnover amount coverage too low",
            "research_status": "failed",
            "research_assessment": {"deployment_eligible": False},
            "summary": {"latest_trade_date": "2026-07-29"},
            "latest_candidates": [{"code": "000001"}],
        },
    )
    monkeypatch.setattr(
        factor_watchlist,
        "import_factor_candidates_to_watchlist",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("watchlist import must not run")),
    )

    result = scheduler._run_factor_mining("2026-07-29")

    assert result["ok"] is False
    assert result["data_status"] == "degraded"
    assert result["research_status"] == "failed"
    assert result["deployment_eligible"] is False
    assert result["candidate_count"] == 1
    assert result["watchlist"] == {
        "ok": False,
        "skipped": True,
        "reason": "data_status_degraded",
    }


def test_factor_mining_records_exploratory_research_without_importing_watchlist(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.factors_zoo import scorer
    from core import factor_watchlist

    fresh_panel = {
        "close": pd.DataFrame(
            {"000001": [10.0]},
            index=pd.to_datetime(["2026-07-29"]),
        )
    }
    candidate = {
        "code": "000001",
        "research_status": "failed",
        "deployment_eligible": False,
        "research_only": True,
        "position_pct": 0.0,
    }
    monkeypatch.setattr(scorer, "build_panel_from_cache", lambda **kwargs: (fresh_panel, ["000001"]))
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "run_relay_research",
        lambda **kwargs: {
            "status": "ok",
            "data_status": "ok",
            "reason": "ok",
            "research_status": "failed",
            "research_assessment": {
                "research_status": "failed",
                "deployment_eligible": False,
                "failure_reasons": ["non_positive_oos_return"],
            },
            "summary": {"latest_trade_date": "2026-07-29"},
            "latest_candidates": [candidate],
        },
    )
    monkeypatch.setattr(
        factor_watchlist,
        "import_factor_candidates_to_watchlist",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("exploratory candidates must not enter the trading watchlist")
        ),
    )

    result = scheduler._run_factor_mining("2026-07-29")

    assert result["ok"] is True
    assert result["data_status"] == "ok"
    assert result["research_status"] == "failed"
    assert result["deployment_eligible"] is False
    assert result["watchlist"] == {
        "ok": False,
        "skipped": True,
        "reason": "exploratory_research_not_deployable",
    }


def _frozen_manifest(required_sessions=20):
    return {
        "strategy_key": "relay_factor_mining_v2",
        "model": {
            "model_version": "relay_factor_mining_v3_nested_ridge",
            "fallback": False,
            "validation_accepted": True,
            "selected_factors": ["ret_5"],
            "coefficients": {"ret_5": 1.0},
            "feature_means": {"ret_5": 0.5},
            "feature_scales": {"ret_5": 0.25},
            "selected_market_gate": {"enabled": False},
        },
        "config": {
            "hold_days": 2,
            "stop_loss": -0.035,
            "take_profit": 0.07,
            "top_k": 1,
            "candidate_pool_size": 3,
            "include_market_signals": False,
        },
        "mining_rules": {},
        "acceptance_rules": {
            "required_sessions": required_sessions,
            "minimum_total_return_pct": 0.0,
            "maximum_drawdown_pct": -10.0,
            "minimum_executed_trades": 1,
            "maximum_protocol_violations": 0,
        },
    }


def _shadow_snapshot(signal_date):
    return {
        "ok": True,
        "data_status": "ok",
        "reason": "ok",
        "signal_date": signal_date,
        "market_gate": {"enabled": False, "pass": True},
        "data_quality": {
            "latest_real_amount_coverage": 1.0,
            "latest_st_status_coverage": 1.0,
            "latest_market_data_trusted": True,
        },
        "candidates": [
            {
                "ts_code": "000001.SZ",
                "code": "000001",
                "trade_date": signal_date,
                "candidate_rank": 1,
                "shadow_target_position_pct": 0.1,
                "position_pct": 0.0,
                "research_status": "locked_shadow",
                "deployment_eligible": False,
                "research_only": True,
                "gate_pass": True,
            }
        ],
    }


def test_prospective_cycle_records_one_immutable_shadow_session(monkeypatch, tmp_path):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.relay_factor_experiment import RelayFactorExperimentStore
    from core import factor_watchlist

    store = RelayFactorExperimentStore(tmp_path / "prospective.sqlite3")
    experiment = store.create_locked_experiment(
        "run-shadow",
        _frozen_manifest(),
        locked_at="2026-07-01 17:10:00",
    )
    monkeypatch.setattr(scheduler, "_factor_experiment_store", lambda: store)
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda panel, manifest: _shadow_snapshot("2026-07-02"),
    )
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "settle_frozen_shadow_candidate",
        lambda panel, candidate, manifest: {
            "status": "pending",
            "reason": "awaiting_exit_data",
        },
    )
    monkeypatch.setattr(
        factor_watchlist,
        "import_factor_candidates_to_watchlist",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("locked shadow candidates must not enter watchlist")
        ),
    )

    first = scheduler._run_prospective_cycle("2026-07-02", panel={})
    second = scheduler._run_prospective_cycle("2026-07-02", panel={})

    assert first["ok"] is True
    assert first["shadow"]["recorded"] is True
    assert second["shadow"]["idempotent"] is True
    progress = store.get_progress(experiment["experiment_id"])
    assert progress["observed_sessions"] == 1
    assert progress["settled_sessions"] == 0
    candidate = store.list_candidates(experiment["experiment_id"])[0]
    assert candidate["selected"] is True
    assert candidate["target_position_pct"] == 0.1
    assert candidate["snapshot"]["research_only"] is True


def test_prospective_cycle_freezes_untrusted_shadow_day_and_fails_fixed_window(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.relay_factor_experiment import RelayFactorExperimentStore

    store = RelayFactorExperimentStore(tmp_path / "prospective.sqlite3")
    experiment = store.create_locked_experiment(
        "run-untrusted-shadow",
        _frozen_manifest(required_sessions=1),
        locked_at="2026-07-01 17:10:00",
    )
    monkeypatch.setattr(scheduler, "_factor_experiment_store", lambda: store)
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda panel, manifest: {
            "ok": False,
            "data_status": "degraded",
            "reason": "turnover_amount_coverage_too_low",
            "signal_date": "2026-07-02",
            "data_quality": {"latest_market_data_trusted": False},
            "candidates": [],
        },
    )

    result = scheduler._run_prospective_cycle("2026-07-02", panel={})

    assert result["shadow"]["recorded"] is True
    assert result["shadow"]["data_trusted"] is False
    session = store.list_sessions(experiment["experiment_id"])[0]
    assert session["source_snapshot"]["failure_reason"] == (
        "turnover_amount_coverage_too_low"
    )
    assert store.get_progress(experiment["experiment_id"])["observed_sessions"] == 1
    assert store.get_experiment(experiment["experiment_id"])["status"] == "failed"


def test_prospective_cycle_backfills_missed_fixed_window_sessions_as_untrusted(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.relay_factor_experiment import RelayFactorExperimentStore

    store = RelayFactorExperimentStore(tmp_path / "prospective.sqlite3")
    experiment = store.create_locked_experiment(
        "run-missed-window",
        _frozen_manifest(required_sessions=3),
        locked_at="2026-07-01 17:10:00",
    )
    monkeypatch.setattr(scheduler, "_factor_experiment_store", lambda: store)
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda panel, manifest: _shadow_snapshot("2026-07-07"),
    )
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "settle_frozen_shadow_candidate",
        lambda panel, candidate, manifest: {
            "status": "pending",
            "reason": "awaiting_exit_data",
        },
    )

    result = scheduler._run_prospective_cycle("2026-07-07", panel={})

    sessions = store.list_sessions(experiment["experiment_id"])
    assert [item["signal_date"] for item in sessions] == [
        "2026-07-02",
        "2026-07-03",
        "2026-07-06",
    ]
    assert all(item["data_trusted"] is False for item in sessions)
    assert all(
        item["source_snapshot"]["failure_reason"]
        == "scheduler_missed_fixed_window_session"
        for item in sessions
    )
    assert result["reconciliation"]["recorded_dates"] == [
        "2026-07-02",
        "2026-07-03",
        "2026-07-06",
    ]
    assert store.get_experiment(experiment["experiment_id"])["status"] == "failed"


def test_prospective_cycle_imports_only_latest_validated_frozen_model(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    from analytics.relay_factor_experiment import RelayFactorExperimentStore
    from core import factor_watchlist

    store = RelayFactorExperimentStore(tmp_path / "prospective.sqlite3")
    experiment = store.create_locked_experiment(
        "run-validated",
        _frozen_manifest(required_sessions=1),
        locked_at="2026-07-01 17:10:00",
    )
    experiment_id = experiment["experiment_id"]
    store.record_session(
        experiment_id,
        "2026-07-02",
        source_snapshot={"market_data_trusted": True},
    )
    store.record_candidates(
        experiment_id,
        "2026-07-02",
        [
            {
                "code": "000001.SZ",
                "candidate_rank": 1,
                "selected": True,
                "target_position_pct": 0.1,
            }
        ],
    )
    store.settle_candidate(
        experiment_id,
        "2026-07-02",
        "000001.SZ",
        entry_date="2026-07-03",
        exit_date="2026-07-06",
        entry_price=10.0,
        exit_price=11.0,
        exit_reason="take_profit",
        realized_return_pct=9.64,
        valuation_path=[
            {"date": "2026-07-03", "return_pct": -0.36},
            {"date": "2026-07-06", "return_pct": 9.64},
        ],
    )
    assert store.evaluate_once(experiment_id)["status"] == "validated"

    monkeypatch.setattr(scheduler, "_factor_experiment_store", lambda: store)
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda panel, manifest: _shadow_snapshot("2026-07-07"),
    )
    captured = {}

    def fake_import(candidates, **kwargs):
        captured["candidates"] = list(candidates)
        captured["kwargs"] = kwargs
        return {"ok": True, "imported": len(candidates)}

    monkeypatch.setattr(
        factor_watchlist,
        "import_factor_candidates_to_watchlist",
        fake_import,
    )

    result = scheduler._run_prospective_cycle("2026-07-07", panel={})

    assert result["ok"] is True
    assert result["production"]["imported"] == 1
    candidate = captured["candidates"][0]
    assert candidate["experiment_id"] == experiment_id
    assert candidate["model_instance_id"] == experiment["model_instance_id"]
    assert candidate["research_status"] == "validated"
    assert candidate["deployment_eligible"] is True
    assert candidate["research_only"] is False
    assert candidate["position_pct"] == 0.1


def test_validated_zero_candidate_day_advances_watchlist_scope_without_fallback(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    import app_state
    from core import factor_watchlist

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    factor_watchlist.import_factor_candidates_to_watchlist(
        [
            {
                **_shadow_snapshot("2026-07-28")["candidates"][0],
                "strategy_key": "relay_factor_mining_v2",
                "model_version": "relay_factor_mining_v3_nested_ridge",
                "experiment_id": "experiment-validated",
                "model_instance_id": "model-validated",
                "validation_accepted": True,
                "research_status": "validated",
                "deployment_eligible": True,
                "research_only": False,
                "live_readiness_status": "ready",
                "position_pct": 0.1,
            }
        ],
        run_id="run-validated",
    )
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda panel, manifest: {
            "ok": True,
            "data_status": "ok",
            "reason": "no_candidates",
            "signal_date": "2026-07-29",
            "candidates": [],
        },
    )
    experiment = {
        "experiment_id": "experiment-validated",
        "model_instance_id": "model-validated",
        "source_run_id": "run-validated",
        "manifest": _frozen_manifest(required_sessions=1),
    }

    result = scheduler._validated_production_candidates(experiment, panel={})

    assert result["ok"] is True
    assert result["imported"] == 0
    assert result["latest_scope"]["trade_date"] == "2026-07-29"
    assert result["latest_scope"]["candidate_count"] == 0
    assert factor_watchlist.factor_watchlist_candidates(
        max_candidates=6,
        latest_only=True,
    ) == []


def test_validated_untrusted_snapshot_advances_empty_watchlist_scope(
    monkeypatch,
    tmp_path,
):
    scheduler, _root = _isolate_scheduler(monkeypatch, tmp_path)
    import app_state
    from core import factor_watchlist

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    factor_watchlist.import_factor_candidates_to_watchlist(
        [
            {
                **_shadow_snapshot("2026-07-28")["candidates"][0],
                "strategy_key": "relay_factor_mining_v2",
                "model_version": "relay_factor_mining_v3_nested_ridge",
                "experiment_id": "experiment-validated",
                "model_instance_id": "model-validated",
                "validation_accepted": True,
                "research_status": "validated",
                "deployment_eligible": True,
                "research_only": False,
                "live_readiness_status": "ready",
                "position_pct": 0.1,
            }
        ],
        run_id="run-validated",
    )
    monkeypatch.setattr(
        scheduler.relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda panel, manifest: {
            "ok": False,
            "data_status": "degraded",
            "reason": "market_data_untrusted",
            "signal_date": "2026-07-29",
            "candidates": [],
        },
    )
    experiment = {
        "experiment_id": "experiment-validated",
        "model_instance_id": "model-validated",
        "source_run_id": "run-validated",
        "manifest": _frozen_manifest(required_sessions=1),
    }

    result = scheduler._validated_production_candidates(experiment, panel={})

    assert result["ok"] is False
    assert result["reason"] == "market_data_untrusted"
    assert result["latest_scope"]["trade_date"] == "2026-07-29"
    assert result["latest_scope"]["candidate_count"] == 0
