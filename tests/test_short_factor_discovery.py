from __future__ import annotations

import json
import math
import warnings
import weakref

import numpy as np
import pandas as pd
import pytest

from analytics import short_factor_discovery as discovery


def _panel(periods: int = 90, codes: int = 24) -> dict[str, pd.DataFrame]:
    index = pd.bdate_range("2026-01-05", periods=periods)
    columns = [f"{value:06d}" for value in range(1, codes + 1)]
    time = np.arange(periods, dtype=float)[:, None]
    cross = np.arange(codes, dtype=float)[None, :]
    close = pd.DataFrame(
        10.0 + time * 0.03 + cross * 0.02,
        index=index,
        columns=columns,
    )
    open_price = close.shift(1).fillna(close.iloc[0]) * (
        1.0 + (cross - codes / 2) * 0.0002
    )
    high = np.maximum(open_price, close) * 1.02
    low = np.minimum(open_price, close) * 0.98
    volume = pd.DataFrame(
        1_000_000.0 + time * 1000.0 + cross * 10_000.0,
        index=index,
        columns=columns,
    )
    amount = close * volume
    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": amount,
        "amount_real": pd.DataFrame(
            1.0,
            index=index,
            columns=columns,
        ),
        "universe_eligible": pd.DataFrame(
            1.0,
            index=index,
            columns=columns,
        ),
    }


def test_build_daily_candidates_is_point_in_time():
    panel = _panel()
    original = discovery.build_daily_candidate_factors(panel)

    changed = {key: value.copy() for key, value in panel.items()}
    cutoff = changed["close"].index[-8]
    for key in ("open", "high", "low", "close", "volume", "amount"):
        changed[key].loc[changed[key].index > cutoff] *= 10.0
    mutated = discovery.build_daily_candidate_factors(changed)

    assert set(discovery.BASE_FACTOR_FORMULAS).issubset(original)
    for name in original:
        pd.testing.assert_frame_equal(
            original[name].loc[:cutoff],
            mutated[name].loc[:cutoff],
        )


def test_daily_candidates_include_extreme_short_structure_grammar():
    factors = discovery.build_daily_candidate_factors(_panel())

    assert {
        "return_1",
        "return_3",
        "return_5",
        "return_10",
        "return_20",
        "momentum_acceleration_3_20",
        "distance_high_20",
        "distance_low_20",
        "range_close_strength_1",
        "gap_hold_1",
        "panic_recovery_1",
        "liquidity_reversal_3_20",
        "price_volume_divergence_5_20",
        "quiet_breakout_20",
        "return_to_risk_5_20",
        "limit_proximity_1",
        "limit_close_strength_1",
        "close_flow_3",
        "close_flow_acceleration_3_10",
        "signed_amount_flow_5",
        "amount_impulse_decay_1_5",
        "range_regime_shift_3_20",
        "overnight_intraday_divergence_3",
        "return_autocorrelation_10",
        "realized_skew_10",
        "idiosyncratic_momentum_5_20",
        "downside_resilience_10",
        "volume_price_elasticity_10",
        "illiquidity_regime_5_20",
        "volume_concentration_5",
        "limit_pressure_persistence_3",
    }.issubset(factors)
    assert all(
        frame.shape == factors["return_1"].shape
        for frame in factors.values()
    )


def test_structural_candidates_are_target_free_and_point_in_time():
    panel = _panel()
    close_factors = discovery.build_daily_candidate_factors(panel)
    dates = panel["close"].index
    codes = panel["close"].columns
    daily_basic = pd.DataFrame(
        [
            {
                "code": code,
                "date": trade_date,
                "turnover_rate": 1.0 + day / 10 + index / 100,
                "turnover_rate_f": 1.2 + day / 10 + index / 100,
                "volume_ratio": 0.8 + index / 100,
                "float_share": 1000.0,
                "total_share": 1500.0,
                "circ_mv": 1800.0 + index,
            }
            for day, trade_date in enumerate(dates)
            for index, code in enumerate(codes)
        ]
    )
    base = {
        **close_factors,
        **discovery.build_daily_basic_candidate_factors(daily_basic),
    }

    original = discovery.build_structural_candidate_factors(base)
    changed_panel = {
        key: value.copy()
        for key, value in panel.items()
    }
    cutoff = dates[-8]
    for key in ("open", "high", "low", "close", "volume", "amount"):
        changed_panel[key].loc[dates > cutoff] *= 10.0
    changed_base = {
        **discovery.build_daily_candidate_factors(changed_panel),
        **discovery.build_daily_basic_candidate_factors(daily_basic),
    }
    mutated = discovery.build_structural_candidate_factors(changed_base)

    assert set(discovery.STRUCTURAL_FACTOR_FORMULAS) == {
        "turnover_limit_pressure",
        "turnover_panic_recovery",
        "quiet_turnover_breakout",
        "strong_stock_dryup_pullback",
        "liquidity_close_confirmation",
        "compression_limit_pressure",
        "flow_breakout_confirmation",
        "idiosyncratic_flow_momentum",
        "compressed_attention_release",
        "downside_resilience_accumulation",
        "persistent_limit_absorption",
        "liquidity_regime_breakout",
        "intraday_leadership_shift",
        "serial_flow_continuation",
    }
    assert set(original) == set(discovery.STRUCTURAL_FACTOR_FORMULAS)
    for name in original:
        pd.testing.assert_frame_equal(
            original[name].loc[:cutoff],
            mutated[name].loc[:cutoff],
        )


def test_align_auction_confirmation_uses_entry_date_not_signal_date():
    signal_dates = pd.to_datetime(["2026-07-30", "2026-07-31"])
    features = pd.DataFrame(
        {
            "trade_date": signal_dates.strftime("%Y-%m-%d"),
            "ts_code": ["000001", "000001"],
            "entry_date": pd.to_datetime(
                ["2026-07-31", "2026-08-03"]
            ),
        }
    )
    auction = pd.DataFrame(
        {
            "code": ["000001"],
            "date": pd.to_datetime(["2026-07-31"]),
            "auction_price": [11.0],
            "pre_close": [10.0],
            "auction_amount": [2_000_000.0],
            "auction_turnover_rate": [0.2],
            "auction_volume_ratio": [1.5],
        }
    )

    aligned = discovery.align_auction_confirmation(features, auction)

    first = aligned.iloc[0]
    second = aligned.iloc[1]
    assert first["auction_trade_date"] == "2026-07-31"
    assert first["auction_gap"] == 0.1
    assert pd.isna(second["auction_gap"])


def test_align_auction_confirmation_requires_exact_entry_date():
    features = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                ["2026-07-30", "2026-07-31"]
            ),
            "ts_code": ["000001", "000001"],
        }
    )
    auction = pd.DataFrame(
        {
            "code": ["000001"],
            "date": pd.to_datetime(["2026-07-31"]),
            "auction_price": [11.0],
            "pre_close": [10.0],
        }
    )

    aligned = discovery.align_auction_confirmation(features, auction)

    assert aligned["auction_trade_date"].isna().all()
    assert aligned["auction_gap"].isna().all()


