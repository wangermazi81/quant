from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd


CANDIDATE_FACTORS = [
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

MODEL_VERSION = "relay_factor_mining_v4_fixed_ridge"
MIN_REAL_AMOUNT_COVERAGE = 0.98
MAX_ABS_FACTOR_CORRELATION = 0.98
MIN_VALIDATION_SAMPLE_COUNT = 10
MIN_VALIDATION_RANK_IC = 0.01
FIXED_MODEL_PARAMETERS = {"max_factors": 6, "ridge_alpha": 5.0}
MODEL_SPEC_PROTOCOL_VERSION = "relay_fixed_ridge_preregistered_v3"
MODEL_SPEC_CALIBRATED_THROUGH = "2026-07-30"
MODEL_CANDIDATES = (FIXED_MODEL_PARAMETERS,)
_DATE_COLUMNS = ("trade_date", "signal_date", "date")
_EPSILON = np.finfo(float).eps

FROZEN_MODEL_PAYLOAD_KEYS = (
    "model_version",
    "fallback",
    "fallback_reason",
    "selected_factors",
    "coefficients",
    "normalized_weights",
    "feature_means",
    "feature_scales",
    "target_col",
    "ridge_alpha",
    "amount_data_coverage",
    "strategy_key",
    "training_scope",
    "selection_source",
    "selected_parameters",
    "model_spec_protocol_version",
    "model_spec_calibrated_through",
    "validation_accepted",
    "validation_rejection_reason",
    "validation_stable",
    "validation_stability_reason",
    "selected_market_gate",
    "fit_start",
    "fit_end",
    "fit_sample_count",
    "fit_label_max_exit",
    "validation_start",
    "validation_end",
    "validation_sample_count",
    "refit_start",
    "refit_end",
    "refit_sample_count",
    "refit_label_max_exit",
    "refit_policy",
    "frozen_model_candidate_eligible",
    "frozen_model_candidate_reason",
)


def _numeric_values(frame: pd.DataFrame, column: str) -> np.ndarray:
    values = pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)
    values[~np.isfinite(values)] = np.nan
    return values


def _date_values(frame: pd.DataFrame) -> np.ndarray:
    for column in _DATE_COLUMNS:
        if column in frame.columns:
            return frame[column].to_numpy()

    if isinstance(frame.index, pd.MultiIndex):
        for level_name in _DATE_COLUMNS:
            if level_name in frame.index.names:
                return frame.index.get_level_values(level_name).to_numpy()

    if isinstance(frame.index, pd.DatetimeIndex) or frame.index.name in _DATE_COLUMNS:
        return frame.index.to_numpy()

    # A frame without a date column is treated as one cross-section.
    return np.zeros(len(frame), dtype=np.int8)


def _percentile_by_date(values: np.ndarray, dates: np.ndarray, method: str = "average") -> np.ndarray:
    ranked = (
        pd.DataFrame({"date": dates, "value": values})
        .groupby("date", sort=False, dropna=False)["value"]
        .rank(method=method, pct=True)
    )
    return ranked.to_numpy(dtype=float)


def _spearman(left: np.ndarray, right: np.ndarray) -> float | None:
    valid = np.isfinite(left) & np.isfinite(right)
    if valid.sum() < 2:
        return None

    left_rank = pd.Series(left[valid]).rank(method="average").to_numpy(dtype=float)
    right_rank = pd.Series(right[valid]).rank(method="average").to_numpy(dtype=float)
    left_rank -= left_rank.mean()
    right_rank -= right_rank.mean()
    denominator = float(np.sqrt(np.dot(left_rank, left_rank) * np.dot(right_rank, right_rank)))
    if denominator <= _EPSILON:
        return None

    correlation = float(np.dot(left_rank, right_rank) / denominator)
    if np.isclose(abs(correlation), 1.0, rtol=0.0, atol=1e-12):
        return 1.0 if correlation > 0.0 else -1.0
    return float(np.clip(correlation, -1.0, 1.0))


