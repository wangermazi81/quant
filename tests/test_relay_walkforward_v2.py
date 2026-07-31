# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics import relay_factor_mining as relay


def _purge_fixture() -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=15)]
    feature_rows = []
    label_rows = []
    for index, trade_date in enumerate(dates):
        exit_date = dates[min(index + 3, len(dates) - 1)]
        for rank, code in enumerate(("AAA", "BBB")):
            feature_rows.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "relay_score": float(100 - rank),
                }
            )
            label_rows.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "entry_date": dates[min(index + 1, len(dates) - 1)],
                    "exit_date": exit_date,
                    "forward_return_pct": 1.0 if code == "AAA" else -1.0,
                    "live_adjusted_return_pct": 0.64 if code == "AAA" else -1.36,
                    "model_target_pct": 0.64 if code == "AAA" else -1.36,
                    "execution_blocked": False,
                    "valuation_path": [
                        {"date": dates[min(index + 1, len(dates) - 1)], "return_pct": 0.0},
                        {"date": exit_date, "return_pct": 0.64 if code == "AAA" else -1.36},
                    ],
                }
            )
    return pd.DataFrame(feature_rows), pd.DataFrame(label_rows), dates


def test_walk_forward_purges_training_labels_that_touch_test_window():
    features, labels, dates = _purge_fixture()

    result = relay.walk_forward_relay(
        features,
        labels,
        train_days=8,
        test_days=4,
        top_k=1,
    )

    window = result["windows"][0]
    assert window["raw_train_end"] == dates[7]
    assert window["train_end"] == dates[4]
    assert window["test_start"] == dates[8]
    assert window["purged_date_count"] == 3
    assert window["purged_sample_count"] == 6
    assert window["train_label_max_exit"] < window["test_start"]


@pytest.mark.parametrize("step_days", [0, -1, 3])
def test_walk_forward_rejects_overlapping_or_non_advancing_step(step_days):
    features, labels, _dates = _purge_fixture()

    with pytest.raises(ValueError, match="step_days"):
        relay.walk_forward_relay(
            features,
            labels,
            train_days=8,
            test_days=4,
            step_days=step_days,
            top_k=1,
        )


def test_walk_forward_reports_cash_aware_portfolio_metrics():
    features, labels, _dates = _purge_fixture()

    result = relay.walk_forward_relay(
        features,
        labels,
        train_days=8,
        test_days=4,
        top_k=1,
    )

    summary = result["summary"]
    assert summary["portfolio_initial_equity"] == 1.0
    assert summary["portfolio_final_equity"] > 1.0
    assert summary["portfolio_total_return_pct"] > 0.0
    assert summary["portfolio_max_gross_exposure"] <= 1.0
    assert summary["portfolio_min_cash"] >= 0.0


def test_walk_forward_marks_unlabeled_oos_feature_as_unevaluable():
    features, labels, dates = _purge_fixture()
    labels = labels[
        ~(
            (labels["trade_date"] == dates[8])
            & (labels["ts_code"] == "AAA")
        )
    ].copy()

    result = relay.walk_forward_relay(
        features,
        labels,
        train_days=8,
        test_days=4,
        top_k=1,
    )

    first_trade = next(item for item in result["trades"] if item["trade_date"] == dates[8])
    assert first_trade["codes"] == ["AAA"]
    assert first_trade["research_evaluable"] is False
    assert first_trade["skip_reason"] == "missing_forward_execution_path"
    assert first_trade["blocked_count"] == 0
    assert first_trade["executable_count"] == 0
    assert first_trade["live_adjusted_return_pct"] is None
    assert result["summary"]["unevaluable_oos_days"] == 1
    assert result["summary"]["unevaluable_oos_slots"] == 1


def test_walk_forward_excludes_selected_pending_exit_from_oos_evidence():
    features, labels, dates = _purge_fixture()
    pending = (labels["trade_date"] == dates[8]) & (labels["ts_code"] == "AAA")
    labels.loc[pending, "forward_return_pct"] = np.nan
    labels.loc[pending, "live_adjusted_return_pct"] = np.nan
    labels.loc[pending, "model_target_pct"] = np.nan
    labels.loc[pending, "exit_date"] = None
    labels.loc[pending, "execution_blocked"] = False
    labels.loc[pending, "label_status"] = "entered_pending_exit"
    labels.loc[pending, "research_evaluable"] = False

    result = relay.walk_forward_relay(
        features,
        labels,
        train_days=8,
        test_days=4,
        top_k=1,
    )

    first_trade = next(item for item in result["trades"] if item["trade_date"] == dates[8])
    assert first_trade["codes"] == ["AAA"]
    assert first_trade["research_evaluable"] is False
    assert first_trade["skip_reason"] == "entered_pending_exit"
    assert result["summary"]["unevaluable_oos_days"] == 1
    assert result["summary"]["unevaluable_oos_slots"] == 1


def test_walk_forward_marks_fully_unlabeled_last_mature_feature_date_unevaluable():
    features, labels, dates = _purge_fixture()
    labels["holding_sessions"] = 3
    labels = labels[labels["trade_date"] < dates[11]].copy()

    result = relay.walk_forward_relay(
        features,
        labels,
        train_days=8,
        test_days=4,
        top_k=1,
    )

    last_trade = next(item for item in result["trades"] if item["trade_date"] == dates[11])
    assert last_trade["codes"] == ["AAA"]
    assert last_trade["research_evaluable"] is False
    assert last_trade["skip_reason"] == "missing_forward_execution_path"
    assert last_trade["blocked_count"] == 0
    assert last_trade["executable_count"] == 0
    assert result["summary"]["unevaluable_oos_days"] == 1
    assert result["summary"]["unevaluable_oos_slots"] == 1