def test_align_auction_confirmation_does_not_bridge_missing_trading_day():
    features = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                ["2026-07-30", "2026-07-31"]
            ),
            "ts_code": ["000001", "000001"],
        }
    )
    auction = pd.DataFrame(
        {
            "code": ["000001"],
            "date": pd.to_datetime(["2026-08-03"]),
            "auction_price": [11.0],
            "pre_close": [10.0],
        }
    )

    aligned = discovery.align_auction_confirmation(features, auction)

    assert aligned["auction_trade_date"].isna().all()
    assert aligned["auction_gap"].isna().all()


def test_align_auction_confirmation_does_not_scan_feature_rows(
    monkeypatch,
):
    features = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                ["2026-07-29", "2026-07-30", "2026-07-30"]
            ),
            "ts_code": ["000001", "000001", "000002"],
            "entry_date": pd.to_datetime(
                ["2026-07-30", "2026-07-31", "2026-07-31"]
            ),
        }
    )
    auction = pd.DataFrame(
        {
            "code": ["000001", "000001", "000002"],
            "date": pd.to_datetime(
                ["2026-07-30", "2026-07-31", "2026-07-31"]
            ),
            "auction_price": [10.5, 10.8, 20.2],
            "pre_close": [10.0, 10.5, 20.0],
            "auction_amount": [1_000_000.0, 1_200_000.0, 900_000.0],
            "auction_turnover_rate": [0.2, 0.3, 0.1],
            "auction_volume_ratio": [1.1, 1.2, 0.8],
        }
    )

    def fail_iterrows(_self):
        raise AssertionError("auction alignment must not scan feature rows")

    monkeypatch.setattr(pd.DataFrame, "iterrows", fail_iterrows)

    aligned = discovery.align_auction_confirmation(features, auction)

    assert aligned["auction_trade_date"].tolist() == [
        "2026-07-30",
        "2026-07-31",
        "2026-07-31",
    ]
    np.testing.assert_allclose(
        aligned["auction_gap"].to_numpy(dtype=float),
        np.array([0.05, 10.8 / 10.5 - 1.0, 0.01]),
    )


def test_align_auction_confirmation_tolerates_missing_optional_values():
    features = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2026-07-30"]),
            "ts_code": ["000001"],
            "entry_date": pd.to_datetime(["2026-07-31"]),
        }
    )
    auction = pd.DataFrame(
        {
            "code": ["000001"],
            "date": pd.to_datetime(["2026-07-31"]),
        }
    )

    aligned = discovery.align_auction_confirmation(features, auction)

    assert aligned.loc[0, "auction_trade_date"] == "2026-07-31"
    assert pd.isna(aligned.loc[0, "auction_gap"])
    assert pd.isna(aligned.loc[0, "auction_amount"])


def test_auction_candidate_factors_fail_closed_without_exact_match():
    labels = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2026-07-30"]),
            "ts_code": ["000001"],
            "entry_date": pd.to_datetime(["2026-07-31"]),
        }
    )
    auction = pd.DataFrame(
        {
            "code": ["000001"],
            "date": pd.to_datetime(["2026-08-03"]),
            "auction_price": [11.0],
            "pre_close": [10.0],
        }
    )

    factors, quality = discovery._auction_candidate_factors(
        labels,
        auction,
    )

    assert factors == {}
    assert quality["coverage"] == 0.0


def test_auction_candidate_factors_omit_empty_optional_sources():
    labels = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2026-07-30"]),
            "ts_code": ["000001"],
            "entry_date": pd.to_datetime(["2026-07-31"]),
        }
    )
    auction = pd.DataFrame(
        {
            "code": ["000001"],
            "date": pd.to_datetime(["2026-07-31"]),
            "auction_amount": [1_000_000.0],
        }
    )

    factors, quality = discovery._auction_candidate_factors(
        labels,
        auction,
    )

    assert set(factors) == {"auction_amount"}
    assert quality["coverage"] == 1.0
    assert factors["auction_amount"].loc[
        pd.Timestamp("2026-07-30"),
        "000001",
    ] == pytest.approx(1_000_000.0)


def test_benjamini_hochberg_controls_false_discoveries():
    accepted = discovery.benjamini_hochberg(
        {
            "strong": 0.001,
            "borderline": 0.04,
            "noise": 0.7,
        },
        alpha=0.05,
    )

    assert accepted == {"strong"}


def test_ic_significance_uses_conservative_newey_west_hac():
    values = pd.Series(
        np.repeat(
            [0.08, -0.01, 0.08, -0.01, 0.08, -0.01],
            20,
        ),
        dtype=float,
    )
    stats = discovery._ic_diagnostics(values)
    iid_t = values.mean() / (
        values.std(ddof=1) / math.sqrt(len(values))
    )
    iid_p = math.erfc(abs(iid_t) / math.sqrt(2.0))

    assert stats["p_value_method"] == "newey_west_hac"
    assert stats["hac_lag"] >= 1
    assert stats["p_value"] >= iid_p


def test_daily_rank_ic_matrix_matches_scalar_implementation():
    factors, labels = _discovery_sample(periods=12, codes=30)
    labels["ts_code"] = discovery._code_key_series(
        labels["ts_code"]
    )
    frame = discovery._factor_long_frame(factors).merge(
        labels,
        on=["trade_date", "ts_code"],
        how="inner",
    )
    frame.loc[
        frame["ts_code"].isin(["000001", "000002"]),
        "noise",
    ] = np.nan

    matrix = discovery._daily_rank_ic_matrix(
        frame,
        ["predictive", "noise"],
        "live_adjusted_return_pct",
        min_cross_section=20,
    )

    for name in ("predictive", "noise"):
        scalar = discovery._daily_rank_ic(
            frame,
            name,
            "live_adjusted_return_pct",
            min_cross_section=20,
        )
        pd.testing.assert_series_equal(
            matrix[name].dropna(),
            scalar,
            check_names=False,
            atol=1e-12,
            rtol=1e-12,
        )


def test_daily_rank_ic_matrix_skips_constant_pairs_without_warning():
    dates = pd.bdate_range("2026-01-05", periods=3)
    codes = [f"{value:06d}" for value in range(1, 31)]
    frame = pd.DataFrame(
        [
            {
                "trade_date": trade_date,
                "ts_code": code,
                "constant": 1.0,
                "varying": float(index),
                "target": float(index),
            }
            for trade_date in dates
            for index, code in enumerate(codes)
        ]
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = discovery._daily_rank_ic_matrix(
            frame,
            ["constant", "varying"],
            "target",
            min_cross_section=20,
        )

    assert result["constant"].isna().all()
    assert result["varying"].eq(1.0).all()
    assert caught == []


def test_market_regime_model_predicts_only_from_signal_date_features():
    dates = pd.bdate_range("2026-01-05", periods=60)
    codes = [f"{value:06d}" for value in range(1, 31)]
    rows = []
    for day, trade_date in enumerate(dates):
        state = 1.0 if day % 4 in {0, 1} else -1.0
        for index, code in enumerate(codes):
            rows.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "return_1": state * 0.01 + index * 1e-6,
                    "return_5": state * 0.02,
                    "return_20": state * 0.03,
                    "range_pct_1": 0.02,
                    "amount_shock_1_20": 1.0 + state * 0.1,
                    "volatility_compression_5_20": 1.0,
                    "live_adjusted_return_pct": state * 1.5,
                }
            )
    frame = pd.DataFrame(rows)

    model = discovery._fit_market_regime_model(
        frame[frame["trade_date"].isin(dates[:40])],
        "live_adjusted_return_pct",
        min_days=20,
    )
    predictions = discovery._predict_market_regime(
        model,
        frame[frame["trade_date"].isin(dates[40:])],
    )

    expected = pd.Series(
        [
            1.0 if day % 4 in {0, 1} else -1.0
            for day in range(40, 60)
        ],
        index=dates[40:],
    )
    assert model["status"] == "fitted"
    assert (predictions.gt(0).astype(float) * 2.0 - 1.0).equals(
        expected
    )


