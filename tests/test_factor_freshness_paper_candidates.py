from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import pytest

from analytics import data_manager as dm
from analytics import short_factor_job
from analytics.factors_zoo import scorer
from core import factor_watchlist


def _trusted_master(codes: list[str], trade_dates: list[str]) -> pd.DataFrame:
    rows = []
    row_count = len(codes) + 1
    for code, trade_date in zip(codes, trade_dates):
        rows.append({
            "code": code,
            "ts_code": f"{code}.{'SH' if code.startswith('6') else 'SZ'}",
            "list_date": "20200101",
            "delist_date": "",
            "list_status": "L",
            "lifecycle_source": "baostock_stock_basic",
            "lifecycle_snapshot_complete": True,
            "universe_scope": "sh_sz_a_share",
            "snapshot_at": "2026-08-04T17:00:00",
            "security_master_pagination_complete": True,
            "security_master_page_requests": 1,
            "security_master_row_count": row_count,
            "security_master_snapshot_complete": True,
            "security_master_snapshot_trade_date": trade_date,
        })
    rows.append({
        "code": "000999",
        "ts_code": "000999.SZ",
        "list_date": "19900101",
        "delist_date": "19900102",
        "list_status": "D",
        "lifecycle_source": "baostock_stock_basic",
        "lifecycle_snapshot_complete": True,
        "universe_scope": "sh_sz_a_share",
        "snapshot_at": "2026-08-04T17:00:00",
        "security_master_pagination_complete": True,
        "security_master_page_requests": 1,
        "security_master_row_count": row_count,
        "security_master_snapshot_complete": True,
        "security_master_snapshot_trade_date": max(trade_dates),
    })
    return pd.DataFrame(rows)


def _daily(path, end: str, periods: int = 70) -> None:
    dates = pd.bdate_range(end=end, periods=periods)
    close = pd.Series(range(10, 10 + periods), dtype=float)
    pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": close * 0.99,
        "high": close * 1.01,
        "low": close * 0.98,
        "close": close,
        "volume": 1000.0,
        "amount": close * 100000.0,
    }).to_csv(path, index=False)


def _freshness(status: str, *, master_fresh: bool, daily_fresh: bool) -> dict:
    return {
        "status": status,
        "expected_trade_date": "2026-08-04",
        "security_master_trade_date": "2026-08-04" if master_fresh else "2026-08-03",
        "daily_cache_trade_date": "2026-08-04" if daily_fresh else "2026-08-03",
        "daily_latest_count": 2 if daily_fresh else 0,
        "min_required": 2,
        "security_master_fresh": master_fresh,
        "daily_cache_fresh": daily_fresh,
    }


