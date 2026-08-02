from __future__ import annotations

import ctypes
import gc
import hashlib
import json
import math
import os
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


BASE_FACTOR_FORMULAS: dict[str, str] = {
    "return_1": "close_t / close_t-1 - 1",
    "return_3": "close_t / close_t-3 - 1",
    "return_5": "close_t / close_t-5 - 1",
    "return_10": "close_t / close_t-10 - 1",
    "return_20": "close_t / close_t-20 - 1",
    "gap_open_1": "open_t / close_t-1 - 1",
    "intraday_absorption_1": "close_t / open_t - 1",
    "close_location_1": "2 * (close_t - low_t) / (high_t - low_t) - 1",
    "upper_shadow_1": "(high_t - max(open_t, close_t)) / close_t-1",
    "lower_shadow_1": "(min(open_t, close_t) - low_t) / close_t-1",
    "range_pct_1": "(high_t - low_t) / close_t-1",
    "trend_efficiency_5": "(close_t / close_t-5 - 1) / sum(abs(ret_1), 5)",
    "trend_consistency_5": "mean(sign(ret_1), 5)",
    "volatility_compression_5_20": "std(ret_1, 5) / std(ret_1, 20)",
    "volume_dryup_5_20": "mean(volume, 5) / mean(volume, 20)",
    "amount_shock_1_20": "amount_t / median(amount, 20)",
    "amihud_impact_5": "mean(abs(ret_1) / amount, 5)",
    "breakout_quality_20": "(close_t / max(high, 20)) * amount_shock_1_20",
    "pullback_dryup_3_20": "-ret_3 * (1 - volume_dryup_5_20)",
    "gap_absorption_1": "intraday_absorption_1 - gap_open_1",
    "momentum_acceleration_3_20": "return_3 - 3 / 20 * return_20",
    "distance_high_20": "close_t / max(high, 20) - 1",
    "distance_low_20": "close_t / min(low, 20) - 1",
    "range_close_strength_1": "range_pct_1 * close_location_1",
    "gap_hold_1": "gap_open_1 * close_location_1",
    "panic_recovery_1": (
        "max(-gap_open_1, 0) * max(intraday_absorption_1, 0) "
        "* amount_shock_1_20"
    ),
    "liquidity_reversal_3_20": "-return_3 * amount_shock_1_20",
    "price_volume_divergence_5_20": (
        "return_5 - log(mean(volume, 5) / mean(volume, 20))"
    ),
    "quiet_breakout_20": (
        "distance_high_20 / volatility_compression_5_20"
    ),
    "return_to_risk_5_20": "return_5 / std(return_1, 20)",
    "limit_proximity_1": "return_1 / point_in_time_limit_rate",
    "limit_close_strength_1": (
        "limit_proximity_1 * (close_location_1 + 1) / 2 "
        "* amount_shock_1_20"
    ),
    "close_flow_3": (
        "sum(close_location_1 * amount, 3) / sum(amount, 3)"
    ),
    "close_flow_acceleration_3_10": (
        "close_flow_3 - sum(close_location_1 * amount, 10) "
        "/ sum(amount, 10)"
    ),
    "signed_amount_flow_5": (
        "sum(sign(return_1) * amount, 5) / sum(amount, 5)"
    ),
    "amount_impulse_decay_1_5": (
        "amount_shock_1_20 - mean(lag(amount_shock_1_20, 1), 5)"
    ),
    "range_regime_shift_3_20": (
        "mean(range_pct_1, 3) / mean(range_pct_1, 20)"
    ),
    "overnight_intraday_divergence_3": (
        "mean(intraday_absorption_1 - gap_open_1, 3)"
    ),
    "return_autocorrelation_10": "corr(return_1, lag(return_1, 1), 10)",
    "realized_skew_10": "skew(return_1, 10)",
    "idiosyncratic_momentum_5_20": (
        "return_5 - beta_20(return_1, market_return_1) "
        "* compound(market_return_1, 5)"
    ),
    "downside_resilience_10": (
        "mean(return_1 - market_return_1, 10) "
        "where market_return_1 < 0"
    ),
    "volume_price_elasticity_10": (
        "cov(return_1, delta(log(volume)), 10) "
        "/ var(delta(log(volume)), 10)"
    ),
    "illiquidity_regime_5_20": (
        "mean(abs(return_1) / amount, 5) "
        "/ mean(abs(return_1) / amount, 20)"
    ),
    "volume_concentration_5": "max(volume, 5) / sum(volume, 5)",
    "limit_pressure_persistence_3": (
        "mean(max(limit_proximity_1, 0) "
        "* (close_location_1 + 1) / 2, 3)"
    ),
}

AUCTION_FACTOR_FORMULAS: dict[str, str] = {
    "auction_gap": "auction_price / pre_close - 1",
    "auction_turnover_rate": "auction_turnover_rate",
    "auction_volume_ratio": "auction_volume_ratio",
    "auction_amount": "auction_amount",
}

DAILY_BASIC_FACTOR_FORMULAS: dict[str, str] = {
    "turnover_rate_1": "daily_basic.turnover_rate",
    "turnover_acceleration_1_5": (
        "turnover_rate_t / mean(turnover_rate, 5)"
    ),
    "turnover_free_spread_1": "turnover_rate_f - turnover_rate",
    "volume_ratio_basic_1": "daily_basic.volume_ratio",
    "log_circ_mv_1": "log(daily_basic.circ_mv)",
    "float_share_ratio_1": "float_share / total_share",
    "liquidity_crowding_1": "turnover_rate * volume_ratio",
}
STRUCTURAL_FACTOR_FORMULAS: dict[str, str] = {
    "turnover_limit_pressure": (
        "rank(turnover_acceleration_1_5) "
        "* rank(limit_close_strength_1)"
    ),
    "turnover_panic_recovery": (
        "rank(turnover_acceleration_1_5) * rank(panic_recovery_1)"
    ),
    "quiet_turnover_breakout": (
        "rank(turnover_acceleration_1_5) * rank(-range_pct_1) "
        "* rank(distance_high_20)"
    ),
    "strong_stock_dryup_pullback": (
        "rank(return_20) * rank(-return_3) "
        "* rank(-volume_dryup_5_20)"
    ),
    "liquidity_close_confirmation": (
        "rank(turnover_rate_1) * rank(close_location_1) "
        "* rank(amount_shock_1_20)"
    ),
    "compression_limit_pressure": (
        "rank(-volatility_compression_5_20) "
        "* rank(limit_proximity_1) * rank(amount_shock_1_20)"
    ),
    "flow_breakout_confirmation": (
        "rank(distance_high_20) * rank(close_flow_3) "
        "* rank(amount_shock_1_20)"
    ),
    "idiosyncratic_flow_momentum": (
        "rank(idiosyncratic_momentum_5_20) "
        "* rank(signed_amount_flow_5) * rank(close_flow_3)"
    ),
    "compressed_attention_release": (
        "rank(-range_regime_shift_3_20) "
        "* rank(amount_impulse_decay_1_5) * rank(close_flow_3)"
    ),
    "downside_resilience_accumulation": (
        "rank(downside_resilience_10) * rank(close_flow_3) "
        "* rank(-volume_concentration_5)"
    ),
    "persistent_limit_absorption": (
        "rank(limit_pressure_persistence_3) "
        "* rank(turnover_acceleration_1_5) * rank(-upper_shadow_1)"
    ),
    "liquidity_regime_breakout": (
        "rank(-illiquidity_regime_5_20) * rank(distance_high_20) "
        "* rank(amount_impulse_decay_1_5)"
    ),
    "intraday_leadership_shift": (
        "rank(overnight_intraday_divergence_3) "
        "* rank(idiosyncratic_momentum_5_20) * rank(close_flow_3)"
    ),
    "serial_flow_continuation": (
        "rank(return_autocorrelation_10) "
        "* rank(signed_amount_flow_5) "
        "* rank(close_flow_acceleration_3_10)"
    ),
}
_STRUCTURAL_FACTOR_INPUTS: dict[
    str,
    tuple[tuple[str, float], ...],
] = {
    "turnover_limit_pressure": (
        ("turnover_acceleration_1_5", 1.0),
        ("limit_close_strength_1", 1.0),
    ),
    "turnover_panic_recovery": (
        ("turnover_acceleration_1_5", 1.0),
        ("panic_recovery_1", 1.0),
    ),
    "quiet_turnover_breakout": (
        ("turnover_acceleration_1_5", 1.0),
        ("range_pct_1", -1.0),
        ("distance_high_20", 1.0),
    ),
    "strong_stock_dryup_pullback": (
        ("return_20", 1.0),
        ("return_3", -1.0),
        ("volume_dryup_5_20", -1.0),
    ),
    "liquidity_close_confirmation": (
        ("turnover_rate_1", 1.0),
        ("close_location_1", 1.0),
        ("amount_shock_1_20", 1.0),
    ),
    "compression_limit_pressure": (
        ("volatility_compression_5_20", -1.0),
        ("limit_proximity_1", 1.0),
        ("amount_shock_1_20", 1.0),
    ),
    "flow_breakout_confirmation": (
        ("distance_high_20", 1.0),
        ("close_flow_3", 1.0),
        ("amount_shock_1_20", 1.0),
    ),
    "idiosyncratic_flow_momentum": (
        ("idiosyncratic_momentum_5_20", 1.0),
        ("signed_amount_flow_5", 1.0),
        ("close_flow_3", 1.0),
    ),
    "compressed_attention_release": (
        ("range_regime_shift_3_20", -1.0),
        ("amount_impulse_decay_1_5", 1.0),
        ("close_flow_3", 1.0),
    ),
    "downside_resilience_accumulation": (
        ("downside_resilience_10", 1.0),
        ("close_flow_3", 1.0),
        ("volume_concentration_5", -1.0),
    ),
    "persistent_limit_absorption": (
        ("limit_pressure_persistence_3", 1.0),
        ("turnover_acceleration_1_5", 1.0),
        ("upper_shadow_1", -1.0),
    ),
    "liquidity_regime_breakout": (
        ("illiquidity_regime_5_20", -1.0),
        ("distance_high_20", 1.0),
        ("amount_impulse_decay_1_5", 1.0),
    ),
    "intraday_leadership_shift": (
        ("overnight_intraday_divergence_3", 1.0),
        ("idiosyncratic_momentum_5_20", 1.0),
        ("close_flow_3", 1.0),
    ),
    "serial_flow_continuation": (
        ("return_autocorrelation_10", 1.0),
        ("signed_amount_flow_5", 1.0),
        ("close_flow_acceleration_3_10", 1.0),
    ),
}
CURATED_INTERACTION_PAIRS = (
    ("auction_gap", "close_flow_3"),
    ("auction_gap", "limit_pressure_persistence_3"),
    ("auction_gap", "idiosyncratic_momentum_5_20"),
    ("auction_turnover_rate", "amount_shock_1_20"),
    ("auction_turnover_rate", "limit_proximity_1"),
    ("auction_volume_ratio", "signed_amount_flow_5"),
    ("auction_volume_ratio", "distance_high_20"),
    ("auction_amount", "turnover_acceleration_1_5"),
    ("return_1", "amount_shock_1_20"),
    ("return_3", "volume_dryup_5_20"),
    ("return_5", "close_flow_3"),
    ("return_20", "idiosyncratic_momentum_5_20"),
    ("gap_open_1", "intraday_absorption_1"),
    ("close_location_1", "amount_shock_1_20"),
    ("close_flow_3", "amount_impulse_decay_1_5"),
    ("close_flow_acceleration_3_10", "range_regime_shift_3_20"),
    ("signed_amount_flow_5", "idiosyncratic_momentum_5_20"),
    ("downside_resilience_10", "amount_shock_1_20"),
    ("distance_high_20", "range_regime_shift_3_20"),
    ("distance_high_20", "volume_concentration_5"),
    ("limit_proximity_1", "turnover_acceleration_1_5"),
    ("limit_pressure_persistence_3", "turnover_acceleration_1_5"),
    ("upper_shadow_1", "amount_shock_1_20"),
    ("lower_shadow_1", "amount_shock_1_20"),
    ("return_autocorrelation_10", "signed_amount_flow_5"),
    ("realized_skew_10", "amount_shock_1_20"),
    ("volume_price_elasticity_10", "return_5"),
    ("illiquidity_regime_5_20", "amount_impulse_decay_1_5"),
    ("trend_efficiency_5", "close_flow_3"),
    ("volatility_compression_5_20", "amount_impulse_decay_1_5"),
    ("overnight_intraday_divergence_3", "close_flow_3"),
    ("panic_recovery_1", "turnover_acceleration_1_5"),
    ("quiet_breakout_20", "close_flow_3"),
    ("liquidity_reversal_3_20", "close_flow_3"),
    ("price_volume_divergence_5_20", "signed_amount_flow_5"),
    ("turnover_free_spread_1", "idiosyncratic_momentum_5_20"),
    ("volume_ratio_basic_1", "close_flow_3"),
    ("float_share_ratio_1", "turnover_rate_1"),
)
AUXILIARY_MIN_CODE_COVERAGE = 0.80
AUCTION_MIN_ALIGNMENT_COVERAGE = 0.80
LABEL_CODE_CHUNK_SIZE = 160
MARKET_REGIME_FEATURES = (
    "market_return_1",
    "market_return_5",
    "market_return_20",
    "market_breadth",
    "market_tail_up",
    "market_tail_down",
    "market_dispersion",
    "market_range",
    "market_amount_shock",
    "market_volatility_compression",
)
MARKET_REGIME_SOURCE_COLUMNS = (
    "return_1",
    "return_5",
    "return_20",
    "range_pct_1",
    "amount_shock_1_20",
    "volatility_compression_5_20",
)


@dataclass(frozen=True)
class WalkForwardWindow:
    window_id: int
    train_dates: tuple[pd.Timestamp, ...]
    test_dates: tuple[pd.Timestamp, ...]
    purged_dates: tuple[pd.Timestamp, ...]


def _float_frame(value: pd.DataFrame) -> pd.DataFrame:
    frame = value.copy()
    frame.index = pd.to_datetime(frame.index).normalize()
    frame.columns = [str(column) for column in frame.columns]
    return frame.apply(pd.to_numeric, errors="coerce").astype(float)


def _factor_storage_frame(value: pd.DataFrame) -> pd.DataFrame:
    frame = value.copy(deep=False)
    frame.index = pd.to_datetime(frame.index).normalize()
    frame.columns = [str(column) for column in frame.columns]
    if not all(
        pd.api.types.is_numeric_dtype(dtype)
        for dtype in frame.dtypes
    ):
        frame = frame.apply(pd.to_numeric, errors="coerce")
    return frame.astype(np.float32, copy=False)


def _code_key_series(values: pd.Series) -> pd.Series:
    text = (
        values.astype("string")
        .str.replace(r"\.0$", "", regex=True)
        .str.split(".", n=1)
        .str[0]
        .str.zfill(6)
    )
    numeric = pd.to_numeric(text, errors="coerce")
    if bool(numeric.notna().all()):
        return numeric.astype(np.int32)
    return text