def test_walk_forward_keeps_untrusted_oos_session_and_fails_it_closed():
    features, labels, dates = _purge_fixture()
    features["market_data_trusted"] = True
    features.loc[
        features["trade_date"] == dates[8],
        "market_data_trusted",
    ] = False

    result = relay.walk_forward_relay(
        features,
        labels,
        train_days=8,
        test_days=4,
        top_k=1,
    )

    first_window = result["windows"][0]
    first_trade = next(
        item for item in result["trades"] if item["trade_date"] == dates[8]
    )
    assert first_window["test_start"] == dates[8]
    assert first_window["test_end"] == dates[11]
    assert first_window["untrusted_oos_sessions"] == 1
    assert first_trade["research_evaluable"] is False
    assert first_trade["skip_reason"] == "untrusted_market_data"
    assert result["summary"]["untrusted_oos_sessions"] == 1


def _model_fixture() -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-03-02", periods=20)]
    feature_rows = []
    label_rows = []
    for date_index, trade_date in enumerate(dates):
        for rank, code in enumerate(("AAA", "BBB", "CCC", "DDD"), start=1):
            feature_rows.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "ret_5": rank / 10.0,
                    "relay_score": float(100 - rank * 10),
                }
            )
            target = float(rank - 2.5)
            label_rows.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "forward_return_pct": target,
                    "live_adjusted_return_pct": target,
                    "model_target_pct": target,
                    "execution_blocked": False,
                    "entry_date": trade_date,
                    "exit_date": trade_date,
                    "valuation_path": [{"date": trade_date, "return_pct": target}],
                }
            )
    return pd.DataFrame(feature_rows), pd.DataFrame(label_rows), dates


def test_walk_forward_fits_factor_model_without_reading_test_labels():
    features, labels, dates = _model_fixture()
    baseline = relay.walk_forward_relay(features, labels, train_days=12, test_days=4, top_k=1)

    mutated = labels.copy()
    mutated.loc[mutated["trade_date"].isin(dates[12:16]), "model_target_pct"] *= -1000
    mutated.loc[mutated["trade_date"].isin(dates[12:16]), "forward_return_pct"] *= -1000
    changed = relay.walk_forward_relay(features, mutated, train_days=12, test_days=4, top_k=1)

    first_model = baseline["windows"][0]["model"]
    changed_model = changed["windows"][0]["model"]
    assert first_model["fallback"] is False
    assert "ret_5" in first_model["selected_factors"]
    assert first_model["coefficients"] == changed_model["coefficients"]
    assert first_model["feature_means"] == changed_model["feature_means"]
    assert first_model["feature_scales"] == changed_model["feature_scales"]
    assert first_model["selection_source"] == "fixed_preregistered_parameters"
    assert first_model["selected_parameters"] == changed_model["selected_parameters"]
    assert first_model["validation_accepted"] is True
    assert first_model["refit_policy"] == "fixed_parameters_on_full_purged_train"
    assert first_model["refit_end"] == dates[11]
    assert baseline["windows"][0]["validation_diagnostics"]["rank_ic"] > 0
    assert baseline["windows"][0]["test_diagnostics"]["rank_ic"] > 0
    assert baseline["summary"]["model_validation_acceptance_rate"] == 1.0
    assert baseline["summary"]["selected_factor_frequency"]["ret_5"]["count"] >= 1
    assert baseline["summary"]["selected_factor_frequency"]["ret_5"]["rate"] == 1.0
    assert baseline["summary"]["selected_parameter_frequency"]
    assert "selected_market_gate_rate" in baseline["summary"]


def test_walk_forward_rejects_model_when_validation_reverses():
    features, labels, dates = _model_fixture()
    labels.loc[labels["trade_date"].isin(dates[9:12]), "model_target_pct"] *= -1.0

    result = relay.walk_forward_relay(
        features,
        labels,
        train_days=12,
        test_days=4,
        top_k=1,
    )

    model = result["windows"][0]["model"]
    assert model["validation_accepted"] is False
    assert model["validation_rejection_reason"] == "validation_rank_ic_below_minimum"
    assert model["validation_stable"] is False
    assert model["validation_stability_reason"] == "validation_rank_ic_below_minimum"
    assert result["windows"][0]["gated_live_trade_count"] == 0


def test_walk_forward_purges_fit_labels_that_touch_validation_window():
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-04-01", periods=24)]
    feature_rows = []
    label_rows = []
    for index, trade_date in enumerate(dates):
        exit_date = dates[min(index + 3, len(dates) - 1)]
        for rank, code in enumerate(("AAA", "BBB")):
            feature_rows.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "ret_5": float(rank),
                    "relay_score": float(10 - rank),
                }
            )
            label_rows.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "entry_date": dates[min(index + 1, len(dates) - 1)],
                    "exit_date": exit_date,
                    "forward_return_pct": 1.0 - rank * 2.0,
                    "live_adjusted_return_pct": 1.0 - rank * 2.0,
                    "model_target_pct": 1.0 - rank * 2.0,
                    "execution_blocked": False,
                    "valuation_path": [{"date": exit_date, "return_pct": 1.0 - rank * 2.0}],
                }
            )

    result = relay.walk_forward_relay(
        pd.DataFrame(feature_rows),
        pd.DataFrame(label_rows),
        train_days=16,
        test_days=4,
        top_k=1,
    )

    model = result["windows"][0]["model"]
    assert model["validation_start"]
    assert model["selection_fit_label_max_exit"] < model["validation_start"]
    assert model["refit_label_max_exit"] < result["windows"][0]["test_start"]
    assert model["validation_purged_sample_count"] > 0