def test_factor_cache_freshness_uses_weakest_master_snapshot_and_cross_section_count(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    master_path = tmp_path / "security_master.csv"
    _trusted_master(
        ["000001", "000002"],
        ["20260804", "20260803"],
    ).to_csv(master_path, index=False)
    _daily(daily_dir / "000001.csv", "2026-08-04")
    _daily(daily_dir / "000002.csv", "2026-08-04")
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(dm, "SECURITY_MASTER_FILE", master_path)
    monkeypatch.setattr(
        dm,
        "expected_completed_trade_date",
        lambda now=None: "2026-08-04",
    )

    result = dm.factor_cache_freshness(
        now=datetime(2026, 8, 4, 17, 10),
        min_required=2,
    )

    assert result["status"] == "stale"
    assert result["security_master_trade_date"] == "2026-08-03"
    assert result["security_master_fresh"] is False
    assert result["daily_cache_fresh"] is True
    assert result["daily_latest_count"] == 2


def test_factor_cache_freshness_rejects_single_latest_file(
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    master_path = tmp_path / "security_master.csv"
    _trusted_master(
        ["000001", "000002"],
        ["20260804", "20260804"],
    ).to_csv(master_path, index=False)
    _daily(daily_dir / "000001.csv", "2026-08-04")
    _daily(daily_dir / "000002.csv", "2026-08-03")
    monkeypatch.setattr(dm, "DATA_DIR", daily_dir)
    monkeypatch.setattr(dm, "SECURITY_MASTER_FILE", master_path)
    monkeypatch.setattr(
        dm,
        "expected_completed_trade_date",
        lambda now=None: "2026-08-04",
    )

    result = dm.factor_cache_freshness(
        now=datetime(2026, 8, 4, 17, 10),
        min_required=2,
    )

    assert result["status"] == "stale"
    assert result["daily_cache_trade_date"] == "2026-08-04"
    assert result["daily_latest_count"] == 1
    assert result["daily_cache_fresh"] is False


def test_short_factor_freshness_refreshes_only_daily_when_master_is_fresh(monkeypatch):
    checks = iter([
        _freshness("stale", master_fresh=True, daily_fresh=False),
        _freshness("fresh", master_fresh=True, daily_fresh=True),
    ])
    refreshes = []
    monkeypatch.setattr(
        short_factor_job.data_manager,
        "factor_cache_freshness",
        lambda **kwargs: next(checks),
    )
    monkeypatch.setattr(
        short_factor_job.data_manager,
        "refresh_factor_daily_cache",
        lambda **kwargs: refreshes.append(kwargs) or {"ok": True},
    )

    result = short_factor_job._ensure_factor_cache_freshness(
        now=datetime(2026, 8, 4, 17, 10),
        days=90,
        min_required=2,
    )

    assert result["status"] == "fresh"
    assert result["refresh_attempted"] is True
    assert refreshes[0]["refresh_security_master"] is False


def test_short_factor_freshness_refreshes_master_when_master_is_stale(monkeypatch):
    checks = iter([
        _freshness("stale", master_fresh=False, daily_fresh=False),
        _freshness("fresh", master_fresh=True, daily_fresh=True),
    ])
    refreshes = []
    monkeypatch.setattr(
        short_factor_job.data_manager,
        "factor_cache_freshness",
        lambda **kwargs: next(checks),
    )
    monkeypatch.setattr(
        short_factor_job.data_manager,
        "refresh_factor_daily_cache",
        lambda **kwargs: refreshes.append(kwargs) or {"ok": True},
    )

    result = short_factor_job._ensure_factor_cache_freshness(
        now=datetime(2026, 8, 4, 17, 10),
        days=90,
        min_required=2,
    )

    assert result["status"] == "fresh"
    assert refreshes[0]["refresh_security_master"] is True


def test_short_factor_job_fails_closed_before_panel_build_when_refresh_stays_stale(monkeypatch):
    builds = []
    monkeypatch.setattr(
        short_factor_job.data_manager,
        "factor_cache_freshness",
        lambda **kwargs: _freshness("stale", master_fresh=False, daily_fresh=False),
    )
    monkeypatch.setattr(
        short_factor_job.data_manager,
        "refresh_factor_daily_cache",
        lambda **kwargs: {"ok": False, "error": "provider unavailable"},
    )
    monkeypatch.setattr(
        short_factor_job.scorer,
        "build_panel_from_cache",
        lambda **kwargs: builds.append(kwargs),
    )

    with pytest.raises(ValueError, match="cache remains stale"):
        short_factor_job.run_discovery_job(
            config={"n_stocks": 2, "days": 90},
            now=datetime(2026, 8, 4, 17, 10),
        )
    assert builds == []


def test_quarantine_round_trip_and_recovery(tmp_path, monkeypatch):
    from analytics import factor_history_quarantine as quarantine

    path = tmp_path / "factor_history_quarantine.json"
    monkeypatch.setattr(quarantine, "QUARANTINE_PATH", path)

    quarantine.record_failures({"000001": "history remains incomplete"})
    assert quarantine.active_codes() == {"000001"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["codes"]["000001"]["reason"] == "history remains incomplete"

    quarantine.clear_codes(["000001"])
    assert quarantine.active_codes() == set()


def test_point_in_time_panel_excludes_quarantined_before_sampling_and_refills(
    tmp_path,
    monkeypatch,
):
    from analytics import factor_history_quarantine as quarantine

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    master_path = tmp_path / "security_master.csv"
    codes = ["000001", "000002", "000003", "000004"]
    _trusted_master(codes, ["20260804"] * len(codes)).to_csv(master_path, index=False)
    for code in codes:
        _daily(daily_dir / f"{code}.csv", "2026-08-04")
    monkeypatch.setattr(scorer, "_DATA_DIR", daily_dir)
    monkeypatch.setattr(scorer, "_SECURITY_MASTER_PATH", master_path)
    monkeypatch.setattr(quarantine, "QUARANTINE_PATH", tmp_path / "quarantine.json")

    sample_dates = pd.bdate_range("2026-05-01", "2026-08-04")
    master = scorer._load_security_master(master_path)
    stable = scorer._master_sample_mask(
        master,
        sample_dates,
        scorer._point_in_time_pool_size(2),
        minimum_history=10,
    )
    initially_selected = [code for code in stable.columns if bool(stable[code].any())]
    quarantine.record_failures({initially_selected[0]: "bad history"})

    panel, used = scorer.build_panel_from_cache(
        n_stocks=2,
        days=60,
        min_history=10,
        require_latest=True,
        point_in_time=True,
        require_security_master=True,
    )

    assert initially_selected[0] not in used
    assert int(panel["universe_eligible"].iloc[-1].sum()) == 2


def _candidate_panel() -> dict[str, pd.DataFrame]:
    dates = pd.bdate_range(end="2026-08-04", periods=30)
    close = pd.DataFrame(
        {
            "000001": [10.0] * 29 + [11.0],
            "600000": [10.0] * 29 + [10.2],
        },
        index=dates,
    )
    zeros = pd.DataFrame(0.0, index=dates, columns=close.columns)
    ones = pd.DataFrame(1.0, index=dates, columns=close.columns)
    return {
        "open": close.copy(),
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": ones * 100000,
        "amount": close * 100000,
        "amount_real": ones,
        "is_st": zeros,
        "st_status_known": ones,
        "universe_eligible": ones,
        "security_master_eligible": ones,
    }


def _accepted_close_report() -> dict:
    return {
        "run_id": "sfd-20260804-171000",
        "data_through": "2026-08-04",
        "config": {"max_interactions": 0},
        "channel_promotion_candidates": {
            "close": [{"factor": "return_1"}],
            "auction_confirmation": [],
        },
        "close_discovery": {
            "windows": [{
                "status": "evaluated",
                "calibration_passed": True,
                "selected_factors": ["return_1"],
                "score_weights": {"return_1": 1.0},
                "factor_diagnostics": {"return_1": {"direction": 1.0}},
            }]
        },
        "factor_catalog": {
            "return_1": {
                "available_at": "signal_date 15:00",
                "decision_scope": "next_session_candidate_pool",
            }
        },
    }


def test_paper_candidate_builder_outputs_stock_not_factor_and_safety_fields(monkeypatch):
    monkeypatch.setattr(
        short_factor_job,
        "_next_trade_date",
        lambda _signal_date: "2026-08-05",
    )
    monkeypatch.setattr(
        factor_watchlist,
        "resolve_watchlist_name",
        lambda ts_code, fallback=None: {"000001": "平安银行", "600000": "浦发银行"}[str(ts_code).split(".")[0]],
    )
    candidates = short_factor_job._build_paper_candidates(
        report=_accepted_close_report(),
        panel=_candidate_panel(),
        daily_basic=pd.DataFrame(),
        codes=["000001", "600000"],
        config={
            "top_k": 1,
            "hold_days": 2,
            "stop_loss": -0.035,
            "take_profit": 0.07,
            "max_interactions": 0,
        },
        now=datetime(2026, 8, 4, 17, 10),
    )

    assert len(candidates) == 1
    assert candidates[0]["code"] == "000001"
    assert candidates[0]["name"] == "平安银行"
    assert candidates[0]["code"] != "return_1"
    assert candidates[0]["state"] == "paper_candidate"
    assert candidates[0]["status"] == "paper_candidate"
    assert candidates[0]["allow_real_order"] is False
    assert candidates[0]["execution_mode"] == "paper"
    assert candidates[0]["position_pct"] == 0
    assert candidates[0]["planned_entry_date"] == "2026-08-05"
    assert candidates[0]["accepted_factors"] == ["return_1"]


def test_paper_candidate_builder_does_not_fake_close_candidates_from_auction_only():
    report = _accepted_close_report()
    report["channel_promotion_candidates"]["close"] = []
    report["channel_promotion_candidates"]["auction_confirmation"] = [
        {"factor": "auction_gap"}
    ]

    candidates = short_factor_job._build_paper_candidates(
        report=report,
        panel=_candidate_panel(),
        daily_basic=pd.DataFrame(),
        codes=["000001", "600000"],
        config={"top_k": 1, "hold_days": 2, "stop_loss": -0.035, "take_profit": 0.07, "max_interactions": 0},
        now=datetime(2026, 8, 4, 17, 10),
    )

    assert candidates == []


def test_paper_candidate_is_never_deployable_or_usable_for_ai_live_order():
    item = {
        "strategy_key": "short_factor_discovery_paper_v1",
        "model_version": "short_factor_discovery_v1",
        "experiment_id": "forced",
        "model_instance_id": "forced",
        "validation_accepted": True,
        "research_status": "validated",
        "deployment_eligible": True,
        "research_only": False,
        "st_status_known": True,
        "is_st": False,
        "position_pct": 0.2,
        "live_readiness_status": "ready",
        "status": "ready",
        "state": "paper_candidate",
        "allow_real_order": False,
        "execution_mode": "paper",
    }

    assert factor_watchlist.is_deployable_factor_candidate(item) is False
    assert factor_watchlist._is_usable_ai_candidate(item) is False


def test_sanitize_watchlist_preserves_paper_safety_and_execution_metadata(monkeypatch):
    monkeypatch.setattr(factor_watchlist, "resolve_watchlist_name", lambda *_args: "平安银行")
    item = factor_watchlist.sanitize_watchlist_item({
        "code": "000001",
        "trade_date": "2026-08-04",
        "signal_trade_date": "2026-08-04",
        "planned_entry_date": "2026-08-05",
        "entry_window": "09:25",
        "state": "paper_candidate",
        "status": "paper_candidate",
        "allow_real_order": False,
        "execution_mode": "paper",
        "factor_model_version": "short_factor_discovery_v1",
        "slippage": 0.002,
        "limit_up_down_handling": "skip_unfillable",
        "suspension_filter": True,
        "st_filter": True,
        "accepted_factors": ["return_1"],
        "factor_score": 0.9,
    })

    assert item is not None
    assert item["state"] == "paper_candidate"
    assert item["allow_real_order"] is False
    assert item["execution_mode"] == "paper"
    assert item["planned_entry_date"] == "2026-08-05"
    assert item["accepted_factors"] == ["return_1"]
    assert item["factor_score"] == 0.9


def test_sanitize_watchlist_rejects_factor_name_as_stock_code():
    assert factor_watchlist.sanitize_watchlist_item({
        "code": "return_1",
        "trade_date": "2026-08-04",
    }) is None
