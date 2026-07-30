import json

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from analytics import relay_factor_model as model_module
from analytics.relay_factor_model import (
    CANDIDATE_FACTORS,
    factor_diagnostics,
    fit_factor_model,
    score_factor_model,
    selection_turnover,
)


EXPECTED_FACTORS = [
    "ret_3",
    "ret_5",
    "ret_10",
    "ret_20",
    "volume_ratio_20",
    "amount_ratio_20",
    "near_high_20",
    "drawdown_20",
    "volatility_10",
    "limit_count_20",
]


def _predictive_frame(dates: int = 6) -> pd.DataFrame:
    rows = []
    for date_offset in range(dates):
        trade_date = pd.Timestamp("2026-01-05") + pd.Timedelta(days=date_offset)
        for rank in range(1, 11):
            rows.append(
                {
                    "trade_date": trade_date,
                    "code": f"{rank:06d}.SZ",
                    "ret_5": float(rank + date_offset * 100),
                    "volatility_10": float(20 - rank + date_offset * 10),
                    "near_high_20": 7.0,
                    "model_target_pct": float(rank - 5),
                    "relay_score": float(rank) / 10.0,
                }
            )
    return pd.DataFrame(rows)


def test_fit_selects_predictive_directions_and_excludes_constant_factor():
    frame = _predictive_frame()
    frame["volatility_10"] = np.tile(
        [10.0, 9.0, 8.0, 7.0, 6.0, 1.0, 5.0, 4.0, 3.0, 2.0],
        frame["trade_date"].nunique(),
    )
    model = fit_factor_model(frame, max_factors=3)

    assert list(CANDIDATE_FACTORS) == EXPECTED_FACTORS
    assert model["fallback"] is False
    assert model["fallback_reason"] is None
    assert model["selected_factors"] == ["ret_5", "volatility_10"]
    assert model["training_ic"]["ret_5"] == 1.0
    assert model["training_ic"]["volatility_10"] < 0.0
    assert "near_high_20" not in model["selected_factors"]
    assert model["coefficients"]["ret_5"] > 0.0
    assert model["coefficients"]["volatility_10"] < 0.0
    assert np.isclose(sum(abs(value) for value in model["normalized_weights"].values()), 1.0)
    assert model["model_version"] == "relay_factor_mining_v3_nested_ridge"
    assert model["ridge_alpha"] == 5.0


def test_fit_excludes_amount_factor_when_only_proxy_turnover_is_available():
    frame = _predictive_frame()
    frame["amount_ratio_20"] = frame["ret_5"]
    frame["amount_data_real"] = False

    model = fit_factor_model(frame, max_factors=3)

    assert "amount_ratio_20" not in model["training_ic"]
    assert "amount_ratio_20" not in model["selected_factors"]


def test_fit_keeps_amount_factor_when_real_coverage_meets_quality_gate():
    frame = _predictive_frame(dates=10)
    frame["amount_ratio_20"] = frame["ret_5"]
    frame["amount_data_real"] = True
    frame.loc[frame.index[:2], "amount_data_real"] = False
    frame.loc[frame.index[:2], "amount_ratio_20"] = np.nan

    model = fit_factor_model(frame, max_factors=10)

    assert "amount_ratio_20" in model["training_ic"]
    assert model["amount_data_coverage"] == 0.98


def test_fit_does_not_select_two_perfectly_correlated_factors():
    frame = _predictive_frame(dates=10)
    signal = frame["ret_5"].copy()
    frame["ret_5"] = 1.0
    frame["volatility_10"] = 1.0
    frame["near_high_20"] = signal
    frame["drawdown_20"] = frame["near_high_20"] - 1.0

    model = fit_factor_model(frame, max_factors=10)

    assert "near_high_20" in model["selected_factors"]
    assert "drawdown_20" not in model["selected_factors"]
    assert model["dropped_correlated_factors"]["drawdown_20"] == "near_high_20"


def test_ridge_coefficients_do_not_change_when_training_rows_are_duplicated():
    frame = _predictive_frame(dates=10)
    baseline = fit_factor_model(frame, max_factors=2, ridge_alpha=5.0)
    duplicated = fit_factor_model(
        pd.concat([frame] * 5, ignore_index=True),
        max_factors=2,
        ridge_alpha=5.0,
    )

    assert duplicated["selected_factors"] == baseline["selected_factors"]
    assert duplicated["coefficients"] == pytest.approx(baseline["coefficients"])


