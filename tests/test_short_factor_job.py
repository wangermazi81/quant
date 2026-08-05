from __future__ import annotations

import json
import weakref
from datetime import datetime

import pandas as pd
import pytest

from analytics import short_factor_job


@pytest.fixture(autouse=True)
def _fresh_factor_cache(monkeypatch):
    def expected_trade_date(now=None):
        current = now or datetime(2026, 7, 31, 17, 10)
        close_guard = current.replace(
            hour=15,
            minute=30,
            second=0,
            microsecond=0,
        )
        candidate = current.date()
        if current < close_guard:
            candidate -= pd.Timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= pd.Timedelta(days=1)
        return candidate.isoformat()

    monkeypatch.setattr(
        short_factor_job.data_manager,
        "expected_completed_trade_date",
        expected_trade_date,
    )

    def fresh_cache(**kwargs):
        expected = expected_trade_date(kwargs.get("now"))
        return {
            "status": "fresh",
            "expected_trade_date": expected,
            "security_master_trade_date": expected,
            "daily_cache_trade_date": expected,
            "daily_latest_count": 2,
            "min_required": 2,
            "security_master_fresh": True,
            "daily_cache_fresh": True,
        }

    monkeypatch.setattr(
        short_factor_job.data_manager,
        "factor_cache_freshness",
        fresh_cache,
    )


def _panel() -> dict[str, pd.DataFrame]:
    dates = pd.bdate_range(end="2026-07-31", periods=90)
    codes = ["000001", "600000"]
    close = pd.DataFrame(
        [[10.0 + row / 100, 20.0 + row / 100] for row in range(90)],
        index=dates,
        columns=codes,
    )
    return {
        "open": close * 0.99,
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": close * 100_000,
        "amount": close * close * 100_000,
        "amount_real": pd.DataFrame(1.0, index=dates, columns=codes),
        "universe_eligible": pd.DataFrame(
            1.0,
            index=dates,
            columns=codes,
        ),
        "security_master_eligible": pd.DataFrame(
            1.0,
            index=dates,
            columns=codes,
        ),
    }


def test_run_discovery_job_isolated_and_writes_latest(
    tmp_path,
    monkeypatch,
):
    calls = []
    auxiliary_load_calls = []
    release_calls = []
    close_refs = []
    auxiliary = {
        "daily_basic": pd.DataFrame(),
        "auction": pd.DataFrame(),
    }
    monkeypatch.setattr(short_factor_job, "ROOT", tmp_path)
    def build_panel(**_kwargs):
        panel = _panel()
        close_refs.append(weakref.ref(panel["close"]))
        return panel, ["000001", "600000"]

    monkeypatch.setattr(
        short_factor_job.scorer,
        "build_panel_from_cache",
        build_panel,
    )
    monkeypatch.setattr(
        short_factor_job.short_factor_st_status,
        "refresh_daily_status_cache",
        lambda codes, start, end: {
            "ok": True,
            "coverage": 1.0,
        },
    )
    monkeypatch.setattr(
        short_factor_job.short_factor_st_status,
        "enrich_panel_daily_status",
        lambda value: (
            {
                **value,
                "is_st": pd.DataFrame(
                    0.0,
                    index=value["close"].index,
                    columns=value["close"].columns,
                ),
                "st_status_known": pd.DataFrame(
                    1.0,
                    index=value["close"].index,
                    columns=value["close"].columns,
                ),
            },
            {
                "coverage": 1.0,
                "known_cells": 180,
                "eligible_cells": 180,
            },
        ),
    )
    monkeypatch.setattr(
        short_factor_job.short_factor_data,
        "refresh_agudata_auxiliary_history",
        lambda codes, start, end, **kwargs: calls.append(
            (list(codes), start, end)
        )
        or {"ok": True, "daily_basic_rows": 2, "auction_rows": 2},
    )
    monkeypatch.setattr(
        short_factor_job.short_factor_data,
        "refresh_baostock_turnover_history",
        lambda codes, start, end, **kwargs: {
            "ok": True,
            "coverage": 1.0,
        },
    )
    monkeypatch.setattr(
        short_factor_job.short_factor_data,
        "load_auxiliary_history",
        lambda **kwargs: auxiliary_load_calls.append(kwargs) or auxiliary,
    )

    def fake_research(panel, **kwargs):
        assert auxiliary == {}
        return {
            "schema_version": "short_factor_discovery_report_v1",
            "run_id": kwargs["run_id"],
            "status": "accepted",
            "research_only": True,
            "deployment_eligible": False,
            "summary": {
                "promotion_candidate_count": 1,
                "oos_rank_ic": 0.03,
                "net_return_pct": 5.0,
            },
            "promotion_candidates": [
                {"factor": "gap_absorption_1"}
            ],
        }

    monkeypatch.setattr(
        short_factor_job.short_factor_discovery,
        "run_short_factor_research",
        fake_research,
    )
    monkeypatch.setattr(
        short_factor_job.short_factor_discovery,
        "_release_research_memory",
        lambda: release_calls.append(close_refs[-1]() is None),
    )

    result = short_factor_job.run_discovery_job(
        config={
            "n_stocks": 2,
            "days": 90,
            "train_days": 40,
            "test_days": 10,
            "step_days": 10,
            "hold_days": 2,
            "top_k": 1,
            "max_factors": 3,
            "max_interactions": 2,
        },
        now=datetime(2026, 7, 31, 17, 10),
    )

    assert result["ok"] is True
    assert result["status"] == "accepted"
    assert result["promotion_candidate_count"] == 1
    assert release_calls == [True]
    assert result["st_status"]["coverage"] == 1.0
    assert calls == [
        (["000001", "600000"], "20260330", "20260731")
    ]
    assert auxiliary_load_calls == [{
        "root": tmp_path / "auxiliary",
        "codes": ["000001", "600000"],
        "start_date": "20260330",
        "end_date": "20260731",
    }]
    latest = json.loads(
        (tmp_path / "latest.json").read_text(encoding="utf-8")
    )
    assert latest["research_only"] is True
    assert latest["deployment_eligible"] is False
    assert not (tmp_path.parent / "relay_factor" / "latest.json").exists()