def test_purged_walk_forward_never_trains_on_test_labels():
    dates = pd.bdate_range("2026-01-05", periods=80)
    windows = discovery.purged_walk_forward_windows(
        dates,
        train_days=40,
        test_days=10,
        step_days=10,
        purge_days=3,
    )

    assert len(windows) == 3
    for window in windows:
        assert window.train_dates[-1] < window.test_dates[0]
        distance = dates.get_loc(window.test_dates[0]) - dates.get_loc(
            window.train_dates[-1]
        )
        assert distance == 4


def test_purged_walk_forward_includes_requested_recent_partial_tail():
    dates = pd.bdate_range("2026-01-05", periods=80)

    windows = discovery.purged_walk_forward_windows(
        dates,
        train_days=40,
        test_days=10,
        step_days=10,
        purge_days=3,
        min_partial_test_days=5,
    )

    assert len(windows) == 4
    assert windows[-1].test_dates == tuple(dates[73:])
    assert len(windows[-1].test_dates) == 7
    assert windows[-1].train_dates[-1] < windows[-1].test_dates[0]


def test_decorrelate_factors_keeps_stronger_candidate():
    index = pd.MultiIndex.from_product(
        [
            pd.bdate_range("2026-01-05", periods=4),
            [f"{value:06d}" for value in range(1, 21)],
        ],
        names=["trade_date", "ts_code"],
    )
    base = np.tile(np.linspace(-1.0, 1.0, 20), 4)
    frame = pd.DataFrame(
        {
            "strong": base,
            "duplicate": base * 2.0,
            "independent": np.sin(np.arange(len(index))),
        },
        index=index,
    )

    selected = discovery.decorrelate_factors(
        frame,
        ["duplicate", "strong", "independent"],
        evidence_scores={
            "strong": 2.0,
            "duplicate": 1.0,
            "independent": 0.5,
        },
        max_abs_correlation=0.8,
    )

    assert selected == ["strong", "independent"]