def test_validation_selects_a_preregistered_model_and_records_diagnostics():
    fit_frame = _predictive_frame(dates=10)
    validation_frame = _predictive_frame(dates=4)

    selection = model_module.select_factor_model(fit_frame, validation_frame)

    assert selection["selection_source"] == "chronological_validation"
    assert selection["validation_accepted"] is True
    assert selection["validation_rejection_reason"] is None
    assert selection["selected_parameters"] in model_module.MODEL_CANDIDATES
    assert selection["validation_diagnostics"]["rank_ic"] > 0.0
    assert len(selection["candidates"]) == len(model_module.MODEL_CANDIDATES)


def test_validation_rejects_model_when_out_of_sample_rank_ic_is_negative():
    fit_frame = _predictive_frame(dates=10)
    validation_frame = _predictive_frame(dates=4)
    validation_frame["model_target_pct"] *= -1.0

    selection = model_module.select_factor_model(fit_frame, validation_frame)

    assert selection["validation_accepted"] is False
    assert selection["validation_rejection_reason"] == "validation_rank_ic_below_minimum"
    assert selection["validation_diagnostics"]["rank_ic"] < 0.0


def test_frozen_model_scores_are_finite_aligned_and_ignore_held_out_labels():
    training = _predictive_frame()
    held_out = _predictive_frame(dates=2).copy()
    held_out.index = pd.Index(range(1000, 1020), name="row_id")

    first_model = fit_factor_model(training, max_factors=2)
    first_scores = score_factor_model(held_out, first_model)
    first_bytes = json.dumps(first_model, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    held_out["model_target_pct"] = np.linspace(1000.0, -1000.0, len(held_out))
    held_out["ret_5"] = held_out.groupby("trade_date")["ret_5"].transform(lambda values: values * 1000.0 + 123.0)
    held_out["volatility_10"] = held_out.groupby("trade_date")["volatility_10"].transform(
        lambda values: values * 7.0 - 50.0
    )

    second_model = fit_factor_model(training, max_factors=2)
    second_scores = score_factor_model(held_out, second_model)
    second_bytes = json.dumps(second_model, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    assert first_scores.index.equals(held_out.index)
    assert np.isfinite(first_scores.to_numpy()).all()
    pdt.assert_series_equal(first_scores, second_scores)
    assert first_bytes == second_bytes


def test_factor_diagnostics_reports_daily_rank_ic_and_five_return_groups():
    frame = pd.DataFrame(
        {
            "trade_date": np.repeat([pd.Timestamp("2026-03-02"), pd.Timestamp("2026-03-03")], 5),
            "model_target_pct": [-2.0, -1.0, 0.0, 1.0, 2.0] * 2,
        },
        index=pd.Index(range(200, 210), name="row_id"),
    )
    scores = pd.Series(
        [-2.0, -1.0, 0.0, 1.0, 2.0] * 2,
        index=frame.index,
        name="factor_score",
    )

    diagnostics = factor_diagnostics(frame, scores, "model_target_pct")

    assert diagnostics["rank_ic"] == 1.0
    assert diagnostics["quantile_returns"] == {
        "1": -2.0,
        "2": -1.0,
        "3": 0.0,
        "4": 1.0,
        "5": 2.0,
    }
    assert diagnostics["top_bottom_spread_pct"] == 4.0
    assert diagnostics["sample_count"] == 10


def test_selection_turnover_averages_consecutive_top_k_changes():
    picks_by_date = [
        ["000001.SZ", "000002.SZ", "000009.SZ"],
        ["000002.SZ", "000003.SZ", "000008.SZ"],
        ["000003.SZ", "000004.SZ", "000007.SZ"],
    ]

    assert selection_turnover(picks_by_date, top_k=2) == 0.5


def test_sparse_training_falls_back_to_relay_score():
    frame = pd.DataFrame(
        {
            "trade_date": [
                pd.Timestamp("2026-04-01"),
                pd.Timestamp("2026-04-02"),
                pd.Timestamp("2026-04-03"),
            ],
            "ret_5": [1.0, 1.0, 1.0],
            "model_target_pct": [0.5, -0.5, 0.25],
            "relay_score": [0.25, np.nan, 0.75],
        },
        index=pd.Index([11, 13, 17], name="row_id"),
    )

    model = fit_factor_model(frame)
    scores = score_factor_model(frame, model)

    assert model["fallback"] is True
    assert model["fallback_reason"]
    assert model["selected_factors"] == []
    assert model["coefficients"] == {}
    assert model["feature_means"] == {}
    assert model["feature_scales"] == {}
    pdt.assert_series_equal(
        scores,
        pd.Series([0.25, np.nan, 0.75], index=frame.index, name="factor_score"),
    )