def test_run_discovery_job_continues_with_cached_data_on_rate_limit(
    tmp_path,
    monkeypatch,
):
    panel = _panel()
    captured = {}
    monkeypatch.setattr(short_factor_job, "ROOT", tmp_path)
    monkeypatch.setattr(
        short_factor_job.scorer,
        "build_panel_from_cache",
        lambda **kwargs: (panel, ["000001", "600000"]),
    )
    monkeypatch.setattr(
        short_factor_job.short_factor_st_status,
        "refresh_daily_status_cache",
        lambda codes, start, end: {
            "ok": True,
            "coverage": 1.0,
        },
    )
    monkeypatch.setattr(
        short_factor_job.short_factor_st_status,
        "enrich_panel_daily_status",
        lambda value: (
            {
                **value,
                "is_st": pd.DataFrame(
                    0.0,
                    index=value["close"].index,
                    columns=value["close"].columns,
                ),
                "st_status_known": pd.DataFrame(
                    1.0,
                    index=value["close"].index,
                    columns=value["close"].columns,
                ),
            },
            {
                "coverage": 1.0,
                "known_cells": 180,
                "eligible_cells": 180,
            },
        ),
    )
    monkeypatch.setattr(
        short_factor_job.short_factor_data,
        "refresh_agudata_auxiliary_history",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            short_factor_job.agudata_provider.AgudataRateLimitError(
                "too fast"
            )
        ),
    )
    monkeypatch.setattr(
        short_factor_job.short_factor_data,
        "refresh_baostock_turnover_history",
        lambda codes, start, end, **kwargs: {
            "ok": True,
            "coverage": 1.0,
        },
    )
    monkeypatch.setattr(
        short_factor_job.short_factor_data,
        "load_auxiliary_history",
        lambda **kwargs: {
            "daily_basic": pd.DataFrame(),
            "auction": pd.DataFrame(),
        },
    )

    def fake_research(panel, **kwargs):
        captured.update(kwargs)
        return {
            "schema_version": "short_factor_discovery_report_v1",
            "channel": "short_factor_discovery",
            "run_id": kwargs["run_id"],
            "status": "rejected",
            "research_only": True,
            "deployment_eligible": False,
            "summary": {},
        }

    monkeypatch.setattr(
        short_factor_job.short_factor_discovery,
        "run_short_factor_research",
        fake_research,
    )

    result = short_factor_job.run_discovery_job(
        config={
            "n_stocks": 2,
            "days": 90,
            "train_days": 40,
            "test_days": 10,
            "step_days": 10,
            "hold_days": 2,
            "top_k": 1,
            "max_factors": 3,
            "max_interactions": 2,
        },
        now=datetime(2026, 7, 31, 17, 10),
    )

    assert result["ok"] is True
    assert result["auxiliary_refresh"]["ok"] is False
    assert (
        result["auxiliary_refresh"]["error_code"]
        == "agudata_rate_limited"
    )
    assert captured["expected_codes"] == ["000001", "600000"]


def test_run_discovery_job_rejects_incomplete_st_status(
    monkeypatch,
):
    close_refs = []
    release_calls = []

    def build_panel(**_kwargs):
        panel = _panel()
        close_refs.append(weakref.ref(panel["close"]))
        return panel, ["000001", "600000"]

    monkeypatch.setattr(
        short_factor_job.scorer,
        "build_panel_from_cache",
        build_panel,
    )
    monkeypatch.setattr(
        short_factor_job.short_factor_st_status,
        "refresh_daily_status_cache",
        lambda codes, start, end: {
            "ok": False,
            "coverage": 0.5,
        },
    )
    monkeypatch.setattr(
        short_factor_job.short_factor_st_status,
        "enrich_panel_daily_status",
        lambda value: (
            value,
            {
                "coverage": 0.5,
                "known_cells": 90,
                "eligible_cells": 180,
            },
        ),
    )
    monkeypatch.setattr(
        short_factor_job.short_factor_discovery,
        "_release_research_memory",
        lambda: release_calls.append(close_refs[-1]() is None),
    )

    with pytest.raises(
        ValueError,
        match="ST status coverage too low",
    ):
        short_factor_job.run_discovery_job(
            config={
                "n_stocks": 2,
                "days": 90,
                "train_days": 40,
                "test_days": 10,
                "step_days": 10,
                "hold_days": 2,
                "top_k": 1,
                "max_factors": 3,
                "max_interactions": 2,
            },
            now=datetime(2026, 7, 31, 17, 10),
        )
    assert release_calls == [True]