def _safe_div(
    numerator: pd.DataFrame,
    denominator: pd.DataFrame,
) -> pd.DataFrame:
    result = numerator.div(denominator.where(denominator != 0))
    return result.replace([np.inf, -np.inf], np.nan)


def build_daily_candidate_factors(
    panel: Mapping[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    required = {"open", "high", "low", "close", "volume", "amount"}
    missing = sorted(required - set(panel))
    if missing:
        raise KeyError(f"daily factor panel missing fields: {missing}")

    frames = {
        key: _float_frame(panel[key])
        for key in required
    }
    index = frames["close"].index
    columns = frames["close"].columns
    for frame in frames.values():
        index = index.intersection(frame.index)
        columns = columns.intersection(frame.columns)
    if index.empty or columns.empty:
        return {
            name: pd.DataFrame(index=index, columns=columns, dtype=float)
            for name in BASE_FACTOR_FORMULAS
        }
    open_price = frames["open"].loc[index, columns].sort_index()
    high = frames["high"].loc[index, columns].sort_index()
    low = frames["low"].loc[index, columns].sort_index()
    close = frames["close"].loc[index, columns].sort_index()
    volume = frames["volume"].loc[index, columns].sort_index()
    amount = frames["amount"].loc[index, columns].sort_index()

    previous_close = close.shift(1)
    ret_1 = _safe_div(close, previous_close) - 1.0
    ret_3 = _safe_div(close, close.shift(3)) - 1.0
    ret_5 = _safe_div(close, close.shift(5)) - 1.0
    ret_10 = _safe_div(close, close.shift(10)) - 1.0
    ret_20 = _safe_div(close, close.shift(20)) - 1.0
    gap_open = _safe_div(open_price, previous_close) - 1.0
    intraday = _safe_div(close, open_price) - 1.0
    daily_range = (high - low).where(high > low)
    close_location = _safe_div(close - low, daily_range) * 2.0 - 1.0
    upper_shadow = _safe_div(
        high - pd.DataFrame(
            np.maximum(open_price.to_numpy(), close.to_numpy()),
            index=index,
            columns=columns,
        ),
        previous_close,
    )
    lower_shadow = _safe_div(
        pd.DataFrame(
            np.minimum(open_price.to_numpy(), close.to_numpy()),
            index=index,
            columns=columns,
        )
        - low,
        previous_close,
    )
    range_pct = _safe_div(high - low, previous_close)
    absolute_path_5 = ret_1.abs().rolling(5, min_periods=5).sum()
    trend_efficiency = _safe_div(
        _safe_div(close, close.shift(5)) - 1.0,
        absolute_path_5,
    )
    trend_consistency = (
        np.sign(ret_1)
        .rolling(5, min_periods=5)
        .mean()
    )
    volatility_5 = ret_1.rolling(5, min_periods=5).std(ddof=1)
    volatility_20 = ret_1.rolling(20, min_periods=10).std(ddof=1)
    volatility_compression = _safe_div(volatility_5, volatility_20)
    volume_dryup = _safe_div(
        volume.rolling(5, min_periods=5).mean(),
        volume.rolling(20, min_periods=10).mean(),
    )
    amount_shock = _safe_div(
        amount,
        amount.rolling(20, min_periods=10).median(),
    )
    amount_in_hundred_million = amount / 100_000_000.0
    daily_amihud = _safe_div(
        ret_1.abs(),
        amount_in_hundred_million,
    )
    amihud = daily_amihud.rolling(5, min_periods=5).mean()
    breakout_quality = (
        _safe_div(
            close,
            high.rolling(20, min_periods=10).max(),
        )
        * amount_shock
    )
    pullback_dryup = -ret_3 * (1.0 - volume_dryup)
    gap_absorption = intraday - gap_open
    momentum_acceleration = ret_3 - ret_20 * (3.0 / 20.0)
    rolling_high_20 = high.rolling(20, min_periods=10).max()
    rolling_low_20 = low.rolling(20, min_periods=10).min()
    distance_high = _safe_div(close, rolling_high_20) - 1.0
    distance_low = _safe_div(close, rolling_low_20) - 1.0
    range_close_strength = range_pct * close_location
    gap_hold = gap_open * close_location
    panic_recovery = (
        (-gap_open).clip(lower=0.0)
        * intraday.clip(lower=0.0)
        * amount_shock
    )
    liquidity_reversal = -ret_3 * amount_shock
    price_volume_divergence = (
        ret_5
        - np.log(volume_dryup.where(volume_dryup > 0.0))
    )
    quiet_breakout = _safe_div(
        distance_high,
        volatility_compression,
    )
    return_to_risk = _safe_div(ret_5, volatility_20)
    close_amount_pressure = close_location * amount
    close_flow_3 = _safe_div(
        close_amount_pressure.rolling(3, min_periods=3).sum(),
        amount.rolling(3, min_periods=3).sum(),
    )
    close_flow_10 = _safe_div(
        close_amount_pressure.rolling(10, min_periods=7).sum(),
        amount.rolling(10, min_periods=7).sum(),
    )
    close_flow_acceleration = close_flow_3 - close_flow_10
    signed_amount_flow = _safe_div(
        (
            np.sign(ret_1) * amount
        ).rolling(5, min_periods=5).sum(),
        amount.rolling(5, min_periods=5).sum(),
    )
    amount_impulse_decay = (
        amount_shock
        - amount_shock.shift(1).rolling(5, min_periods=3).mean()
    )
    range_regime_shift = _safe_div(
        range_pct.rolling(3, min_periods=3).mean(),
        range_pct.rolling(20, min_periods=10).mean(),
    )
    overnight_intraday_divergence = (
        intraday - gap_open
    ).rolling(3, min_periods=3).mean()
    return_autocorrelation = ret_1.rolling(
        10,
        min_periods=8,
    ).corr(ret_1.shift(1))
    realized_skew = ret_1.rolling(
        10,
        min_periods=8,
    ).skew()
    market_return_1 = ret_1.median(axis=1, skipna=True)
    market_variance_20 = market_return_1.rolling(
        20,
        min_periods=10,
    ).var(ddof=1)
    market_beta_20 = (
        ret_1.rolling(20, min_periods=10)
        .cov(market_return_1)
        .div(market_variance_20, axis=0)
    )
    market_return_5 = (
        (1.0 + market_return_1)
        .rolling(5, min_periods=5)
        .apply(np.prod, raw=True)
        - 1.0
    )
    idiosyncratic_momentum = (
        ret_5 - market_beta_20.mul(market_return_5, axis=0)
    )
    market_excess_return = ret_1.sub(market_return_1, axis=0)
    downside_mask = pd.DataFrame(
        np.repeat(
            market_return_1.lt(0.0).to_numpy()[:, None],
            len(columns),
            axis=1,
        ),
        index=index,
        columns=columns,
    )
    downside_resilience = (
        market_excess_return.where(downside_mask)
        .rolling(10, min_periods=3)
        .mean()
    )
    log_volume_change = np.log(
        volume.where(volume > 0.0)
    ).diff()
    volume_price_elasticity = _safe_div(
        ret_1.rolling(10, min_periods=8).cov(log_volume_change),
        log_volume_change.rolling(
            10,
            min_periods=8,
        ).var(ddof=1),
    )
    illiquidity_regime = _safe_div(
        daily_amihud.rolling(5, min_periods=5).mean(),
        daily_amihud.rolling(20, min_periods=10).mean(),
    )
    volume_concentration = _safe_div(
        volume.rolling(5, min_periods=5).max(),
        volume.rolling(5, min_periods=5).sum(),
    )
    limit_rates = pd.Series(
        {
            code: (
                0.20
                if str(code).startswith(
                    ("300", "301", "688", "689")
                )
                else 0.10
            )
            for code in columns
        },
        dtype=float,
    )
    limit_rate = pd.DataFrame(
        np.tile(limit_rates.to_numpy(), (len(index), 1)),
        index=index,
        columns=columns,
    )
    raw_is_st = panel.get("is_st")
    if isinstance(raw_is_st, pd.DataFrame):
        st_mask = (
            _float_frame(raw_is_st)
            .reindex(index=index, columns=columns)
            .fillna(0.0)
            .gt(0.0)
        )
        limit_rate = limit_rate.where(~st_mask, 0.05)
    limit_proximity = _safe_div(ret_1, limit_rate)
    limit_close_strength = (
        limit_proximity
        * ((close_location + 1.0) / 2.0)
        * amount_shock
    )
    limit_pressure_persistence = (
        limit_proximity.clip(lower=0.0)
        * ((close_location + 1.0) / 2.0)
    ).rolling(3, min_periods=2).mean()

    factors = {
        "return_1": ret_1,
        "return_3": ret_3,
        "return_5": ret_5,
        "return_10": ret_10,
        "return_20": ret_20,
        "gap_open_1": gap_open,
        "intraday_absorption_1": intraday,
        "close_location_1": close_location,
        "upper_shadow_1": upper_shadow,
        "lower_shadow_1": lower_shadow,
        "range_pct_1": range_pct,
        "trend_efficiency_5": trend_efficiency,
        "trend_consistency_5": trend_consistency,
        "volatility_compression_5_20": volatility_compression,
        "volume_dryup_5_20": volume_dryup,
        "amount_shock_1_20": amount_shock,
        "amihud_impact_5": amihud,
        "breakout_quality_20": breakout_quality,
        "pullback_dryup_3_20": pullback_dryup,
        "gap_absorption_1": gap_absorption,
        "momentum_acceleration_3_20": momentum_acceleration,
        "distance_high_20": distance_high,
        "distance_low_20": distance_low,
        "range_close_strength_1": range_close_strength,
        "gap_hold_1": gap_hold,
        "panic_recovery_1": panic_recovery,
        "liquidity_reversal_3_20": liquidity_reversal,
        "price_volume_divergence_5_20": price_volume_divergence,
        "quiet_breakout_20": quiet_breakout,
        "return_to_risk_5_20": return_to_risk,
        "limit_proximity_1": limit_proximity,
        "limit_close_strength_1": limit_close_strength,
        "close_flow_3": close_flow_3,
        "close_flow_acceleration_3_10": close_flow_acceleration,
        "signed_amount_flow_5": signed_amount_flow,
        "amount_impulse_decay_1_5": amount_impulse_decay,
        "range_regime_shift_3_20": range_regime_shift,
        "overnight_intraday_divergence_3": (
            overnight_intraday_divergence
        ),
        "return_autocorrelation_10": return_autocorrelation,
        "realized_skew_10": realized_skew,
        "idiosyncratic_momentum_5_20": idiosyncratic_momentum,
        "downside_resilience_10": downside_resilience,
        "volume_price_elasticity_10": volume_price_elasticity,
        "illiquidity_regime_5_20": illiquidity_regime,
        "volume_concentration_5": volume_concentration,
        "limit_pressure_persistence_3": limit_pressure_persistence,
    }
    eligibility = panel.get("universe_eligible")
    if isinstance(eligibility, pd.DataFrame):
        mask = (
            _float_frame(eligibility)
            .reindex(index=index, columns=columns)
            .fillna(0.0)
            .astype(bool)
        )
        factors = {
            name: frame.where(mask)
            for name, frame in factors.items()
        }
    return factors


def align_auction_confirmation(
    features: pd.DataFrame,
    auction: pd.DataFrame,
) -> pd.DataFrame:
    result = features.copy().reset_index(drop=True)
    output_columns = [
        "auction_trade_date",
        "auction_gap",
        "auction_amount",
        "auction_turnover_rate",
        "auction_volume_ratio",
    ]
    for column in output_columns:
        result[column] = pd.NA
    if result.empty or auction is None or auction.empty:
        return result

    data = auction.copy()
    data["code"] = (
        data["code"]
        .astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.zfill(6)
    )
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    data = (
        data.dropna(subset=["date"])
        .sort_values(["code", "date"], kind="stable")
        .drop_duplicates(["code", "date"], keep="first")
        .reset_index(drop=True)
    )
    if not data.empty:
        code_source = (
            result["ts_code"]
            if "ts_code" in result
            else result.get(
                "code",
                pd.Series("", index=result.index),
            )
        )
        feature_codes = (
            code_source.astype(str)
            .str.replace(r"\.0$", "", regex=True)
            .str.zfill(6)
        )
        entry_dates = pd.to_datetime(
            (
                result["entry_date"]
                if "entry_date" in result
                else pd.Series(pd.NaT, index=result.index)
            ),
            errors="coerce",
        ).dt.normalize()
        matched_rows = np.full(len(result), -1, dtype=np.int64)

        exact_mask = entry_dates.notna()
        if bool(exact_mask.any()):
            lookup = pd.Series(
                data.index.to_numpy(dtype=np.int64),
                index=pd.MultiIndex.from_frame(
                    data[["code", "date"]]
                ),
            )
            exact_keys = pd.MultiIndex.from_arrays(
                [
                    feature_codes.loc[exact_mask].to_numpy(),
                    entry_dates.loc[exact_mask].to_numpy(),
                ],
                names=["code", "date"],
            )
            exact_matches = lookup.reindex(exact_keys).to_numpy()
            exact_positions = np.flatnonzero(
                exact_mask.to_numpy()
            )
            found = pd.notna(exact_matches)
            matched_rows[exact_positions[found]] = (
                exact_matches[found].astype(np.int64)
            )

        matched_mask = matched_rows >= 0
        if bool(matched_mask.any()):
            matched = data.iloc[matched_rows[matched_mask]]
            result.loc[
                matched_mask,
                "auction_trade_date",
            ] = matched["date"].dt.strftime("%Y-%m-%d").to_numpy()

            def numeric_values(column: str) -> np.ndarray:
                if column not in matched:
                    return np.full(len(matched), np.nan, dtype=float)
                return pd.to_numeric(
                    matched[column],
                    errors="coerce",
                ).to_numpy(dtype=float)

            auction_price = numeric_values("auction_price")
            previous_close = numeric_values("pre_close")
            gap = np.full(len(matched), np.nan, dtype=float)
            valid_gap = (
                np.isfinite(auction_price)
                & np.isfinite(previous_close)
                & (previous_close > 0)
            )
            gap[valid_gap] = np.round(
                auction_price[valid_gap]
                / previous_close[valid_gap]
                - 1.0,
                10,
            )
            result.loc[matched_mask, "auction_gap"] = gap
            for source, target in (
                ("auction_amount", "auction_amount"),
                ("auction_turnover_rate", "auction_turnover_rate"),
                ("auction_volume_ratio", "auction_volume_ratio"),
            ):
                values = (
                    matched[source].to_numpy()
                    if source in matched
                    else np.full(len(matched), np.nan)
                )
                result.loc[matched_mask, target] = values
    for column in output_columns[1:]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    return result


def benjamini_hochberg(
    p_values: Mapping[str, float],
    *,
    alpha: float = 0.05,
) -> set[str]:
    if not 0.0 < float(alpha) < 1.0:
        raise ValueError("alpha must be between zero and one")
    cleaned = []
    for name, raw_value in p_values.items():
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and 0.0 <= value <= 1.0:
            cleaned.append((str(name), value))
    cleaned.sort(key=lambda item: (item[1], item[0]))
    count = len(cleaned)
    cutoff = -1
    for index, (_name, value) in enumerate(cleaned, start=1):
        if value <= float(alpha) * index / count:
            cutoff = index
    if cutoff < 1:
        return set()
    return {name for name, _value in cleaned[:cutoff]}


def purged_walk_forward_windows(
    dates: Sequence[Any],
    *,
    train_days: int,
    test_days: int,
    step_days: int,
    purge_days: int,
    min_partial_test_days: int = 0,
) -> list[WalkForwardWindow]:
    for name, value in (
        ("train_days", train_days),
        ("test_days", test_days),
        ("step_days", step_days),
        ("purge_days", purge_days),
        ("min_partial_test_days", min_partial_test_days),
    ):
        if isinstance(value, bool) or int(value) != value or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if train_days < 1 or test_days < 1 or step_days < 1:
        raise ValueError("train, test and step days must be positive")
    if int(min_partial_test_days) > int(test_days):
        raise ValueError(
            "min_partial_test_days must not exceed test_days"
        )
    normalized = tuple(
        sorted(
            {
                pd.Timestamp(value).normalize()
                for value in dates
                if pd.notna(value)
            }
        )
    )
    windows = []
    test_start = int(train_days) + int(purge_days)
    window_id = 1
    while test_start < len(normalized):
        remaining_test_days = len(normalized) - test_start
        full_window = remaining_test_days >= int(test_days)
        if (
            not full_window
            and (
                int(min_partial_test_days) < 1
                or remaining_test_days < int(min_partial_test_days)
            )
        ):
            break
        train_end = test_start - int(purge_days)
        train_start = train_end - int(train_days)
        if train_start < 0:
            test_start += int(step_days)
            continue
        test_end = min(
            test_start + int(test_days),
            len(normalized),
        )
        windows.append(
            WalkForwardWindow(
                window_id=window_id,
                train_dates=normalized[train_start:train_end],
                purged_dates=normalized[train_end:test_start],
                test_dates=normalized[test_start:test_end],
            )
        )
        window_id += 1
        if not full_window:
            break
        test_start += int(step_days)
    return windows


def decorrelate_factors(
    values: pd.DataFrame,
    candidates: Sequence[str],
    *,
    evidence_scores: Mapping[str, float],
    max_abs_correlation: float = 0.85,
) -> list[str]:
    if not 0.0 <= float(max_abs_correlation) < 1.0:
        raise ValueError("max_abs_correlation must be in [0, 1)")
    available = [
        str(name)
        for name in candidates
        if str(name) in values.columns
    ]
    ordered = sorted(
        available,
        key=lambda name: (
            -float(evidence_scores.get(name, float("-inf"))),
            name,
        ),
    )
    selected: list[str] = []
    numeric = values[ordered].apply(pd.to_numeric, errors="coerce")
    for name in ordered:
        keep = True
        for existing in selected:
            pair = numeric[[name, existing]].dropna()
            if len(pair) < 3:
                continue
            if (
                pair[name].nunique(dropna=True) < 2
                or pair[existing].nunique(dropna=True) < 2
            ):
                continue
            correlation = pair[name].corr(pair[existing], method="spearman")
            if (
                pd.notna(correlation)
                and abs(float(correlation)) > float(max_abs_correlation)
            ):
                keep = False
                break
        if keep:
            selected.append(name)
    return selected


def build_expression_candidates(
    base_factors: Mapping[str, pd.DataFrame],
    *,
    max_interactions: int = 30,
) -> dict[str, pd.DataFrame]:
    names, interactions = _expression_candidate_plan(
        base_factors,
        max_interactions=max_interactions,
    )
    result = {
        name: _factor_storage_frame(base_factors[name])
        for name in names
    }
    for interaction_name, left_name, right_name in interactions:
        left = result[left_name]
        right = result[right_name]
        index = left.index.intersection(right.index)
        columns = left.columns.intersection(right.columns)
        if index.empty or columns.empty:
            continue
        left_rank = left.loc[index, columns].rank(
            axis=1,
            pct=True,
            method="average",
        )
        right_rank = right.loc[index, columns].rank(
            axis=1,
            pct=True,
            method="average",
        )
        result[interaction_name] = (
            (left_rank - 0.5) * (right_rank - 0.5)
        ).astype(np.float32, copy=False)
    return result


def _expression_candidate_plan(
    base_factors: Mapping[str, pd.DataFrame],
    *,
    max_interactions: int,
) -> tuple[list[str], list[tuple[str, str, str]]]:
    if isinstance(max_interactions, bool) or int(max_interactions) < 0:
        raise ValueError("max_interactions must be non-negative")
    names = sorted(
        str(name)
        for name, frame in base_factors.items()
        if isinstance(frame, pd.DataFrame)
    )
    available = set(names)
    interactions: list[tuple[str, str, str]] = []
    used_pairs: set[frozenset[str]] = set()

    def append_interaction(left_name: str, right_name: str) -> None:
        if len(interactions) >= int(max_interactions):
            return
        if (
            left_name not in available
            or right_name not in available
            or left_name == right_name
        ):
            return
        pair_key = frozenset((left_name, right_name))
        if pair_key in used_pairs:
            return
        left = base_factors[left_name]
        right = base_factors[right_name]
        left_index = pd.DatetimeIndex(
            pd.to_datetime(left.index, errors="coerce")
        ).normalize()
        right_index = pd.DatetimeIndex(
            pd.to_datetime(right.index, errors="coerce")
        ).normalize()
        if (
            left_index.intersection(right_index).empty
            or pd.Index([str(value) for value in left.columns])
            .intersection(
                pd.Index([str(value) for value in right.columns])
            )
            .empty
        ):
            return
        interactions.append(
            (
                f"{left_name}__x__{right_name}",
                left_name,
                right_name,
            )
        )
        used_pairs.add(pair_key)

    for left_name, right_name in CURATED_INTERACTION_PAIRS:
        append_interaction(left_name, right_name)
    for left_name, right_name in combinations(names, 2):
        append_interaction(left_name, right_name)
        if len(interactions) >= int(max_interactions):
            break
    return names, interactions


def _assemble_factor_long_frame(
    names: Sequence[str],
    *,
    index: pd.DatetimeIndex,
    columns: pd.Index,
    load_frame: Callable[[str], pd.DataFrame],
    release_frame: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    ordered = sorted(set(str(name) for name in names))
    if not ordered:
        return pd.DataFrame()
    index = pd.DatetimeIndex(index).dropna().sort_values()
    columns = pd.Index(
        [str(column) for column in columns],
        dtype="object",
    ).sort_values()
    if index.empty or columns.empty:
        return pd.DataFrame(
            columns=["trade_date", "ts_code", *ordered]
        )
    row_count = len(index) * len(columns)
    values = np.empty(
        (row_count, len(ordered)),
        dtype=np.float32,
    )
    for position, name in enumerate(ordered):
        normalized = _factor_storage_frame(
            load_frame(name)
        ).reindex(
            index=index,
            columns=columns,
        )
        values[:, position] = normalized.to_numpy(
            dtype=np.float32,
            copy=False,
        ).reshape(-1)
        del normalized
        if release_frame is not None:
            release_frame(name)
    usable = ~np.isnan(values).all(axis=1)
    factor_values = values if bool(usable.all()) else values[usable]
    result = pd.DataFrame(
        factor_values,
        columns=ordered,
        copy=False,
    )
    dates = np.repeat(index.to_numpy(), len(columns))
    code_axis = _code_key_series(
        pd.Series(columns.to_numpy())
    ).to_numpy()
    codes = np.tile(code_axis, len(index))
    if not bool(usable.all()):
        dates = dates[usable]
        codes = codes[usable]
    result.insert(0, "ts_code", codes)
    result.insert(0, "trade_date", dates)
    return result


def _build_expression_feature_frame(
    base_factors: Mapping[str, pd.DataFrame],
    *,
    max_interactions: int = 30,
    consume_base: bool = False,
    candidate_plan: tuple[
        Sequence[str],
        Sequence[tuple[str, str, str]],
    ]
    | None = None,
) -> tuple[list[str], pd.DataFrame]:
    if candidate_plan is None:
        base_names, interactions = _expression_candidate_plan(
            base_factors,
            max_interactions=max_interactions,
        )
    else:
        base_names = [
            str(name)
            for name in candidate_plan[0]
        ]
        interactions = [
            (
                str(name),
                str(left_name),
                str(right_name),
            )
            for name, left_name, right_name in candidate_plan[1]
        ]
    candidate_names = [
        *base_names,
        *(name for name, _left, _right in interactions),
    ]
    if not base_names:
        return candidate_names, pd.DataFrame()
    index = pd.DatetimeIndex([])
    columns = pd.Index([], dtype="object")
    for name in base_names:
        frame = base_factors[name]
        index = index.union(
            pd.DatetimeIndex(
                pd.to_datetime(frame.index, errors="coerce")
            ).normalize()
        )
        columns = columns.union(
            pd.Index(
                [str(column) for column in frame.columns],
                dtype="object",
            )
        )
    interaction_sources = {
        name: (left, right)
        for name, left, right in interactions
    }
    if consume_base:
        if not isinstance(base_factors, MutableMapping):
            raise TypeError(
                "consume_base requires a mutable factor mapping"
            )
        sources: Mapping[str, pd.DataFrame] = dict(base_factors)
        base_factors.clear()
    else:
        sources = base_factors
    remaining_uses = {name: 0 for name in base_names}
    for _name, left_name, right_name in interactions:
        remaining_uses[left_name] += 1
        remaining_uses[right_name] += 1
    loaded_bases: set[str] = set()

    def load_frame(name: str) -> pd.DataFrame:
        sources = interaction_sources.get(name)
        if sources is None:
            return source_frames[name]
        left = _factor_storage_frame(source_frames[sources[0]])
        right = _factor_storage_frame(source_frames[sources[1]])
        shared_index = left.index.intersection(right.index)
        shared_columns = left.columns.intersection(right.columns)
        left_rank = left.loc[shared_index, shared_columns].rank(
            axis=1,
            pct=True,
            method="average",
        )
        right_rank = right.loc[shared_index, shared_columns].rank(
            axis=1,
            pct=True,
            method="average",
        )
        return (
            (left_rank - 0.5) * (right_rank - 0.5)
        ).astype(np.float32, copy=False)

    source_frames = sources

    def release_frame(name: str) -> None:
        if not consume_base:
            return
        interaction = interaction_sources.get(name)
        if interaction is None:
            loaded_bases.add(name)
            if remaining_uses[name] == 0:
                source_frames.pop(name, None)
            return
        for source_name in interaction:
            remaining_uses[source_name] -= 1
            if (
                remaining_uses[source_name] == 0
                and source_name in loaded_bases
            ):
                source_frames.pop(source_name, None)

    return candidate_names, _assemble_factor_long_frame(
        candidate_names,
        index=index,
        columns=columns,
        load_frame=load_frame,
        release_frame=release_frame,
    )


def build_structural_candidate_factors(
    base_factors: Mapping[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    for name, inputs in _STRUCTURAL_FACTOR_INPUTS.items():
        if any(
            source not in base_factors
            or not isinstance(base_factors[source], pd.DataFrame)
            for source, _direction in inputs
        ):
            continue
        frames = [
            _float_frame(base_factors[source])
            for source, _direction in inputs
        ]
        index = frames[0].index
        columns = frames[0].columns
        for frame in frames[1:]:
            index = index.intersection(frame.index)
            columns = columns.intersection(frame.columns)
        if index.empty or columns.empty:
            continue
        interaction = pd.DataFrame(
            1.0,
            index=index,
            columns=columns,
        )
        for frame, (_source, direction) in zip(frames, inputs):
            oriented = frame.loc[index, columns] * float(direction)
            interaction *= oriented.rank(
                axis=1,
                pct=True,
                method="average",
            )
        result[name] = _factor_storage_frame(interaction)
    return result


def build_daily_basic_candidate_factors(
    daily_basic: pd.DataFrame | None,
) -> dict[str, pd.DataFrame]:
    if daily_basic is None or daily_basic.empty:
        return {
            name: pd.DataFrame(dtype=float)
            for name in DAILY_BASIC_FACTOR_FORMULAS
        }
    required = {
        "code",
        "date",
        "turnover_rate",
        "turnover_rate_f",
        "volume_ratio",
        "float_share",
        "total_share",
        "circ_mv",
    }
    missing = sorted(required - set(daily_basic.columns))
    if missing:
        raise KeyError(f"daily-basic data missing fields: {missing}")
    data = daily_basic.copy()
    data["code"] = (
        data["code"]
        .astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.zfill(6)
    )
    data["date"] = pd.to_datetime(
        data["date"],
        errors="coerce",
    ).dt.normalize()
    numeric = sorted(required - {"code", "date"})
    for column in numeric:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = (
        data.dropna(subset=["date"])
        .drop_duplicates(["code", "date"], keep="last")
        .sort_values(["date", "code"])
    )

    def wide(column: str) -> pd.DataFrame:
        return (
            data.pivot(index="date", columns="code", values=column)
            .sort_index()
            .sort_index(axis=1)
            .astype(float)
        )

    turnover = wide("turnover_rate")
    turnover_free = wide("turnover_rate_f")
    volume_ratio = wide("volume_ratio")
    float_share = wide("float_share")
    total_share = wide("total_share")
    circ_mv = wide("circ_mv")
    acceleration = _safe_div(
        turnover,
        turnover.rolling(5, min_periods=3).mean(),
    )
    factors = {
        "turnover_rate_1": turnover,
        "turnover_acceleration_1_5": acceleration,
        "turnover_free_spread_1": turnover_free - turnover,
        "volume_ratio_basic_1": volume_ratio,
        "log_circ_mv_1": np.log(circ_mv.where(circ_mv > 0)),
        "float_share_ratio_1": _safe_div(float_share, total_share),
        "liquidity_crowding_1": turnover * volume_ratio,
    }
    return factors


def _factor_long_frame(
    factors: Mapping[str, pd.DataFrame],
) -> pd.DataFrame:
    names = [
        str(name)
        for name in sorted(factors)
        if isinstance(factors[name], pd.DataFrame)
    ]
    if not names:
        return pd.DataFrame()
    index = pd.DatetimeIndex([])
    columns = pd.Index([], dtype="object")
    for name in names:
        frame = factors[name]
        frame_index = pd.DatetimeIndex(
            pd.to_datetime(frame.index, errors="coerce")
        ).normalize()
        frame_columns = pd.Index(
            [str(column) for column in frame.columns],
            dtype="object",
        )
        index = index.union(frame_index)
        columns = columns.union(frame_columns)
    return _assemble_factor_long_frame(
        names,
        index=index,
        columns=columns,
        load_frame=factors.__getitem__,
    )


def _daily_rank_ic(
    frame: pd.DataFrame,
    factor: str,
    target: str,
    *,
    min_cross_section: int,
) -> pd.Series:
    values: dict[pd.Timestamp, float] = {}
    for trade_date, group in frame.groupby("trade_date", sort=True):
        pair = group[[factor, target]].apply(
            pd.to_numeric,
            errors="coerce",
        ).dropna()
        if len(pair) < int(min_cross_section):
            continue
        if pair[factor].nunique() < 2 or pair[target].nunique() < 2:
            continue
        correlation = pair[factor].corr(pair[target], method="spearman")
        if pd.notna(correlation) and math.isfinite(float(correlation)):
            values[pd.Timestamp(trade_date).normalize()] = float(
                correlation
            )
    return pd.Series(values, dtype=float).sort_index()


def _daily_rank_ic_matrix(
    frame: pd.DataFrame,
    factors: Sequence[str],
    target: str,
    *,
    min_cross_section: int,
) -> pd.DataFrame:
    names = [
        str(name)
        for name in factors
        if str(name) in frame.columns
    ]
    if not names:
        return pd.DataFrame(dtype=float)
    rows = {}
    for trade_date, group in frame.groupby(
        "trade_date",
        sort=True,
    ):
        target_values = pd.to_numeric(
            group[target],
            errors="coerce",
        )
        factor_values = group[names].apply(
            pd.to_numeric,
            errors="coerce",
        )
        target_matrix = pd.DataFrame(
            np.repeat(
                target_values.to_numpy()[:, None],
                len(names),
                axis=1,
            ),
            index=factor_values.index,
            columns=factor_values.columns,
        )
        pair_valid = factor_values.notna() & target_matrix.notna()
        pair_factors = factor_values.where(pair_valid)
        pair_targets = target_matrix.where(pair_valid)
        counts = pair_valid.sum(axis=0)
        factor_unique = pair_factors.nunique(
            axis=0,
            dropna=True,
        )
        target_unique = pair_targets.nunique(
            axis=0,
            dropna=True,
        )
        usable = (
            counts.ge(int(min_cross_section))
            & factor_unique.ge(2)
            & target_unique.ge(2)
        )
        correlations = pd.Series(np.nan, index=names, dtype=float)
        usable_names = usable[usable].index.tolist()
        if usable_names:
            factor_ranks = pair_factors[usable_names].rank(
                axis=0,
                method="average",
            )
            target_ranks = pair_targets[usable_names].rank(
                axis=0,
                method="average",
            )
            correlations.loc[usable_names] = factor_ranks.corrwith(
                target_ranks,
                axis=0,
                method="pearson",
            )
        rows[pd.Timestamp(trade_date).normalize()] = correlations
    if not rows:
        return pd.DataFrame(columns=names, dtype=float)
    return (
        pd.DataFrame.from_dict(rows, orient="index")
        .reindex(columns=names)
        .sort_index()
        .astype(float)
    )


def _candidate_non_null_row_mask(
    frame: pd.DataFrame,
    candidate_names: Sequence[str],
) -> np.ndarray:
    mask = np.zeros(len(frame), dtype=bool)
    for name in candidate_names:
        if name not in frame:
            continue
        np.logical_or(
            mask,
            frame[name].notna().to_numpy(copy=False),
            out=mask,
        )
    return mask


def _market_regime_daily_frame(
    frame: pd.DataFrame,
) -> pd.DataFrame:
    if frame.empty or "trade_date" not in frame:
        return pd.DataFrame(columns=MARKET_REGIME_FEATURES)
    source_columns = [
        "trade_date",
        *(
            name
            for name in MARKET_REGIME_SOURCE_COLUMNS
            if name in frame
        ),
    ]
    data = frame.loc[:, source_columns].copy()
    data["trade_date"] = pd.to_datetime(
        data["trade_date"],
        errors="coerce",
    ).dt.normalize()
    dates = pd.DatetimeIndex(
        sorted(data["trade_date"].dropna().unique())
    )
    result = pd.DataFrame(index=dates)
    sources = {
        "market_return_1": ("return_1", "median"),
        "market_return_5": ("return_5", "median"),
        "market_return_20": ("return_20", "median"),
        "market_dispersion": ("return_1", "std"),
        "market_range": ("range_pct_1", "median"),
        "market_amount_shock": (
            "amount_shock_1_20",
            "median",
        ),
        "market_volatility_compression": (
            "volatility_compression_5_20",
            "median",
        ),
    }
    for output, (source, aggregation) in sources.items():
        if source not in data:
            result[output] = np.nan
            continue
        values = pd.to_numeric(data[source], errors="coerce")
        grouped = values.groupby(data["trade_date"])
        result[output] = (
            grouped.median()
            if aggregation == "median"
            else grouped.std(ddof=1)
        )
    if "return_1" in data:
        returns = pd.to_numeric(
            data["return_1"],
            errors="coerce",
        )
        valid = returns.notna()
        grouped_valid = valid.groupby(data["trade_date"]).sum()
        positive = (
            (returns > 0.0)
            .where(valid, False)
            .groupby(data["trade_date"])
            .sum()
        )
        tail_up = (
            (returns >= 0.05)
            .where(valid, False)
            .groupby(data["trade_date"])
            .sum()
        )
        tail_down = (
            (returns <= -0.05)
            .where(valid, False)
            .groupby(data["trade_date"])
            .sum()
        )
        denominator = grouped_valid.where(grouped_valid > 0)
        result["market_breadth"] = positive / denominator
        result["market_tail_up"] = tail_up / denominator
        result["market_tail_down"] = tail_down / denominator
    else:
        for name in (
            "market_breadth",
            "market_tail_up",
            "market_tail_down",
        ):
            result[name] = np.nan
    return result.reindex(columns=MARKET_REGIME_FEATURES)


def _fit_market_regime_model(
    frame: pd.DataFrame,
    target: str,
    *,
    min_days: int = 40,
    ridge_alpha: float = 10.0,
) -> dict[str, Any]:
    daily_features = _market_regime_daily_frame(frame)
    if target not in frame:
        return {"status": "unavailable", "reason": "target_missing"}
    daily_target = (
        pd.to_numeric(frame[target], errors="coerce")
        .groupby(pd.to_datetime(frame["trade_date"]).dt.normalize())
        .mean()
        .rename("_target")
    )
    data = daily_features.join(daily_target, how="inner")
    feature_names = [
        name
        for name in MARKET_REGIME_FEATURES
        if (
            name in data
            and data[name].notna().sum() >= int(min_days)
            and float(data[name].std(ddof=1) or 0.0)
            > np.finfo(float).eps
        )
    ]
    if len(feature_names) < 2:
        return {
            "status": "unavailable",
            "reason": "insufficient_market_features",
        }
    clean = data[[*feature_names, "_target"]].dropna()
    if len(clean) < int(min_days):
        return {
            "status": "unavailable",
            "reason": "insufficient_fit_days",
            "fit_days": int(len(clean)),
        }
    means = clean[feature_names].mean()
    scales = clean[feature_names].std(ddof=1)
    usable = scales.gt(np.finfo(float).eps)
    feature_names = [
        name for name in feature_names if bool(usable[name])
    ]
    x = (
        (clean[feature_names] - means[feature_names])
        / scales[feature_names]
    ).to_numpy(dtype=float)
    y = clean["_target"].to_numpy(dtype=float)
    design = np.column_stack([np.ones(len(x)), x])
    penalty = np.eye(design.shape[1], dtype=float)
    penalty[0, 0] = 0.0
    coefficients = np.linalg.solve(
        design.T @ design + float(ridge_alpha) * penalty,
        design.T @ y,
    )
    return {
        "status": "fitted",
        "feature_names": feature_names,
        "fit_days": int(len(clean)),
        "ridge_alpha": float(ridge_alpha),
        "intercept": float(coefficients[0]),
        "coefficients": {
            name: float(coefficients[index + 1])
            for index, name in enumerate(feature_names)
        },
        "means": {
            name: float(means[name])
            for name in feature_names
        },
        "scales": {
            name: float(scales[name])
            for name in feature_names
        },
    }


def _predict_market_regime(
    model: Mapping[str, Any],
    frame: pd.DataFrame,
) -> pd.Series:
    daily = _market_regime_daily_frame(frame)
    if model.get("status") != "fitted" or daily.empty:
        return pd.Series(dtype=float)
    feature_names = [
        str(name)
        for name in model.get("feature_names") or []
    ]
    means = pd.Series(model.get("means") or {}, dtype=float)
    scales = pd.Series(model.get("scales") or {}, dtype=float)
    coefficients = pd.Series(
        model.get("coefficients") or {},
        dtype=float,
    )
    if (
        not feature_names
        or not set(feature_names).issubset(daily.columns)
    ):
        return pd.Series(dtype=float)
    standardized = (
        daily[feature_names].fillna(means)
        - means
    ).div(scales)
    prediction = (
        float(model.get("intercept") or 0.0)
        + standardized.mul(coefficients, axis=1).sum(axis=1)
    )
    return prediction.astype(float).sort_index()


def _ic_diagnostics(ic: pd.Series) -> dict[str, Any]:
    clean = pd.to_numeric(ic, errors="coerce").dropna()
    count = int(len(clean))
    mean = float(clean.mean()) if count else 0.0
    std = float(clean.std(ddof=1)) if count > 1 else 0.0
    hac_lag = 0
    standard_error = 0.0
    if count < 3:
        p_value = 1.0
    elif std <= np.finfo(float).eps:
        p_value = 0.0 if abs(mean) > np.finfo(float).eps else 1.0
    else:
        hac_lag = max(
            1,
            min(
                count - 1,
                int(
                    math.floor(
                        4.0 * (count / 100.0) ** (2.0 / 9.0)
                    )
                ),
            ),
        )
        centered = clean.to_numpy(dtype=float) - mean
        long_run_variance = float(
            np.dot(centered, centered) / count
        )
        for lag in range(1, hac_lag + 1):
            covariance = float(
                np.dot(centered[lag:], centered[:-lag]) / count
            )
            weight = 1.0 - lag / (hac_lag + 1.0)
            long_run_variance += 2.0 * weight * covariance
        iid_mean_variance = (std * std) / count
        hac_mean_variance = max(
            0.0,
            long_run_variance / count,
        )
        standard_error = math.sqrt(
            max(iid_mean_variance, hac_mean_variance)
        )
        t_value = mean / standard_error
        p_value = math.erfc(abs(t_value) / math.sqrt(2.0))
    icir = mean / std if std > np.finfo(float).eps else 0.0
    return {
        "ic_count": count,
        "mean_rank_ic": round(mean, 8),
        "ic_std": round(std, 8),
        "icir": round(icir, 8),
        "p_value": round(min(1.0, max(0.0, p_value)), 10),
        "p_value_method": "newey_west_hac",
        "hac_lag": hac_lag,
        "standard_error": round(standard_error, 10),
    }


def _true_marker(value: Any) -> bool:
    if pd.isna(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
        }
    return bool(value)


def _portfolio_metrics(
    picks: pd.DataFrame,
    target: str,
) -> dict[str, float | int | list[float]]:
    if picks.empty:
        return {
            "trade_count": 0,
            "hit_rate": 0.0,
            "avg_return_pct": 0.0,
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "daily_returns_pct": [],
        }
    returns = pd.to_numeric(picks[target], errors="coerce").dropna()
    daily = (
        picks.assign(_return=pd.to_numeric(picks[target], errors="coerce"))
        .dropna(subset=["_return"])
        .groupby("trade_date", sort=True)["_return"]
        .mean()
    )
    wealth = (1.0 + daily / 100.0).cumprod()
    drawdown = wealth / wealth.cummax() - 1.0
    return {
        "trade_count": int(len(returns)),
        "hit_rate": round(float((returns > 0).mean()), 6),
        "avg_return_pct": round(float(returns.mean()), 6),
        "total_return_pct": round(
            float((wealth.iloc[-1] - 1.0) * 100.0),
            6,
        ),
        "max_drawdown_pct": round(
            float(drawdown.min() * 100.0),
            6,
        ),
        "daily_returns_pct": [
            round(float(value), 8)
            for value in daily.tolist()
        ],
    }


def _summary_from_windows(
    windows: Sequence[Mapping[str, Any]],
) -> dict[str, float | int]:
    valid = [
        window
        for window in windows
        if int(window.get("trade_count") or 0) > 0
    ]
    daily_values = [
        float(value)
        for window in valid
        for value in window.get("daily_returns_pct") or []
    ]
    daily = pd.Series(daily_values, dtype=float)
    if daily.empty:
        total_return = 0.0
        max_drawdown = 0.0
    else:
        wealth = (1.0 + daily / 100.0).cumprod()
        total_return = float((wealth.iloc[-1] - 1.0) * 100.0)
        max_drawdown = float(
            (wealth / wealth.cummax() - 1.0).min() * 100.0
        )
    rank_ics = [
        float(window["oos_rank_ic"])
        for window in valid
        if window.get("oos_rank_ic") is not None
    ]
    trade_count = sum(int(window["trade_count"]) for window in valid)
    weighted_hits = sum(
        float(window["hit_rate"]) * int(window["trade_count"])
        for window in valid
    )
    weighted_returns = sum(
        float(window["avg_return_pct"]) * int(window["trade_count"])
        for window in valid
    )
    return {
        "window_count": len(valid),
        "trade_count": trade_count,
        "hit_rate": round(
            weighted_hits / trade_count if trade_count else 0.0,
            6,
        ),
        "avg_return_pct": round(
            weighted_returns / trade_count if trade_count else 0.0,
            6,
        ),
        "total_return_pct": round(total_return, 6),
        "max_drawdown_pct": round(max_drawdown, 6),
        "oos_rank_ic": round(
            float(np.mean(rank_ics)) if rank_ics else 0.0,
            8,
        ),
        "positive_window_rate": round(
            (
                sum(
                    float(window["total_return_pct"]) > 0
                    for window in valid
                )
                / len(valid)
            )
            if valid
            else 0.0,
            6,
        ),
    }


def _composite_scores(
    frame: pd.DataFrame,
    selected: Sequence[str],
    directions: Mapping[str, float],
    weights: Mapping[str, float] | None = None,
) -> pd.DataFrame:
    result = frame.copy()
    score_columns = []
    for name in selected:
        oriented = (
            pd.to_numeric(result[name], errors="coerce")
            * float(directions[name])
        )
        score_name = f"_score_{name}"
        result[score_name] = oriented.groupby(
            result["trade_date"]
        ).rank(
            pct=True,
            method="average",
        )
        score_columns.append(score_name)
    if score_columns:
        resolved_weights = pd.Series(
            {
                f"_score_{name}": float(
                    (weights or {}).get(name, 1.0)
                )
                for name in selected
            },
            dtype=float,
        )
        scores = result[score_columns]
        denominator = scores.notna().mul(
            resolved_weights,
            axis=1,
        ).sum(axis=1)
        result["_composite_score"] = (
            scores.mul(resolved_weights, axis=1)
            .sum(axis=1, min_count=1)
            .div(denominator.where(denominator > 0.0))
        )
    else:
        result["_composite_score"] = np.nan
    return result


def _fit_score_weights(
    selected: Sequence[str],
    diagnostics: Mapping[str, Mapping[str, Any]],
) -> dict[str, float]:
    raw = {}
    for name in selected:
        details = diagnostics.get(name) or {}
        mean_ic = abs(float(details.get("mean_rank_ic") or 0.0))
        ic_std = max(
            0.01,
            float(details.get("ic_std") or 0.0),
        )
        raw[str(name)] = min(5.0, mean_ic / ic_std)
    total = sum(raw.values())
    if total <= np.finfo(float).eps and raw:
        return {
            name: 1.0 / len(raw)
            for name in raw
        }
    return {
        name: value / total
        for name, value in raw.items()
    }


def _top_daily_picks(
    frame: pd.DataFrame,
    target: str,
    *,
    top_k: int,
    score_column: str = "_composite_score",
) -> pd.DataFrame:
    return (
        frame.dropna(subset=[score_column, target])
        .sort_values(
            ["trade_date", score_column, "ts_code"],
            ascending=[True, False, True],
            kind="stable",
        )
        .groupby("trade_date", sort=False, group_keys=False)
        .head(int(top_k))
    )


def run_walk_forward_discovery(
    factors: Mapping[str, pd.DataFrame],
    labels: pd.DataFrame,
    *,
    target_col: str = "live_adjusted_return_pct",
    train_days: int = 120,
    test_days: int = 20,
    step_days: int = 20,
    purge_days: int = 3,
    min_partial_test_days: int = 0,
    top_k: int = 10,
    max_factors: int = 6,
    fdr_alpha: float = 0.05,
    max_abs_correlation: float = 0.85,
    min_cross_section: int = 20,
    _prepared_feature_frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    if step_days < test_days:
        raise ValueError("step_days must be at least test_days")
    if top_k < 1 or max_factors < 1:
        raise ValueError("top_k and max_factors must be positive")
    feature_frame = (
        _prepared_feature_frame.copy(deep=False)
        if isinstance(_prepared_feature_frame, pd.DataFrame)
        else _factor_long_frame(factors)
    )
    if feature_frame.empty:
        return {
            "status": "rejected",
            "reason": "no_candidate_factors",
            "summary": _summary_from_windows([]),
            "windows": [],
            "factor_summary": {},
        }
    candidate_names = [
        name
        for name in sorted(factors)
        if name in feature_frame.columns
    ]
    if not candidate_names:
        return {
            "status": "rejected",
            "reason": "no_candidate_factors",
            "summary": _summary_from_windows([]),
            "windows": [],
            "factor_summary": {},
        }
    candidate_row_valid = _candidate_non_null_row_mask(
        feature_frame,
        candidate_names,
    )
    if not bool(candidate_row_valid.all()):
        feature_frame = feature_frame.loc[
            candidate_row_valid
        ].copy(deep=False)
    if feature_frame.empty:
        return {
            "status": "rejected",
            "reason": "no_candidate_factors",
            "summary": _summary_from_windows([]),
            "windows": [],
            "factor_summary": {},
        }
    target = target_col
    if target not in labels.columns:
        if "forward_return_pct" not in labels.columns:
            raise KeyError(f"labels missing target column: {target_col}")
        target = "forward_return_pct"
    label_input_columns = [
        column
        for column in (
            "trade_date",
            "ts_code",
            target,
            "exit_date",
            "research_evaluable",
            "execution_blocked",
        )
        if column in labels
    ]
    label_frame = labels.loc[:, label_input_columns].copy()
    label_frame["trade_date"] = pd.to_datetime(
        label_frame["trade_date"],
        errors="coerce",
    ).dt.normalize()
    label_frame["ts_code"] = _code_key_series(
        label_frame["ts_code"]
    )
    label_frame[target] = pd.to_numeric(
        label_frame[target],
        errors="coerce",
    )
    if "research_evaluable" in label_frame.columns:
        label_frame = label_frame[
            label_frame["research_evaluable"].map(_true_marker)
        ]
    if "execution_blocked" in label_frame.columns:
        label_frame = label_frame[
            ~label_frame["execution_blocked"].map(_true_marker)
        ]
    label_columns = ["trade_date", "ts_code", target]
    if "exit_date" in label_frame.columns:
        label_frame["exit_date"] = pd.to_datetime(
            label_frame["exit_date"],
            errors="coerce",
        ).dt.normalize()
        label_columns.append("exit_date")
    feature_frame["trade_date"] = pd.to_datetime(
        feature_frame["trade_date"],
        errors="coerce",
    ).dt.normalize()
    feature_frame["ts_code"] = _code_key_series(
        feature_frame["ts_code"]
    )
    key_columns = ["trade_date", "ts_code"]
    if (
        feature_frame.duplicated(key_columns).any()
        or label_frame.duplicated(key_columns).any()
    ):
        raise pd.errors.MergeError(
            "factor and label keys must be one-to-one"
        )
    feature_index = pd.MultiIndex.from_frame(
        feature_frame[key_columns]
    )
    indexed_labels = label_frame[label_columns].set_index(
        key_columns
    )
    aligned_labels = indexed_labels.reindex(feature_index)
    merged = feature_frame
    merged[target] = aligned_labels[target].to_numpy()
    if "exit_date" in aligned_labels:
        merged["exit_date"] = aligned_labels["exit_date"].to_numpy()
    merged["_label_valid"] = merged[target].notna().to_numpy()
    dates = sorted(
        merged.loc[
            merged["_label_valid"],
            "trade_date",
        ].dropna().unique()
    )
    del aligned_labels
    del indexed_labels
    del label_frame
    del feature_index
    windows = purged_walk_forward_windows(
        dates,
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
        purge_days=purge_days,
        min_partial_test_days=min_partial_test_days,
    )
    reports: list[dict[str, Any]] = []
    factor_oos: dict[str, list[float]] = {
        name: [] for name in candidate_names
    }
    factor_selected: dict[str, int] = {
        name: 0 for name in candidate_names
    }
    factor_oos_returns: dict[str, list[float]] = {
        name: [] for name in candidate_names
    }

    for window in windows:
        train_mask = (
            merged["_label_valid"]
            & merged["trade_date"].isin(window.train_dates)
        )
        if "exit_date" in merged.columns:
            train_mask &= (
                merged["exit_date"].notna()
                & merged["exit_date"].lt(window.test_dates[0])
            )
        test_mask = (
            merged["_label_valid"]
            & merged["trade_date"].isin(window.test_dates)
        )
        train_dates = sorted(
            merged.loc[
                train_mask,
                "trade_date",
            ].dropna().unique()
        )
        calibration_days = min(
            max(10, int(test_days)),
            max(0, len(train_dates) - int(purge_days) - 20),
        )
        fit_count = (
            len(train_dates)
            - int(purge_days)
            - calibration_days
        )
        fit_dates = train_dates[:fit_count]
        calibration_dates = (
            train_dates[-calibration_days:]
            if calibration_days
            else []
        )
        fit_mask = (
            train_mask
            & merged["trade_date"].isin(fit_dates)
        )
        calibration_mask = (
            train_mask
            & merged["trade_date"].isin(calibration_dates)
        )
        if (
            "exit_date" in merged.columns
            and calibration_dates
        ):
            fit_mask &= (
                merged["exit_date"].notna()
                & merged["exit_date"].lt(calibration_dates[0])
            )
        fit_columns = list(
            dict.fromkeys(
                [
                    "trade_date",
                    "ts_code",
                    target,
                    *candidate_names,
                    *(
                        name
                        for name in MARKET_REGIME_SOURCE_COLUMNS
                        if name in merged.columns
                    ),
                ]
            )
        )
        fit = merged.loc[fit_mask, fit_columns]
        train_sample_count = int(train_mask.sum())
        fit_sample_count = int(fit_mask.sum())
        calibration_sample_count = int(
            calibration_mask.sum()
        )
        test_sample_count = int(test_mask.sum())
        if (
            "exit_date" in merged.columns
            and bool(train_mask.any())
        ):
            train_exit = merged.loc[train_mask, "exit_date"]
            train_label_max_exit = (
                pd.Timestamp(train_exit.max()).strftime("%Y-%m-%d")
                if train_exit.notna().any()
                else None
            )
        else:
            train_label_max_exit = None
        diagnostics: dict[str, dict[str, Any]] = {}
        p_values: dict[str, float] = {}
        evidence_scores: dict[str, float] = {}
        directions: dict[str, float] = {}
        fit_ic = _daily_rank_ic_matrix(
            fit,
            candidate_names,
            target,
            min_cross_section=min_cross_section,
        )
        for name in candidate_names:
            ic = (
                fit_ic[name].dropna()
                if name in fit_ic
                else pd.Series(dtype=float)
            )
            stats = _ic_diagnostics(ic)
            diagnostics[name] = stats
            p_values[name] = float(stats["p_value"])
            direction = (
                1.0
                if float(stats["mean_rank_ic"]) >= 0
                else -1.0
            )
            directions[name] = direction
            evidence_scores[name] = (
                abs(float(stats["mean_rank_ic"]))
                * math.sqrt(max(1, int(stats["ic_count"])))
                / max(float(stats["ic_std"]), 1e-6)
            )
        del fit_ic
        accepted = benjamini_hochberg(
            p_values,
            alpha=fdr_alpha,
        )
        eligible = [
            name
            for name in candidate_names
            if name in accepted
            and int(diagnostics[name]["ic_count"]) >= 3
            and abs(float(diagnostics[name]["mean_rank_ic"])) >= 0.005
        ]
        selected = decorrelate_factors(
            fit,
            eligible,
            evidence_scores=evidence_scores,
            max_abs_correlation=max_abs_correlation,
        )[: int(max_factors)]
        score_weights = _fit_score_weights(
            selected,
            diagnostics,
        )
        market_fit_columns = list(
            dict.fromkeys(
                [
                    "trade_date",
                    target,
                    *(
                        name
                        for name in MARKET_REGIME_SOURCE_COLUMNS
                        if name in fit.columns
                    ),
                ]
            )
        )
        market_fit = fit.loc[:, market_fit_columns]
        del fit
        market_model = _fit_market_regime_model(
            market_fit,
            target,
            min_days=max(
                20,
                min(40, max(20, len(fit_dates) // 2)),
            ),
        )
        del market_fit
        scoring_columns = list(
            dict.fromkeys(
                [
                    "trade_date",
                    "ts_code",
                    target,
                    *selected,
                    *(
                        name
                        for name in MARKET_REGIME_SOURCE_COLUMNS
                        if name in merged.columns
                    ),
                ]
            )
        )
        calibration = merged.loc[
            calibration_mask,
            scoring_columns,
        ]
        test = merged.loc[
            test_mask,
            scoring_columns,
        ]
        calibration_market_prediction = _predict_market_regime(
            market_model,
            calibration,
        )
        market_model_applied = bool(
            market_model.get("status") == "fitted"
            and not calibration_market_prediction.empty
        )
        if market_model_applied:
            calibration_trade_dates = set(
                calibration_market_prediction[
                    calibration_market_prediction > 0.0
                ].index
            )
            calibration_for_scoring = calibration[
                calibration["trade_date"].isin(
                    calibration_trade_dates
                )
            ]
        else:
            calibration_trade_dates = set(calibration_dates)
            calibration_for_scoring = calibration
        calibration_scored = _composite_scores(
            calibration_for_scoring,
            selected,
            directions,
            score_weights,
        )
        calibration_picks = _top_daily_picks(
            calibration_scored,
            target,
            top_k=top_k,
        )
        calibration_metrics = _portfolio_metrics(
            calibration_picks,
            target,
        )
        calibration_passed = bool(
            selected
            and int(calibration_metrics["trade_count"])
            >= int(top_k) * 3
            and float(calibration_metrics["avg_return_pct"]) > 0.0
            and float(calibration_metrics["total_return_pct"]) > 0.0
            and float(calibration_metrics["max_drawdown_pct"]) >= -10.0
        )
        if calibration_passed:
            for name in selected:
                factor_selected[name] += 1

        report: dict[str, Any] = {
            "window_id": window.window_id,
            "train_start": window.train_dates[0].strftime("%Y-%m-%d"),
            "train_end": window.train_dates[-1].strftime("%Y-%m-%d"),
            "test_start": window.test_dates[0].strftime("%Y-%m-%d"),
            "test_end": window.test_dates[-1].strftime("%Y-%m-%d"),
            "test_dates": [
                value.strftime("%Y-%m-%d")
                for value in window.test_dates
            ],
            "purged_dates": [
                value.strftime("%Y-%m-%d")
                for value in window.purged_dates
            ],
            "train_sample_count": train_sample_count,
            "fit_sample_count": fit_sample_count,
            "calibration_sample_count": calibration_sample_count,
            "fit_end": (
                pd.Timestamp(fit_dates[-1]).strftime("%Y-%m-%d")
                if fit_dates
                else None
            ),
            "calibration_start": (
                pd.Timestamp(calibration_dates[0]).strftime(
                    "%Y-%m-%d"
                )
                if calibration_dates
                else None
            ),
            "calibration_end": (
                pd.Timestamp(calibration_dates[-1]).strftime(
                    "%Y-%m-%d"
                )
                if calibration_dates
                else None
            ),
            "calibration": {
                key: value
                for key, value in calibration_metrics.items()
                if key != "daily_returns_pct"
            },
            "calibration_passed": calibration_passed,
            "market_regime": {
                **market_model,
                "threshold_pct": 0.0,
                "applied": market_model_applied,
                "calibration_trade_date_count": len(
                    calibration_trade_dates
                ),
                "calibration_total_date_count": len(
                    calibration_dates
                ),
            },
            "test_sample_count": test_sample_count,
            "train_label_max_exit": train_label_max_exit,
            "selected_factors": selected,
            "score_weights": score_weights,
            "factor_diagnostics": {
                name: {
                    **diagnostics[name],
                    "direction": int(directions[name]),
                    "fdr_accepted": name in accepted,
                }
                for name in candidate_names
            },
        }
        if not selected or test.empty or not calibration_passed:
            report.update(
                {
                    "status": (
                        "calibration_rejected"
                        if selected and not calibration_passed
                        else "no_selected_factors"
                    ),
                    "oos_rank_ic": None,
                    **_portfolio_metrics(pd.DataFrame(), target),
                }
            )
            reports.append(report)
            continue

        if market_model_applied:
            test_market_prediction = _predict_market_regime(
                market_model,
                test,
            )
            test_trade_dates = set(
                test_market_prediction[
                    test_market_prediction > 0.0
                ].index
            )
            test = test[
                test["trade_date"].isin(test_trade_dates)
            ]
        else:
            test_trade_dates = set(window.test_dates)
        report["market_regime"]["test_trade_date_count"] = len(
            test_trade_dates
        )
        report["market_regime"]["test_total_date_count"] = len(
            window.test_dates
        )
        if test.empty:
            report.update(
                {
                    "status": "market_regime_no_trade",
                    "oos_rank_ic": None,
                    **_portfolio_metrics(pd.DataFrame(), target),
                }
            )
            reports.append(report)
            continue
        test = _composite_scores(
            test,
            selected,
            directions,
            score_weights,
        )
        oos_matrix = _daily_rank_ic_matrix(
            test,
            selected,
            target,
            min_cross_section=min_cross_section,
        )
        oos_factor_diagnostics = {}
        for name in selected:
            oriented = (
                pd.to_numeric(test[name], errors="coerce")
                * directions[name]
            )
            oos_ic = (
                oos_matrix[name].dropna() * directions[name]
                if name in oos_matrix
                else pd.Series(dtype=float)
            )
            stats = _ic_diagnostics(oos_ic)
            individual = test.assign(
                _factor_score=oriented.groupby(
                    test["trade_date"]
                ).rank(
                    pct=True,
                    method="average",
                )
            )
            individual_picks = _top_daily_picks(
                individual,
                target,
                top_k=top_k,
                score_column="_factor_score",
            )
            individual_metrics = _portfolio_metrics(
                individual_picks,
                target,
            )
            oos_factor_diagnostics[name] = {
                **stats,
                "avg_return_pct": individual_metrics[
                    "avg_return_pct"
                ],
                "total_return_pct": individual_metrics[
                    "total_return_pct"
                ],
                "hit_rate": individual_metrics["hit_rate"],
                "trade_count": individual_metrics["trade_count"],
            }
            factor_oos[name].append(float(stats["mean_rank_ic"]))
            factor_oos_returns[name].append(
                float(individual_metrics["avg_return_pct"])
            )
        composite_ic = _daily_rank_ic(
            test,
            "_composite_score",
            target,
            min_cross_section=min_cross_section,
        )
        picks = _top_daily_picks(
            test,
            target,
            top_k=top_k,
        )
        portfolio = _portfolio_metrics(picks, target)
        report.update(
            {
                "status": "evaluated",
                "oos_rank_ic": (
                    round(float(composite_ic.mean()), 8)
                    if not composite_ic.empty
                    else None
                ),
                "oos_factor_diagnostics": oos_factor_diagnostics,
                **portfolio,
            }
        )
        reports.append(report)

    summary = _summary_from_windows(reports)
    accepted_status = (
        int(summary["window_count"]) >= 3
        and int(summary["trade_count"]) >= 30
        and float(summary["oos_rank_ic"]) >= 0.01
        and float(summary["positive_window_rate"]) >= 0.6
        and float(summary["avg_return_pct"]) > 0.0
        and float(summary["total_return_pct"]) > 0.0
        and float(summary["max_drawdown_pct"]) >= -20.0
    )
    factor_summary = {}
    minimum_selected = max(
        3,
        math.ceil(max(1, len(reports)) / 2),
    )
    for name in candidate_names:
        oos_values = factor_oos[name]
        return_values = factor_oos_returns[name]
        oos_mean = float(np.mean(oos_values)) if oos_values else 0.0
        oos_return_mean = (
            float(np.mean(return_values))
            if return_values
            else 0.0
        )
        factor_summary[name] = {
            "selected_windows": factor_selected[name],
            "oos_window_count": len(oos_values),
            "oos_mean_rank_ic": round(oos_mean, 8),
            "positive_oos_window_rate": round(
                (
                    sum(value > 0 for value in oos_values)
                    / len(oos_values)
                )
                if oos_values
                else 0.0,
                6,
            ),
            "oos_mean_return_pct": round(oos_return_mean, 8),
            "positive_return_window_rate": round(
                (
                    sum(value > 0 for value in return_values)
                    / len(return_values)
                )
                if return_values
                else 0.0,
                6,
            ),
            "promotion_eligible": bool(
                factor_selected[name] >= minimum_selected
                and len(oos_values) >= minimum_selected
                and oos_mean >= 0.01
                and oos_return_mean > 0.0
                and (
                    sum(value > 0 for value in return_values)
                    / len(return_values)
                    if return_values
                    else 0.0
                )
                >= 0.6
            ),
        }
    return {
        "status": "accepted" if accepted_status else "rejected",
        "reason": (
            "all_oos_gates_passed"
            if accepted_status
            else "oos_gates_not_met"
        ),
        "target_col": target,
        "selection_policy": {
            "fdr": "benjamini_hochberg_on_newey_west_hac",
            "fdr_alpha": float(fdr_alpha),
            "max_abs_correlation": float(max_abs_correlation),
            "purge_days": int(purge_days),
            "test_weights_refit": False,
            "nested_calibration": (
                "fit_hac_fdr_then_positive_cost_calibration"
            ),
            "market_regime_gate": (
                "fit_only_ridge_prediction_above_zero"
            ),
            "execution_filter": (
                "research_evaluable_and_not_execution_blocked"
            ),
        },
        "summary": summary,
        "windows": reports,
        "factor_summary": factor_summary,
    }


def _execution_factor_summary(
    candidate_names: Sequence[str],
    windows: Sequence[Mapping[str, Any]],
    *,
    window_count: int,
) -> dict[str, dict[str, Any]]:
    minimum_selected = max(
        3,
        math.ceil(max(1, int(window_count)) / 2),
    )
    result = {}
    for name in candidate_names:
        selected_windows = 0
        oos_values = []
        return_values = []
        for window in windows:
            if (
                window.get("status") != "evaluated"
                or int(window.get("trade_count") or 0) <= 0
                or name not in (window.get("selected_factors") or [])
            ):
                continue
            selected_windows += 1
            details = (
                (window.get("oos_factor_diagnostics") or {}).get(name)
                or {}
            )
            if details.get("mean_rank_ic") is not None:
                oos_values.append(float(details["mean_rank_ic"]))
            if details.get("avg_return_pct") is not None:
                return_values.append(float(details["avg_return_pct"]))
        oos_mean = float(np.mean(oos_values)) if oos_values else 0.0
        return_mean = (
            float(np.mean(return_values))
            if return_values
            else 0.0
        )
        positive_oos_rate = (
            sum(value > 0.0 for value in oos_values) / len(oos_values)
            if oos_values
            else 0.0
        )
        positive_return_rate = (
            sum(value > 0.0 for value in return_values)
            / len(return_values)
            if return_values
            else 0.0
        )
        result[str(name)] = {
            "selected_windows": selected_windows,
            "oos_window_count": len(oos_values),
            "oos_mean_rank_ic": round(oos_mean, 8),
            "positive_oos_window_rate": round(
                positive_oos_rate,
                6,
            ),
            "oos_mean_return_pct": round(return_mean, 8),
            "positive_return_window_rate": round(
                positive_return_rate,
                6,
            ),
            "promotion_eligible": bool(
                selected_windows >= minimum_selected
                and len(oos_values) >= minimum_selected
                and oos_mean >= 0.01
                and return_mean > 0.0
                and positive_return_rate >= 0.6
            ),
        }
    return result


def _execution_window_signature(
    window: Mapping[str, Any],
) -> tuple[Any, ...]:
    return (
        window.get("train_start"),
        window.get("train_end"),
        window.get("fit_end"),
        window.get("calibration_start"),
        window.get("calibration_end"),
        tuple(window.get("purged_dates") or []),
        tuple(window.get("test_dates") or []),
    )


def _combine_execution_discovery_reports(
    candidate_names: Sequence[str],
    reports: Mapping[str, Mapping[str, Any]],
    *,
    execution_metadata: Mapping[str, Mapping[str, Any]] | None,
    target_col: str,
    effective_fdr_alpha: float,
) -> dict[str, Any]:
    names = sorted(str(name) for name in reports)
    if not names:
        raise ValueError("execution reports must not be empty")
    metadata = {
        str(name): dict(details)
        for name, details in (execution_metadata or {}).items()
        if isinstance(details, Mapping)
    }
    window_ids = sorted(
        {
            int(window["window_id"])
            for report in reports.values()
            for window in report.get("windows") or []
        }
    )
    by_execution = {
        name: {
            int(window["window_id"]): window
            for window in reports[name].get("windows") or []
        }
        for name in names
    }
    selected_windows = []
    for window_id in window_ids:
        aligned_windows = {
            name: by_execution[name].get(window_id)
            for name in names
        }
        signatures = {
            _execution_window_signature(window)
            for window in aligned_windows.values()
            if window is not None
        }
        if (
            any(window is None for window in aligned_windows.values())
            or len(signatures) != 1
        ):
            template = next(
                (
                    window
                    for window in aligned_windows.values()
                    if window is not None
                ),
                {"window_id": window_id},
            )
            mismatch = {
                key: template[key]
                for key in (
                    "window_id",
                    "train_start",
                    "train_end",
                    "fit_end",
                    "calibration_start",
                    "calibration_end",
                    "purged_dates",
                    "test_start",
                    "test_end",
                    "test_dates",
                )
                if key in template
            }
            mismatch.update(
                {
                    "status": "execution_window_mismatch",
                    "reason": (
                        "execution_label_calendars_not_aligned"
                    ),
                    "calibration_passed": False,
                    "selected_factors": [],
                    "score_weights": {},
                    "selected_execution": None,
                    "execution_selection": {
                        "policy": "strict_window_alignment",
                        "selected": None,
                        "candidates": [
                            {
                                "execution": name,
                                "metadata": metadata.get(name, {}),
                                "status": (
                                    str(window.get("status") or "")
                                    if window is not None
                                    else "missing_window"
                                ),
                                "window_signature": (
                                    list(
                                        _execution_window_signature(
                                            window
                                        )
                                    )
                                    if window is not None
                                    else None
                                ),
                            }
                            for name, window in aligned_windows.items()
                        ],
                    },
                    "oos_rank_ic": None,
                    **_portfolio_metrics(
                        pd.DataFrame(),
                        target_col,
                    ),
                }
            )
            selected_windows.append(mismatch)
            continue
        candidates = []
        eligible = []
        for index, name in enumerate(names):
            window = by_execution[name].get(window_id)
            if window is None:
                continue
            calibration = dict(window.get("calibration") or {})
            average_return = float(
                calibration.get("avg_return_pct") or 0.0
            )
            max_drawdown = float(
                calibration.get("max_drawdown_pct") or 0.0
            )
            selection_score = (
                average_return
                - 0.10 * abs(min(0.0, max_drawdown))
            )
            details = {
                "execution": name,
                "metadata": metadata.get(name, {}),
                "status": str(window.get("status") or ""),
                "calibration_passed": bool(
                    window.get("calibration_passed")
                ),
                "calibration": calibration,
                "selection_score": round(selection_score, 8),
                "selected_factors": list(
                    window.get("selected_factors") or []
                ),
            }
            candidates.append(details)
            if details["calibration_passed"]:
                hold_days = int(
                    details["metadata"].get("hold_days")
                    or 1_000_000
                )
                eligible.append(
                    (
                        selection_score,
                        average_return,
                        max_drawdown,
                        -hold_days,
                        -index,
                        name,
                    )
                )
        if eligible:
            selected_name = max(eligible)[-1]
            selected = dict(by_execution[selected_name][window_id])
        else:
            selected_name = None
            template = next(
                (
                    by_execution[name][window_id]
                    for name in names
                    if window_id in by_execution[name]
                ),
                {"window_id": window_id},
            )
            selected = dict(template)
            selected.update(
                {
                    "status": "execution_calibration_rejected",
                    "selected_factors": [],
                    "score_weights": {},
                    "oos_rank_ic": None,
                    **_portfolio_metrics(
                        pd.DataFrame(),
                        target_col,
                    ),
                }
            )
        selected["selected_execution"] = selected_name
        selected["execution_selection"] = {
            "policy": (
                "max_calibration_avg_return_minus_10pct_drawdown"
            ),
            "selected": selected_name,
            "candidates": candidates,
        }
        selected_windows.append(selected)

    summary = _summary_from_windows(selected_windows)
    accepted_status = (
        int(summary["window_count"]) >= 3
        and int(summary["trade_count"]) >= 30
        and float(summary["oos_rank_ic"]) >= 0.01
        and float(summary["positive_window_rate"]) >= 0.6
        and float(summary["avg_return_pct"]) > 0.0
        and float(summary["total_return_pct"]) > 0.0
        and float(summary["max_drawdown_pct"]) >= -20.0
    )
    factor_summary = _execution_factor_summary(
        sorted(str(name) for name in candidate_names),
        selected_windows,
        window_count=len(selected_windows),
    )
    first_policy = dict(
        reports[names[0]].get("selection_policy") or {}
    )
    first_policy.update(
        {
            "execution_selection": "calibration_only",
            "execution_window_alignment": (
                "strict_train_calibration_test_signature"
            ),
            "execution_candidates": names,
            "execution_family_correction": "bonferroni",
            "effective_fdr_alpha": float(effective_fdr_alpha),
            "test_execution_refit": False,
        }
    )
    return {
        "status": "accepted" if accepted_status else "rejected",
        "reason": (
            "all_oos_gates_passed"
            if accepted_status
            else "oos_gates_not_met"
        ),
        "target_col": target_col,
        "selection_policy": first_policy,
        "summary": summary,
        "windows": selected_windows,
        "factor_summary": factor_summary,
    }


def run_walk_forward_execution_discovery(
    factors: Mapping[str, pd.DataFrame],
    execution_labels: Mapping[str, pd.DataFrame],
    *,
    execution_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    target_col: str = "live_adjusted_return_pct",
    fdr_alpha: float = 0.05,
    _prepared_feature_frame: pd.DataFrame | None = None,
    **discovery_kwargs: Any,
) -> dict[str, Any]:
    labels_by_execution = {
        str(name): frame
        for name, frame in execution_labels.items()
        if isinstance(frame, pd.DataFrame)
    }
    if not labels_by_execution:
        raise ValueError("execution_labels must not be empty")
    metadata = {
        str(name): dict(details)
        for name, details in (execution_metadata or {}).items()
        if isinstance(details, Mapping)
    }
    names = sorted(labels_by_execution)
    effective_alpha = float(fdr_alpha) / len(names)
    prepared_feature_frame = (
        _prepared_feature_frame
        if isinstance(_prepared_feature_frame, pd.DataFrame)
        else _factor_long_frame(factors)
    )
    reports = {
        name: run_walk_forward_discovery(
            factors,
            labels_by_execution[name],
            target_col=target_col,
            fdr_alpha=effective_alpha,
            _prepared_feature_frame=prepared_feature_frame,
            **discovery_kwargs,
        )
        for name in names
    }
    return _combine_execution_discovery_reports(
        sorted(str(name) for name in factors),
        reports,
        execution_metadata=metadata,
        target_col=target_col,
        effective_fdr_alpha=effective_alpha,
    )


def _run_generated_execution_discovery(
    factors: Mapping[str, pd.DataFrame],
    prepared_feature_frame: pd.DataFrame,
    panel: Mapping[str, pd.DataFrame],
    *,
    hold_days_candidates: Sequence[int],
    stop_loss: float,
    take_profit: float,
    fdr_alpha: float,
    discovery_kwargs: Mapping[str, Any],
) -> tuple[dict[str, Any], str | None]:
    holds = [int(value) for value in hold_days_candidates]
    if not holds:
        raise ValueError("hold_days_candidates must not be empty")
    names = [f"hold_{value}" for value in holds]
    effective_alpha = float(fdr_alpha) / len(names)
    metadata = {
        f"hold_{value}": {"hold_days": value}
        for value in holds
    }
    reports = {}
    maximum_exit: pd.Timestamp | None = None
    for value, name in zip(holds, names):
        execution_labels = _build_chunked_execution_labels(
            panel,
            hold_days=value,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        if (
            "exit_date" in execution_labels
            and execution_labels["exit_date"].notna().any()
        ):
            current_exit = pd.Timestamp(
                execution_labels["exit_date"].max()
            ).normalize()
            maximum_exit = (
                current_exit
                if maximum_exit is None
                else max(maximum_exit, current_exit)
            )
        reports[name] = run_walk_forward_discovery(
            factors,
            execution_labels,
            fdr_alpha=effective_alpha,
            _prepared_feature_frame=prepared_feature_frame,
            **dict(discovery_kwargs),
        )
        del execution_labels
        _release_research_memory()
    return (
        _combine_execution_discovery_reports(
            sorted(str(name) for name in factors),
            reports,
            execution_metadata=metadata,
            target_col="live_adjusted_return_pct",
            effective_fdr_alpha=effective_alpha,
        ),
        (
            maximum_exit.strftime("%Y-%m-%d")
            if maximum_exit is not None
            else None
        ),
    )


def _release_research_memory() -> None:
    gc.collect()
    try:
        if os.name == "posix":
            malloc_trim = getattr(
                ctypes.CDLL(None),
                "malloc_trim",
                None,
            )
            if malloc_trim is not None:
                malloc_trim(0)
    except (AttributeError, OSError):
        pass


def _run_generated_dual_execution_discovery(
    close_factors: Mapping[str, pd.DataFrame],
    auction_factors: Mapping[str, pd.DataFrame],
    prepared_feature_frame: pd.DataFrame,
    panel: Mapping[str, pd.DataFrame],
    *,
    hold_days_candidates: Sequence[int],
    stop_loss: float,
    take_profit: float,
    fdr_alpha: float,
    discovery_kwargs: Mapping[str, Any],
    prebuilt_execution_labels: MutableMapping[
        int,
        pd.DataFrame,
    ]
    | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    holds = [int(value) for value in hold_days_candidates]
    if not holds:
        raise ValueError("hold_days_candidates must not be empty")
    names = [f"hold_{value}" for value in holds]
    effective_alpha = float(fdr_alpha) / len(names)
    metadata = {
        f"hold_{value}": {"hold_days": value}
        for value in holds
    }
    cached = prebuilt_execution_labels or {}
    close_reports = {}
    auction_reports = {}
    maximum_exit: pd.Timestamp | None = None
    for value, name in zip(holds, names):
        execution_labels = cached.pop(value, None)
        if execution_labels is None:
            execution_labels = _build_chunked_execution_labels(
                panel,
                hold_days=value,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
        if (
            "exit_date" in execution_labels
            and execution_labels["exit_date"].notna().any()
        ):
            current_exit = pd.Timestamp(
                execution_labels["exit_date"].max()
            ).normalize()
            maximum_exit = (
                current_exit
                if maximum_exit is None
                else max(maximum_exit, current_exit)
            )
        close_reports[name] = run_walk_forward_discovery(
            close_factors,
            execution_labels,
            fdr_alpha=effective_alpha,
            _prepared_feature_frame=prepared_feature_frame,
            **dict(discovery_kwargs),
        )
        _release_research_memory()
        auction_reports[name] = run_walk_forward_discovery(
            auction_factors,
            execution_labels,
            fdr_alpha=effective_alpha,
            _prepared_feature_frame=prepared_feature_frame,
            **dict(discovery_kwargs),
        )
        del execution_labels
        _release_research_memory()
    cached.clear()
    return (
        _combine_execution_discovery_reports(
            sorted(str(name) for name in close_factors),
            close_reports,
            execution_metadata=metadata,
            target_col="live_adjusted_return_pct",
            effective_fdr_alpha=effective_alpha,
        ),
        _combine_execution_discovery_reports(
            sorted(str(name) for name in auction_factors),
            auction_reports,
            execution_metadata=metadata,
            target_col="live_adjusted_return_pct",
            effective_fdr_alpha=effective_alpha,
        ),
        (
            maximum_exit.strftime("%Y-%m-%d")
            if maximum_exit is not None
            else None
        ),
    )


def _auction_candidate_factors(
    labels: pd.DataFrame,
    auction: pd.DataFrame | None,
) -> tuple[dict[str, pd.DataFrame], dict[str, float | int]]:
    empty_quality: dict[str, float | int] = {
        "eligible_rows": 0,
        "matched_rows": 0,
        "coverage": 0.0,
    }
    if auction is None or auction.empty:
        return {}, empty_quality
    columns = ["trade_date", "ts_code"]
    if "entry_date" in labels.columns:
        columns.append("entry_date")
    aligned = align_auction_confirmation(
        labels[columns].drop_duplicates(
            ["trade_date", "ts_code"],
            keep="last",
        ),
        auction,
    )
    if aligned.empty:
        return {}, empty_quality
    entry_dates = pd.to_datetime(
        (
            aligned["entry_date"]
            if "entry_date" in aligned
            else pd.Series(pd.NaT, index=aligned.index)
        ),
        errors="coerce",
    )
    eligible = entry_dates.notna()
    matched = (
        eligible
        & aligned["auction_trade_date"].notna()
    )
    eligible_rows = int(eligible.sum())
    matched_rows = int(matched.sum())
    alignment_coverage = (
        matched_rows / eligible_rows
        if eligible_rows
        else 0.0
    )
    quality: dict[str, float | int] = {
        "eligible_rows": eligible_rows,
        "matched_rows": matched_rows,
        "coverage": float(alignment_coverage),
    }
    if alignment_coverage < AUCTION_MIN_ALIGNMENT_COVERAGE:
        return {}, quality
    aligned["trade_date"] = pd.to_datetime(
        aligned["trade_date"],
        errors="coerce",
    ).dt.normalize()
    aligned["ts_code"] = (
        aligned["ts_code"]
        .astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.zfill(6)
    )
    source_columns = {
        "auction_gap": "auction_gap",
        "auction_turnover_rate": "auction_turnover_rate",
        "auction_volume_ratio": "auction_volume_ratio",
        "auction_amount": "auction_amount",
    }
    factors = {}
    for name, source in source_columns.items():
        factor = (
            aligned.pivot(
                index="trade_date",
                columns="ts_code",
                values=source,
            )
            .sort_index()
            .sort_index(axis=1)
            .astype(np.float32)
        )
        if factor.notna().to_numpy().any():
            factors[name] = factor
    return factors, quality


def _factor_catalog(
    factor_names: Sequence[str],
) -> dict[str, dict[str, str]]:
    formulas = {
        **BASE_FACTOR_FORMULAS,
        **DAILY_BASIC_FACTOR_FORMULAS,
        **STRUCTURAL_FACTOR_FORMULAS,
        **AUCTION_FACTOR_FORMULAS,
    }
    catalog = {}
    for name in sorted(set(str(value) for value in factor_names)):
        if name in formulas:
            formula = formulas[name]
        elif "__x__" in name:
            left, right = name.split("__x__", 1)
            formula = (
                f"(rank({left}) - 0.5) "
                f"* (rank({right}) - 0.5)"
            )
        else:
            formula = name
        auction_based = (
            name in AUCTION_FACTOR_FORMULAS
            or any(
                part in AUCTION_FACTOR_FORMULAS
                for part in name.split("__x__")
            )
        )
        catalog[name] = {
            "formula": formula,
            "available_at": (
                "entry_date 09:25"
                if auction_based
                else "signal_date 15:00"
            ),
            "decision_scope": (
                "auction_confirmation"
                if auction_based
                else "next_session_candidate_pool"
            ),
        }
    return catalog


def _promotion_candidates(
    report: Mapping[str, Any],
    *,
    scope: str,
) -> list[dict[str, Any]]:
    summary = report.get("factor_summary")
    if not isinstance(summary, Mapping):
        return []
    rows = []
    for name, raw in summary.items():
        details = raw if isinstance(raw, Mapping) else {}
        if not details.get("promotion_eligible"):
            continue
        rows.append(
            {
                "factor": str(name),
                "scope": scope,
                "selected_windows": int(
                    details.get("selected_windows") or 0
                ),
                "oos_window_count": int(
                    details.get("oos_window_count") or 0
                ),
                "oos_mean_rank_ic": float(
                    details.get("oos_mean_rank_ic") or 0.0
                ),
                "positive_oos_window_rate": float(
                    details.get("positive_oos_window_rate") or 0.0
                ),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            -row["oos_mean_rank_ic"],
            -row["selected_windows"],
            row["factor"],
        ),
    )


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    material = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _maximum_label_exit(
    execution_labels: Mapping[str, pd.DataFrame],
) -> str | None:
    values = []
    for frame in execution_labels.values():
        if not isinstance(frame, pd.DataFrame) or "exit_date" not in frame:
            continue
        parsed = pd.to_datetime(
            frame["exit_date"],
            errors="coerce",
        ).dropna()
        if not parsed.empty:
            values.append(pd.Timestamp(parsed.max()))
    return max(values).strftime("%Y-%m-%d") if values else None


_COMPACT_EXECUTION_LABEL_COLUMNS = (
    "trade_date",
    "ts_code",
    "entry_date",
    "exit_date",
    "live_adjusted_return_pct",
    "forward_return_pct",
    "research_evaluable",
    "execution_blocked",
)


def _compact_execution_labels(
    labels: pd.DataFrame,
) -> pd.DataFrame:
    if not isinstance(labels, pd.DataFrame):
        raise TypeError("labels must be a DataFrame")
    result = labels[
        [
            column
            for column in _COMPACT_EXECUTION_LABEL_COLUMNS
            if column in labels.columns
        ]
    ].copy()
    for column in ("trade_date", "entry_date", "exit_date"):
        if column in result:
            result[column] = pd.to_datetime(
                result[column],
                errors="coerce",
            ).dt.normalize()
    if "ts_code" in result:
        result["ts_code"] = _code_key_series(result["ts_code"])
    return result


def _build_chunked_execution_labels(
    panel: Mapping[str, pd.DataFrame],
    *,
    hold_days: int,
    stop_loss: float,
    take_profit: float,
    chunk_size: int = LABEL_CODE_CHUNK_SIZE,
) -> pd.DataFrame:
    from analytics import relay_factor_mining

    if (
        isinstance(chunk_size, bool)
        or int(chunk_size) != chunk_size
        or int(chunk_size) < 1
    ):
        raise ValueError("chunk_size must be a positive integer")
    close = panel.get("close")
    if not isinstance(close, pd.DataFrame) or close.empty:
        raise ValueError("label panel close matrix is empty")
    codes = list(close.columns)
    compact_chunks = []
    for offset in range(0, len(codes), int(chunk_size)):
        chunk_codes = codes[offset : offset + int(chunk_size)]
        chunk_panel = {}
        for name, frame in panel.items():
            if (
                isinstance(frame, pd.DataFrame)
                and set(chunk_codes).issubset(frame.columns)
            ):
                chunk_panel[name] = frame.loc[:, chunk_codes]
            else:
                chunk_panel[name] = frame
        generated = relay_factor_mining.build_label_frame(
            chunk_panel,
            hold_days=hold_days,
            stop_loss=stop_loss,
            take_profit=take_profit,
            compact_diagnostics=True,
        )
        compact_chunks.append(
            _compact_execution_labels(generated)
        )
        del generated
        del chunk_panel
    if not compact_chunks:
        return pd.DataFrame()
    combined = pd.concat(compact_chunks, ignore_index=True)
    compact_chunks.clear()
    combined.sort_values(
        ["trade_date", "ts_code"],
        kind="stable",
        ignore_index=True,
        inplace=True,
    )
    return combined


def run_short_factor_research(
    panel: Mapping[str, pd.DataFrame],
    *,
    labels: pd.DataFrame | None = None,
    daily_basic: pd.DataFrame | None = None,
    auction: pd.DataFrame | None = None,
    expected_codes: Sequence[str] | None = None,
    out_dir: str | Path | None = None,
    hold_days: int = 2,
    hold_days_candidates: Sequence[int] | None = (1, 2, 3),
    stop_loss: float = -0.035,
    take_profit: float = 0.07,
    train_days: int = 120,
    test_days: int = 20,
    step_days: int = 20,
    purge_days: int | None = None,
    min_partial_test_days: int | None = None,
    top_k: int = 10,
    max_factors: int = 6,
    max_interactions: int = 30,
    fdr_alpha: float = 0.05,
    max_abs_correlation: float = 0.85,
    min_cross_section: int = 20,
    run_id: str | None = None,
) -> dict[str, Any]:
    generated_execution_labels = labels is None
    if labels is None:
        candidate_values = (
            tuple(hold_days_candidates)
            if hold_days_candidates is not None
            else (hold_days,)
        )
        normalized_holds = []
        for raw in candidate_values:
            if isinstance(raw, bool) or int(raw) != raw or int(raw) < 1:
                raise ValueError(
                    "hold_days_candidates must contain positive integers"
                )
            value = int(raw)
            if value not in normalized_holds:
                normalized_holds.append(value)
        if not normalized_holds:
            raise ValueError("hold_days_candidates must not be empty")
        normalized_holds.sort()
        execution_labels = None
    else:
        normalized_holds = [int(hold_days)]
        labels = _compact_execution_labels(labels)
        execution_labels = {f"hold_{hold_days}": labels}
    resolved_purge = (
        int(purge_days)
        if purge_days is not None
        else max(normalized_holds) + 1
    )
    resolved_partial_test_days = (
        min(10, int(test_days))
        if min_partial_test_days is None
        else int(min_partial_test_days)
    )
    if (
        isinstance(min_partial_test_days, bool)
        or not 0 <= resolved_partial_test_days <= int(test_days)
    ):
        raise ValueError(
            "min_partial_test_days must be between zero and test_days"
        )
    expected_code_set = {
        str(value).split(".", 1)[0].zfill(6)
        for value in (expected_codes or [])
        if str(value).split(".", 1)[0].isdigit()
    }

    def auxiliary_codes(
        frame: pd.DataFrame | None,
    ) -> set[str]:
        if frame is None or frame.empty or "code" not in frame:
            return set()
        values = (
            frame["code"]
            .astype(str)
            .str.replace(r"\.0$", "", regex=True)
            .str.split(".", n=1)
            .str[0]
            .str.zfill(6)
        )
        observed = {
            value
            for value in values
            if isinstance(value, str) and value.isdigit()
        }
        return (
            observed & expected_code_set
            if expected_code_set
            else observed
        )

    daily_basic_codes = auxiliary_codes(daily_basic)
    auction_codes = auxiliary_codes(auction)
    daily_basic_row_count = (
        int(len(daily_basic))
        if daily_basic is not None
        else 0
    )
    auction_row_count = (
        int(len(auction))
        if auction is not None
        else 0
    )
    auction_available_through = None
    if auction is not None and not auction.empty and "date" in auction:
        auction_dates = pd.to_datetime(
            auction["date"],
            errors="coerce",
        ).dropna()
        if not auction_dates.empty:
            auction_available_through = auction_dates.max().strftime(
                "%Y-%m-%d"
            )
    expected_code_count = len(expected_code_set)
    daily_basic_coverage = (
        len(daily_basic_codes) / expected_code_count
        if expected_code_count
        else (1.0 if daily_basic_codes else 0.0)
    )
    auction_coverage = (
        len(auction_codes) / expected_code_count
        if expected_code_count
        else (1.0 if auction_codes else 0.0)
    )
    daily_basic_used = bool(
        daily_basic is not None
        and not daily_basic.empty
        and (
            not expected_code_count
            or daily_basic_coverage
            >= AUXILIARY_MIN_CODE_COVERAGE
        )
    )
    auction_history_eligible = bool(
        auction is not None
        and not auction.empty
        and (
            not expected_code_count
            or auction_coverage
            >= AUXILIARY_MIN_CODE_COVERAGE
        )
    )
    close_base = build_daily_candidate_factors(panel)
    if daily_basic_used:
        close_base.update(
            build_daily_basic_candidate_factors(daily_basic)
        )
    close_base.update(
        build_structural_candidate_factors(close_base)
    )
    close_base_names, close_interactions = (
        _expression_candidate_plan(
            close_base,
            max_interactions=max_interactions,
        )
    )
    close_candidate_names = [
        *close_base_names,
        *(
            name
            for name, _left, _right in close_interactions
        ),
    ]
    prebuilt_execution_labels: dict[int, pd.DataFrame] = {}
    auction_base: dict[str, pd.DataFrame] = {}
    auction_alignment: dict[str, float | int] = {
        "eligible_rows": 0,
        "matched_rows": 0,
        "coverage": 0.0,
    }
    if auction_history_eligible:
        if generated_execution_labels:
            anchor_hold = (
                int(hold_days)
                if int(hold_days) in normalized_holds
                else normalized_holds[0]
            )
            auction_labels = _build_chunked_execution_labels(
                panel,
                hold_days=anchor_hold,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            auction_base, auction_alignment = _auction_candidate_factors(
                auction_labels,
                auction,
            )
            prebuilt_execution_labels[anchor_hold] = (
                auction_labels
            )
        else:
            auction_base, auction_alignment = _auction_candidate_factors(
                labels,
                auction,
            )
    del daily_basic
    del auction
    auction_used = bool(auction_base)
    if auction_base:
        auction_source_names = set(auction_base)
        close_base.update(auction_base)
        auction_base.clear()
        auction_all_base_names, auction_all_interactions = (
            _expression_candidate_plan(
                close_base,
                max_interactions=max_interactions,
            )
        )
        auction_interactions = [
            interaction
            for interaction in auction_all_interactions
            if (
                interaction[1] in auction_source_names
                or interaction[2] in auction_source_names
            )
        ]
        auction_candidate_names = [
            *sorted(auction_source_names),
            *(
                name
                for name, _left, _right in auction_interactions
            ),
        ]
        union_interactions = []
        union_interaction_names: set[str] = set()
        for interaction in (
            *close_interactions,
            *auction_interactions,
        ):
            if interaction[0] in union_interaction_names:
                continue
            union_interactions.append(interaction)
            union_interaction_names.add(interaction[0])
        (
            _union_candidate_names,
            feature_frame,
        ) = _build_expression_feature_frame(
            close_base,
            max_interactions=max_interactions,
            consume_base=True,
            candidate_plan=(
                auction_all_base_names,
                union_interactions,
            ),
        )
    else:
        (
            close_candidate_names,
            feature_frame,
        ) = _build_expression_feature_frame(
            close_base,
            max_interactions=max_interactions,
            consume_base=True,
        )
        auction_candidate_names = []
    common_args = {
        "train_days": train_days,
        "test_days": test_days,
        "step_days": step_days,
        "purge_days": resolved_purge,
        "min_partial_test_days": resolved_partial_test_days,
        "top_k": top_k,
        "max_factors": max_factors,
        "fdr_alpha": fdr_alpha,
        "max_abs_correlation": max_abs_correlation,
        "min_cross_section": min_cross_section,
    }
    close_factor_refs = {
        name: pd.DataFrame()
        for name in close_candidate_names
    }
    auction_factor_refs = {
        name: pd.DataFrame()
        for name in auction_candidate_names
    }
    if auction_row_count < 1:
        unavailable_auction_reason = "auction_history_not_loaded"
    elif not auction_history_eligible:
        unavailable_auction_reason = (
            "auction_history_coverage_below_threshold"
        )
    else:
        unavailable_auction_reason = (
            "auction_entry_alignment_unavailable"
        )
    unavailable_auction_report = {
        "status": "unavailable",
        "reason": unavailable_auction_reason,
        "summary": _summary_from_windows([]),
        "windows": [],
        "factor_summary": {},
    }
    if generated_execution_labels:
        generated_args = dict(common_args)
        generated_args.pop("fdr_alpha")
        if auction_factor_refs:
            (
                close_report,
                auction_report,
                label_max_exit,
            ) = _run_generated_dual_execution_discovery(
                close_factor_refs,
                auction_factor_refs,
                feature_frame,
                panel,
                hold_days_candidates=normalized_holds,
                stop_loss=stop_loss,
                take_profit=take_profit,
                fdr_alpha=fdr_alpha,
                discovery_kwargs=generated_args,
                prebuilt_execution_labels=(
                    prebuilt_execution_labels
                ),
            )
        else:
            prebuilt_execution_labels.clear()
            (
                close_report,
                label_max_exit,
            ) = _run_generated_execution_discovery(
                close_factor_refs,
                feature_frame,
                panel,
                hold_days_candidates=normalized_holds,
                stop_loss=stop_loss,
                take_profit=take_profit,
                fdr_alpha=fdr_alpha,
                discovery_kwargs=generated_args,
            )
            auction_report = unavailable_auction_report
    else:
        close_report = run_walk_forward_discovery(
            close_factor_refs,
            labels,
            _prepared_feature_frame=feature_frame,
            **common_args,
        )
        label_max_exit = _maximum_label_exit(execution_labels)
        auction_report = (
            run_walk_forward_discovery(
                auction_factor_refs,
                labels,
                _prepared_feature_frame=feature_frame,
                **common_args,
            )
            if auction_factor_refs
            else unavailable_auction_report
        )
    del close_factor_refs
    del auction_factor_refs
    del feature_frame
    factor_names = [
        *close_candidate_names,
        *auction_candidate_names,
    ]
    close_index = panel.get("close")
    if isinstance(close_index, pd.DataFrame) and not close_index.empty:
        data_through = pd.Timestamp(close_index.index.max()).strftime(
            "%Y-%m-%d"
        )
    else:
        data_through = None
    amount_real = panel.get("amount_real")
    if isinstance(amount_real, pd.DataFrame) and not amount_real.empty:
        amount_real_coverage = float(
            pd.to_numeric(
                amount_real.stack(future_stack=True),
                errors="coerce",
            )
            .dropna()
            .mean()
        )
    else:
        amount_real_coverage = 0.0
    point_in_time_universe = all(
        isinstance(panel.get(field), pd.DataFrame)
        for field in (
            "universe_eligible",
            "security_master_eligible",
        )
    )
    st_status_known = panel.get("st_status_known")
    if (
        isinstance(close_index, pd.DataFrame)
        and not close_index.empty
        and isinstance(st_status_known, pd.DataFrame)
    ):
        st_eligible = close_index.notna()
        for field in (
            "universe_eligible",
            "security_master_eligible",
            "continuity_eligible",
        ):
            value = panel.get(field)
            if not isinstance(value, pd.DataFrame):
                continue
            aligned = value.reindex(
                index=close_index.index,
                columns=close_index.columns,
            )
            st_eligible &= aligned.apply(
                pd.to_numeric,
                errors="coerce",
            ).fillna(0.0).gt(0.0)
        aligned_known = st_status_known.reindex(
            index=close_index.index,
            columns=close_index.columns,
        ).apply(pd.to_numeric, errors="coerce").fillna(0.0).gt(0.0)
        st_eligible_cells = int(st_eligible.sum().sum())
        st_known_cells = int(
            (aligned_known & st_eligible).sum().sum()
        )
        st_status_coverage = (
            st_known_cells / st_eligible_cells
            if st_eligible_cells
            else 0.0
        )
    else:
        st_eligible_cells = 0
        st_known_cells = 0
        st_status_coverage = 0.0
    quality_issues = []
    if not point_in_time_universe:
        quality_issues.append("point_in_time_universe_not_verified")
    if amount_real_coverage < 0.98:
        quality_issues.append("real_amount_coverage_below_98pct")
    if st_status_coverage < 0.98:
        quality_issues.append("st_status_coverage_below_98pct")
    if (
        expected_code_count
        and daily_basic_coverage < AUXILIARY_MIN_CODE_COVERAGE
    ):
        quality_issues.append(
            "daily_basic_coverage_below_80pct"
        )
    if (
        expected_code_count
        and auction_coverage < AUXILIARY_MIN_CODE_COVERAGE
    ):
        quality_issues.append(
            "auction_coverage_below_80pct"
        )
    if auction_history_eligible and not auction_used:
        quality_issues.append(
            "auction_entry_alignment_unavailable"
        )
    resolved_run_id = str(
        run_id
        or datetime.now().strftime("sfd-%Y%m%d-%H%M%S")
    )
    config = {
        "hold_days": int(hold_days),
        "hold_days_candidates": normalized_holds,
        "stop_loss": float(stop_loss),
        "take_profit": float(take_profit),
        "train_days": int(train_days),
        "test_days": int(test_days),
        "step_days": int(step_days),
        "purge_days": int(resolved_purge),
        "min_partial_test_days": int(resolved_partial_test_days),
        "top_k": int(top_k),
        "max_factors": int(max_factors),
        "max_interactions": int(max_interactions),
        "fdr_alpha": float(fdr_alpha),
        "max_abs_correlation": float(max_abs_correlation),
        "min_cross_section": int(min_cross_section),
    }
    grammar = {
        "base": BASE_FACTOR_FORMULAS,
        "daily_basic": DAILY_BASIC_FACTOR_FORMULAS,
        "structural": STRUCTURAL_FACTOR_FORMULAS,
        "auction": AUCTION_FACTOR_FORMULAS,
        "interaction_operator": "centered_cross_section_rank_product",
        "curated_interaction_pairs": [
            list(pair)
            for pair in CURATED_INTERACTION_PAIRS
        ],
        "max_interactions": int(max_interactions),
    }
    close_promotions = _promotion_candidates(
        close_report,
        scope="close",
    )
    auction_promotions = _promotion_candidates(
        auction_report,
        scope="auction_confirmation",
    )
    if auction_used:
        primary_channel = "auction_confirmation"
        primary_report = auction_report
        primary_candidate_names = auction_candidate_names
        promotion_candidates = auction_promotions
    else:
        primary_channel = "close"
        primary_report = close_report
        primary_candidate_names = close_candidate_names
        promotion_candidates = close_promotions
    primary_summary = primary_report.get("summary") or {}
    report = {
        "schema_version": "short_factor_discovery_report_v1",
        "channel": "short_factor_discovery",
        "run_id": resolved_run_id,
        "protocol_version": "short_factor_discovery_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": str(primary_report.get("status") or "rejected"),
        "primary_channel": primary_channel,
        "research_only": True,
        "deployment_eligible": False,
        "production_write_attempted": False,
        "data_through": data_through,
        "as_of": {
            "feature_trade_date": data_through,
            "label_max_exit": label_max_exit,
            "auction_available_through": auction_available_through,
        },
        "data_quality": {
            "status": "ok" if not quality_issues else "degraded",
            "point_in_time_universe": point_in_time_universe,
            "amount_real_coverage": round(amount_real_coverage, 6),
            "st_status_coverage": round(st_status_coverage, 6),
            "st_status_known_cells": st_known_cells,
            "st_status_eligible_cells": st_eligible_cells,
            "issues": quality_issues,
        },
        "protocol": {
            "version": "short_factor_discovery_v1",
            "lookahead_policy": "close_t_features_t1_entry",
            "multiple_testing": (
                "benjamini_hochberg_on_newey_west_hac"
            ),
            "candidate_grammar_sha256": _canonical_sha256(grammar),
            "config_sha256": _canonical_sha256(config),
        },
        "config": config,
        "lookahead_policy": {
            "close_factors": "signal_date_close_for_next_session",
            "auction_factors": "entry_date_09:25_confirmation_only",
            "purge_days": resolved_purge,
            "test_window_refit": False,
            "execution_horizon_selection": "calibration_only",
        },
        "data_coverage": {
            "expected_code_count": expected_code_count,
            "daily_basic_rows": daily_basic_row_count,
            "daily_basic_code_count": len(daily_basic_codes),
            "daily_basic_coverage": round(
                daily_basic_coverage,
                6,
            ),
            "daily_basic_used": daily_basic_used,
            "auction_rows": auction_row_count,
            "auction_code_count": len(auction_codes),
            "auction_coverage": round(
                auction_coverage,
                6,
            ),
            "auction_history_eligible": auction_history_eligible,
            "auction_alignment_eligible_rows": int(
                auction_alignment["eligible_rows"]
            ),
            "auction_alignment_matched_rows": int(
                auction_alignment["matched_rows"]
            ),
            "auction_alignment_coverage": round(
                float(auction_alignment["coverage"]),
                6,
            ),
            "auction_used": auction_used,
        },
        "summary": {
            "tested_factor_count": int(len(primary_candidate_names)),
            "promotion_candidate_count": len(promotion_candidates),
            "oos_window_count": int(
                primary_summary.get("window_count") or 0
            ),
            "oos_rank_ic": float(
                primary_summary.get("oos_rank_ic") or 0.0
            ),
            "net_return_pct": float(
                primary_summary.get("total_return_pct") or 0.0
            ),
            "max_drawdown_pct": float(
                primary_summary.get("max_drawdown_pct") or 0.0
            ),
        },
        "factor_catalog": _factor_catalog(factor_names),
        "close_discovery": close_report,
        "auction_confirmation_discovery": auction_report,
        "promotion_candidates": promotion_candidates,
        "channel_promotion_candidates": {
            "close": close_promotions,
            "auction_confirmation": auction_promotions,
        },
        "rejections": [],
        "warnings": quality_issues,
    }
    if out_dir is not None:
        _write_json_atomic(
            Path(out_dir) / "discovery_report.json",
            report,
        )
    return report