def _mean_daily_spearman(dates: np.ndarray, left: np.ndarray, right: np.ndarray) -> float | None:
    grouped = pd.DataFrame({"date": dates, "left": left, "right": right}).groupby("date", sort=False, dropna=False)
    daily_ics = []
    for _, group in grouped:
        daily_ic = _spearman(
            group["left"].to_numpy(dtype=float),
            group["right"].to_numpy(dtype=float),
        )
        if daily_ic is not None:
            daily_ics.append(daily_ic)

    if not daily_ics:
        return None
    mean_ic = float(np.mean(daily_ics))
    if np.isclose(abs(mean_ic), 1.0, rtol=0.0, atol=1e-12):
        return 1.0 if mean_ic > 0.0 else -1.0
    return mean_ic


def _fallback_model(
    target_col: str,
    ridge_alpha: float,
    reason: str,
    training_ic: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    return {
        "selected_factors": [],
        "coefficients": {},
        "normalized_weights": {},
        "feature_means": {},
        "feature_scales": {},
        "training_ic": dict(training_ic or {}),
        "dropped_correlated_factors": {},
        "target_col": target_col,
        "ridge_alpha": float(ridge_alpha),
        "model_version": MODEL_VERSION,
        "fallback": True,
        "fallback_reason": reason,
    }


def _prepare_factor_training(frame: pd.DataFrame, target_col: str) -> dict[str, Any]:
    if frame.empty:
        return {"error": "empty_training_frame", "training_ic": {}}
    if target_col not in frame.columns:
        return {"error": f"missing_target:{target_col}", "training_ic": {}}

    available_factors = [factor for factor in CANDIDATE_FACTORS if factor in frame.columns]
    amount_data_coverage = None
    real_amount_mask: np.ndarray | None = None
    if "amount_data_real" in frame.columns:
        real_amount = frame["amount_data_real"].map(
            lambda value: value is True
            or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "y"})
        )
        real_amount_mask = real_amount.to_numpy(dtype=bool)
        amount_data_coverage = float(real_amount.mean()) if len(real_amount) else 0.0
        if amount_data_coverage < MIN_REAL_AMOUNT_COVERAGE:
            available_factors = [factor for factor in available_factors if factor != "amount_ratio_20"]
    if not available_factors:
        return {
            "error": "missing_candidate_factors",
            "training_ic": {},
            "amount_data_coverage": amount_data_coverage,
        }

    dates = _date_values(frame)
    target = _numeric_values(frame, target_col)
    if np.isfinite(target).sum() < 2:
        return {
            "error": "insufficient_finite_targets",
            "training_ic": {},
            "amount_data_coverage": amount_data_coverage,
        }

    ranked_features: dict[str, np.ndarray] = {}
    training_ic: dict[str, float] = {}
    for factor in available_factors:
        factor_values = _numeric_values(frame, factor)
        if factor == "amount_ratio_20" and real_amount_mask is not None:
            factor_values = np.where(real_amount_mask, factor_values, np.nan)
        ranked = _percentile_by_date(factor_values, dates)
        factor_ic = _mean_daily_spearman(dates, ranked, target)
        if factor_ic is None:
            continue
        ranked_features[factor] = ranked
        training_ic[factor] = factor_ic
    if not training_ic:
        return {
            "error": "no_computable_factor_ic",
            "training_ic": {},
            "amount_data_coverage": amount_data_coverage,
        }
    return {
        "error": None,
        "target": target,
        "ranked_features": ranked_features,
        "training_ic": training_ic,
        "amount_data_coverage": amount_data_coverage,
    }