def test_decorrelate_factors_skips_constant_pairs_without_warning():
    index = pd.MultiIndex.from_product(
        [
            pd.bdate_range("2026-01-05", periods=4),
            [f"{value:06d}" for value in range(1, 21)],
        ],
        names=["trade_date", "ts_code"],
    )
    frame = pd.DataFrame(
        {
            "constant": 1.0,
            "varying": np.arange(len(index), dtype=float),
        },
        index=index,
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        selected = discovery.decorrelate_factors(
            frame,
            ["constant", "varying"],
            evidence_scores={"constant": 2.0, "varying": 1.0},
        )

    assert selected == ["constant", "varying"]
    assert caught == []


def _discovery_sample(
    periods: int = 120,
    codes: int = 30,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    dates = pd.bdate_range("2025-12-01", periods=periods)
    names = [f"{value:06d}" for value in range(1, codes + 1)]
    cross = np.linspace(-1.0, 1.0, codes)
    predictive = pd.DataFrame(
        np.tile(cross, (periods, 1)),
        index=dates,
        columns=names,
    )
    noise = pd.DataFrame(
        np.sin(
            np.arange(periods * codes, dtype=float).reshape(periods, codes)
            * 0.73
        ),
        index=dates,
        columns=names,
    )
    labels = predictive.stack().rename("live_adjusted_return_pct").reset_index()
    labels.columns = [
        "trade_date",
        "ts_code",
        "live_adjusted_return_pct",
    ]
    labels["live_adjusted_return_pct"] *= 2.0
    return {"predictive": predictive, "noise": noise}, labels


def test_expression_candidates_are_deterministic_and_target_free():
    factors, _labels = _discovery_sample(periods=30)

    first = discovery.build_expression_candidates(
        factors,
        max_interactions=1,
    )
    second = discovery.build_expression_candidates(
        {key: factors[key] for key in reversed(factors)},
        max_interactions=1,
    )

    assert list(first) == list(second)
    assert list(first) == [
        "noise",
        "predictive",
        "noise__x__predictive",
    ]
    pd.testing.assert_frame_equal(
        first["noise__x__predictive"],
        second["noise__x__predictive"],
    )
    assert all(
        set(frame.dtypes.astype(str)) == {"float32"}
        for frame in first.values()
    )


def test_expression_candidates_prioritize_domain_pairs_before_fallback():
    all_factors = discovery.build_daily_candidate_factors(_panel())
    factors = {
        name: all_factors[name]
        for name in (
            "return_1",
            "amount_shock_1_20",
            "return_3",
            "volume_dryup_5_20",
            "return_5",
            "close_flow_3",
        )
    }

    _names, interactions = discovery._expression_candidate_plan(
        {key: factors[key] for key in reversed(factors)},
        max_interactions=3,
    )

    assert interactions == [
        (
            "return_1__x__amount_shock_1_20",
            "return_1",
            "amount_shock_1_20",
        ),
        (
            "return_3__x__volume_dryup_5_20",
            "return_3",
            "volume_dryup_5_20",
        ),
        (
            "return_5__x__close_flow_3",
            "return_5",
            "close_flow_3",
        ),
    ]


def test_expression_candidate_plan_reserves_default_auction_interactions():
    index = pd.bdate_range("2026-07-01", periods=2)
    columns = ["000001", "600000"]
    factor_names = {
        name
        for pair in discovery.CURATED_INTERACTION_PAIRS
        for name in pair
    }
    factor_names.update(discovery.AUCTION_FACTOR_FORMULAS)
    factors = {
        name: pd.DataFrame(1.0, index=index, columns=columns)
        for name in factor_names
    }

    _names, interactions = discovery._expression_candidate_plan(
        factors,
        max_interactions=30,
    )

    assert len(interactions) == 30
    assert any(
        left in discovery.AUCTION_FACTOR_FORMULAS
        or right in discovery.AUCTION_FACTOR_FORMULAS
        for _name, left, right in interactions
    )


def test_expression_candidate_plan_keeps_close_default_interactions():
    auction_names = set(discovery.AUCTION_FACTOR_FORMULAS)
    expected_pairs = [
        pair
        for pair in discovery.CURATED_INTERACTION_PAIRS
        if not auction_names.intersection(pair)
    ]
    index = pd.bdate_range("2026-07-01", periods=2)
    columns = ["000001", "600000"]
    factors = {
        name: pd.DataFrame(1.0, index=index, columns=columns)
        for pair in expected_pairs
        for name in pair
    }

    _names, interactions = discovery._expression_candidate_plan(
        factors,
        max_interactions=30,
    )

    assert [
        (left, right)
        for _name, left, right in interactions
    ] == expected_pairs


def test_expression_feature_frame_matches_materialized_candidates():
    base = discovery.build_daily_candidate_factors(_panel())
    base.update(discovery.build_structural_candidate_factors(base))
    materialized = discovery.build_expression_candidates(
        base,
        max_interactions=12,
    )

    names, streamed = discovery._build_expression_feature_frame(
        base,
        max_interactions=12,
    )
    expected = discovery._factor_long_frame(materialized)

    assert names == list(materialized)
    pd.testing.assert_frame_equal(
        streamed,
        expected,
        check_exact=True,
    )


def test_expression_feature_plan_normalizes_mixed_date_index_types():
    dates = pd.bdate_range("2026-07-01", periods=4)
    base = {
        "left": pd.DataFrame(
            [[1.0], [2.0], [3.0], [4.0]],
            index=dates.strftime("%Y-%m-%d"),
            columns=["000001"],
        ),
        "right": pd.DataFrame(
            [[4.0], [3.0], [2.0], [1.0]],
            index=dates,
            columns=["000001"],
        ),
    }
    materialized = discovery.build_expression_candidates(
        base,
        max_interactions=1,
    )

    names, streamed = discovery._build_expression_feature_frame(
        base,
        max_interactions=1,
    )

    assert "left__x__right" in names
    pd.testing.assert_frame_equal(
        streamed,
        discovery._factor_long_frame(materialized),
        check_exact=True,
    )


def test_expression_feature_frame_can_release_consumed_base_matrices():
    base = discovery.build_daily_candidate_factors(_panel())
    base.update(discovery.build_structural_candidate_factors(base))
    materialized = discovery.build_expression_candidates(
        base,
        max_interactions=8,
    )
    consumable = dict(base)

    names, streamed = discovery._build_expression_feature_frame(
        consumable,
        max_interactions=8,
        consume_base=True,
    )

    assert consumable == {}
    assert names == list(materialized)
    pd.testing.assert_frame_equal(
        streamed,
        discovery._factor_long_frame(materialized),
        check_exact=True,
    )


def test_factor_builders_preserve_precision_until_materialization():
    panel = _panel()
    close = discovery.build_daily_candidate_factors(panel)
    rows = []
    for index, trade_date in enumerate(panel["close"].index):
        for code in panel["close"].columns:
            rows.append(
                {
                    "code": code,
                    "date": trade_date,
                    "turnover_rate": 1.0 + index / 10,
                    "turnover_rate_f": 1.2 + index / 10,
                    "volume_ratio": 0.8 + index / 100,
                    "float_share": 1000.0,
                    "total_share": 1500.0,
                    "circ_mv": 1800.0 + index,
                }
            )
    close.update(
        discovery.build_daily_basic_candidate_factors(
            pd.DataFrame(rows)
        )
    )
    structural = discovery.build_structural_candidate_factors(close)
    materialized = discovery.build_expression_candidates(
        {**close, **structural},
        max_interactions=2,
    )

    assert close
    assert structural
    assert all(
        set(frame.dtypes) == {np.dtype("float64")}
        for frame in close.values()
        if not frame.empty
    )
    assert all(
        set(frame.dtypes) == {np.dtype("float32")}
        for frame in structural.values()
        if not frame.empty
    )
    assert all(
        set(frame.dtypes) == {np.dtype("float32")}
        for frame in materialized.values()
        if not frame.empty
    )


def test_structural_rank_uses_float64_values_before_storage():
    dates = pd.DatetimeIndex(["2026-07-31"])
    columns = ["000001", "000002", "000003"]
    close_values = pd.DataFrame(
        [[1.0, 1.0 + 1e-8, 1.0 + 2e-8]],
        index=dates,
        columns=columns,
    )
    pressure_values = pd.DataFrame(
        [[2.0, 2.0 + 2e-8, 2.0 + 4e-8]],
        index=dates,
        columns=columns,
    )

    factors = discovery.build_structural_candidate_factors(
        {
            "turnover_acceleration_1_5": close_values,
            "limit_close_strength_1": pressure_values,
        }
    )

    result = factors["turnover_limit_pressure"]
    np.testing.assert_allclose(
        result.iloc[0].to_numpy(dtype=float),
        np.array([1.0 / 9.0, 4.0 / 9.0, 1.0]),
        rtol=1e-7,
        atol=1e-7,
    )
    assert set(result.dtypes) == {np.dtype("float32")}


def test_factor_long_frame_uses_float32_storage():
    factors, _labels = _discovery_sample(periods=30)

    frame = discovery._factor_long_frame(factors)

    assert frame["predictive"].dtype == np.dtype("float32")
    assert frame["noise"].dtype == np.dtype("float32")


def test_compact_execution_labels_drops_diagnostics():
    labels = pd.DataFrame(
        {
            "trade_date": ["2026-07-30"],
            "ts_code": ["000001"],
            "entry_date": ["2026-07-31"],
            "exit_date": ["2026-08-03"],
            "live_adjusted_return_pct": [1.2],
            "forward_return_pct": [1.4],
            "research_evaluable": [True],
            "execution_blocked": [False],
            "valuation_path": [[{"return_pct": 1.2}]],
            "execution_flags": [[]],
            "unused_diagnostic": ["large"],
        }
    )

    compact = discovery._compact_execution_labels(labels)

    assert list(compact.columns) == [
        "trade_date",
        "ts_code",
        "entry_date",
        "exit_date",
        "live_adjusted_return_pct",
        "forward_return_pct",
        "research_evaluable",
        "execution_blocked",
    ]
    assert "valuation_path" not in compact
    assert compact["trade_date"].dtype == np.dtype("datetime64[ns]")
    assert compact["entry_date"].dtype == np.dtype("datetime64[ns]")
    assert compact["exit_date"].dtype == np.dtype("datetime64[ns]")
    assert compact["ts_code"].dtype == np.dtype("int32")


def test_chunked_execution_labels_match_frozen_full_panel_builder():
    from analytics import relay_factor_mining

    panel = _panel(periods=45, codes=18)
    expected = discovery._compact_execution_labels(
        relay_factor_mining.build_label_frame(
            dict(panel),
            hold_days=2,
            stop_loss=-0.035,
            take_profit=0.07,
            compact_diagnostics=True,
        )
    ).sort_values(
        ["trade_date", "ts_code"],
        kind="stable",
        ignore_index=True,
    )

    actual = discovery._build_chunked_execution_labels(
        panel,
        hold_days=2,
        stop_loss=-0.035,
        take_profit=0.07,
        chunk_size=7,
    )

    pd.testing.assert_frame_equal(actual, expected, check_exact=True)


def test_walk_forward_discovery_selects_cost_positive_oos_factor():
    factors, labels = _discovery_sample()

    report = discovery.run_walk_forward_discovery(
        factors,
        labels,
        train_days=60,
        test_days=10,
        step_days=10,
        purge_days=3,
        top_k=5,
        max_factors=3,
        fdr_alpha=0.05,
        min_cross_section=20,
    )

    assert report["status"] == "accepted", report
    assert len(report["windows"]) == 5
    assert report["factor_summary"]["predictive"]["selected_windows"] == 5
    assert report["summary"]["oos_rank_ic"] > 0.9
    assert report["summary"]["total_return_pct"] > 0.0
    assert report["summary"]["positive_window_rate"] == 1.0
    for window in report["windows"]:
        assert sum(window["score_weights"].values()) == pytest.approx(
            1.0
        )


def test_walk_forward_scoring_drops_unselected_candidate_columns(
    monkeypatch,
):
    factors, labels = _discovery_sample()
    factors["unused_constant"] = pd.DataFrame(
        1.0,
        index=factors["predictive"].index,
        columns=factors["predictive"].columns,
    )
    observed_columns = []
    original = discovery._composite_scores

    def inspected(frame, selected, directions, weights):
        if selected:
            observed_columns.append(set(frame.columns))
        return original(frame, selected, directions, weights)

    monkeypatch.setattr(discovery, "_composite_scores", inspected)

    discovery.run_walk_forward_discovery(
        factors,
        labels,
        train_days=60,
        test_days=10,
        step_days=10,
        purge_days=3,
        top_k=5,
        max_factors=3,
        fdr_alpha=0.05,
        min_cross_section=20,
    )

    assert observed_columns
    assert all(
        "unused_constant" not in columns
        for columns in observed_columns
    )


def test_walk_forward_ignores_rows_with_only_other_channel_values():
    factors, labels = _discovery_sample()
    masked_factors = {
        name: frame.copy()
        for name, frame in factors.items()
    }
    trade_date = masked_factors["predictive"].index[65]
    code = masked_factors["predictive"].columns[0]
    for frame in masked_factors.values():
        frame.loc[trade_date, code] = np.nan

    baseline = discovery.run_walk_forward_discovery(
        masked_factors,
        labels,
        train_days=60,
        test_days=10,
        step_days=10,
        purge_days=3,
        top_k=5,
        max_factors=3,
        fdr_alpha=0.05,
        min_cross_section=20,
    )
    union_frame = discovery._factor_long_frame(factors)
    row = (
        union_frame["trade_date"].eq(trade_date)
        & union_frame["ts_code"].eq(int(code))
    )
    union_frame.loc[row, list(factors)] = np.nan
    union_frame["auction_gap"] = np.where(row, 0.05, np.nan)

    actual = discovery.run_walk_forward_discovery(
        {
            name: pd.DataFrame()
            for name in masked_factors
        },
        labels,
        _prepared_feature_frame=union_frame,
        train_days=60,
        test_days=10,
        step_days=10,
        purge_days=3,
        top_k=5,
        max_factors=3,
        fdr_alpha=0.05,
        min_cross_section=20,
    )

    assert actual == baseline


def test_candidate_row_mask_is_computed_without_wide_frame_copy():
    frame = pd.DataFrame(
        {
            "left": [1.0, np.nan, np.nan],
            "right": [np.nan, 2.0, np.nan],
            "other_channel": [np.nan, np.nan, 3.0],
        }
    )

    mask = discovery._candidate_non_null_row_mask(
        frame,
        ["left", "right"],
    )

    np.testing.assert_array_equal(
        mask,
        np.array([True, True, False]),
    )


def test_market_regime_frame_copies_only_required_source_columns():
    class CopyGuardFrame(pd.DataFrame):
        @property
        def _constructor(self):
            return CopyGuardFrame

        def copy(self, deep=True):
            assert "unused_candidate" not in self.columns
            return super().copy(deep=deep)

    dates = pd.to_datetime(
        ["2026-07-30", "2026-07-30", "2026-07-31"]
    )
    frame = CopyGuardFrame(
        {
            "trade_date": dates,
            "return_1": [0.01, -0.01, 0.02],
            "unused_candidate": [1.0, 2.0, 3.0],
        }
    )

    result = discovery._market_regime_daily_frame(frame)

    assert list(result.index) == list(pd.DatetimeIndex(dates.unique()))
    assert "market_return_1" in result


def test_release_research_memory_does_not_trim_windows_working_set(
    monkeypatch,
):
    class Guard:
        def __getattr__(self, _name):
            raise AssertionError(
                "active Windows working set must not be trimmed"
            )

    collect_calls = []
    monkeypatch.setattr(
        discovery.gc,
        "collect",
        lambda: collect_calls.append(True),
    )
    monkeypatch.setattr(discovery.os, "name", "nt")
    monkeypatch.setattr(
        discovery.ctypes,
        "windll",
        Guard(),
        raising=False,
    )

    discovery._release_research_memory()

    assert collect_calls == [True]


def test_walk_forward_fit_keeps_market_sources_from_prepared_frame(
    monkeypatch,
):
    factors, labels = _discovery_sample()
    prepared_factors = {"predictive": factors["predictive"]}
    for index, name in enumerate(
        discovery.MARKET_REGIME_SOURCE_COLUMNS
    ):
        prepared_factors[name] = (
            factors["noise"] + float(index) / 100.0
        )
    prepared = discovery._factor_long_frame(prepared_factors)
    observed_columns = []
    original = discovery._fit_market_regime_model

    def inspected(frame, target, *, min_days):
        observed_columns.append(set(frame.columns))
        return original(frame, target, min_days=min_days)

    monkeypatch.setattr(
        discovery,
        "_fit_market_regime_model",
        inspected,
    )

    discovery.run_walk_forward_discovery(
        {"predictive": pd.DataFrame()},
        labels,
        _prepared_feature_frame=prepared,
        train_days=60,
        test_days=10,
        step_days=10,
        purge_days=3,
        top_k=5,
        min_cross_section=20,
    )

    assert observed_columns
    required = set(discovery.MARKET_REGIME_SOURCE_COLUMNS)
    assert all(
        required.issubset(columns)
        for columns in observed_columns
    )


def test_high_ic_factor_is_not_promoted_when_calibration_returns_are_negative():
    factors, labels = _discovery_sample()
    labels["live_adjusted_return_pct"] = (
        labels["live_adjusted_return_pct"] * 0.1 - 2.0
    )

    report = discovery.run_walk_forward_discovery(
        factors,
        labels,
        train_days=60,
        test_days=10,
        step_days=10,
        purge_days=3,
        top_k=5,
        max_factors=3,
        fdr_alpha=0.05,
        min_cross_section=20,
    )

    assert report["status"] == "rejected"
    assert all(
        window["status"] == "calibration_rejected"
        for window in report["windows"]
    )
    assert (
        report["factor_summary"]["predictive"][
            "promotion_eligible"
        ]
        is False
    )
    assert report["summary"]["trade_count"] == 0


def test_test_labels_cannot_change_window_factor_selection():
    factors, labels = _discovery_sample()
    baseline = discovery.run_walk_forward_discovery(
        factors,
        labels,
        train_days=60,
        test_days=10,
        step_days=10,
        purge_days=3,
        top_k=5,
        min_cross_section=20,
    )
    mutated = labels.copy()
    first_test_dates = set(baseline["windows"][0]["test_dates"])
    mask = mutated["trade_date"].dt.strftime("%Y-%m-%d").isin(
        first_test_dates
    )
    mutated.loc[mask, "live_adjusted_return_pct"] *= -100.0

    changed = discovery.run_walk_forward_discovery(
        factors,
        mutated,
        train_days=60,
        test_days=10,
        step_days=10,
        purge_days=3,
        top_k=5,
        min_cross_section=20,
    )

    assert (
        baseline["windows"][0]["selected_factors"]
        == changed["windows"][0]["selected_factors"]
    )
    assert (
        baseline["windows"][0]["factor_diagnostics"]
        == changed["windows"][0]["factor_diagnostics"]
    )
    assert (
        baseline["windows"][0]["total_return_pct"]
        != changed["windows"][0]["total_return_pct"]
    )


def test_execution_horizon_is_selected_on_calibration_only():
    factors, one_day = _discovery_sample()
    three_day = one_day.copy()
    three_day["live_adjusted_return_pct"] *= 0.5
    common = {
        "train_days": 60,
        "test_days": 10,
        "step_days": 10,
        "purge_days": 4,
        "top_k": 5,
        "max_factors": 3,
        "fdr_alpha": 0.05,
        "min_cross_section": 20,
    }

    baseline = discovery.run_walk_forward_execution_discovery(
        factors,
        {
            "hold_1": one_day,
            "hold_3": three_day,
        },
        execution_metadata={
            "hold_1": {"hold_days": 1},
            "hold_3": {"hold_days": 3},
        },
        **common,
    )
    first_test_dates = set(baseline["windows"][0]["test_dates"])
    changed_one_day = one_day.copy()
    changed_one_day.loc[
        changed_one_day["trade_date"]
        .dt.strftime("%Y-%m-%d")
        .isin(first_test_dates),
        "live_adjusted_return_pct",
    ] *= -100.0
    changed = discovery.run_walk_forward_execution_discovery(
        factors,
        {
            "hold_1": changed_one_day,
            "hold_3": three_day,
        },
        execution_metadata={
            "hold_1": {"hold_days": 1},
            "hold_3": {"hold_days": 3},
        },
        **common,
    )

    assert baseline["windows"][0]["selected_execution"] == "hold_1"
    assert changed["windows"][0]["selected_execution"] == "hold_1"
    assert (
        baseline["windows"][0]["execution_selection"]
        == changed["windows"][0]["execution_selection"]
    )
    assert (
        baseline["windows"][0]["total_return_pct"]
        != changed["windows"][0]["total_return_pct"]
    )
    assert baseline["selection_policy"]["execution_selection"] == (
        "calibration_only"
    )
    assert baseline["selection_policy"]["effective_fdr_alpha"] == (
        pytest.approx(0.025)
    )
    assert all(
        "oos_rank_ic" not in candidate
        for candidate in baseline["windows"][0][
            "execution_selection"
        ]["candidates"]
    )


def test_execution_horizons_reject_misaligned_walk_forward_windows():
    factors, one_day = _discovery_sample(periods=100)
    first_date = one_day["trade_date"].min()
    shifted = one_day.loc[
        one_day["trade_date"].ne(first_date)
    ].copy()

    report = discovery.run_walk_forward_execution_discovery(
        factors,
        {
            "hold_1": one_day,
            "hold_3": shifted,
        },
        execution_metadata={
            "hold_1": {"hold_days": 1},
            "hold_3": {"hold_days": 3},
        },
        train_days=60,
        test_days=10,
        step_days=10,
        purge_days=4,
        top_k=5,
        max_factors=3,
        fdr_alpha=0.05,
        min_cross_section=20,
    )

    assert report["windows"][0]["status"] == (
        "execution_window_mismatch"
    )
    assert report["windows"][0]["selected_execution"] is None
    assert report["windows"][0]["trade_count"] == 0


def test_execution_discovery_builds_factor_long_frame_once(
    monkeypatch,
):
    factors, one_day = _discovery_sample(periods=90)
    two_day = one_day.copy()
    calls = 0
    original = discovery._factor_long_frame

    def counted(value):
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(discovery, "_factor_long_frame", counted)

    discovery.run_walk_forward_execution_discovery(
        factors,
        {"hold_1": one_day, "hold_2": two_day},
        execution_metadata={
            "hold_1": {"hold_days": 1},
            "hold_2": {"hold_days": 2},
        },
        train_days=50,
        test_days=10,
        step_days=10,
        purge_days=3,
        top_k=5,
        min_cross_section=20,
    )

    assert calls == 1


def test_generated_execution_discovery_releases_each_label_before_next(
    monkeypatch,
):
    factors, labels = _discovery_sample(periods=10)
    feature_frame = discovery._factor_long_frame(factors)
    observed: list[weakref.ReferenceType[pd.DataFrame]] = []
    built_holds = []

    def fake_build_label_frame(
        _panel,
        *,
        hold_days,
        **_kwargs,
    ):
        assert all(reference() is None for reference in observed)
        built_holds.append(hold_days)
        return labels.assign(
            entry_date=labels["trade_date"] + pd.offsets.BDay(1),
            exit_date=(
                labels["trade_date"]
                + pd.offsets.BDay(int(hold_days) + 1)
            ),
            research_evaluable=True,
            execution_blocked=False,
        )

    def fake_discovery(_factors, execution_labels, **_kwargs):
        observed.append(weakref.ref(execution_labels))
        return {
            "selection_policy": {},
            "windows": [],
            "factor_summary": {},
            "summary": discovery._summary_from_windows([]),
        }

    from analytics import relay_factor_mining

    monkeypatch.setattr(
        relay_factor_mining,
        "build_label_frame",
        fake_build_label_frame,
    )
    monkeypatch.setattr(
        discovery,
        "run_walk_forward_discovery",
        fake_discovery,
    )

    report, label_max_exit = (
        discovery._run_generated_execution_discovery(
            factors,
            feature_frame,
            {"close": factors["predictive"]},
            hold_days_candidates=(1, 2, 3),
            stop_loss=-0.035,
            take_profit=0.07,
            fdr_alpha=0.05,
            discovery_kwargs={},
        )
    )

    assert built_holds == [1, 2, 3]
    assert all(reference() is None for reference in observed)
    assert report["selection_policy"]["execution_candidates"] == [
        "hold_1",
        "hold_2",
        "hold_3",
    ]
    assert label_max_exit is not None


def test_execution_factor_requires_half_of_all_scheduled_windows(
    monkeypatch,
):
    windows = []
    for window_id in range(1, 7):
        evaluated = window_id == 1
        windows.append(
            {
                "window_id": window_id,
                "test_dates": [f"2026-07-{window_id:02d}"],
                "status": (
                    "evaluated"
                    if evaluated
                    else "calibration_rejected"
                ),
                "calibration_passed": evaluated,
                "calibration": {
                    "trade_count": 20 if evaluated else 0,
                    "avg_return_pct": 1.0 if evaluated else 0.0,
                    "total_return_pct": 1.0 if evaluated else 0.0,
                    "max_drawdown_pct": 0.0,
                    "hit_rate": 1.0 if evaluated else 0.0,
                },
                "selected_factors": ["predictive"] if evaluated else [],
                "score_weights": {"predictive": 1.0} if evaluated else {},
                "oos_rank_ic": 0.2 if evaluated else None,
                "oos_factor_diagnostics": (
                    {
                        "predictive": {
                            "mean_rank_ic": 0.2,
                            "avg_return_pct": 1.0,
                        }
                    }
                    if evaluated
                    else {}
                ),
                "trade_count": 20 if evaluated else 0,
                "hit_rate": 1.0 if evaluated else 0.0,
                "avg_return_pct": 1.0 if evaluated else 0.0,
                "total_return_pct": 1.0 if evaluated else 0.0,
                "max_drawdown_pct": 0.0,
                "daily_returns_pct": [1.0] if evaluated else [],
            }
        )

    monkeypatch.setattr(
        discovery,
        "run_walk_forward_discovery",
        lambda *_args, **_kwargs: {
            "selection_policy": {},
            "windows": windows,
        },
    )
    factors, labels = _discovery_sample(periods=10)

    report = discovery.run_walk_forward_execution_discovery(
        factors,
        {"hold_1": labels},
        execution_metadata={"hold_1": {"hold_days": 1}},
    )

    details = report["factor_summary"]["predictive"]
    assert details["selected_windows"] == 1
    assert details["oos_window_count"] == 1
    assert details["promotion_eligible"] is False


def test_label_max_exit_uses_all_execution_candidates():
    first = pd.DataFrame(
        {"exit_date": ["2026-07-20", "2026-07-21"]}
    )
    second = pd.DataFrame(
        {"exit_date": ["2026-07-22", None]}
    )

    value = discovery._maximum_label_exit(
        {"hold_1": first, "hold_3": second}
    )

    assert value == "2026-07-22"


def test_walk_forward_filters_labels_that_exit_in_test_window():
    factors, labels = _discovery_sample()
    dates = sorted(labels["trade_date"].unique())
    labels["exit_date"] = labels["trade_date"] + pd.offsets.BDay(2)
    overlap_signal_date = dates[59]
    first_test_start = dates[63]
    labels.loc[
        labels["trade_date"].eq(overlap_signal_date),
        "exit_date",
    ] = first_test_start

    report = discovery.run_walk_forward_discovery(
        factors,
        labels,
        train_days=60,
        test_days=10,
        step_days=10,
        purge_days=3,
        top_k=5,
        min_cross_section=20,
    )

    first = report["windows"][0]
    assert first["train_sample_count"] == 59 * 30
    assert first["train_label_max_exit"] < first["test_start"]


def test_walk_forward_excludes_unexecutable_and_unevaluable_labels():
    factors, labels = _discovery_sample()
    labels["execution_blocked"] = labels["ts_code"].isin(
        [f"{value:06d}" for value in range(26, 31)]
    )
    labels["research_evaluable"] = ~labels["ts_code"].isin(
        ["000001", "000002"]
    )

    report = discovery.run_walk_forward_discovery(
        factors,
        labels,
        train_days=60,
        test_days=10,
        step_days=10,
        purge_days=3,
        top_k=5,
        min_cross_section=20,
    )

    first = report["windows"][0]
    assert first["train_sample_count"] == 60 * 23
    assert first["test_sample_count"] == 10 * 23
    assert first["trade_count"] == 10 * 5


def test_daily_basic_candidates_preserve_missing_observations():
    dates = pd.bdate_range("2026-07-20", periods=6)
    rows = []
    for code in ("000001", "000002"):
        for index, trade_date in enumerate(dates):
            if code == "000002" and index == 3:
                continue
            rows.append(
                {
                    "code": code,
                    "date": trade_date,
                    "turnover_rate": 1.0 + index,
                    "turnover_rate_f": 1.2 + index,
                    "volume_ratio": 0.8 + index / 10,
                    "float_share": 1000.0,
                    "total_share": 1500.0,
                    "float_mv": 2000.0 + index,
                    "circ_mv": 1800.0 + index,
                }
            )

    factors = discovery.build_daily_basic_candidate_factors(
        pd.DataFrame(rows)
    )

    assert set(discovery.DAILY_BASIC_FACTOR_FORMULAS).issubset(factors)
    assert pd.isna(
        factors["turnover_rate_1"].loc[dates[3], "000002"]
    )
    assert not pd.isna(
        factors["turnover_rate_1"].loc[dates[3], "000001"]
    )


def test_run_short_factor_research_writes_auditable_report(tmp_path):
    panel = _panel(periods=120, codes=30)
    panel["is_st"] = pd.DataFrame(
        0.0,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    panel["st_status_known"] = pd.DataFrame(
        1.0,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    _factors, labels = _discovery_sample(periods=120, codes=30)
    label_dates = sorted(labels["trade_date"].unique())
    panel_dates = list(panel["close"].index)
    labels["trade_date"] = labels["trade_date"].map(
        dict(zip(label_dates, panel_dates))
    )

    report = discovery.run_short_factor_research(
        panel,
        labels=labels,
        out_dir=tmp_path,
        train_days=60,
        test_days=10,
        step_days=10,
        purge_days=3,
        top_k=5,
        max_interactions=2,
        min_cross_section=20,
    )

    path = tmp_path / "discovery_report.json"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == report
    assert report["schema_version"] == (
        "short_factor_discovery_report_v1"
    )
    assert report["channel"] == "short_factor_discovery"
    assert report["protocol_version"] == "short_factor_discovery_v1"
    assert report["research_only"] is True
    assert report["deployment_eligible"] is False
    assert report["production_write_attempted"] is False
    assert report["config"]["min_partial_test_days"] == 10
    assert len(report["protocol"]["candidate_grammar_sha256"]) == 64
    assert len(report["protocol"]["config_sha256"]) == 64
    assert report["lookahead_policy"]["close_factors"] == (
        "signal_date_close_for_next_session"
    )
    assert report["lookahead_policy"]["auction_factors"] == (
        "entry_date_09:25_confirmation_only"
    )
    assert len(report["close_discovery"]["windows"]) == 5
    assert all(
        "calibration_passed" in window
        for window in report["close_discovery"]["windows"]
    )
    assert report["data_quality"]["st_status_coverage"] == 1.0
    assert "factor_catalog" in report
    assert report["primary_channel"] == "close"
    interaction_name = "return_1__x__amount_shock_1_20"
    assert report["factor_catalog"][interaction_name]["formula"] == (
        "(rank(return_1) - 0.5) "
        "* (rank(amount_shock_1_20) - 0.5)"
    )
    grammar = {
        "base": discovery.BASE_FACTOR_FORMULAS,
        "daily_basic": discovery.DAILY_BASIC_FACTOR_FORMULAS,
        "structural": discovery.STRUCTURAL_FACTOR_FORMULAS,
        "auction": discovery.AUCTION_FACTOR_FORMULAS,
        "interaction_operator": "centered_cross_section_rank_product",
        "curated_interaction_pairs": [
            list(pair)
            for pair in discovery.CURATED_INTERACTION_PAIRS
        ],
        "max_interactions": 2,
    }
    assert report["protocol"]["candidate_grammar_sha256"] == (
        discovery._canonical_sha256(grammar)
    )


def test_research_report_distinguishes_unaligned_auction_history(
    tmp_path,
):
    panel = _panel(periods=80, codes=24)
    panel["security_master_eligible"] = pd.DataFrame(
        1.0,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    panel["st_status_known"] = pd.DataFrame(
        1.0,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    _factors, labels = _discovery_sample(periods=80, codes=24)
    label_dates = sorted(labels["trade_date"].unique())
    panel_dates = list(panel["close"].index)
    labels["trade_date"] = labels["trade_date"].map(
        dict(zip(label_dates, panel_dates))
    )
    labels["entry_date"] = (
        pd.to_datetime(labels["trade_date"]) + pd.offsets.BDay(1)
    )
    auction = pd.DataFrame(
        {
            "code": list(panel["close"].columns),
            "date": pd.Timestamp("2025-12-31"),
            "auction_price": 10.1,
            "pre_close": 10.0,
        }
    )
    auction.loc[0, "date"] = labels["entry_date"].dropna().iloc[0]

    report = discovery.run_short_factor_research(
        panel,
        labels=labels,
        auction=auction,
        expected_codes=list(panel["close"].columns),
        out_dir=tmp_path,
        train_days=40,
        test_days=10,
        step_days=10,
        purge_days=3,
        top_k=5,
        max_interactions=2,
        min_cross_section=20,
    )

    assert report["data_coverage"]["auction_history_eligible"] is True
    assert report["data_coverage"]["auction_used"] is False
    assert (
        report["data_coverage"]["auction_alignment_coverage"]
        < discovery.AUCTION_MIN_ALIGNMENT_COVERAGE
    )
    assert "auction_entry_alignment_unavailable" in (
        report["data_quality"]["issues"]
    )
    assert report["auction_confirmation_discovery"]["reason"] == (
        "auction_entry_alignment_unavailable"
    )


def test_generated_research_streams_close_and_auction_executions(
    monkeypatch,
):
    panel = _panel(periods=40, codes=24)
    _factors, labels = _discovery_sample(periods=40, codes=24)
    labels = labels.assign(
        entry_date=labels["trade_date"] + pd.offsets.BDay(1),
        exit_date=labels["trade_date"] + pd.offsets.BDay(3),
        research_evaluable=True,
        execution_blocked=False,
    )
    build_calls = []
    discovery_calls = []
    expression_calls = []
    memory_release_calls = []
    original_expression_builder = (
        discovery._build_expression_feature_frame
    )

    def fake_build_label_frame(
        _panel,
        *,
        hold_days,
        **_kwargs,
    ):
        build_calls.append(int(hold_days))
        return labels.assign(
            exit_date=(
                labels["trade_date"]
                + pd.offsets.BDay(int(hold_days) + 1)
            )
        )

    def fake_discovery(candidate_factors, _labels, **kwargs):
        discovery_calls.append(
            {
                "factor_count": len(candidate_factors),
                "factor_names": set(candidate_factors),
                "fdr_alpha": float(kwargs["fdr_alpha"]),
                "missing_factors": (
                    set(candidate_factors)
                    - set(kwargs["_prepared_feature_frame"].columns)
                ),
            }
        )
        return {
            "selection_policy": {},
            "windows": [],
            "factor_summary": {},
            "summary": discovery._summary_from_windows([]),
        }

    from analytics import relay_factor_mining

    monkeypatch.setattr(
        relay_factor_mining,
        "build_label_frame",
        fake_build_label_frame,
    )
    monkeypatch.setattr(
        discovery,
        "run_walk_forward_discovery",
        fake_discovery,
    )
    monkeypatch.setattr(
        discovery,
        "_release_research_memory",
        lambda: memory_release_calls.append(True),
    )

    def tracked_expression_builder(
        base_factors,
        *,
        max_interactions=30,
        consume_base=False,
        candidate_plan=None,
    ):
        expression_calls.append(
            {
                "factor_names": set(base_factors),
                "consume_base": bool(consume_base),
            }
        )
        return original_expression_builder(
            base_factors,
            max_interactions=max_interactions,
            consume_base=consume_base,
            candidate_plan=candidate_plan,
        )

    monkeypatch.setattr(
        discovery,
        "_build_expression_feature_frame",
        tracked_expression_builder,
    )
    auction = labels[
        ["ts_code", "entry_date"]
    ].drop_duplicates().rename(
        columns={"ts_code": "code", "entry_date": "date"}
    )
    auction["auction_price"] = 10.1
    auction["pre_close"] = 10.0
    auction["auction_amount"] = 1_000_000.0
    auction["auction_turnover_rate"] = 0.5
    auction["auction_volume_ratio"] = 1.2

    report = discovery.run_short_factor_research(
        panel,
        auction=auction,
        expected_codes=list(panel["close"].columns),
        hold_days_candidates=(1, 2, 3),
        max_interactions=30,
    )

    assert build_calls == [2, 1, 3]
    assert len(discovery_calls) == 6
    assert len(memory_release_calls) == 6
    assert all(
        call["fdr_alpha"] == pytest.approx(0.05 / 3)
        for call in discovery_calls
    )
    assert all(
        not call["missing_factors"]
        for call in discovery_calls
    )
    assert all(
        not any(
            name.startswith("auction_")
            or "__x__auction_" in name
            for name in call["factor_names"]
        )
        for call in discovery_calls[::2]
    )
    assert all(
        all(
            any(
                part in discovery.AUCTION_FACTOR_FORMULAS
                for part in name.split("__x__")
            )
            for name in call["factor_names"]
        )
        for call in discovery_calls[1::2]
    )
    assert len(expression_calls) == 1
    assert expression_calls[0]["consume_base"] is True
    assert "auction_gap" in expression_calls[0]["factor_names"]
    expected_executions = ["hold_1", "hold_2", "hold_3"]
    assert report["close_discovery"]["selection_policy"][
        "execution_candidates"
    ] == expected_executions
    assert report["auction_confirmation_discovery"][
        "selection_policy"
    ]["execution_candidates"] == expected_executions
    assert report["as_of"]["label_max_exit"] is not None
    assert "auction_gap" in report["factor_catalog"]
    assert report["primary_channel"] == "auction_confirmation"


def test_partial_auxiliary_coverage_is_excluded_from_candidates():
    panel = _panel(periods=120, codes=30)
    _factors, labels = _discovery_sample(periods=120, codes=30)
    dates = panel["close"].index
    rows = []
    for code in [f"{value:06d}" for value in range(1, 6)]:
        for trade_date in dates:
            rows.append(
                {
                    "code": code,
                    "date": trade_date,
                    "turnover_rate": 1.0,
                    "turnover_rate_f": 1.2,
                    "volume_ratio": 0.9,
                    "float_share": 1000.0,
                    "total_share": 1500.0,
                    "circ_mv": 1800.0,
                }
            )

    report = discovery.run_short_factor_research(
        panel,
        labels=labels,
        daily_basic=pd.DataFrame(rows),
        expected_codes=[
            f"{value:06d}" for value in range(1, 31)
        ],
        train_days=60,
        test_days=10,
        step_days=10,
        purge_days=3,
        top_k=5,
        max_interactions=0,
        min_cross_section=20,
    )

    assert report["data_coverage"]["daily_basic_code_count"] == 5
    assert report["data_coverage"]["daily_basic_coverage"] == (
        pytest.approx(5 / 30, abs=1e-6)
    )
    assert report["data_coverage"]["daily_basic_used"] is False
    assert "daily_basic_coverage_below_80pct" in report["warnings"]
    assert "turnover_rate_1" not in report["factor_catalog"]