def fit_factor_model(
    frame: pd.DataFrame,
    target_col: str = "model_target_pct",
    max_factors: int = 6,
    ridge_alpha: float = 5.0,
    *,
    _prepared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fit a deterministic ridge model using only the supplied training frame."""
    try:
        factor_limit = int(max_factors)
        alpha = float(ridge_alpha)
    except (TypeError, ValueError):
        return _fallback_model(target_col, 0.0, "invalid_model_parameters")

    if factor_limit <= 0:
        return _fallback_model(target_col, alpha, "max_factors_must_be_positive")
    if not np.isfinite(alpha) or alpha < 0.0:
        return _fallback_model(target_col, alpha, "ridge_alpha_must_be_non_negative")
    prepared = _prepared if isinstance(_prepared, dict) else _prepare_factor_training(frame, target_col)
    training_ic = dict(prepared.get("training_ic") or {})
    if prepared.get("error"):
        return _fallback_model(target_col, alpha, str(prepared["error"]), training_ic)
    target = prepared["target"]
    ranked_features = prepared["ranked_features"]
    amount_data_coverage = prepared.get("amount_data_coverage")

    factor_order = {factor: position for position, factor in enumerate(CANDIDATE_FACTORS)}
    ranked_candidates = sorted(
        training_ic,
        key=lambda factor: (-abs(training_ic[factor]), factor_order[factor]),
    )
    selected: list[str] = []
    dropped_correlated_factors: dict[str, str] = {}
    target_mask = np.isfinite(target)
    for factor in ranked_candidates:
        factor_values = np.where(target_mask, ranked_features[factor], np.nan)
        correlated_with = None
        for retained in selected:
            retained_values = np.where(target_mask, ranked_features[retained], np.nan)
            correlation = _spearman(factor_values, retained_values)
            if correlation is not None and abs(correlation) >= MAX_ABS_FACTOR_CORRELATION:
                correlated_with = retained
                break
        if correlated_with is not None:
            dropped_correlated_factors[factor] = correlated_with
            continue
        selected.append(factor)
        if len(selected) >= factor_limit:
            break

    usable_factors = []
    feature_means: dict[str, float] = {}
    feature_scales: dict[str, float] = {}
    feature_columns = []
    for factor in selected:
        ranked = ranked_features[factor]
        finite = target_mask & np.isfinite(ranked)
        if finite.sum() < 2:
            continue
        mean = float(np.mean(ranked[finite]))
        scale = float(np.std(ranked[finite], ddof=0))
        if not np.isfinite(scale) or scale <= _EPSILON:
            continue

        filled = np.where(np.isfinite(ranked[target_mask]), ranked[target_mask], mean)
        feature_columns.append((filled - mean) / scale)
        usable_factors.append(factor)
        feature_means[factor] = mean
        feature_scales[factor] = scale

    if not usable_factors:
        return _fallback_model(target_col, alpha, "no_variable_selected_factors", training_ic)

    design = np.column_stack(feature_columns)
    response = target[target_mask]
    sample_count = float(len(response))
    regularized_gram = design.T @ design / sample_count + alpha * np.eye(len(usable_factors), dtype=float)
    coefficients_array = np.linalg.pinv(regularized_gram) @ (design.T @ response / sample_count)
    if not np.isfinite(coefficients_array).all():
        return _fallback_model(target_col, alpha, "non_finite_ridge_coefficients", training_ic)

    coefficient_norm = float(np.abs(coefficients_array).sum())
    if coefficient_norm <= _EPSILON:
        return _fallback_model(target_col, alpha, "zero_ridge_weights", training_ic)

    coefficients = {
        factor: float(coefficient) for factor, coefficient in zip(usable_factors, coefficients_array, strict=True)
    }
    normalized_weights = {factor: float(coefficient / coefficient_norm) for factor, coefficient in coefficients.items()}

    return {
        "selected_factors": usable_factors,
        "coefficients": coefficients,
        "normalized_weights": normalized_weights,
        "feature_means": feature_means,
        "feature_scales": feature_scales,
        "training_ic": training_ic,
        "dropped_correlated_factors": dropped_correlated_factors,
        "target_col": target_col,
        "ridge_alpha": alpha,
        "model_version": MODEL_VERSION,
        "amount_data_coverage": None if amount_data_coverage is None else round(amount_data_coverage, 4),
        "fallback": False,
        "fallback_reason": None,
    }


def _relay_scores(frame: pd.DataFrame) -> pd.Series:
    if "relay_score" not in frame.columns:
        values = np.zeros(len(frame), dtype=float)
    else:
        values = pd.to_numeric(frame["relay_score"], errors="coerce").to_numpy(dtype=float, copy=True)
    return pd.Series(values, index=frame.index, name="factor_score", dtype=float)


def factor_model_payload(model: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(model, Mapping):
        raise TypeError("factor model must be an object")
    return {
        key: model[key]
        for key in FROZEN_MODEL_PAYLOAD_KEYS
        if key in model
    }


def factor_model_digest(model: Mapping[str, Any]) -> str:
    payload = factor_model_payload(model)
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=lambda value: value.item(),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _strict_model_error(
    frame: pd.DataFrame,
    model: Mapping[str, Any],
) -> str | None:
    selected = model.get("selected_factors")
    if (
        model.get("fallback") is not False
        or not isinstance(selected, (list, tuple))
        or not selected
        or len(set(selected)) != len(selected)
        or any(factor not in CANDIDATE_FACTORS for factor in selected)
    ):
        return "frozen factor model is incomplete"
    missing_columns = [factor for factor in selected if factor not in frame.columns]
    if missing_columns:
        return "missing frozen factor columns: " + ",".join(missing_columns)
    for field in ("coefficients", "feature_means", "feature_scales"):
        values = model.get(field)
        if not isinstance(values, Mapping) or any(
            factor not in values for factor in selected
        ):
            return "frozen factor model is incomplete"
    for factor in selected:
        try:
            coefficient = float(model["coefficients"][factor])
            mean = float(model["feature_means"][factor])
            scale = float(model["feature_scales"][factor])
        except (TypeError, ValueError):
            return "frozen factor model contains invalid parameters"
        if (
            not np.isfinite(coefficient)
            or not np.isfinite(mean)
            or not np.isfinite(scale)
            or scale <= _EPSILON
        ):
            return "frozen factor model contains invalid parameters"
    return None


def score_factor_model(
    frame: pd.DataFrame,
    model: dict,
    *,
    strict: bool = False,
) -> pd.Series:
    """Apply a frozen model without consulting labels in the scoring frame."""
    if strict:
        error = _strict_model_error(frame, model)
        if error:
            raise ValueError(error)
    selected = list(model.get("selected_factors") or [])
    if model.get("fallback", False) or not selected:
        return _relay_scores(frame)
    if any(factor not in frame.columns for factor in selected):
        return _relay_scores(frame)

    means = model.get("feature_means") or {}
    scales = model.get("feature_scales") or {}
    coefficients = model.get("coefficients") or {}
    if any(factor not in means or factor not in scales or factor not in coefficients for factor in selected):
        return _relay_scores(frame)

    dates = _date_values(frame)
    real_amount_mask: np.ndarray | None = None
    if "amount_data_real" in frame.columns:
        real_amount_mask = frame["amount_data_real"].map(
            lambda value: value is True
            or (
                isinstance(value, str)
                and value.strip().lower() in {"1", "true", "yes", "y"}
            )
        ).to_numpy(dtype=bool)
    scores = np.zeros(len(frame), dtype=float)
    for factor in selected:
        mean = float(means[factor])
        scale = float(scales[factor])
        coefficient = float(coefficients[factor])
        if not np.isfinite(mean) or not np.isfinite(scale) or scale <= _EPSILON or not np.isfinite(coefficient):
            return _relay_scores(frame)

        factor_values = _numeric_values(frame, factor)
        if factor == "amount_ratio_20" and real_amount_mask is not None:
            factor_values = np.where(real_amount_mask, factor_values, np.nan)
        ranked = _percentile_by_date(factor_values, dates)
        ranked = np.where(np.isfinite(ranked), ranked, mean)
        scores += ((ranked - mean) / scale) * coefficient

    if not np.isfinite(scores).all():
        return _relay_scores(frame)
    return pd.Series(scores, index=frame.index, name="factor_score", dtype=float)


def factor_diagnostics(frame: pd.DataFrame, scores: pd.Series, target_col: str) -> dict[str, Any]:
    """Report daily rank IC and daily score-quintile target returns."""
    empty_groups = {str(group): None for group in range(1, 6)}
    if target_col not in frame.columns or frame.empty:
        return {
            "rank_ic": None,
            "quantile_returns": empty_groups,
            "top_bottom_spread_pct": None,
            "sample_count": 0,
        }

    if scores.index.equals(frame.index):
        score_values = pd.to_numeric(scores, errors="coerce").to_numpy(dtype=float)
    else:
        score_values = pd.to_numeric(scores.reindex(frame.index), errors="coerce").to_numpy(dtype=float)
    score_values[~np.isfinite(score_values)] = np.nan

    target = _numeric_values(frame, target_col)
    dates = _date_values(frame)
    valid = np.isfinite(score_values) & np.isfinite(target)
    sample_count = int(valid.sum())
    rank_ic = _mean_daily_spearman(dates, score_values, target)

    quantile_returns = empty_groups.copy()
    if sample_count:
        percentile_scores = _percentile_by_date(score_values, dates, method="first")
        quantiles = np.ceil(percentile_scores * 5.0)
        quantiles = np.clip(quantiles, 1.0, 5.0)
        for group in range(1, 6):
            group_mask = valid & (quantiles == float(group))
            if group_mask.any():
                quantile_returns[str(group)] = float(np.mean(target[group_mask]))

    bottom = quantile_returns["1"]
    top = quantile_returns["5"]
    spread = None if bottom is None or top is None else float(top - bottom)
    return {
        "rank_ic": rank_ic,
        "quantile_returns": quantile_returns,
        "top_bottom_spread_pct": spread,
        "sample_count": sample_count,
    }


def select_factor_model(
    fit_frame: pd.DataFrame,
    validation_frame: pd.DataFrame,
    target_col: str = "model_target_pct",
) -> dict[str, Any]:
    """Fit the preregistered model and report chronological validation stability."""
    parameters = dict(FIXED_MODEL_PARAMETERS)
    prepared = _prepare_factor_training(fit_frame, target_col)
    model = fit_factor_model(
        fit_frame,
        target_col=target_col,
        max_factors=parameters["max_factors"],
        ridge_alpha=parameters["ridge_alpha"],
        _prepared=prepared,
    )
    scores = score_factor_model(validation_frame, model)
    diagnostics = factor_diagnostics(validation_frame, scores, target_col=target_col)
    sample_count = int(diagnostics.get("sample_count") or 0)
    rank_ic = diagnostics.get("rank_ic")
    top_return = (diagnostics.get("quantile_returns") or {}).get("5")
    if model.get("fallback"):
        stable = False
        stability_reason = "validation_model_fallback"
    elif sample_count < MIN_VALIDATION_SAMPLE_COUNT:
        stable = False
        stability_reason = "insufficient_validation_samples"
    elif rank_ic is None or not np.isfinite(rank_ic) or float(rank_ic) < MIN_VALIDATION_RANK_IC:
        stable = False
        stability_reason = "validation_rank_ic_below_minimum"
    elif top_return is None or not np.isfinite(top_return) or float(top_return) <= 0.0:
        stable = False
        stability_reason = "validation_top_quantile_non_positive"
    else:
        stable = True
        stability_reason = None

    return {
        "selection_source": "fixed_preregistered_parameters",
        "selected_parameters": parameters,
        "model_spec_protocol_version": MODEL_SPEC_PROTOCOL_VERSION,
        "model_spec_calibrated_through": MODEL_SPEC_CALIBRATED_THROUGH,
        "validation_accepted": stable,
        "validation_rejection_reason": stability_reason,
        "validation_stable": stable,
        "validation_stability_reason": stability_reason,
        "validation_diagnostics": diagnostics,
        "model": model,
        "candidates": [
            {
                "parameters": parameters,
                "fallback": bool(model.get("fallback")),
                "selected_factors": list(model.get("selected_factors") or []),
                "validation_diagnostics": diagnostics,
            }
        ],
    }


def selection_turnover(picks_by_date: Sequence[Sequence[str]], top_k: int) -> float:
    """Average one-way constituent turnover across consecutive pick dates."""
    try:
        selection_size = int(top_k)
    except (TypeError, ValueError):
        return 0.0
    if selection_size <= 0:
        return 0.0

    pick_groups = list(picks_by_date.values()) if isinstance(picks_by_date, Mapping) else list(picks_by_date)
    if len(pick_groups) < 2:
        return 0.0

    top_sets = []
    for picks in pick_groups:
        unique_picks = list(dict.fromkeys(picks or ()))[:selection_size]
        top_sets.append(set(unique_picks))

    turnovers = [1.0 - len(previous & current) / selection_size for previous, current in zip(top_sets, top_sets[1:])]
    return float(np.mean(turnovers))
