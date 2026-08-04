# -*- coding: utf-8 -*-
from __future__ import annotations
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics import relay_factor_mining as relay
from analytics.factors_zoo import scorer


@pytest.fixture(autouse=True)
def _deterministic_research_evidence_key(monkeypatch):
    monkeypatch.setattr(
        relay,
        "_research_evidence_key",
        lambda: b"test-research-evidence-key",
    )


def _current_protocol_binding() -> dict:
    return relay.build_current_frozen_protocol_binding()


def _panel(days: int = 40) -> dict[str, pd.DataFrame]:
    dates = pd.bdate_range("2026-01-02", periods=days)
    strong = np.linspace(10.0, 18.0, days)
    steady = np.linspace(10.0, 12.0, days)
    weak = np.linspace(12.0, 9.0, days)
    close = pd.DataFrame(
        {"STRONG": strong, "STEADY": steady, "WEAK": weak},
        index=dates,
        dtype=float,
    )
    volume = pd.DataFrame(
        {
            "STRONG": np.r_[np.full(days - 8, 100.0), np.full(8, 260.0)],
            "STEADY": np.full(days, 130.0),
            "WEAK": np.full(days, 90.0),
        },
        index=dates,
        dtype=float,
    )
    return {
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": volume,
        "amount": close * volume * 100.0,
        "amount_real": pd.DataFrame(
            True,
            index=dates,
            columns=close.columns,
        ),
    }


def test_feature_frame_is_point_in_time_and_has_required_fields():
    panel = _panel()
    baseline = relay.build_feature_frame(panel)

    shocked = {key: value.copy() for key, value in panel.items()}
    for frame in shocked.values():
        numeric_columns = [
            column
            for column in frame.columns
            if not pd.api.types.is_bool_dtype(frame[column].dtype)
        ]
        frame.loc[frame.index[-1], numeric_columns] *= 100
    changed = relay.build_feature_frame(shocked)

    compare_date = baseline["trade_date"].sort_values().unique()[-2]
    before = baseline[baseline["trade_date"] == compare_date].sort_values("ts_code").reset_index(drop=True)
    after = changed[changed["trade_date"] == compare_date].sort_values("ts_code").reset_index(drop=True)
    pd.testing.assert_series_equal(before["relay_score"], after["relay_score"], check_names=False)

    latest = baseline.sort_values(["trade_date", "relay_score"]).iloc[-1]
    assert latest["available_at"] == f"{latest['trade_date']} 15:00:00"
    assert {"ret_5", "volume_ratio_20", "near_high_20", "relay_score", "risk_flags"}.issubset(baseline.columns)


def test_feature_frame_excludes_point_in_time_pool_outsiders_from_ranks():
    panel = _panel(days=40)
    eligible = pd.DataFrame(
        True,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    eligible["WEAK"] = False
    panel["universe_eligible"] = eligible

    full = relay.build_feature_frame(panel)
    reduced_panel = {
        key: value[["STRONG", "STEADY"]].copy()
        for key, value in panel.items()
    }
    reduced = relay.build_feature_frame(reduced_panel)

    latest = full["trade_date"].max()
    full_scores = (
        full[full["trade_date"] == latest]
        .set_index("ts_code")["relay_score"]
        .sort_index()
    )
    reduced_scores = (
        reduced[reduced["trade_date"] == latest]
        .set_index("ts_code")["relay_score"]
        .sort_index()
    )
    pd.testing.assert_series_equal(full_scores, reduced_scores)
    assert "WEAK" not in full_scores.index


def test_label_frame_excludes_signal_date_universe_outsiders():
    panel = _panel(days=40)
    eligible = pd.DataFrame(
        True,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    eligible["WEAK"] = False
    panel["universe_eligible"] = eligible

    labels = relay.build_label_frame(panel, hold_days=1)

    assert set(labels["ts_code"]) == {"STRONG", "STEADY"}


def test_compact_label_paths_restore_exact_portfolio_valuation_path():
    panel = _panel(days=40)
    panel["amount_real"] = pd.DataFrame(
        True,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    panel["is_st"] = pd.DataFrame(
        False,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    panel["st_status_known"] = pd.DataFrame(
        True,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )

    labels = relay.build_label_frame(
        panel,
        hold_days=1,
        compact_diagnostics=True,
    )
    label = labels.iloc[0]

    assert set(labels.columns) == set(relay._RESEARCH_LABEL_COLUMNS)
    assert isinstance(label["valuation_path"], str)
    assert relay._portfolio_attempt(
        label,
        gate_pass=True,
    )["valuation_path"] == json.loads(label["valuation_path"])


def test_portfolio_attempt_normalizes_blocked_execution_evidence():
    attempt = relay._portfolio_attempt(
        pd.Series(
            {
                "trade_date": "2026-08-03",
                "ts_code": "000001.SZ",
                "entry_date": "2026-08-04",
                "exit_date": "2026-08-05",
                "execution_blocked": True,
                "forward_return_pct": -4.5,
                "live_adjusted_return_pct": -4.9055,
                "valuation_path": [
                    {"date": "2026-08-04", "return_pct": -1.0},
                    {"date": "2026-08-05", "return_pct": -4.9055},
                ],
                "exit_reason": "st_status_unknown",
            }
        ),
        gate_pass=True,
    )

    assert attempt["execution_blocked"] is True
    assert attempt["forward_return_pct"] == -4.5
    assert attempt["live_adjusted_return_pct"] == 0.0
    assert attempt["valuation_path"] == []
    assert attempt["exit_date"] == attempt["entry_date"]
    assert attempt["exit_reason"] == "st_status_unknown"


def test_research_merge_discards_columns_unused_by_model_or_execution():
    features = relay.build_feature_frame(_panel(days=40))
    labels = relay.build_label_frame(_panel(days=40), hold_days=1)

    merged = relay._merge_research_universe(features, labels)

    assert "available_at" not in merged
    assert "risk_flags" not in merged
    assert "entry_price" not in merged
    assert {
        "trade_date",
        "ts_code",
        "relay_score",
        "model_target_pct",
        "execution_flags",
        "valuation_path",
        *relay.CANDIDATE_FACTORS,
    }.issubset(merged.columns)


def test_feature_frame_marks_amount_proxy_when_cache_has_no_real_amount():
    panel = _panel()
    panel.pop("amount")
    panel.pop("amount_real")

    features = relay.build_feature_frame(panel)

    assert set(features["amount_data_source"]) == {"close_times_volume_proxy"}
    assert not features["amount_data_real"].astype(bool).any()


def test_feature_frame_treats_amount_without_provenance_marker_as_untrusted():
    panel = _panel()
    panel.pop("amount_real")
    assert "amount" in panel

    features = relay.build_feature_frame(panel)

    assert set(features["amount_data_source"]) == {
        "close_times_volume_proxy"
    }
    assert not features["amount_data_real"].astype(bool).any()
    assert features["amount_ratio_20"].isna().all()


def test_feature_frame_preserves_mixed_amount_sources_per_stock():
    panel = _panel()
    amount_real = pd.DataFrame(True, index=panel["close"].index, columns=panel["close"].columns)
    amount_real["WEAK"] = False
    panel["amount"]["WEAK"] = panel["close"]["WEAK"] * panel["volume"]["WEAK"]
    panel["amount_real"] = amount_real

    features = relay.build_feature_frame(panel)
    latest = features[features["trade_date"] == features["trade_date"].max()]

    assert set(latest["ts_code"]) == {"STRONG", "STEADY", "WEAK"}
    assert set(latest.loc[latest["amount_data_real"], "amount_data_source"]) == {"cache_amount"}
    assert set(latest.loc[~latest["amount_data_real"], "amount_data_source"]) == {"close_times_volume_proxy"}


def test_feature_frame_neutralizes_amount_factor_when_rolling_window_uses_proxy():
    panel = _panel(days=55)
    panel["amount_real"] = pd.DataFrame(
        True,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    missing_date = panel["close"].index[-5]
    panel["amount_real"].loc[missing_date, "STRONG"] = False
    panel["amount"].loc[missing_date, "STRONG"] = (
        panel["close"].loc[missing_date, "STRONG"]
        * panel["volume"].loc[missing_date, "STRONG"]
    )

    features = relay.build_feature_frame(panel)
    row = features[
        (features["trade_date"] == missing_date.strftime("%Y-%m-%d"))
        & (features["ts_code"] == "STRONG")
    ].iloc[0]

    assert bool(row["amount_data_real"]) is False
    assert pd.isna(row["amount_ratio_20"])
    assert pd.notna(row["relay_score"])


def test_feature_frame_preserves_partial_point_in_time_st_knowledge():
    panel = _panel()
    panel["is_st"] = pd.DataFrame(False, index=panel["close"].index, columns=panel["close"].columns)
    panel["st_status_known"] = pd.DataFrame(True, index=panel["close"].index, columns=panel["close"].columns)
    panel["is_st"].iloc[-1, panel["is_st"].columns.get_loc("STRONG")] = True
    panel["st_status_known"].iloc[-1, panel["st_status_known"].columns.get_loc("WEAK")] = False

    latest = relay.build_feature_frame(panel)
    latest = latest[latest["trade_date"] == latest["trade_date"].max()].set_index("ts_code")

    assert bool(latest.loc["STRONG", "is_st"]) is True
    assert bool(latest.loc["STRONG", "st_status_known"]) is True
    assert bool(latest.loc["WEAK", "is_st"]) is False
    assert bool(latest.loc["WEAK", "st_status_known"]) is False


def test_unknown_st_status_neutralizes_limit_count_without_dropping_other_features():
    dates = pd.bdate_range("2026-01-02", periods=30)
    close = pd.DataFrame(10.0, index=dates, columns=["KNOWN_ST", "UNKNOWN"])
    close.iloc[-1] = 10.5
    panel = {
        "close": close,
        "open": close.copy(),
        "high": close.copy(),
        "low": close.copy(),
        "volume": pd.DataFrame(1000.0, index=dates, columns=close.columns),
        "amount": pd.DataFrame(100000.0, index=dates, columns=close.columns),
        "is_st": pd.DataFrame(
            {"KNOWN_ST": [True] * len(dates), "UNKNOWN": [False] * len(dates)},
            index=dates,
        ),
        "st_status_known": pd.DataFrame(
            {"KNOWN_ST": [True] * len(dates), "UNKNOWN": [False] * len(dates)},
            index=dates,
        ),
    }

    latest = relay.build_feature_frame(panel)
    latest = latest[latest["trade_date"] == dates[-1].strftime("%Y-%m-%d")].set_index("ts_code")

    assert float(latest.loc["KNOWN_ST", "limit_count_20"]) >= 1.0
    assert pd.isna(latest.loc["UNKNOWN", "limit_count_20"])
    assert pd.notna(latest.loc["UNKNOWN", "relay_score"])


def test_factor_mining_rules_are_normalized_and_explainable():
    rules = relay.normalize_mining_rules(
        {
            "weights": {"ret_5": 0.4, "unknown_factor": 999},
            "risk_thresholds": {"high_volatility": 0.05},
            "tier_thresholds": {"strong_percentile": 0.96, "watch_percentile": 0.88},
            "filters": {"min_volume_ratio_20": 1.2, "exclude_high_risk": True},
        }
    )

    assert rules["weights"]["ret_5"] == 0.4
    assert "unknown_factor" not in rules["weights"]
    assert rules["risk_thresholds"]["high_volatility"] == 0.05
    assert rules["tier_thresholds"]["strong_percentile"] == 0.96
    assert rules["filters"]["exclude_high_risk"] is True
    assert rules["descriptions"]["weights"]["ret_5"]


def test_custom_factor_weights_change_relay_score():
    panel = _panel(days=55)
    baseline = relay.build_feature_frame(panel)
    custom = relay.build_feature_frame(
        panel,
        mining_rules={
            "weights": {
                "ret_5": 0.0,
                "ret_10": 0.0,
                "ret_20": 0.0,
                "volume_ratio_20": 0.8,
                "amount_ratio_20": 0.0,
                "near_high_20": 0.0,
                "limit_count_20": 0.0,
                "volatility_10": 0.0,
                "overheat_ret_3": 0.0,
            }
        },
    )

    compare_date = baseline["trade_date"].sort_values().unique()[-1]
    before = baseline[baseline["trade_date"] == compare_date].sort_values("ts_code").reset_index(drop=True)
    after = custom[custom["trade_date"] == compare_date].sort_values("ts_code").reset_index(drop=True)

    assert not before["relay_score"].round(8).equals(after["relay_score"].round(8))


def test_label_frame_uses_next_day_entry_and_future_exit_only_as_label():
    dates = pd.bdate_range("2026-01-02", periods=4)
    close = pd.DataFrame({"AAA": [10.0, 20.0, 10.0, 10.0]}, index=dates)
    panel = {"close": close, "open": close.copy(), "high": close, "low": close, "volume": close}

    labels = relay.build_label_frame(panel, hold_days=1)
    first = labels[labels["trade_date"] == "2026-01-02"].iloc[0]

    assert first["entry_date"] == "2026-01-05"
    assert first["exit_date"] == "2026-01-06"
    assert first["forward_return_pct"] == -50.0


def test_label_frame_does_not_fallback_missing_entry_open_to_close():
    dates = pd.bdate_range("2026-01-02", periods=4)
    close = pd.DataFrame({"AAA": [10.0, 10.5, 10.8, 11.0]}, index=dates)
    open_price = close.copy()
    open_price.iloc[1, 0] = np.nan
    panel = {
        "close": close,
        "open": open_price,
        "high": close * 1.01,
        "low": close * 0.99,
        "volume": pd.DataFrame(1000.0, index=dates, columns=["AAA"]),
        "amount": close * 1000.0,
        "amount_real": pd.DataFrame(True, index=dates, columns=["AAA"]),
        "is_st": pd.DataFrame(False, index=dates, columns=["AAA"]),
        "st_status_known": pd.DataFrame(True, index=dates, columns=["AAA"]),
    }

    labels = relay.build_label_frame(panel, hold_days=1)
    first = labels[labels["trade_date"] == dates[0].strftime("%Y-%m-%d")].iloc[0]

    assert pd.isna(first["entry_price"])
    assert bool(first["execution_blocked"]) is True
    assert first["execution_flags"] == ["entry_bar_unavailable"]
    assert first["model_target_pct"] == 0.0


def test_label_frame_marks_live_execution_risks_and_cost_adjusted_return():
    dates = pd.bdate_range("2026-01-02", periods=8)
    close = pd.DataFrame({"AAA": [10.0, 10.0, 10.5, 10.8, 11.0, 11.2, 11.3, 11.4]}, index=dates)
    open_ = close.copy()
    open_.iloc[1, 0] = 11.0
    panel = {
        "close": close,
        "open": open_,
        "high": close * 1.02,
        "low": close * 0.98,
        "volume": pd.DataFrame({"AAA": [1000.0] * len(dates)}, index=dates),
        "amount": close * 100000.0,
    }

    labels = relay.build_label_frame(panel, hold_days=1)
    first = labels[labels["trade_date"] == "2026-01-02"].iloc[0]

    assert first["entry_gap_pct"] == 10.0
    # 从 DataFrame 取回的布尔标量是 numpy.bool_，`is True` 对它恒为 False。
    # 用 bool() 归一化后再比 is True，仍保留「必须是真布尔、不能靠 1/非空串蒙混」的严格度。
    assert bool(first["execution_blocked"]) is True
    assert "entry_near_limit_up" in first["execution_flags"]
    assert first["live_adjusted_return_pct"] < first["forward_return_pct"]


def test_label_frame_does_not_use_entry_day_full_amount_at_the_open():
    dates = pd.bdate_range("2026-01-02", periods=10)
    close = pd.DataFrame({"AAA": np.linspace(10.0, 10.9, len(dates))}, index=dates)
    amount = pd.DataFrame({"AAA": [100000.0] * len(dates)}, index=dates)
    panel = {
        "close": close,
        "open": close.copy(),
        "high": close * 1.02,
        "low": close * 0.98,
        "volume": pd.DataFrame({"AAA": [1000.0] * len(dates)}, index=dates),
        "amount": amount,
    }
    signal_date = dates[5].strftime("%Y-%m-%d")
    baseline = relay.build_label_frame(panel, hold_days=1)

    changed = {key: value.copy() for key, value in panel.items()}
    changed["amount"].iloc[6, 0] = 1.0
    mutated = relay.build_label_frame(changed, hold_days=1)

    before = baseline[baseline["trade_date"] == signal_date].iloc[0]
    after = mutated[mutated["trade_date"] == signal_date].iloc[0]
    assert before["entry_date"] == after["entry_date"]
    assert before["execution_flags"] == after["execution_flags"]
    assert before["model_target_pct"] == after["model_target_pct"]


def test_label_frame_blocks_signal_row_without_real_turnover_amount():
    dates = pd.bdate_range("2026-01-02", periods=8)
    close = pd.DataFrame({"AAA": np.linspace(10.0, 10.7, len(dates))}, index=dates)
    amount_real = pd.DataFrame(True, index=dates, columns=["AAA"])
    amount_real.iloc[0, 0] = False
    panel = {
        "close": close,
        "open": close.copy(),
        "high": close * 1.01,
        "low": close * 0.99,
        "volume": pd.DataFrame({"AAA": [1000.0] * len(dates)}, index=dates),
        "amount": close * 1000.0,
        "amount_real": amount_real,
    }

    labels = relay.build_label_frame(panel, hold_days=1)
    first = labels[labels["trade_date"] == dates[0].strftime("%Y-%m-%d")].iloc[0]

    assert bool(first["amount_data_real"]) is False
    assert bool(first["execution_blocked"]) is True
    assert "real_amount_unavailable" in first["execution_flags"]
    assert first["model_target_pct"] == 0.0


def test_label_frame_defers_exit_when_deadline_volume_is_zero():
    dates = pd.bdate_range("2026-01-02", periods=5)
    close = pd.DataFrame({"AAA": [10.0, 10.0, 10.0, 8.9, 9.0]}, index=dates)
    open_price = pd.DataFrame({"AAA": [10.0, 10.0, 10.0, 8.8, 8.9]}, index=dates)
    panel = {
        "close": close,
        "open": open_price,
        "high": pd.DataFrame({"AAA": [10.1, 10.2, 10.0, 9.0, 9.1]}, index=dates),
        "low": pd.DataFrame({"AAA": [9.9, 9.8, 10.0, 8.7, 8.8]}, index=dates),
        "volume": pd.DataFrame({"AAA": [1000.0, 1000.0, 0.0, 1000.0, 1000.0]}, index=dates),
        "amount": pd.DataFrame({"AAA": [10000.0, 10000.0, 0.0, 8900.0, 9000.0]}, index=dates),
    }

    labels = relay.build_label_frame(panel, hold_days=1)
    first = labels[labels["trade_date"] == dates[0].strftime("%Y-%m-%d")].iloc[0]

    assert first["exit_date"] == dates[3].strftime("%Y-%m-%d")
    assert first["exit_reason"] == "deferred_missing_bar_exit"
    assert first["exit_price"] == 8.8


def test_label_frame_keeps_deferred_exit_when_deadline_close_is_missing():
    dates = pd.bdate_range("2026-01-02", periods=5)
    close = pd.DataFrame({"AAA": [10.0, 10.0, np.nan, 8.9, 9.0]}, index=dates)
    panel = {
        "close": close,
        "open": pd.DataFrame(
            {"AAA": [10.0, 10.0, np.nan, 8.8, 8.9]},
            index=dates,
        ),
        "high": pd.DataFrame(
            {"AAA": [10.1, 10.2, np.nan, 9.0, 9.1]},
            index=dates,
        ),
        "low": pd.DataFrame(
            {"AAA": [9.9, 9.8, np.nan, 8.7, 8.8]},
            index=dates,
        ),
        "volume": pd.DataFrame(
            {"AAA": [1000.0, 1000.0, 0.0, 1000.0, 1000.0]},
            index=dates,
        ),
        "amount": close
        * pd.DataFrame(
            {"AAA": [1000.0, 1000.0, 0.0, 1000.0, 1000.0]},
            index=dates,
        ),
        "amount_real": pd.DataFrame(True, index=dates, columns=["AAA"]),
        "is_st": pd.DataFrame(False, index=dates, columns=["AAA"]),
        "st_status_known": pd.DataFrame(True, index=dates, columns=["AAA"]),
    }

    labels = relay.build_label_frame(panel, hold_days=1)
    first = labels[labels["trade_date"] == dates[0].strftime("%Y-%m-%d")].iloc[0]

    assert first["exit_date"] == dates[3].strftime("%Y-%m-%d")
    assert first["exit_reason"] == "deferred_missing_bar_exit"
    assert first["exit_price"] == 8.8
    assert first["forward_return_pct"] == -12.0
    assert first["model_target_pct"] < -12.0


def test_label_frame_marks_entered_trade_without_exit_data_as_unevaluable():
    dates = pd.bdate_range("2026-01-02", periods=3)
    close = pd.DataFrame({"AAA": [10.0, 10.0, np.nan]}, index=dates)
    panel = {
        "close": close,
        "open": close.copy(),
        "high": close.copy(),
        "low": close.copy(),
        "volume": pd.DataFrame({"AAA": [1000.0, 1000.0, 0.0]}, index=dates),
        "amount": close * 1000.0,
        "amount_real": pd.DataFrame(True, index=dates, columns=["AAA"]),
        "is_st": pd.DataFrame(False, index=dates, columns=["AAA"]),
        "st_status_known": pd.DataFrame(True, index=dates, columns=["AAA"]),
    }

    labels = relay.build_label_frame(panel, hold_days=1)
    first = labels[labels["trade_date"] == dates[0].strftime("%Y-%m-%d")].iloc[0]

    assert first["entry_date"] == dates[1].strftime("%Y-%m-%d")
    assert first["label_status"] == "entered_pending_exit"
    assert bool(first["research_evaluable"]) is False
    assert pd.isna(first["model_target_pct"])
    assert pd.isna(first["exit_date"])


def test_label_frame_uses_st_limit_status_for_next_open_risk():
    dates = pd.bdate_range("2026-01-02", periods=8)
    close = pd.DataFrame({"000001.SZ": [10.0] * len(dates)}, index=dates)
    open_price = close.copy()
    open_price.iloc[1, 0] = 10.48
    panel = {
        "close": close,
        "open": open_price,
        "high": pd.DataFrame({"000001.SZ": [10.6] * len(dates)}, index=dates),
        "low": pd.DataFrame({"000001.SZ": [9.8] * len(dates)}, index=dates),
        "volume": pd.DataFrame({"000001.SZ": [1000.0] * len(dates)}, index=dates),
        "amount": pd.DataFrame({"000001.SZ": [100000.0] * len(dates)}, index=dates),
        "is_st": pd.DataFrame(True, index=dates, columns=["000001.SZ"]),
    }

    labels = relay.build_label_frame(panel, hold_days=1)
    first = labels[labels["trade_date"] == dates[0].strftime("%Y-%m-%d")].iloc[0]

    assert bool(first["is_st"]) is True
    assert bool(first["execution_blocked"]) is True
    assert "entry_near_limit_up" in first["execution_flags"]


def test_label_frame_excludes_known_st_even_without_limit_risk():
    dates = pd.bdate_range("2026-01-02", periods=5)
    close = pd.DataFrame(
        {"000001.SZ": [10.0, 10.0, 10.5, 10.8, 11.0]},
        index=dates,
    )
    panel = {
        "close": close,
        "open": close.copy(),
        "high": close * 1.01,
        "low": close * 0.99,
        "volume": pd.DataFrame(
            {"000001.SZ": [1000.0] * len(dates)},
            index=dates,
        ),
        "amount": close * 1000.0,
        "is_st": pd.DataFrame(True, index=dates, columns=["000001.SZ"]),
    }

    labels = relay.build_label_frame(panel, hold_days=1)
    first = labels[labels["trade_date"] == dates[0].strftime("%Y-%m-%d")].iloc[0]

    assert bool(first["is_st"]) is True
    assert bool(first["execution_blocked"]) is True
    assert "known_st_excluded" in first["execution_flags"]
    assert first["model_target_pct"] == 0.0


def test_label_frame_uses_conservative_st_threshold_when_status_is_unknown():
    dates = pd.bdate_range("2026-01-02", periods=8)
    close = pd.DataFrame({"000001.SZ": [10.0] * len(dates)}, index=dates)
    open_price = close.copy()
    open_price.iloc[1, 0] = 10.48
    panel = {
        "close": close,
        "open": open_price,
        "high": pd.DataFrame({"000001.SZ": [10.6] * len(dates)}, index=dates),
        "low": pd.DataFrame({"000001.SZ": [9.8] * len(dates)}, index=dates),
        "volume": pd.DataFrame({"000001.SZ": [1000.0] * len(dates)}, index=dates),
        "amount": pd.DataFrame({"000001.SZ": [100000.0] * len(dates)}, index=dates),
    }

    labels = relay.build_label_frame(panel, hold_days=1)
    first = labels[labels["trade_date"] == dates[0].strftime("%Y-%m-%d")].iloc[0]

    assert bool(first["st_status_known"]) is False
    assert bool(first["execution_blocked"]) is True
    assert "entry_near_limit_up" in first["execution_flags"]


def test_label_frame_blocks_unknown_entry_st_status_without_other_risks():
    dates = pd.bdate_range("2026-01-02", periods=5)
    close = pd.DataFrame({"000001.SZ": [10.0] * len(dates)}, index=dates)
    panel = {
        "close": close,
        "open": close.copy(),
        "high": close * 1.01,
        "low": close * 0.99,
        "volume": pd.DataFrame(1000.0, index=dates, columns=["000001.SZ"]),
        "amount": close * 1000.0,
        "amount_real": pd.DataFrame(
            True,
            index=dates,
            columns=["000001.SZ"],
        ),
        "is_st": pd.DataFrame(
            False,
            index=dates,
            columns=["000001.SZ"],
        ),
        "st_status_known": pd.DataFrame(
            False,
            index=dates,
            columns=["000001.SZ"],
        ),
    }

    labels = relay.build_label_frame(panel, hold_days=1)
    first = labels[labels["trade_date"] == dates[0].strftime("%Y-%m-%d")].iloc[0]

    assert bool(first["execution_blocked"]) is True
    assert first["execution_flags"] == ["st_status_unknown"]
    assert first["exit_reason"] == "max_holding_period"
    assert first["model_target_pct"] == 0.0


def test_trade_summary_counts_first_trade_loss_from_initial_equity():
    summary = relay._trade_summary([-10.0, 5.0])

    assert summary["total_return_pct"] == -5.5
    assert summary["max_drawdown_pct"] == -10.0


def test_run_research_writes_report_and_latest_candidates(tmp_path):
    result = relay.run_relay_research(
        panel=_panel(days=55),
        out_dir=tmp_path,
        train_days=25,
        test_days=10,
        hold_days=2,
        top_k=2,
    )

    assert result["status"] == "degraded"
    assert result["summary"]["feature_rows"] > 0
    assert result["summary"]["label_rows"] > 0
    assert result["summary"]["lookahead_policy"] == "features_current_or_past_labels_t_plus_1"
    assert "live_hit_rate" in result["summary"]
    assert "live_total_return_pct" in result["summary"]
    assert "live_execution_block_rate" in result["summary"]
    assert result["summary"]["amount_data_real"] is True
    assert result["summary"]["amount_data_source"] == "cache_amount"
    assert result["summary"]["panel_stock_count"] == 3
    assert result["summary"]["panel_max_abs_close_return"] < 0.35
    assert result["summary"]["st_status_coverage"] == 0.0
    assert result["summary"]["latest_st_status_coverage"] == 0.0
    assert (
        result["config"]["data_quality_policy"]["discontinuous_price_history"]
        == "quarantine_from_first_detectable_break"
    )
    assert (
        result["config"]["data_quality_policy"]["st_status_unknown"]
        == "display_only_zero_position_and_reject_execution"
    )
    assert (tmp_path / "features.csv").exists()
    assert (tmp_path / "labels.csv").exists()
    assert (tmp_path / "walkforward_report.json").exists()
    assert (tmp_path / "latest_candidates.json").exists()
    assert (tmp_path / "factor_model.json").exists()
    assert result["model"]["model_version"] == "relay_factor_mining_v4_fixed_ridge"
    assert result["summary"]["backtest_policy"]["purge"] == "exit_date_before_test_start"

    candidates = json.loads((tmp_path / "latest_candidates.json").read_text(encoding="utf-8"))
    assert candidates
    assert {"ts_code", "trade_date", "relay_score", "confidence", "entry_mode", "stop_loss", "take_profit"}.issubset(
        candidates[0]
    )


def test_run_research_never_aggregates_feedback_without_an_owner(
    tmp_path,
    monkeypatch,
):
    from analytics import live_factor_feedback

    monkeypatch.setattr(
        live_factor_feedback,
        "sync_paper_trades",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("feedback sync must be skipped without an owner")
        ),
    )
    monkeypatch.setattr(
        live_factor_feedback,
        "build_code_feedback",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("all-user feedback aggregation is forbidden")
        ),
    )

    result = relay.run_relay_research(
        panel=_panel(days=55),
        out_dir=tmp_path,
        train_days=25,
        test_days=10,
        hold_days=2,
        top_k=2,
        feedback_user_id=None,
        load_live_feedback=True,
    )

    assert result["summary"]["live_feedback_code_count"] == 0
    assert result["summary"]["live_feedback_user_id"] is None
    assert result["summary"]["live_feedback_status"] == "degraded"
    assert (
        result["summary"]["live_feedback_error"]
        == "feedback_owner_unavailable"
    )


def test_run_research_requires_security_master_for_cache_panel(tmp_path, monkeypatch):
    captured = {}
    panel = _panel(days=55)

    def fake_build_panel_from_cache(**kwargs):
        captured.update(kwargs)
        return panel, list(panel["close"].columns)

    monkeypatch.setattr(relay, "build_panel_from_cache", fake_build_panel_from_cache)

    relay.run_relay_research(
        panel=None,
        out_dir=tmp_path,
        n_stocks=2,
        days=55,
        train_days=25,
        test_days=10,
        top_k=2,
        require_latest_data=False,
    )

    assert captured["point_in_time"] is True
    assert captured["require_security_master"] is True


def test_run_research_uses_reserve_then_selects_requested_daily_universe(
    tmp_path,
    monkeypatch,
):
    panel = _panel(days=55)
    panel["close"]["RESERVE"] = panel["close"]["STEADY"] * 1.01
    for field in ("open", "high", "low", "volume", "amount", "amount_real"):
        panel[field]["RESERVE"] = panel[field]["STEADY"]
    panel["universe_eligible"] = pd.DataFrame(
        True,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    captured = {}

    def fake_build_panel_from_cache(**kwargs):
        captured.update(kwargs)
        return panel, list(panel["close"].columns)

    monkeypatch.setattr(
        relay,
        "build_panel_from_cache",
        fake_build_panel_from_cache,
    )

    result = relay.run_relay_research(
        panel=None,
        out_dir=tmp_path,
        n_stocks=3,
        days=55,
        train_days=25,
        test_days=10,
        top_k=2,
        require_latest_data=False,
        load_live_feedback=False,
    )

    assert captured["n_stocks"] == 4
    assert result["summary"]["universe_required_count"] == 3
    assert result["summary"]["universe_eligible_count_max"] == 3
    assert result["summary"]["latest_universe_eligible_count"] == 3


def test_universe_audit_rejects_equal_count_code_substitution():
    dates = pd.to_datetime(["2026-07-29", "2026-07-30"])
    panel = {
        "close": pd.DataFrame(
            {
                "000001": [10.0, 10.1],
                "000002": [20.0, 20.1],
                "000003": [30.0, 30.1],
            },
            index=dates,
        ),
        "universe_eligible": pd.DataFrame(
            {
                "000001": [True, True],
                "000002": [True, True],
                "000003": [False, False],
            },
            index=dates,
        ),
    }
    features = pd.DataFrame(
        [
            {"trade_date": trade_date, "ts_code": code}
            for trade_date in ("2026-07-29", "2026-07-30")
            for code in ("000001", "000003")
        ]
    )

    audit = relay._universe_eligibility_audit(
        panel,
        features,
        required_count=2,
    )

    assert audit["coverage"] == 0.0
    assert audit["identity_coverage"] == 0.0
    assert audit["latest_identity_match"] is False
    assert all(
        item["required_codes_sha256"]
        != item["panel_codes_sha256"]
        for item in audit["date_code_digests"]
    )


def _raw_fingerprint_fixture(days: int = 8):
    panel = _panel(days=days)
    panel["is_st"] = pd.DataFrame(
        False,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    panel["st_status_known"] = pd.DataFrame(
        True,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    for field in ("universe_eligible", "security_master_eligible"):
        panel[field] = pd.DataFrame(
            True,
            index=panel["close"].index,
            columns=panel["close"].columns,
        )
    codes = list(panel["close"].columns)
    security_master = pd.DataFrame(
        {
            "code": codes,
            "list_date": ["20200101"] * len(codes),
            "delist_date": [""] * len(codes),
            "snapshot_at": ["2026-07-31T17:10:00+08:00"] * len(codes),
        }
    )
    trade_dates = [
        value.strftime("%Y-%m-%d")
        for value in panel["close"].index
    ]
    market_signals = pd.DataFrame(
        {
            "trade_date": trade_dates,
            "market_data_trusted": [True] * len(trade_dates),
            "market_data_status": ["ok"] * len(trade_dates),
            "market_data_reason": [""] * len(trade_dates),
            "market_emotion_score": [65.0] * len(trade_dates),
            "up_count": list(range(100, 100 + len(trade_dates))),
            "down_count": list(range(50, 50 + len(trade_dates))),
            "turnover_amount_yi": [18000.0] * len(trade_dates),
        }
    )
    calendar_snapshot = {
        "snapshot_version": "test_calendar_v1",
        "start_date": trade_dates[0],
        "end_date": trade_dates[-1],
        "open_days": trade_dates,
    }
    return panel, security_master, market_signals, calendar_snapshot


def test_raw_panel_fingerprint_is_stable_across_frame_order():
    panel, master, signals, calendar = _raw_fingerprint_fixture()
    first = relay.build_raw_data_fingerprint(
        panel,
        security_master=master,
        market_signal_frame=signals,
        calendar_snapshot=calendar,
        source_kind="trusted_cache",
    )
    reordered_panel = {
        key: frame.iloc[::-1, ::-1]
        for key, frame in reversed(list(panel.items()))
    }
    second = relay.build_raw_data_fingerprint(
        reordered_panel,
        security_master=master.iloc[::-1, ::-1],
        market_signal_frame=signals.iloc[::-1, ::-1],
        calendar_snapshot=json.loads(
            json.dumps(calendar, sort_keys=True)
        ),
        source_kind="trusted_cache",
    )

    assert first["root_sha256"] == second["root_sha256"]
    assert first["lock_eligible"] is True


def test_raw_fingerprint_binds_market_signals_and_security_master():
    panel, master, signals, calendar = _raw_fingerprint_fixture()
    baseline = relay.build_raw_data_fingerprint(
        panel,
        security_master=master,
        market_signal_frame=signals,
        calendar_snapshot=calendar,
        source_kind="trusted_cache",
    )
    changed_master = master.copy()
    changed_master.loc[0, "list_date"] = "20210101"
    master_result = relay.build_raw_data_fingerprint(
        panel,
        security_master=changed_master,
        market_signal_frame=signals,
        calendar_snapshot=calendar,
        source_kind="trusted_cache",
    )
    changed_signals = signals.copy()
    changed_signals.loc[0, "up_count"] += 1
    signal_result = relay.build_raw_data_fingerprint(
        panel,
        security_master=master,
        market_signal_frame=changed_signals,
        calendar_snapshot=calendar,
        source_kind="trusted_cache",
    )

    assert master_result["root_sha256"] != baseline["root_sha256"]
    assert signal_result["root_sha256"] != baseline["root_sha256"]
    assert (
        master_result["blocks"]["security_master"]["sha256"]
        != baseline["blocks"]["security_master"]["sha256"]
    )
    assert (
        signal_result["blocks"]["market_signals"]["sha256"]
        != baseline["blocks"]["market_signals"]["sha256"]
    )


def test_preregistered_oos_rejects_raw_panel_fingerprint_mismatch(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    panel, master, signals, calendar = _raw_fingerprint_fixture()
    fingerprint = relay.build_raw_data_fingerprint(
        panel,
        security_master=master,
        market_signal_frame=signals,
        calendar_snapshot=calendar,
        source_kind="trusted_cache",
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    walk_forward["raw_data_fingerprint"] = fingerprint
    preregistered = walk_forward["preregistered_oos"]
    preregistered["raw_data_fingerprint"] = fingerprint
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(preregistered)
    )
    changed_signals = signals.copy()
    changed_signals.loc[0, "up_count"] += 1
    walk_forward["raw_data_fingerprint"] = (
        relay.build_raw_data_fingerprint(
            panel,
            security_master=master,
            market_signal_frame=changed_signals,
            calendar_snapshot=calendar,
            source_kind="trusted_cache",
        )
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert assessment["preregistered_oos_evidence_verified"] is False
    assert "raw_data_fingerprint_mismatch" in assessment["failure_reasons"]


def test_injected_research_panel_is_never_lock_eligible(tmp_path):
    result = relay.run_relay_research(
        panel=_panel(days=100),
        out_dir=tmp_path,
        n_stocks=3,
        train_days=40,
        test_days=10,
        top_k=2,
        require_latest_data=False,
        load_live_feedback=False,
    )

    assert result["raw_data_fingerprint"]["source_kind"] == "injected"
    assert result["raw_data_fingerprint"]["lock_eligible"] is False
    assert "injected_research_panel_not_lock_eligible" in (
        result["research_assessment"]["failure_reasons"]
    )


def test_cache_loaded_research_panel_keeps_trusted_provenance(
    tmp_path,
    monkeypatch,
):
    panel, master, signals, calendar = _raw_fingerprint_fixture(days=100)
    trusted_panel = scorer._TrustedCachePanel(
        panel,
        security_master_snapshot=master,
        snapshot_reference="test-cache-snapshot",
    )
    monkeypatch.setattr(
        relay,
        "_build_raw_calendar_snapshot",
        lambda _panel: calendar,
    )

    result = relay.run_relay_research(
        panel=trusted_panel,
        market_signal_frame=signals,
        out_dir=tmp_path,
        n_stocks=3,
        train_days=40,
        test_days=10,
        top_k=2,
        require_latest_data=False,
        load_live_feedback=False,
    )

    assert result["raw_data_fingerprint"]["source_kind"] == "trusted_cache"
    assert result["raw_data_fingerprint"]["lock_eligible"] is True


def test_deterministic_benchmark_selection_is_row_order_invariant():
    codes = ["AAA", "BBB", "CCC", "DDD", "EEE"]

    first = relay._deterministic_benchmark_codes(
        codes,
        trade_date="2026-08-03",
        top_k=2,
    )
    second = relay._deterministic_benchmark_codes(
        list(reversed(codes)),
        trade_date="2026-08-03",
        top_k=2,
    )

    assert first == ["EEE", "DDD"]
    assert second == first


def test_preregistered_windows_use_observed_oos_diagnostics():
    registration = relay.load_model_preregistration()
    model = relay._registered_model_from_preregistration(registration)
    assert model["validation_stable"] is False
    dates = [
        value.strftime("%Y-%m-%d")
        for value in pd.bdate_range("2026-08-03", periods=4)
    ]
    trades = []
    attempts = []
    benchmark_attempts = []
    diagnostics = []
    for index, trade_date in enumerate(dates):
        strategy_code = f"STRATEGY{index}"
        benchmark_code = f"CONTROL{index}"
        attempts.append(
            {
                "trade_date": trade_date,
                "ts_code": strategy_code,
                "entry_date": trade_date,
                "exit_date": trade_date,
                "execution_blocked": False,
                "forward_return_pct": 1.0,
                "live_adjusted_return_pct": 1.0,
                "valuation_path": [
                    {"date": trade_date, "return_pct": 1.0}
                ],
                "exit_reason": "take_profit",
                "gate_pass": True,
            }
        )
        benchmark_attempts.append(
            {
                "trade_date": trade_date,
                "ts_code": benchmark_code,
                "entry_date": trade_date,
                "exit_date": trade_date,
                "execution_blocked": False,
                "forward_return_pct": 0.1,
                "live_adjusted_return_pct": 0.1,
                "valuation_path": [
                    {"date": trade_date, "return_pct": 0.1}
                ],
                "exit_reason": "take_profit",
                "gate_pass": True,
            }
        )
        trades.append(
            {
                "trade_date": trade_date,
                "codes": [strategy_code],
                "return_pct": 1.0,
                "live_adjusted_return_pct": 1.0,
                "research_evaluable": True,
                "gate_pass": True,
                "gate_reason": "gate_disabled",
                "model_sha256": relay.factor_model_digest(model),
            }
        )
        diagnostics.append(
            {
                "trade_date": trade_date,
                "universe_codes": [
                    benchmark_code,
                    strategy_code,
                ],
                "universe_codes_sha256": "0" * 64,
                "sample_count": 100,
                "rank_ic": 0.05,
                "bottom_quantile_return_pct": -0.5,
                "top_quantile_return_pct": 1.0,
                "top_bottom_spread_pct": 1.5,
                "benchmark_codes": [benchmark_code],
                "benchmark_evaluable": True,
            }
        )

    results = relay._build_preregistered_oos_results(
        session_dates=dates,
        trades=trades,
        attempts=attempts,
        benchmark_attempts=benchmark_attempts,
        session_diagnostics=diagnostics,
        model=model,
        test_days=2,
        top_k=1,
        holding_sessions=1,
        validation_rules={
            "minimum_diagnostic_sample_count_per_session": 20,
            "minimum_oos_mean_rank_ic": 0.01,
            "minimum_positive_rank_ic_session_rate": 0.5,
            "minimum_oos_mean_spread_pct": 0.0,
            "minimum_positive_spread_session_rate": 0.5,
        },
    )

    assert len(results["windows"]) == 2
    assert len(
        {window["window_id"] for window in results["windows"]}
    ) == 2
    assert all(
        window["oos_diagnostic_pass"] is True
        for window in results["windows"]
    )
    assert all(
        "model_validation_stable" not in window
        for window in results["windows"]
    )
    assert all(
        window["live_excess_return_pct"] > 0.0
        for window in results["windows"]
    )


def _fixed_window_sequence_fixture() -> tuple[
    list[str],
    list[dict],
    str,
]:
    session_dates = [
        value.strftime("%Y-%m-%d")
        for value in pd.bdate_range("2026-08-03", periods=4)
    ]
    model_sha256 = "a" * 64
    windows = []
    for window_index in range(2):
        window_dates = session_dates[
            window_index * 2 : (window_index + 1) * 2
        ]
        material = {
            "policy_version": relay.OOS_DIAGNOSTIC_POLICY_VERSION,
            "model_sha256": model_sha256,
            "window_index": window_index,
            "session_dates": window_dates,
        }
        windows.append(
            {
                "window_id": hashlib.sha256(
                    json.dumps(
                        material,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "window_index": window_index,
                "test_start": window_dates[0],
                "test_end": window_dates[-1],
                "session_count": 2,
                "complete": True,
                "model_sha256": model_sha256,
            }
        )
    return session_dates, windows, model_sha256


def test_fixed_oos_window_sequence_accepts_exact_calendar_prefix():
    session_dates, windows, model_sha256 = (
        _fixed_window_sequence_fixture()
    )

    assert relay._validate_fixed_oos_window_sequence(
        windows,
        session_dates=session_dates,
        expected_session_dates=session_dates,
        model_sha256=model_sha256,
        window_sessions=2,
    )


@pytest.mark.parametrize(
    "tampering",
    [
        "missing_trading_day",
        "duplicate_window_id",
        "shifted_window_boundary",
    ],
)
def test_fixed_oos_window_sequence_rejects_noncanonical_partition(
    tampering,
):
    expected_dates, windows, model_sha256 = (
        _fixed_window_sequence_fixture()
    )
    session_dates = list(expected_dates)
    if tampering == "missing_trading_day":
        session_dates[1] = "2026-08-10"
    elif tampering == "duplicate_window_id":
        windows[1]["window_id"] = windows[0]["window_id"]
    else:
        windows[1]["test_start"] = windows[0]["test_end"]

    assert not relay._validate_fixed_oos_window_sequence(
        windows,
        session_dates=session_dates,
        expected_session_dates=expected_dates,
        model_sha256=model_sha256,
        window_sessions=2,
    )


def test_excess_bootstrap_ci_is_deterministic_for_paired_curves():
    dates = [
        value.strftime("%Y-%m-%d")
        for value in pd.bdate_range("2026-08-03", periods=25)
    ]
    strategy_equity = 1.0
    strategy_curve = [
        {"date": "initial", "equity": strategy_equity}
    ]
    benchmark_curve = [{"date": "initial", "equity": 1.0}]
    for trade_date in dates:
        strategy_equity *= 1.01
        strategy_curve.append(
            {"date": trade_date, "equity": strategy_equity}
        )
        benchmark_curve.append(
            {"date": trade_date, "equity": 1.0}
        )
    strategy = {"equity_curve": strategy_curve}
    benchmark = {"equity_curve": benchmark_curve}

    first = relay._bootstrap_excess_return_ci(
        strategy,
        benchmark,
        samples=500,
        block_size=5,
    )
    second = relay._bootstrap_excess_return_ci(
        strategy,
        benchmark,
        samples=500,
        block_size=5,
    )

    assert first == second
    assert first["samples"] == 500
    assert first["paired_return_count"] == 25
    assert first["lower"] > 0.0


def test_run_research_stays_degraded_when_walkforward_exists_but_st_coverage_is_missing(tmp_path):
    result = relay.run_relay_research(
        panel=_panel(days=100),
        out_dir=tmp_path,
        train_days=40,
        test_days=10,
        hold_days=2,
        top_k=2,
    )

    assert result["summary"]["window_count"] > 0
    assert result["status"] == "degraded"
    assert "ST status coverage" in result["reason"]


def test_run_research_publishes_only_the_preregistered_calibration_model(
    tmp_path,
):
    result = relay.run_relay_research(
        panel=_panel(days=100),
        out_dir=tmp_path,
        train_days=40,
        test_days=10,
        hold_days=2,
        top_k=2,
    )

    registration = relay.load_model_preregistration()
    model = result["model"]

    assert registration["ok"] is True
    assert model["training_scope"] == "preregistered_calibration_only"
    assert model["preregistration_record_sha256"] == registration["sha256"]
    assert model["refit_end"] <= registration["record"]["calibrated_through"]
    assert (
        model["refit_label_max_exit"]
        <= registration["record"]["calibrated_through"]
    )


def test_run_research_lockable_report_preserves_frozen_model_digest(
    tmp_path,
    monkeypatch,
):
    registration = _eligible_registration(n_stocks=3)
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    evidence = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
        n_stocks=3,
    )
    walk_forward = {
        **evidence,
        "summary": {
            "window_count": len(evidence["windows"]),
            "untrusted_oos_sessions": 0,
            "unevaluable_oos_slots": 0,
        },
        "trades": [],
        "latest_model": evidence["registered_model"],
    }
    def frozen_walk_forward(*args, **kwargs):
        result = json.loads(json.dumps(walk_forward))
        preregistered = result["preregistered_oos"]
        preregistered["data_quality_assessment"] = kwargs[
            "data_quality_assessment"
        ]
        preregistered["raw_data_fingerprint"] = kwargs[
            "raw_data_fingerprint"
        ]
        result["preregistered_oos"] = (
            relay._seal_preregistered_oos_evidence(preregistered)
        )
        result["raw_data_fingerprint"] = kwargs[
            "raw_data_fingerprint"
        ]
        return result

    monkeypatch.setattr(
        relay,
        "walk_forward_relay",
        frozen_walk_forward,
    )
    panel = _panel(days=55)
    signed_cutoff = evidence["preregistered_oos"]["observation_cutoff"]
    feature_cutoff = (
        pd.Timestamp(signed_cutoff) + pd.offsets.BDay(3)
    ).strftime("%Y-%m-%d")
    panel_dates = pd.bdate_range(
        end=feature_cutoff,
        periods=len(panel["close"].index),
    )
    for frame in panel.values():
        frame.index = panel_dates
    index = panel["close"].index
    columns = panel["close"].columns
    panel["is_st"] = pd.DataFrame(
        False,
        index=index,
        columns=columns,
    )
    panel["st_status_known"] = pd.DataFrame(
        True,
        index=index,
        columns=columns,
    )
    panel["universe_eligible"] = pd.DataFrame(
        True,
        index=index,
        columns=columns,
    )
    panel["security_master_eligible"] = pd.DataFrame(
        True,
        index=index,
        columns=columns,
    )
    security_master = pd.DataFrame(
        {
            "code": list(columns),
            "list_date": ["20200101"] * len(columns),
            "delist_date": [""] * len(columns),
            "snapshot_at": ["2026-07-31T17:10:00+08:00"] * len(columns),
        }
    )
    panel = scorer._TrustedCachePanel(
        panel,
        security_master_snapshot=security_master,
        snapshot_reference="test-cache-snapshot",
    )
    raw_calendar = {
        "snapshot_version": "test_calendar_v1",
        "start_date": index.min().strftime("%Y-%m-%d"),
        "end_date": index.max().strftime("%Y-%m-%d"),
        "open_days": index.strftime("%Y-%m-%d").tolist(),
    }
    monkeypatch.setattr(
        relay,
        "_build_raw_calendar_snapshot",
        lambda _panel: raw_calendar,
    )
    market_signals = pd.DataFrame(
        {
            "trade_date": index.strftime("%Y-%m-%d"),
            "market_data_trusted": True,
            "market_data_status": "ok",
            "market_data_reason": "",
            "market_emotion_score": 50.0,
        }
    )

    report = relay.run_relay_research(
        panel=panel,
        out_dir=tmp_path,
        n_stocks=3,
        days=180,
        train_days=60,
        test_days=2,
        hold_days=2,
        top_k=5,
        candidate_pool_size=100,
        market_signal_frame=market_signals,
        load_live_feedback=False,
    )

    assert report["status"] == "ok"
    assert report["summary"]["amount_data_coverage"] == 1.0
    assert report["summary"]["latest_trade_date"] == feature_cutoff
    assert report["summary"]["latest_oos_observation_date"] == signed_cutoff
    assert relay.factor_model_digest(report["model"]) == (
        registration["model_sha256"]
    )
    report["generated_at"] = pd.Timestamp(
        report["preregistered_oos"]["feature_freshness"]["as_of"]
    ).strftime("%Y-%m-%d %H:%M:%S")
    manifest = _build_manifest_for_test(report)
    assert relay.factor_model_digest(manifest["model"]) == (
        registration["model_sha256"]
    )


def test_run_research_degrades_when_real_amount_coverage_is_too_low(tmp_path):
    panel = _panel(days=100)
    panel["amount_real"] = pd.DataFrame(
        False,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    panel["amount"] = panel["close"] * panel["volume"]
    panel["is_st"] = pd.DataFrame(
        False,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    panel["st_status_known"] = pd.DataFrame(
        True,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )

    result = relay.run_relay_research(
        panel=panel,
        out_dir=tmp_path,
        train_days=40,
        test_days=10,
        hold_days=2,
        top_k=2,
    )

    assert result["summary"]["window_count"] > 0
    assert result["summary"]["amount_data_coverage"] == 0.0
    assert result["status"] == "degraded"
    assert "turnover amount coverage" in result["reason"]


def test_run_research_degrades_without_point_in_time_universe_markers(tmp_path):
    panel = _panel(days=100)
    panel["is_st"] = pd.DataFrame(
        False,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    panel["st_status_known"] = pd.DataFrame(
        True,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )

    result = relay.run_relay_research(
        panel=panel,
        out_dir=tmp_path,
        train_days=40,
        test_days=10,
        hold_days=2,
        top_k=2,
    )

    assert result["summary"]["window_count"] > 0
    assert result["summary"]["universe_eligibility_coverage"] == 0.0
    assert result["status"] == "degraded"
    assert "point-in-time universe size too low" in result["reason"]


def test_run_research_fails_closed_when_any_required_date_has_too_few_stocks(
    tmp_path,
):
    panel = _panel(days=100)
    index = panel["close"].index
    columns = panel["close"].columns
    panel["is_st"] = pd.DataFrame(False, index=index, columns=columns)
    panel["st_status_known"] = pd.DataFrame(True, index=index, columns=columns)
    eligible = pd.DataFrame(True, index=index, columns=columns)
    eligible.loc[index[-10], columns[-1]] = False
    panel["universe_eligible"] = eligible

    result = relay.run_relay_research(
        panel=panel,
        out_dir=tmp_path,
        train_days=40,
        test_days=10,
        hold_days=2,
        top_k=2,
    )

    summary = result["summary"]
    assert summary["universe_required_count"] == len(columns)
    assert summary["universe_eligible_count_min"] == len(columns) - 1
    assert summary["universe_eligible_count_max"] == len(columns)
    assert summary["latest_universe_eligible_count"] == len(columns)
    assert summary["universe_eligibility_coverage"] < 1.0
    assert result["status"] == "degraded"
    assert "point-in-time universe size too low" in result["reason"]


def test_universe_audit_overrides_optimistic_research_assessment(
    tmp_path,
    monkeypatch,
):
    panel = _panel(days=100)
    index = panel["close"].index
    columns = panel["close"].columns
    panel["is_st"] = pd.DataFrame(False, index=index, columns=columns)
    panel["st_status_known"] = pd.DataFrame(True, index=index, columns=columns)
    eligible = pd.DataFrame(True, index=index, columns=columns)
    eligible.loc[index[-10], columns[-1]] = False
    panel["universe_eligible"] = eligible
    original_assessment = relay.assess_research_result

    def optimistic_assessment(walk_forward):
        assessment = original_assessment(walk_forward)
        assessment.update(
            {
                "research_status": "validated",
                "metric_gate_passed": True,
                "lock_eligible": True,
                "deployment_eligible": True,
                "research_only": False,
                "failure_reasons": [],
            }
        )
        return assessment

    monkeypatch.setattr(
        relay,
        "assess_research_result",
        optimistic_assessment,
    )

    result = relay.run_relay_research(
        panel=panel,
        out_dir=tmp_path,
        train_days=40,
        test_days=10,
        hold_days=2,
        top_k=2,
    )

    assessment = result["research_assessment"]
    assert assessment["research_status"] == "failed"
    assert assessment["lock_eligible"] is False
    assert assessment["deployment_eligible"] is False
    assert assessment["research_only"] is True
    assert "universe_size_below_required" in assessment["failure_reasons"]


def test_failed_market_gate_makes_every_latest_candidate_non_executable(tmp_path, monkeypatch):
    panel = _panel(days=55)
    dates = panel["close"].index.strftime("%Y-%m-%d")
    market_signals = pd.DataFrame(
        {
            "trade_date": dates,
            "market_data_trusted": True,
            "market_data_status": "ok",
            "market_data_reason": "",
            "market_emotion_score": 90.0,
        }
    )
    monkeypatch.setattr(relay, "_resolve_stock_name", lambda code: str(code))

    report = relay.run_relay_research(
        panel=panel,
        out_dir=tmp_path,
        train_days=25,
        test_days=10,
        hold_days=2,
        top_k=2,
        market_signal_frame=market_signals,
    )

    assert report["summary"]["gate_today"]["pass"] is False
    assert report["latest_candidates"]
    for candidate in report["latest_candidates"]:
        assert candidate["candidate_tier"] == "observe"
        assert candidate["position_pct"] == 0.0
        assert candidate["live_readiness_status"] == "avoid"
        assert "market_gate_blocked" in candidate["live_warning_keys"]


def test_latest_candidates_keep_research_pool_larger_than_trade_top_k():
    rows = []
    for idx in range(10):
        rows.append(
            {
                "trade_date": "2026-07-09",
                "ts_code": f"00000{idx}",
                "relay_score": 100 - idx * 3,
                "ret_5": 0.03,
                "volume_ratio_20": 1.5,
                "near_high_20": 0.95,
                "limit_count_20": 1,
                "risk_flags": "",
                "available_at": "2026-07-09 15:00:00",
            }
        )
    features = pd.DataFrame(rows)

    candidates = relay.build_latest_candidates(features, top_k=2, candidate_pool_size=6)

    assert len(candidates) == 6
    assert candidates[0]["candidate_rank"] == 1
    assert candidates[0]["candidate_tier"] == "strong"
    assert candidates[0]["score_percentile"] == 1.0
    assert candidates[-1]["candidate_pool_size"] == 6
    assert candidates[-1]["latest_universe_size"] == 10


def test_latest_candidates_make_unknown_st_status_non_executable():
    features = relay.build_feature_frame(_panel())

    candidates = relay.build_latest_candidates(features, top_k=1, candidate_pool_size=1)

    assert candidates
    assert candidates[0]["st_status_known"] is False
    assert candidates[0]["candidate_tier"] == "observe"
    assert candidates[0]["live_readiness_status"] == "avoid"
    assert candidates[0]["position_pct"] == 0.0
    assert "st_status_unknown" in candidates[0]["live_warning_keys"]


def test_label_frame_defers_suspension_then_locked_limit_down_exit():
    dates = pd.bdate_range("2026-07-01", periods=5)
    close = pd.DataFrame(
        {"000001.SZ": [10.0, 10.0, None, 9.05, 8.9]},
        index=dates,
    )
    panel = {
        "open": pd.DataFrame(
            {"000001.SZ": [10.0, 10.0, None, 9.05, 8.8]},
            index=dates,
        ),
        "high": pd.DataFrame(
            {"000001.SZ": [10.1, 10.2, None, 9.05, 9.0]},
            index=dates,
        ),
        "low": pd.DataFrame(
            {"000001.SZ": [9.9, 9.8, None, 9.05, 8.7]},
            index=dates,
        ),
        "close": close,
        "volume": pd.DataFrame(
            {"000001.SZ": [1000.0, 1000.0, None, 1000.0, 1000.0]},
            index=dates,
        ),
        "amount": close * 1000.0,
        "amount_real": pd.DataFrame(
            True,
            index=dates,
            columns=["000001.SZ"],
        ),
        "is_st": pd.DataFrame(False, index=dates, columns=["000001.SZ"]),
        "st_status_known": pd.DataFrame(
            True,
            index=dates,
            columns=["000001.SZ"],
        ),
    }

    labels = relay.build_label_frame(panel, hold_days=1)
    first = labels[
        labels["trade_date"] == dates[0].strftime("%Y-%m-%d")
    ].iloc[0]

    assert first["exit_date"] == dates[4].strftime("%Y-%m-%d")
    assert first["exit_reason"] == "deferred_limit_down_exit"
    assert first["exit_price"] == 8.8
    assert first["plan_return_pct"] == pytest.approx(-12.0)


def test_latest_candidates_never_assign_a_position_to_known_st_stock():
    features = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-09",
                "ts_code": "000001.SZ",
                "relay_score": 99.0,
                "ret_3": 0.03,
                "ret_5": 0.05,
                "volatility_10": 0.02,
                "volume_ratio_20": 1.8,
                "near_high_20": 0.96,
                "limit_count_20": 1,
                "risk_flags": "",
                "is_st": True,
                "st_status_known": True,
                "available_at": "2026-07-09 15:00:00",
            }
        ]
    )

    candidates = relay.build_latest_candidates(features, top_k=1, candidate_pool_size=1)

    assert candidates[0]["candidate_tier"] == "observe"
    assert candidates[0]["live_readiness_status"] == "avoid"
    assert candidates[0]["position_pct"] == 0.0
    assert "known_st" in candidates[0]["live_warning_keys"]


def test_latest_candidates_resolve_stock_name(monkeypatch):
    from analytics import data_manager as dm

    monkeypatch.setattr(dm, "get_stock_name", lambda code: "平安银行")
    features = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-13",
                "ts_code": "000001.SZ",
                "relay_score": 91,
                "ret_5": 0.06,
                "volume_ratio_20": 1.9,
                "near_high_20": 0.98,
                "limit_count_20": 2,
                "risk_flags": "",
                "available_at": "2026-07-13 15:00:00",
            }
        ]
    )

    candidate = relay.build_latest_candidates(features, top_k=1, candidate_pool_size=1)[0]

    assert candidate["name"] == "平安银行"


def test_latest_candidates_include_human_readable_factor_explanation():
    features = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-09",
                "ts_code": "AAA",
                "relay_score": 91,
                "ret_5": 0.06,
                "ret_10": 0.11,
                "ret_20": 0.18,
                "volume_ratio_20": 1.9,
                "amount_ratio_20": 2.1,
                "near_high_20": 0.98,
                "limit_count_20": 2,
                "volatility_10": 0.04,
                "ret_3": 0.08,
                "risk_flags": "",
                "market_emotion_score": 72,
                "available_at": "2026-07-09 15:00:00",
            }
        ]
    )

    candidate = relay.build_latest_candidates(features, top_k=1, candidate_pool_size=1)[0]
    explanation = candidate["factor_explain"]

    assert explanation["factor_version"] == "relay_factor_mining_v4_fixed_ridge"
    assert "二三板接力" in explanation["summary"]
    assert explanation["matched_factors"]
    assert explanation["risk_controls"]
    assert explanation["trading_plan"]["hold_days"] == 2
    assert explanation["update_policy"]["signal_refresh"] == "每个交易日收盘后刷新候选池"
    assert explanation["feature_snapshot"]["volume_ratio_20"] == 1.9


def test_latest_candidate_with_proxy_amount_is_observe_only():
    features = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-09",
                "ts_code": "AAA",
                "relay_score": 95.0,
                "ret_5": 0.08,
                "ret_10": 0.12,
                "ret_20": 0.18,
                "volume_ratio_20": 1.8,
                "amount_ratio_20": 2.5,
                "amount_data_real": False,
                "near_high_20": 0.98,
                "limit_count_20": 2,
                "volatility_10": 0.03,
                "ret_3": 0.05,
                "risk_flags": "",
            }
        ]
    )

    candidate = relay.build_latest_candidates(features, top_k=1)[0]

    assert candidate["candidate_tier"] == "observe"
    assert candidate["position_pct"] == 0.0
    assert "real_amount_unavailable" in candidate["live_warning_keys"]


def test_latest_candidates_downgrade_overheated_live_unreliable_names():
    features = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-09",
                "ts_code": "AAA",
                "relay_score": 96,
                "ret_3": 0.22,
                "ret_5": 0.18,
                "volume_ratio_20": 0.7,
                "near_high_20": 0.999,
                "limit_count_20": 3,
                "volatility_10": 0.09,
                "risk_flags": "overextended_3d,high_volatility,weak_volume",
                "market_emotion_score": 45,
                "available_at": "2026-07-09 15:00:00",
            }
        ]
    )

    candidate = relay.build_latest_candidates(features, top_k=1, candidate_pool_size=1)[0]

    assert candidate["candidate_tier"] == "observe"
    assert candidate["position_pct"] == 0.0
    assert candidate["live_readiness_status"] == "avoid"
    assert candidate["live_warnings"]
    assert candidate["factor_explain"]["live_reliability"]["status"] == "avoid"


def test_latest_candidates_downgrade_from_live_feedback():
    features = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-09",
                "ts_code": "AAA.SZ",
                "relay_score": 95,
                "ret_3": 0.04,
                "ret_5": 0.06,
                "volume_ratio_20": 2.0,
                "near_high_20": 0.97,
                "limit_count_20": 1,
                "volatility_10": 0.03,
                "risk_flags": "",
                "market_emotion_score": 72,
                "available_at": "2026-07-09 15:00:00",
            }
        ]
    )

    candidate = relay.build_latest_candidates(
        features,
        top_k=1,
        candidate_pool_size=1,
        live_feedback={
            "AAA": {
                "blocked_rate": 0.75,
                "execution_rate": 0.20,
                "near_limit_rate": 0.50,
                "buy_vote_count": 5,
                "paper_trade_count": 4,
                "paper_avg_pnl_pct": -2.4,
                "paper_win_rate": 0.25,
                "sample_count": 9,
                "last_feedback_at": "2026-07-09 09:35:00",
            }
        },
    )[0]

    assert candidate["candidate_tier"] == "observe"
    assert candidate["position_pct"] == 0.0
    assert candidate["live_readiness_status"] == "avoid"
    assert "feedback_frequent_blocked" in candidate["live_warning_keys"]
    assert candidate["factor_explain"]["live_reliability"]["feedback"]["blocked_rate"] == 0.75


def test_latest_candidates_apply_visible_filters_and_custom_tiers():
    features = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-09",
                "ts_code": "AAA",
                "relay_score": 90,
                "ret_5": 0.05,
                "volume_ratio_20": 2.0,
                "near_high_20": 0.98,
                "limit_count_20": 2,
                "risk_flags": "",
                "available_at": "2026-07-09 15:00:00",
            },
            {
                "trade_date": "2026-07-09",
                "ts_code": "BBB",
                "relay_score": 80,
                "ret_5": 0.02,
                "volume_ratio_20": 0.8,
                "near_high_20": 0.90,
                "limit_count_20": 0,
                "risk_flags": "weak_volume",
                "available_at": "2026-07-09 15:00:00",
            },
        ]
    )

    candidates = relay.build_latest_candidates(
        features,
        top_k=2,
        candidate_pool_size=2,
        mining_rules={
            "tier_thresholds": {"strong_percentile": 0.9, "watch_percentile": 0.7},
            "filters": {"min_volume_ratio_20": 1.0, "exclude_high_risk": True},
        },
    )

    assert [c["ts_code"] for c in candidates] == ["AAA"]
    assert candidates[0]["candidate_tier"] == "strong"
    assert candidates[0]["active_filters"]["min_volume_ratio_20"] == 1.0


def test_latest_candidates_use_frozen_model_score_instead_of_legacy_score():
    features = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-29",
                "ts_code": "AAA",
                "relay_score": 99.0,
                "ret_5": 0.01,
                "volume_ratio_20": 1.5,
                "near_high_20": 0.95,
                "limit_count_20": 1,
                "risk_flags": "",
            },
            {
                "trade_date": "2026-07-29",
                "ts_code": "BBB",
                "relay_score": 60.0,
                "ret_5": 0.20,
                "volume_ratio_20": 1.5,
                "near_high_20": 0.95,
                "limit_count_20": 1,
                "risk_flags": "",
            },
        ]
    )
    model = {
        "model_version": "relay_factor_mining_v2_ridge",
        "fallback": False,
        "selected_factors": ["ret_5"],
        "coefficients": {"ret_5": 2.0},
        "normalized_weights": {"ret_5": 1.0},
        "feature_means": {"ret_5": 0.5},
        "feature_scales": {"ret_5": 0.25},
    }

    candidates = relay.build_latest_candidates(
        features,
        top_k=2,
        candidate_pool_size=2,
        factor_model=model,
    )

    assert [candidate["ts_code"] for candidate in candidates] == ["BBB", "AAA"]
    assert candidates[0]["relay_score"] == 60.0
    assert candidates[0]["factor_model_score"] > candidates[1]["factor_model_score"]
    assert candidates[0]["model_version"] == "relay_factor_mining_v2_ridge"
    assert candidates[0]["strategy_key"] == "relay_factor_mining_v2"


def test_latest_candidates_are_non_executable_when_validation_rejects_model():
    features = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-29",
                "ts_code": "AAA",
                "relay_score": 99.0,
                "ret_5": 0.20,
                "volume_ratio_20": 1.5,
                "near_high_20": 0.95,
                "limit_count_20": 1,
                "risk_flags": "",
            }
        ]
    )
    model = {
        "model_version": "relay_factor_mining_v2_ridge",
        "fallback": False,
        "validation_accepted": False,
        "validation_rejection_reason": "validation_rank_ic_below_minimum",
        "selected_factors": ["ret_5"],
        "coefficients": {"ret_5": 2.0},
        "normalized_weights": {"ret_5": 1.0},
        "feature_means": {"ret_5": 0.5},
        "feature_scales": {"ret_5": 0.25},
    }

    candidate = relay.build_latest_candidates(
        features,
        top_k=1,
        candidate_pool_size=1,
        factor_model=model,
    )[0]

    assert candidate["candidate_tier"] == "observe"
    assert candidate["position_pct"] == 0.0
    assert candidate["live_readiness_status"] == "avoid"
    assert candidate["validation_accepted"] is False
    assert "model_validation_rejected" in candidate["live_warning_keys"]


def test_latest_candidates_become_executable_only_after_research_validation():
    features = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-29",
                "ts_code": "AAA",
                "relay_score": 99.0,
                "ret_5": 0.20,
                "volume_ratio_20": 1.5,
                "near_high_20": 0.95,
                "limit_count_20": 1,
                "risk_flags": "",
            }
        ]
    )
    model = {
        "model_version": "relay_factor_mining_v3_nested_ridge",
        "fallback": False,
        "validation_accepted": True,
        "validation_stable": True,
        "selected_factors": ["ret_5"],
        "coefficients": {"ret_5": 2.0},
        "normalized_weights": {"ret_5": 1.0},
        "feature_means": {"ret_5": 0.5},
        "feature_scales": {"ret_5": 0.25},
    }
    assessment = {
        "research_status": "validated",
        "deployment_eligible": True,
        "research_only": False,
    }

    candidate = relay.build_latest_candidates(
        features,
        top_k=1,
        candidate_pool_size=1,
        factor_model=model,
        research_assessment=assessment,
    )[0]

    assert candidate["validation_accepted"] is True
    assert candidate["research_status"] == "validated"
    assert candidate["deployment_eligible"] is True
    assert candidate["research_only"] is False
    assert candidate["position_pct"] > 0.0


def test_frozen_shadow_snapshot_scores_without_refitting_and_never_becomes_executable(monkeypatch):
    panel = _panel(days=55)
    panel["amount_real"] = pd.DataFrame(
        True,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    panel["is_st"] = pd.DataFrame(
        False,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    panel["st_status_known"] = pd.DataFrame(
        True,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    manifest = {
        "experiment_id": "exp-locked-1",
        "model_instance_id": "model-frozen-1",
        "source_run_id": "run-locked-1",
        "protocol_binding": _current_protocol_binding(),
        "model": {
            "model_version": "relay_factor_mining_v3_nested_ridge",
            "fallback": False,
            "validation_accepted": True,
            "validation_stable": True,
            "model_spec_protocol_version": "relay_fixed_ridge_preregistered_v1",
            "model_spec_calibrated_through": "2026-07-30",
            "selected_factors": ["ret_5"],
            "coefficients": {"ret_5": 2.0},
            "normalized_weights": {"ret_5": 1.0},
            "feature_means": {"ret_5": 0.5},
            "feature_scales": {"ret_5": 0.25},
            "selected_market_gate": {"enabled": False},
        },
        "config": {
            "hold_days": 2,
            "stop_loss": -0.035,
            "take_profit": 0.07,
            "top_k": 3,
            "candidate_pool_size": 3,
            "include_market_signals": False,
        },
        "mining_rules": relay.normalize_mining_rules(None),
    }
    monkeypatch.setattr(
        relay,
        "fit_factor_model",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("frozen scoring must not refit")),
    )
    monkeypatch.setattr(relay, "_resolve_stock_name", lambda code: str(code))

    snapshot = relay.build_frozen_shadow_snapshot(panel, manifest)

    assert snapshot["ok"] is True
    assert snapshot["data_status"] == "ok"
    assert snapshot["signal_date"] == panel["close"].index[-1].strftime("%Y-%m-%d")
    assert snapshot["candidates"]
    assert any(candidate["shadow_target_position_pct"] > 0.0 for candidate in snapshot["candidates"])
    for candidate in snapshot["candidates"]:
        assert candidate["experiment_id"] == "exp-locked-1"
        assert candidate["model_instance_id"] == "model-frozen-1"
        assert candidate["source_run_id"] == "run-locked-1"
        assert candidate["research_status"] == "locked_shadow"
        assert candidate["deployment_eligible"] is False
        assert candidate["research_only"] is True
        assert candidate["position_pct"] == 0.0
    expected_control_codes = relay._deterministic_benchmark_codes(
        list(panel["close"].columns),
        trade_date=snapshot["signal_date"],
        top_k=3,
    )
    assert snapshot["control_policy_version"] == (
        relay.BENCHMARK_POLICY_VERSION
    )
    assert [
        candidate["ts_code"]
        for candidate in snapshot["control_candidates"]
    ] == expected_control_codes
    assert all(
        candidate["prospective_track"] == "control"
        and candidate["shadow_target_position_pct"]
        == pytest.approx(1.0 / 3.0)
        and candidate["position_pct"] == 0.0
        for candidate in snapshot["control_candidates"]
    )


def test_frozen_shadow_snapshot_records_market_gate_as_zero_exposure(monkeypatch):
    panel = _panel(days=55)
    panel["amount_real"] = pd.DataFrame(
        True,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    panel["is_st"] = pd.DataFrame(
        False,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    panel["st_status_known"] = pd.DataFrame(
        True,
        index=panel["close"].index,
        columns=panel["close"].columns,
    )
    market_signals = pd.DataFrame(
        {
            "trade_date": panel["close"].index.strftime("%Y-%m-%d"),
            "market_data_trusted": True,
            "market_data_status": "ok",
            "market_data_reason": "",
            "market_emotion_score": 90.0,
        }
    )
    manifest = {
        "experiment_id": "exp-gated",
        "model_instance_id": "model-gated",
        "source_run_id": "run-gated",
        "protocol_binding": _current_protocol_binding(),
        "model": {
            "model_version": "relay_factor_mining_v3_nested_ridge",
            "fallback": False,
            "validation_accepted": True,
            "validation_stable": True,
            "model_spec_protocol_version": "relay_fixed_ridge_preregistered_v1",
            "model_spec_calibrated_through": "2026-07-30",
            "selected_factors": ["ret_5"],
            "coefficients": {"ret_5": 2.0},
            "normalized_weights": {"ret_5": 1.0},
            "feature_means": {"ret_5": 0.5},
            "feature_scales": {"ret_5": 0.25},
            "selected_market_gate": {
                "enabled": True,
                "emo_min": 20.0,
                "emo_max": 70.0,
                "require_cooling": True,
            },
        },
        "config": {
            "hold_days": 2,
            "stop_loss": -0.035,
            "take_profit": 0.07,
            "top_k": 3,
            "candidate_pool_size": 3,
            "include_market_signals": True,
        },
        "mining_rules": relay.normalize_mining_rules(None),
    }
    monkeypatch.setattr(relay, "_resolve_stock_name", lambda code: str(code))

    snapshot = relay.build_frozen_shadow_snapshot(
        panel,
        manifest,
        market_signal_frame=market_signals,
    )

    assert snapshot["ok"] is True
    assert snapshot["market_gate"]["pass"] is False
    assert snapshot["candidates"]
    assert all(candidate["gate_pass"] is False for candidate in snapshot["candidates"])
    assert all(candidate["shadow_target_position_pct"] == 0.0 for candidate in snapshot["candidates"])
    assert all(candidate["position_pct"] == 0.0 for candidate in snapshot["candidates"])


def test_frozen_shadow_candidate_waits_for_real_exit_data():
    dates = pd.bdate_range("2026-07-01", periods=2)
    close = pd.DataFrame({"000001.SZ": [10.0, 10.2]}, index=dates)
    panel = {
        "open": pd.DataFrame({"000001.SZ": [10.0, 10.1]}, index=dates),
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": pd.DataFrame({"000001.SZ": [1000.0, 1000.0]}, index=dates),
        "is_st": pd.DataFrame(False, index=dates, columns=["000001.SZ"]),
        "st_status_known": pd.DataFrame(True, index=dates, columns=["000001.SZ"]),
    }
    candidate = {
        "ts_code": "000001.SZ",
        "trade_date": "2026-07-01",
        "gate_pass": True,
        "shadow_target_position_pct": 0.1,
        "factor_explain": {"feature_snapshot": {"amount_ratio_20": 1.2}},
    }
    manifest = {
        "protocol_binding": _current_protocol_binding(),
        "config": {
            "hold_days": 2,
            "stop_loss": -0.035,
            "take_profit": 0.07,
        }
    }

    settlement = relay.settle_frozen_shadow_candidate(panel, candidate, manifest)

    assert settlement["status"] == "pending"
    assert settlement["reason"] == "awaiting_exit_data"


def test_frozen_shadow_candidate_settles_zero_volume_entry_as_blocked():
    dates = pd.bdate_range("2026-07-01", periods=4)
    close = pd.DataFrame(
        {"000001.SZ": [10.0, 10.0, 10.2, 10.3]},
        index=dates,
    )
    panel = {
        "open": close.copy(),
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": pd.DataFrame(
            {"000001.SZ": [1000.0, 0.0, 1200.0, 1300.0]},
            index=dates,
        ),
        "is_st": pd.DataFrame(False, index=dates, columns=["000001.SZ"]),
        "st_status_known": pd.DataFrame(
            True,
            index=dates,
            columns=["000001.SZ"],
        ),
    }
    candidate = {
        "ts_code": "000001.SZ",
        "trade_date": "2026-07-01",
        "gate_pass": True,
        "shadow_target_position_pct": 0.1,
        "factor_explain": {"feature_snapshot": {"amount_ratio_20": 1.2}},
    }
    manifest = {
        "protocol_binding": _current_protocol_binding(),
        "config": {
            "hold_days": 2,
            "stop_loss": -0.035,
            "take_profit": 0.07,
        }
    }

    settlement = relay.settle_frozen_shadow_candidate(panel, candidate, manifest)

    assert settlement["status"] == "settled"
    assert settlement["execution_blocked"] is True
    assert settlement["entry_date"] == "2026-07-02"
    assert settlement["block_reasons"] == ["entry_suspended_or_zero_volume"]
    assert settlement["exit_reason"] == "entry_suspended_or_zero_volume"
    assert settlement["realized_return_pct"] == 0.0


@pytest.mark.parametrize(
    ("st_status_known", "is_st", "expected_reason"),
    [
        (False, False, "st_status_unknown"),
        (True, True, "known_st_excluded"),
    ],
)
def test_frozen_shadow_candidate_blocks_untrusted_or_known_st_entry(
    st_status_known,
    is_st,
    expected_reason,
):
    dates = pd.bdate_range("2026-07-01", periods=4)
    close = pd.DataFrame(
        {"000001.SZ": [10.0, 10.0, 10.2, 10.3]},
        index=dates,
    )
    panel = {
        "open": close.copy(),
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": pd.DataFrame(
            1000.0,
            index=dates,
            columns=["000001.SZ"],
        ),
        "is_st": pd.DataFrame(
            is_st,
            index=dates,
            columns=["000001.SZ"],
        ),
        "st_status_known": pd.DataFrame(
            st_status_known,
            index=dates,
            columns=["000001.SZ"],
        ),
    }
    candidate = {
        "ts_code": "000001.SZ",
        "trade_date": "2026-07-01",
        "gate_pass": True,
        "shadow_target_position_pct": 0.1,
        "factor_explain": {"feature_snapshot": {"amount_ratio_20": 1.2}},
    }
    manifest = {
        "protocol_binding": _current_protocol_binding(),
        "config": {
            "hold_days": 1,
            "stop_loss": -0.035,
            "take_profit": 0.07,
        },
    }

    settlement = relay.settle_frozen_shadow_candidate(
        panel,
        candidate,
        manifest,
    )

    assert settlement["status"] == "settled"
    assert settlement["execution_blocked"] is True
    assert settlement["block_reasons"] == [expected_reason]
    assert settlement["exit_reason"] == expected_reason
    assert settlement["realized_return_pct"] == 0.0
    assert settlement["valuation_path"] == []


def test_frozen_shadow_candidate_settles_from_real_trade_path():
    dates = pd.bdate_range("2026-07-01", periods=4)
    close = pd.DataFrame(
        {"000001.SZ": [10.0, 10.1, 10.7, 10.8]},
        index=dates,
    )
    panel = {
        "open": pd.DataFrame(
            {"000001.SZ": [10.0, 10.1, 10.2, 10.75]},
            index=dates,
        ),
        "high": pd.DataFrame(
            {"000001.SZ": [10.1, 10.3, 11.0, 10.9]},
            index=dates,
        ),
        "low": pd.DataFrame(
            {"000001.SZ": [9.9, 10.0, 10.1, 10.6]},
            index=dates,
        ),
        "close": close,
        "volume": pd.DataFrame(
            {"000001.SZ": [1000.0, 1000.0, 1000.0, 1000.0]},
            index=dates,
        ),
        "is_st": pd.DataFrame(False, index=dates, columns=["000001.SZ"]),
        "st_status_known": pd.DataFrame(True, index=dates, columns=["000001.SZ"]),
    }
    candidate = {
        "ts_code": "000001.SZ",
        "trade_date": "2026-07-01",
        "gate_pass": True,
        "shadow_target_position_pct": 0.1,
        "factor_explain": {"feature_snapshot": {"amount_ratio_20": 1.2}},
    }
    manifest = {
        "protocol_binding": _current_protocol_binding(),
        "config": {
            "hold_days": 2,
            "stop_loss": -0.035,
            "take_profit": 0.07,
        }
    }

    settlement = relay.settle_frozen_shadow_candidate(panel, candidate, manifest)

    assert settlement["status"] == "settled"
    assert settlement["execution_blocked"] is False
    assert settlement["entry_date"] == "2026-07-02"
    assert settlement["exit_date"] == "2026-07-03"
    assert settlement["exit_reason"] == "take_profit"
    assert settlement["realized_return_pct"] == 6.64
    assert settlement["valuation_path"]


def test_frozen_shadow_candidate_uses_manifest_execution_costs():
    dates = pd.bdate_range("2026-07-01", periods=4)
    close = pd.DataFrame(
        {"000001.SZ": [10.0, 10.1, 10.7, 10.8]},
        index=dates,
    )
    panel = {
        "open": pd.DataFrame(
            {"000001.SZ": [10.0, 10.1, 10.2, 10.75]},
            index=dates,
        ),
        "high": pd.DataFrame(
            {"000001.SZ": [10.1, 10.3, 11.0, 10.9]},
            index=dates,
        ),
        "low": pd.DataFrame(
            {"000001.SZ": [9.9, 10.0, 10.1, 10.6]},
            index=dates,
        ),
        "close": close,
        "volume": pd.DataFrame(
            {"000001.SZ": [1000.0, 1000.0, 1000.0, 1000.0]},
            index=dates,
        ),
        "is_st": pd.DataFrame(False, index=dates, columns=["000001.SZ"]),
        "st_status_known": pd.DataFrame(True, index=dates, columns=["000001.SZ"]),
    }
    candidate = {
        "ts_code": "000001.SZ",
        "trade_date": "2026-07-01",
        "gate_pass": True,
        "shadow_target_position_pct": 0.1,
        "factor_explain": {"feature_snapshot": {"amount_ratio_20": 1.2}},
    }
    manifest = {
        "protocol_binding": _current_protocol_binding(),
        "config": {
            "hold_days": 2,
            "stop_loss": -0.035,
            "take_profit": 0.07,
        },
        "execution_rules": {
            **relay.LIVE_EXECUTION_RULES,
            "slippage_pct": 1.20,
        },
    }

    settlement = relay.settle_frozen_shadow_candidate(panel, candidate, manifest)

    assert settlement["status"] == "settled"
    assert settlement["realized_return_pct"] == 5.64


def test_frozen_shadow_snapshot_rejects_forged_protocol_fingerprint():
    manifest = {
        "protocol_binding": _current_protocol_binding(),
        "model": {
            "fallback": False,
            "validation_accepted": True,
            "validation_stable": True,
            "selected_factors": ["ret_5"],
        },
    }
    first_path = next(iter(manifest["protocol_binding"]["source_sha256"]))
    manifest["protocol_binding"]["source_sha256"][first_path] = "0" * 64

    snapshot = relay.build_frozen_shadow_snapshot(_panel(days=55), manifest)

    assert snapshot["ok"] is False
    assert snapshot["reason"] == "frozen_protocol_fingerprint_mismatch"
    assert snapshot["candidates"] == []


def test_frozen_shadow_settlement_rejects_forged_protocol_fingerprint():
    manifest = {"protocol_binding": _current_protocol_binding()}
    first_path = next(iter(manifest["protocol_binding"]["source_sha256"]))
    manifest["protocol_binding"]["source_sha256"][first_path] = "f" * 64

    settlement = relay.settle_frozen_shadow_candidate(
        {},
        {
            "gate_pass": False,
            "shadow_target_position_pct": 0.0,
        },
        manifest,
    )

    assert settlement == {
        "status": "rejected",
        "reason": "frozen_protocol_fingerprint_mismatch",
    }


def test_run_research_reports_candidate_pool_summary(tmp_path):
    result = relay.run_relay_research(
        panel=_panel(days=55),
        out_dir=tmp_path,
        train_days=25,
        test_days=10,
        hold_days=2,
        top_k=1,
        candidate_pool_size=3,
    )

    assert result["config"]["candidate_pool_size"] == 3
    assert result["mining_rules"]["weights"]["ret_5"] == relay.DEFAULT_MINING_RULES["weights"]["ret_5"]
    assert result["condition_explain"]
    assert result["summary"]["latest_candidate_count"] == 3
    assert sum(result["summary"]["latest_candidate_tier_counts"].values()) == 3
    assert result["data_status"] == result["status"]
    assert result["research_status"] == "failed"
    assert result["research_assessment"]["deployment_eligible"] is False
    assert all(candidate["candidate_tier"] == "observe" for candidate in result["latest_candidates"])
    assert all(candidate["position_pct"] == 0.0 for candidate in result["latest_candidates"])


def test_latest_candidates_apply_frozen_fallback_direction():
    features = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-29",
                "ts_code": "AAA",
                "relay_score": 10.0,
                "risk_flags": "",
            },
            {
                "trade_date": "2026-07-29",
                "ts_code": "BBB",
                "relay_score": 90.0,
                "risk_flags": "",
            },
        ]
    )
    fallback_model = {
        "model_version": "relay_factor_mining_v2_ridge",
        "fallback": True,
        "fallback_reason": "test",
        "fallback_direction": -1,
        "selected_factors": [],
    }

    candidates = relay.build_latest_candidates(
        features,
        top_k=1,
        candidate_pool_size=2,
        factor_model=fallback_model,
    )

    assert [candidate["ts_code"] for candidate in candidates] == ["AAA", "BBB"]


def test_run_research_uses_same_trade_plan_for_labels_candidates_and_report(tmp_path):
    result = relay.run_relay_research(
        panel=_panel(days=55),
        out_dir=tmp_path,
        train_days=25,
        test_days=10,
        hold_days=2,
        stop_loss=-0.04,
        take_profit=0.08,
        top_k=1,
        candidate_pool_size=1,
    )

    assert result["config"]["stop_loss"] == -0.04
    assert result["config"]["take_profit"] == 0.08
    assert result["latest_candidates"][0]["stop_loss"] == -0.04
    assert result["latest_candidates"][0]["take_profit"] == 0.08
    labels = pd.read_csv(tmp_path / "labels.csv")
    assert set(labels["path_policy"]) == {"t_plus_1_stop_first_on_same_bar"}


def test_run_research_degrades_when_samples_are_insufficient(tmp_path):
    result = relay.run_relay_research(panel=_panel(days=12), out_dir=tmp_path, train_days=20, test_days=5)

    assert result["status"] == "degraded"
    assert result["summary"]["window_count"] == 0
    assert "insufficient" in result["reason"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("take_profit", True),
        ("candidate_pool_size", True),
        ("include_market_signals", 1),
    ],
)
def test_run_research_rejects_boolean_or_coerced_config_before_data_access(
    tmp_path,
    field,
    value,
):
    kwargs = {field: value}

    with pytest.raises(ValueError, match=field):
        relay.run_relay_research(
            panel={},
            out_dir=tmp_path,
            **kwargs,
        )


def _eligible_registration(
    *,
    n_stocks: int = 800,
    top_k: int = 5,
    hold_days: int = 2,
    test_days: int = 2,
) -> dict:
    source = relay.load_model_preregistration()
    assert source["ok"] is True
    model = {
        "model_version": relay.MODEL_VERSION,
        "fallback": False,
        "fallback_reason": None,
        "selected_factors": ["ret_5"],
        "coefficients": {"ret_5": 1.0},
        "normalized_weights": {"ret_5": 1.0},
        "feature_means": {"ret_5": 0.5},
        "feature_scales": {"ret_5": 0.25},
        "target_col": "model_target_pct",
        "ridge_alpha": 5.0,
        "strategy_key": relay.STRATEGY_KEY,
        "training_scope": "preregistered_calibration_only",
        "selection_source": "test_preregistered_executable_model",
        "selected_parameters": dict(relay.FIXED_MODEL_PARAMETERS),
        "model_spec_protocol_version": relay.MODEL_SPEC_PROTOCOL_VERSION,
        "model_spec_calibrated_through": (
            relay.MODEL_SPEC_CALIBRATED_THROUGH
        ),
        "validation_accepted": True,
        "validation_rejection_reason": None,
        "validation_stable": True,
        "validation_stability_reason": "ok",
        "selected_market_gate": {"enabled": False},
        "fit_start": "2026-01-01",
        "fit_end": "2026-06-01",
        "fit_sample_count": 1000,
        "fit_label_max_exit": "2026-06-02",
        "validation_start": "2026-06-03",
        "validation_end": "2026-06-30",
        "validation_sample_count": 200,
        "refit_start": "2026-01-01",
        "refit_end": relay.MODEL_SPEC_CALIBRATED_THROUGH,
        "refit_sample_count": 1200,
        "refit_label_max_exit": relay.MODEL_SPEC_CALIBRATED_THROUGH,
        "refit_policy": "fixed_registered_parameters_on_calibration_rows_only",
        "frozen_model_candidate_eligible": True,
        "frozen_model_candidate_reason": "ok",
    }
    model_sha256 = relay.factor_model_digest(model)
    record = json.loads(json.dumps(source["record"]))
    record["executable_model"] = model
    record["executable_model_sha256"] = model_sha256
    record["research_protocol"] = _locking_protocol_fixture(
        n_stocks=n_stocks,
        top_k=top_k,
        hold_days=hold_days,
        test_days=test_days,
    )
    record["research_validation_rules"].update(
        {
            "evaluation_window_count": 12,
            "window_sessions": test_days,
            "oos_session_count": 12 * test_days,
            "stop_rule": "first_fixed_prefix_only",
            "minimum_diagnostic_sample_count_per_session": 20,
            "minimum_diagnostic_window_acceptance_rate": 0.50,
            "minimum_oos_mean_rank_ic": 0.02,
            "minimum_positive_rank_ic_session_rate": 0.55,
            "minimum_oos_mean_spread_pct": 0.20,
            "minimum_positive_spread_session_rate": 0.55,
            "minimum_excess_return_pct": 0.0,
            "minimum_excess_bootstrap_lower_return_pct": 0.0,
        }
    )
    record["research_validation_rules"].pop(
        "minimum_validation_acceptance_rate",
        None,
    )
    record["prospective_acceptance_rules"] = {
        "protocol_version": (
            "prospective_60_session_paired_control_v3"
        ),
        "required_sessions": 60,
        "minimum_total_return_pct": 2.0,
        "minimum_excess_return_pct": 0.0,
        "maximum_drawdown_pct": -10.0,
        "minimum_executed_trades": 60,
        "minimum_bootstrap_lower_return_pct": 0.0,
        "minimum_excess_bootstrap_lower_return_pct": 0.0,
        "maximum_protocol_violations": 0,
        "preregistered_at": relay.MODEL_SPEC_REGISTERED_AT,
        "calibrated_through": relay.MODEL_SPEC_CALIBRATED_THROUGH,
    }
    return {
        "ok": True,
        "reason": "ok",
        "record": record,
        "sha256": "1" * 64,
        "model_sha256": model_sha256,
        "current_protocol": True,
    }


def _locking_protocol_fixture(
    *,
    n_stocks: int = 800,
    top_k: int = 5,
    hold_days: int = 2,
    test_days: int = 2,
) -> dict:
    return {
        "protocol_version": "relay_locking_protocol_v2",
        "config": {
            "n_stocks": n_stocks,
            "days": 180,
            "train_days": 60,
            "test_days": test_days,
            "step_days": test_days,
            "hold_days": hold_days,
            "stop_loss": -0.035,
            "take_profit": 0.07,
            "top_k": top_k,
            "candidate_pool_size": 100,
            "include_market_signals": True,
            "model_version": relay.MODEL_VERSION,
            "universe_selection": (
                "point_in_time_eligible_stable_sha256_sample"
            ),
            "data_quality_policy": {
                "discontinuous_price_history": (
                    "quarantine_from_first_detectable_break"
                ),
                "max_abs_daily_return": relay.MAX_ABS_DAILY_RETURN,
                "amount_without_real_cache": (
                    "proxy_for_legacy_rules_excluded_from_v2_model"
                ),
                "minimum_real_amount_coverage": (
                    relay.MIN_REAL_AMOUNT_COVERAGE
                ),
                "st_status_source": (
                    "point_in_time_cache_is_st_then_name"
                ),
                "st_status_unknown": (
                    "display_only_zero_position_and_reject_execution"
                ),
                "universe_eligibility_missing": (
                    "research_data_status_degraded"
                ),
            },
        },
        "mining_rules": relay.normalize_mining_rules(None),
        "execution_rules": relay.normalize_execution_rules(),
        "backtest_policy": {
            "purge": "exit_date_before_test_start",
            "validation": "chronological_tail_20pct",
            "entry": "next_session_open",
            "same_bar_stop_target": "stop_first_conservative",
            "blocked_slots": "remain_cash_without_redistribution",
            "capital": "cash_aware_overlapping_positions",
            "maximum_deferred_exit_sessions": 20,
            "compounding": True,
        },
    }


def _calendar_snapshot_fixture(start_date: str, end_date: str) -> dict:
    closed_2026 = [
        "2026-01-01",
        "2026-01-02",
        "2026-02-16",
        "2026-02-17",
        "2026-02-18",
        "2026-02-19",
        "2026-02-20",
        "2026-02-23",
        "2026-04-06",
        "2026-05-01",
        "2026-05-04",
        "2026-05-05",
        "2026-06-19",
        "2026-09-25",
        "2026-10-01",
        "2026-10-02",
        "2026-10-05",
        "2026-10-06",
        "2026-10-07",
    ]
    open_days = [
        value.strftime("%Y-%m-%d")
        for value in pd.bdate_range(
            "2026-01-01",
            "2026-12-31",
        )
        if value.strftime("%Y-%m-%d") not in closed_2026
    ]
    open_days_sha256 = hashlib.sha256(
        json.dumps(
            open_days,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    material = {
        "snapshot_version": "tushare_trade_calendar_snapshot_v2",
        "source": "tushare_trade_cal",
        "start_date": start_date,
        "end_date": end_date,
        "closed_days": {"2026": closed_2026},
        "provenance": {
            "2026": {
                "provenance_version": (
                    "tushare_trade_cal_provenance_v1"
                ),
                "source": "tushare_trade_cal",
                "provider_mode": "replay",
                "fetched_at": "2026-07-30T16:10:00+08:00",
                "open_days_count": len(open_days),
                "open_days_sha256": open_days_sha256,
            }
        },
    }
    payload = json.dumps(
        material,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **material,
        "snapshot_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _data_quality_assessment_fixture(
    n_stocks: int = 800,
) -> dict:
    return {
        "assessment_version": "relay_data_quality_assessment_v1",
        "amount_data_coverage": 1.0,
        "latest_amount_data_coverage": 1.0,
        "st_status_coverage": 1.0,
        "latest_st_status_coverage": 1.0,
        "universe_eligibility_coverage": 1.0,
        "latest_universe_eligibility_coverage": 1.0,
        "panel_max_abs_close_return": 0.10,
        "panel_stock_count": n_stocks,
        "universe_required_count": n_stocks,
        "status": "ok",
        "failure_reasons": [],
    }


def _assessment_fixture(
    daily_return: float,
    *,
    window_return: float,
    registration: dict | None = None,
    n_stocks: int = 800,
    session_count: int = 24,
) -> dict:
    registration = registration or relay.load_model_preregistration()
    assert registration["ok"] is True
    registered_model = relay._registered_model_from_preregistration(
        registration
    )
    registered_model["calibration_data_match"] = True
    raw_panel, raw_master, raw_signals, raw_calendar = (
        _raw_fingerprint_fixture()
    )
    raw_data_fingerprint = relay.build_raw_data_fingerprint(
        raw_panel,
        security_master=raw_master,
        market_signal_frame=raw_signals,
        calendar_snapshot=raw_calendar,
        source_kind="trusted_cache",
    )
    top_k = 5
    holding_sessions = 3
    window_sessions = 2
    session_dates = [
        value.strftime("%Y-%m-%d")
        for value in pd.bdate_range(
            registration["record"]["oos_not_before"],
            periods=session_count,
        )
    ]
    latest_feature_date = (
        pd.Timestamp(session_dates[-1])
        + pd.offsets.BDay(holding_sessions)
    ).strftime("%Y-%m-%d")
    attempts = []
    benchmark_attempts = []
    session_diagnostics = []
    trades = []
    for session_index, trade_date in enumerate(session_dates):
        signal_date = pd.Timestamp(trade_date)
        entry_date = (signal_date + pd.offsets.BDay(1)).strftime(
            "%Y-%m-%d"
        )
        exit_date = (signal_date + pd.offsets.BDay(2)).strftime(
            "%Y-%m-%d"
        )
        codes = [
            f"TEST{session_index:02d}{rank:02d}"
            for rank in range(top_k)
        ]
        universe_codes = sorted(
            [
                *codes,
                *[
                    f"CONTROL{session_index:02d}{rank:02d}"
                    for rank in range(20)
                ],
            ]
        )
        benchmark_codes = relay._deterministic_benchmark_codes(
            universe_codes,
            trade_date=trade_date,
            top_k=top_k,
        )
        session_diagnostics.append(
            {
                "trade_date": trade_date,
                "universe_codes": universe_codes,
                "universe_codes_sha256": (
                    relay._canonical_codes_sha256(universe_codes)
                ),
                "sample_count": len(universe_codes),
                "rank_ic": 0.05,
                "bottom_quantile_return_pct": -0.5,
                "top_quantile_return_pct": 1.0,
                "top_bottom_spread_pct": 1.5,
                "benchmark_codes": benchmark_codes,
                "benchmark_evaluable": True,
            }
        )
        for code in benchmark_codes:
            benchmark_attempts.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "execution_blocked": False,
                    "forward_return_pct": 0.0,
                    "live_adjusted_return_pct": 0.0,
                    "valuation_path": [
                        {
                            "date": entry_date,
                            "return_pct": 0.0,
                        },
                        {
                            "date": exit_date,
                            "return_pct": 0.0,
                        },
                    ],
                    "exit_reason": "max_holding_period",
                    "gate_pass": True,
                }
            )
        for code in codes:
            attempts.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "execution_blocked": False,
                    "forward_return_pct": float(window_return),
                    "live_adjusted_return_pct": float(window_return),
                    "valuation_path": [
                        {
                            "date": entry_date,
                            "return_pct": round(
                                float(window_return) / 2.0,
                                4,
                            ),
                        },
                        {
                            "date": exit_date,
                            "return_pct": float(window_return),
                        },
                    ],
                    "exit_reason": "take_profit",
                    "gate_pass": True,
                }
            )
        trades.append(
            {
                "trade_date": trade_date,
                "codes": codes,
                "return_pct": float(window_return),
                "live_adjusted_return_pct": float(window_return),
                "research_evaluable": True,
                "gate_pass": True,
                "gate_reason": "gate_disabled",
                "model_sha256": registration["model_sha256"],
            }
        )
    results = relay._build_preregistered_oos_results(
        session_dates=session_dates,
        trades=trades,
        attempts=attempts,
        benchmark_attempts=benchmark_attempts,
        session_diagnostics=session_diagnostics,
        model=registered_model,
        test_days=window_sessions,
        top_k=top_k,
        holding_sessions=holding_sessions,
        validation_rules=registration["record"][
            "research_validation_rules"
        ],
    )
    portfolio = results["portfolio"]
    windows = results["windows"]
    preregistered_oos = relay._seal_preregistered_oos_evidence(
        {
            "evidence_version": (
                relay.PREREGISTERED_OOS_EVIDENCE_VERSION
            ),
            "protocol_version": relay.MODEL_SPEC_PROTOCOL_VERSION,
            "calibrated_through": relay.MODEL_SPEC_CALIBRATED_THROUGH,
            "oos_not_before": registration["record"]["oos_not_before"],
            "registration_sha256": registration["sha256"],
            "protocol_binding": _current_protocol_binding(),
            "model_sha256": registration["model_sha256"],
            "raw_data_fingerprint": raw_data_fingerprint,
            "diagnostic_policy_version": (
                relay.OOS_DIAGNOSTIC_POLICY_VERSION
            ),
            "benchmark_policy_version": (
                relay.BENCHMARK_POLICY_VERSION
            ),
            "window_count": len(windows),
            "window_sessions": window_sessions,
            "top_k": top_k,
            "holding_sessions": holding_sessions,
            "feature_freshness": {
                "policy_version": (
                    "authoritative_completed_session_v2"
                ),
                "clock_source": "system",
                "as_of": (
                    f"{latest_feature_date}T17:10:00+08:00"
                ),
                "latest_feature_date": latest_feature_date,
                "expected_latest_feature_date": (
                    latest_feature_date
                ),
                "maturity_lag_sessions": holding_sessions,
            },
            "calendar_snapshot": _calendar_snapshot_fixture(
                registration["record"]["oos_not_before"],
                max([
                    latest_feature_date,
                    *[
                        str(attempt["exit_date"])
                        for attempt in attempts
                    ],
                ]),
            ),
            "observation_cutoff": session_dates[-1],
            "pending_session_dates": [],
            "data_quality_assessment": (
                _data_quality_assessment_fixture(n_stocks)
            ),
            "locking_protocol": _locking_protocol_fixture(
                n_stocks=n_stocks,
                top_k=top_k,
                hold_days=holding_sessions - 1,
                test_days=window_sessions,
            ),
            "session_dates": session_dates,
            "windows": windows,
            "trades": trades,
            "attempts": attempts,
            "session_diagnostics": session_diagnostics,
            "benchmark_attempts": benchmark_attempts,
            "portfolio": portfolio,
            "gated_portfolio": results["gated_portfolio"],
            "benchmark_portfolio": results[
                "benchmark_portfolio"
            ],
            "gated_benchmark_portfolio": results[
                "gated_benchmark_portfolio"
            ],
        }
    )
    return {
        "windows": windows,
        "gated_portfolio": results["gated_portfolio"],
        "portfolio": portfolio,
        "benchmark_portfolio": results["benchmark_portfolio"],
        "gated_benchmark_portfolio": results[
            "gated_benchmark_portfolio"
        ],
        "registered_model": registered_model,
        "preregistered_oos": preregistered_oos,
        "raw_data_fingerprint": raw_data_fingerprint,
    }


def _lockable_report_fixture(registration: dict) -> dict:
    evidence = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    assessment = relay.assess_research_result(evidence)
    assert assessment["lock_eligible"] is True
    protocol = evidence["preregistered_oos"]["locking_protocol"]
    generated_at = pd.Timestamp(
        evidence["preregistered_oos"]["feature_freshness"]["as_of"]
    ).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "status": "ok",
        "data_status": "ok",
        "generated_at": generated_at,
        "research_status": "exploratory",
        "research_assessment": assessment,
        "model": evidence["registered_model"],
        "config": json.loads(json.dumps(protocol["config"])),
        "mining_rules": json.loads(
            json.dumps(protocol["mining_rules"])
        ),
        "execution_rules": json.loads(
            json.dumps(protocol["execution_rules"])
        ),
        "summary": {
            "backtest_policy": {"entry": "next_session_open"},
            "latest_trade_date": evidence["preregistered_oos"][
                "observation_cutoff"
            ],
            "latest_oos_observation_date": (
                evidence["preregistered_oos"]["observation_cutoff"]
            ),
            **_data_quality_assessment_fixture(),
        },
        "windows": evidence["windows"],
        "portfolio": evidence["portfolio"],
        "gated_portfolio": evidence["gated_portfolio"],
        "preregistered_oos": evidence["preregistered_oos"],
        "raw_data_fingerprint": evidence["raw_data_fingerprint"],
    }


def _build_manifest_for_test(report: dict) -> dict:
    try:
        validation_now = pd.Timestamp(
            report["preregistered_oos"][
                "feature_freshness"
            ]["as_of"]
        ).to_pydatetime()
    except (KeyError, TypeError, ValueError):
        return relay.build_frozen_experiment_manifest(report)
    return relay.build_frozen_experiment_manifest(
        report,
        _validation_now=validation_now,
    )


def _rebuild_and_seal_preregistered_oos(walk_forward: dict) -> None:
    preregistered = walk_forward["preregistered_oos"]
    registration = relay.load_model_preregistration()
    results = relay._build_preregistered_oos_results(
        session_dates=preregistered["session_dates"],
        trades=preregistered["trades"],
        attempts=preregistered["attempts"],
        benchmark_attempts=preregistered["benchmark_attempts"],
        session_diagnostics=preregistered["session_diagnostics"],
        model=walk_forward["registered_model"],
        test_days=preregistered["window_sessions"],
        top_k=preregistered["top_k"],
        holding_sessions=preregistered["holding_sessions"],
        validation_rules=registration["record"][
            "research_validation_rules"
        ],
    )
    preregistered["windows"] = results["windows"]
    preregistered["window_count"] = len(results["windows"])
    preregistered["portfolio"] = results["portfolio"]
    preregistered["gated_portfolio"] = results["gated_portfolio"]
    preregistered["benchmark_portfolio"] = results[
        "benchmark_portfolio"
    ]
    preregistered["gated_benchmark_portfolio"] = results[
        "gated_benchmark_portfolio"
    ]
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(preregistered)
    )


def test_research_assessment_fails_negative_out_of_sample_result():
    assessment = relay.assess_research_result(
        _assessment_fixture(-0.001, window_return=-1.0)
    )

    assert assessment["research_status"] == "failed"
    assert assessment["deployment_eligible"] is False
    assert "non_positive_oos_return" in assessment["failure_reasons"]


def test_research_assessment_can_only_make_an_exploratory_run_lock_eligible(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    assessment = relay.assess_research_result(
        _assessment_fixture(
            0.001,
            window_return=1.0,
            registration=registration,
        ),
        prospective_sessions=20,
    )

    assert assessment["metric_gate_passed"] is True
    assert assessment["bootstrap_total_return_ci_pct"]["lower"] > 0.0
    assert assessment["diagnostic_window_acceptance_rate"] == 1.0
    assert assessment["oos_mean_rank_ic"] == 0.05
    assert assessment["oos_positive_rank_ic_session_rate"] == 1.0
    assert assessment["oos_mean_top_bottom_spread_pct"] == 1.5
    assert assessment["oos_positive_spread_session_rate"] == 1.0
    assert assessment["assessment_benchmark_source"] == "gated"
    assert assessment["oos_excess_return_pct"] > 0.0
    assert assessment["bootstrap_excess_return_ci_pct"]["lower"] > 0.0
    assert assessment["research_status"] == "exploratory"
    assert assessment["lock_eligible"] is True
    assert assessment["prospective_sessions_required"] == 60
    assert assessment["prospective_sessions_observed"] == 0
    assert assessment["prospective_sessions_source"] == "frozen_experiment_store_only"
    assert assessment["deployment_eligible"] is False


def test_preregistered_oos_evidence_seal_detects_aggregate_tampering(
    monkeypatch,
):
    monkeypatch.setattr(
        relay,
        "_research_evidence_key",
        lambda: b"test-research-evidence-key",
        raising=False,
    )
    evidence = {
        "evidence_version": "relay_preregistered_oos_evidence_v1",
        "protocol_version": relay.MODEL_SPEC_PROTOCOL_VERSION,
        "portfolio": {
            "total_return_pct": 3.0,
            "executed_trade_count": 120,
        },
    }

    sealed = relay._seal_preregistered_oos_evidence(evidence)

    assert relay._verify_preregistered_oos_evidence_seal(sealed) is True
    sealed["portfolio"]["total_return_pct"] = 99.0
    assert relay._verify_preregistered_oos_evidence_seal(sealed) is False


def test_research_assessment_rejects_forged_benchmark_selection(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    assert relay.assess_research_result(walk_forward)[
        "preregistered_oos_evidence_verified"
    ] is True
    evidence = walk_forward["preregistered_oos"]
    evidence["session_diagnostics"][0][
        "benchmark_codes"
    ] = ["FORGED"]
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(evidence)
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False


def test_research_assessment_rejects_benchmark_path_mismatch(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    assert relay.assess_research_result(walk_forward)[
        "preregistered_oos_evidence_verified"
    ] is True
    evidence = walk_forward["preregistered_oos"]
    evidence["benchmark_attempts"][0][
        "valuation_path"
    ][-1]["return_pct"] = 99.0
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(evidence)
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False


def test_research_assessment_rejects_signed_aggregates_without_raw_attempts(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    monkeypatch.setattr(
        relay,
        "_research_evidence_key",
        lambda: b"test-research-evidence-key",
        raising=False,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    preregistered.update(
        {
            "evidence_version": "relay_preregistered_oos_evidence_v1",
            "top_k": 5,
            "holding_sessions": 1,
            "window_sessions": 2,
            "attempts": [],
        }
    )
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(preregistered)
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert (
        "preregistered_oos_evidence_mismatch"
        in assessment["failure_reasons"]
    )


def test_research_assessment_rejects_resigned_window_aggregate_tampering(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    preregistered["windows"][0]["gated_live_total_return_pct"] = 99.0
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(preregistered)
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["oos_total_return_pct"] == 0.0
    assert (
        "preregistered_oos_evidence_mismatch"
        in assessment["failure_reasons"]
    )


def test_research_assessment_fails_closed_without_persistent_evidence_key(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    monkeypatch.setattr(relay, "_research_evidence_key", lambda: None)

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert assessment["preregistered_oos_evidence_verified"] is False
    assert (
        "preregistered_oos_evidence_mismatch"
        in assessment["failure_reasons"]
    )


def test_research_assessment_rejects_rolling_evidence_without_frozen_model():
    walk_forward = _assessment_fixture(0.001, window_return=1.0)
    walk_forward.pop("registered_model")

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert (
        "preregistered_frozen_model_unavailable"
        in assessment["failure_reasons"]
    )


def test_repository_model_preregistration_is_external_and_hashed():
    registration = relay.load_model_preregistration()

    assert registration["ok"] is True
    assert registration["current_protocol"] is True
    assert Path(registration["path"]).name == (
        "relay_fixed_ridge_preregistered_v5.json"
    )
    assert registration["record"]["schema_version"] == 4
    assert registration["record"]["protocol_version"] == (
        relay.MODEL_SPEC_PROTOCOL_VERSION
    )
    assert registration["record"]["calibrated_through"] == (
        relay.MODEL_SPEC_CALIBRATED_THROUGH
    )
    assert registration["record"]["oos_not_before"] > (
        registration["record"]["calibrated_through"]
    )
    research_protocol = registration["record"]["research_protocol"]
    assert research_protocol["config"] == {
        "n_stocks": 800,
        "days": 320,
        "train_days": 60,
        "test_days": 10,
        "step_days": 10,
        "hold_days": 2,
        "stop_loss": -0.035,
        "take_profit": 0.07,
        "top_k": 10,
        "candidate_pool_size": 100,
        "include_market_signals": True,
        "model_version": relay.MODEL_VERSION,
        "universe_selection": (
            "point_in_time_eligible_stable_sha256_sample"
        ),
        "data_quality_policy": relay.FROZEN_DATA_QUALITY_POLICY,
    }
    assert research_protocol["mining_rules"] == (
        relay.normalize_mining_rules(None)
    )
    assert research_protocol["execution_rules"] == (
        relay.normalize_execution_rules()
    )
    assert research_protocol["backtest_policy"] == (
        relay.FROZEN_BACKTEST_POLICY
    )
    assert registration["record"]["research_validation_rules"] == (
        relay.RESEARCH_VALIDATION_RULES
    )
    assert (
        "minimum_validation_acceptance_rate"
        not in registration["record"]["research_validation_rules"]
    )
    assert registration["record"]["prospective_acceptance_rules"] == (
        relay.PROSPECTIVE_DUAL_TRACK_ACCEPTANCE_RULES
    )
    assert len(registration["sha256"]) == 64


def test_legacy_v4_model_preregistration_remains_auditable():
    path = (
        Path(relay.__file__).parent
        / "preregistrations"
        / "relay_fixed_ridge_preregistered_v4.json"
    )

    registration = relay.load_model_preregistration(path)

    assert registration["ok"] is True
    assert registration["current_protocol"] is False
    assert registration["record"]["schema_version"] == 3
    assert registration["record"]["protocol_version"] == (
        "relay_fixed_ridge_preregistered_v4"
    )
    canonical_bytes = path.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(canonical_bytes).hexdigest() == (
        "dde238528d350d2153464b86e6bf3b9dbc7b0bd1648805ed84cf2f1653501180"
    )


def test_legacy_v4_registration_cannot_make_a_model_deployable():
    registration = _eligible_registration()
    registration["current_protocol"] = False
    model = relay._registered_model_from_preregistration(registration)

    assert (
        relay._registered_frozen_model_is_eligible(model, registration)
        is False
    )


def test_repository_model_preregistration_rejects_tampered_content(tmp_path):
    registration = relay.load_model_preregistration()
    assert registration["ok"] is True
    tampered = json.loads(json.dumps(registration["record"]))
    tampered["parameters"]["ridge_alpha"] = 0.0
    path = tmp_path / "tampered-preregistration.json"
    path.write_text(
        json.dumps(tampered, ensure_ascii=False),
        encoding="utf-8",
    )

    result = relay.load_model_preregistration(path)

    assert result["ok"] is False
    assert result["reason"] == "preregistration_record_mismatch"
    assert result["sha256"] is None


@pytest.mark.parametrize(
    ("field_path", "tampered_value"),
    [
        (("registered_at",), {"unexpected": "object"}),
        (("oos_not_before",), ["2026-07-31"]),
        (
            ("executable_model", "coefficients", "ret_5"),
            {"unexpected": "coefficient"},
        ),
        (("calibration_evidence", "row_count"), "not-an-integer"),
        (("calibration_evidence", "row_count"), {"unexpected": "count"}),
        (("calibration_evidence", "trade_date_start"), ["2026-01-28"]),
        (("calibration_evidence", "trade_date_end"), {"date": "2026-07-27"}),
        (("calibration_evidence", "label_max_exit"), ["2026-07-30"]),
    ],
)
def test_repository_model_preregistration_rejects_malicious_field_types(
    tmp_path,
    field_path,
    tampered_value,
):
    registration = relay.load_model_preregistration()
    assert registration["ok"] is True
    tampered = json.loads(json.dumps(registration["record"]))
    target = tampered
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = tampered_value
    path = tmp_path / "malicious-preregistration.json"
    path.write_text(
        json.dumps(tampered, ensure_ascii=False),
        encoding="utf-8",
    )

    result = relay.load_model_preregistration(path)

    assert result["ok"] is False
    assert result["reason"] in {
        "preregistration_record_invalid",
        "preregistration_record_mismatch",
    }
    assert result["sha256"] is None


def test_research_assessment_fails_closed_without_repository_registration(
    monkeypatch,
    tmp_path,
):
    walk_forward = _assessment_fixture(0.001, window_return=1.0)
    monkeypatch.setattr(
        relay,
        "MODEL_PREREGISTRATION_PATH",
        tmp_path / "missing-preregistration.json",
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert (
        "preregistration_record_unavailable"
        in assessment["failure_reasons"]
    )


def test_registered_frozen_model_rejects_malformed_training_dates():
    registration = relay.load_model_preregistration()
    model = dict(
        _assessment_fixture(
            0.001,
            window_return=1.0,
        )["registered_model"]
    )
    model["refit_end"] = "0000-not-a-date"
    model["refit_label_max_exit"] = "0000-not-a-date"

    assert (
        relay._registered_frozen_model_is_eligible(
            model,
            registration,
        )
        is False
    )


def test_registered_frozen_model_rejects_coefficient_tampering():
    registration = relay.load_model_preregistration()
    assert registration["ok"] is True
    model = relay._registered_model_from_preregistration(registration)
    assert model["selected_factors"]
    model["coefficients"] = dict(model["coefficients"])
    selected = model["selected_factors"][0]
    model["coefficients"][selected] = (
        float(model["coefficients"][selected]) + 1.0
    )
    model["validation_accepted"] = True
    model["validation_stable"] = True
    model["frozen_model_candidate_eligible"] = True

    assert (
        relay._registered_frozen_model_is_eligible(model, registration)
        is False
    )


def test_research_assessment_fails_closed_on_malformed_model_payload(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    walk_forward["registered_model"]["selected_factors"] = [object()]

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert assessment["preregistered_oos_evidence_verified"] is False
    assert (
        "preregistered_oos_evidence_mismatch"
        in assessment["failure_reasons"]
    )


def test_research_assessment_rejects_return_path_mismatch(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    for attempt in preregistered["attempts"]:
        attempt["live_adjusted_return_pct"] = -99.0
    results = relay._build_preregistered_oos_results(
        session_dates=preregistered["session_dates"],
        trades=preregistered["trades"],
        attempts=preregistered["attempts"],
        model=walk_forward["registered_model"],
        test_days=preregistered["window_sessions"],
        top_k=preregistered["top_k"],
        holding_sessions=preregistered["holding_sessions"],
    )
    preregistered["windows"] = results["windows"]
    preregistered["portfolio"] = results["portfolio"]
    preregistered["gated_portfolio"] = results["gated_portfolio"]
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(preregistered)
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert assessment["preregistered_oos_evidence_verified"] is False
    assert (
        "preregistered_oos_evidence_mismatch"
        in assessment["failure_reasons"]
    )


def test_research_assessment_rejects_trade_return_mismatch(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    preregistered["trades"][0]["return_pct"] = 999.0
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(preregistered)
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("live_adjusted_return_pct", True),
        ("live_adjusted_return_pct", "1.0"),
        ("path_return_pct", True),
        ("path_return_pct", "1.0"),
    ],
)
def test_research_assessment_rejects_malformed_attempt_numbers(
    monkeypatch,
    field,
    value,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    attempt = preregistered["attempts"][0]
    if field == "path_return_pct":
        attempt["valuation_path"][-1]["return_pct"] = value
    else:
        attempt[field] = value
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(preregistered)
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert assessment["preregistered_oos_evidence_verified"] is False
    assert (
        "preregistered_oos_evidence_mismatch"
        in assessment["failure_reasons"]
    )


def test_research_assessment_accepts_blocked_zero_return_with_empty_path(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    attempt = preregistered["attempts"][0]
    attempt["execution_blocked"] = True
    attempt["exit_date"] = attempt["entry_date"]
    attempt["exit_reason"] = "st_status_unknown"
    attempt["live_adjusted_return_pct"] = 0.0
    attempt["valuation_path"] = []
    trade = preregistered["trades"][0]
    trade["live_adjusted_return_pct"] = 0.8
    _rebuild_and_seal_preregistered_oos(walk_forward)

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is True
    assert assessment["lock_eligible"] is True


def test_research_assessment_accepts_bounded_deferred_exit_path(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    attempt = walk_forward["preregistered_oos"]["attempts"][0]
    entry = pd.Timestamp(attempt["entry_date"])
    path_dates = [
        (entry + pd.offsets.BDay(offset)).strftime("%Y-%m-%d")
        for offset in range(4)
    ]
    attempt["exit_date"] = path_dates[-1]
    attempt["exit_reason"] = "deferred_missing_bar_exit"
    attempt["valuation_path"] = [
        {"date": value, "return_pct": float(index + 1) / 4.0}
        for index, value in enumerate(path_dates)
    ]
    _rebuild_and_seal_preregistered_oos(walk_forward)

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is True
    assert assessment["lock_eligible"] is True


def test_research_assessment_rejects_path_beyond_deferred_exit_limit(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    attempt = walk_forward["preregistered_oos"]["attempts"][0]
    entry = pd.Timestamp(attempt["entry_date"])
    path_dates = [
        (entry + pd.offsets.BDay(offset)).strftime("%Y-%m-%d")
        for offset in range(24)
    ]
    attempt["exit_date"] = path_dates[-1]
    attempt["exit_reason"] = "deferred_missing_bar_exit"
    attempt["valuation_path"] = [
        {
            "date": value,
            "return_pct": float(index + 1) / float(len(path_dates)),
        }
        for index, value in enumerate(path_dates)
    ]
    _rebuild_and_seal_preregistered_oos(walk_forward)

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False


def test_research_assessment_rejects_missing_signed_oos_sessions(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    removed_dates = set(preregistered["session_dates"][4:6])
    preregistered["session_dates"] = [
        value
        for value in preregistered["session_dates"]
        if value not in removed_dates
    ]
    preregistered["trades"] = [
        value
        for value in preregistered["trades"]
        if value["trade_date"] not in removed_dates
    ]
    preregistered["attempts"] = [
        value
        for value in preregistered["attempts"]
        if value["trade_date"] not in removed_dates
    ]
    _rebuild_and_seal_preregistered_oos(walk_forward)

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False


def test_calendar_snapshot_fails_closed_without_authoritative_year(
    monkeypatch,
):
    from market import trading_calendar

    monkeypatch.setattr(
        trading_calendar,
        "closed_days",
        lambda year: None,
    )

    assert (
        relay._build_evidence_calendar_snapshot(
            "2026-08-03",
            "2026-08-07",
        )
        is None
    )


def test_calendar_snapshot_rejects_implausible_empty_holiday_year():
    assert (
        relay._build_evidence_calendar_snapshot(
            "2026-08-03",
            "2026-08-07",
            closed_days_by_year={"2026": []},
        )
        is None
    )


@pytest.mark.parametrize("tampering", ["sparse_path", "delayed_entry"])
def test_research_assessment_rejects_execution_calendar_tampering(
    monkeypatch,
    tampering,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    attempt = walk_forward["preregistered_oos"]["attempts"][0]
    entry = pd.Timestamp(attempt["entry_date"])
    if tampering == "sparse_path":
        delayed_exit = (
            entry + pd.offsets.BDay(4)
        ).strftime("%Y-%m-%d")
        attempt["exit_date"] = delayed_exit
        attempt["valuation_path"] = [
            {"date": attempt["entry_date"], "return_pct": 0.5},
            {"date": delayed_exit, "return_pct": 1.0},
        ]
    else:
        delayed_entry = (
            entry + pd.offsets.BDay(1)
        ).strftime("%Y-%m-%d")
        attempt["entry_date"] = delayed_entry
        attempt["exit_date"] = delayed_entry
        attempt["valuation_path"] = [
            {"date": delayed_entry, "return_pct": 1.0},
        ]
    _rebuild_and_seal_preregistered_oos(walk_forward)

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False


@pytest.mark.parametrize(
    ("execution_blocked", "exit_reason"),
    [
        (False, "totally_unknown_exit"),
        (True, "take_profit"),
    ],
)
def test_research_assessment_rejects_exit_reason_outside_strict_schema(
    monkeypatch,
    execution_blocked,
    exit_reason,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    attempt = preregistered["attempts"][0]
    attempt["exit_reason"] = exit_reason
    if execution_blocked:
        attempt["execution_blocked"] = True
        attempt["exit_date"] = attempt["entry_date"]
        attempt["live_adjusted_return_pct"] = 0.0
        attempt["valuation_path"] = []
        preregistered["trades"][0]["live_adjusted_return_pct"] = 0.8
    _rebuild_and_seal_preregistered_oos(walk_forward)

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False


def test_research_assessment_rejects_resigned_oos_prefix_shift(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]

    def shift(value: str) -> str:
        return (
            pd.Timestamp(value) + pd.offsets.BDay(2)
        ).strftime("%Y-%m-%d")

    preregistered["session_dates"] = [
        shift(value) for value in preregistered["session_dates"]
    ]
    preregistered["observation_cutoff"] = shift(
        preregistered["observation_cutoff"]
    )
    for trade in preregistered["trades"]:
        trade["trade_date"] = shift(trade["trade_date"])
    for attempt in preregistered["attempts"]:
        attempt["trade_date"] = shift(attempt["trade_date"])
        attempt["entry_date"] = shift(attempt["entry_date"])
        attempt["exit_date"] = shift(attempt["exit_date"])
        for point in attempt["valuation_path"]:
            point["date"] = shift(point["date"])
    preregistered["calendar_snapshot"] = _calendar_snapshot_fixture(
        preregistered["session_dates"][0],
        max(
            attempt["exit_date"]
            for attempt in preregistered["attempts"]
        ),
    )
    _rebuild_and_seal_preregistered_oos(walk_forward)

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False


def test_research_assessment_rejects_resigned_pending_oos_tail(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    pending_date = (
        pd.Timestamp(preregistered["session_dates"][-1])
        + pd.offsets.BDay(1)
    ).strftime("%Y-%m-%d")
    preregistered["observation_cutoff"] = pending_date
    preregistered["pending_session_dates"] = [pending_date]
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(preregistered)
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False
    assert (
        "preregistered_oos_evidence_mismatch"
        in assessment["failure_reasons"]
    )


def test_research_assessment_rejects_resigned_unregistered_research_parameters(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    preregistered["locking_protocol"]["config"]["days"] += 1
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(preregistered)
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False


def test_research_assessment_rejects_resigned_evidence_from_other_source_bundle(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    protocol_binding = _current_protocol_binding()
    first_path = next(iter(protocol_binding["source_sha256"]))
    protocol_binding["source_sha256"][first_path] = "0" * 64
    preregistered["protocol_binding"] = protocol_binding
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(preregistered)
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False


def test_research_assessment_rejects_resigned_evidence_after_source_change(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    changed_binding = _current_protocol_binding()
    first_path = next(iter(changed_binding["source_sha256"]))
    changed_binding["source_sha256"][first_path] = "0" * 64
    material = {
        key: changed_binding[key]
        for key in (
            "binding_version",
            "source_hash_policy",
            "protocol_versions",
            "source_sha256",
        )
    }
    changed_binding["bundle_sha256"] = hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    monkeypatch.setattr(
        relay,
        "build_current_frozen_protocol_binding",
        lambda: json.loads(json.dumps(changed_binding)),
    )
    preregistered = walk_forward["preregistered_oos"]
    preregistered["protocol_binding"] = changed_binding
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(preregistered)
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False


def test_protocol_binding_normalizes_text_line_endings(monkeypatch):
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _path: b"first\r\nsecond\r\n",
    )
    windows_binding = relay.build_current_frozen_protocol_binding()
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _path: b"first\nsecond\n",
    )
    linux_binding = relay.build_current_frozen_protocol_binding()

    assert windows_binding["source_sha256"] == (
        linux_binding["source_sha256"]
    )
    assert windows_binding["bundle_sha256"] == (
        linux_binding["bundle_sha256"]
    )


def test_research_assessment_rejects_resigned_truncated_oos_tail(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
        session_count=26,
    )
    preregistered = walk_forward["preregistered_oos"]
    removed_date = preregistered["session_dates"][-1]
    preregistered["session_dates"] = (
        preregistered["session_dates"][:-1]
    )
    preregistered["observation_cutoff"] = (
        preregistered["session_dates"][-1]
    )
    preregistered["trades"] = [
        trade
        for trade in preregistered["trades"]
        if trade["trade_date"] != removed_date
    ]
    preregistered["attempts"] = [
        attempt
        for attempt in preregistered["attempts"]
        if attempt["trade_date"] != removed_date
    ]
    _rebuild_and_seal_preregistered_oos(walk_forward)

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False


def test_partial_oos_window_is_signed_but_cannot_pass_lock_gate(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
        session_count=25,
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["oos_window_count"] == 0
    assert assessment["oos_complete_window_count"] == 0
    assert "fixed_oos_window_mismatch" in assessment["failure_reasons"]


def test_fixed_oos_gate_rejects_thirteenth_complete_window(
    monkeypatch,
):
    registration = _eligible_registration()
    registration["record"]["research_validation_rules"].update(
        {
            "evaluation_window_count": 12,
            "window_sessions": 2,
            "oos_session_count": 24,
            "stop_rule": "first_fixed_prefix_only",
        }
    )
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
        session_count=26,
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert assessment["preregistered_oos_evidence_verified"] is False
    assert "fixed_oos_window_mismatch" in assessment["failure_reasons"]


def test_preregistered_oos_builder_stops_at_fixed_window_count(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
        session_count=26,
    )
    evidence = walk_forward["preregistered_oos"]

    results = relay._build_preregistered_oos_results(
        session_dates=evidence["session_dates"],
        trades=evidence["trades"],
        attempts=evidence["attempts"],
        model=walk_forward["registered_model"],
        test_days=2,
        top_k=5,
        holding_sessions=3,
        evaluation_window_count=12,
    )

    assert len(results["windows"]) == 12
    assert results["windows"][-1]["test_end"] == (
        evidence["session_dates"][23]
    )
    assert all(
        attempt["trade_date"] <= evidence["session_dates"][23]
        for attempt in results["attempts"]
    )


def test_injected_research_clock_cannot_grant_lock_eligibility(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    preregistered["feature_freshness"]["clock_source"] = "injected"
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(preregistered)
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert "non_system_research_clock" in assessment["failure_reasons"]


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("trade", "live_adjusted_return_pct", "not-a-number"),
        ("attempt", "unexpected_field", "ignored"),
    ],
)
def test_research_assessment_rejects_malformed_evidence_schema(
    monkeypatch,
    target,
    field,
    value,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    record = (
        preregistered["trades"][0]
        if target == "trade"
        else preregistered["attempts"][0]
    )
    record[field] = value
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(preregistered)
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False


def test_research_assessment_rejects_extreme_integer_without_raising(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    preregistered["attempts"][0]["live_adjusted_return_pct"] = 10**400
    walk_forward["preregistered_oos"] = (
        relay._seal_preregistered_oos_evidence(preregistered)
    )

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is False
    assert assessment["lock_eligible"] is False


def test_preregistered_oos_is_scored_by_registered_model_not_rolling_model(
    monkeypatch,
):
    from market import trading_calendar

    calendar = _calendar_snapshot_fixture(
        "2026-08-03",
        "2026-08-31",
    )
    monkeypatch.setattr(
        trading_calendar,
        "closed_days",
        lambda year: calendar["closed_days"].get(str(year)),
    )
    monkeypatch.setattr(
        trading_calendar,
        "calendar_provenance",
        lambda year: calendar["provenance"].get(str(year)),
    )
    dates = [
        value.strftime("%Y-%m-%d")
        for value in pd.bdate_range("2026-08-03", periods=9)
    ]
    feature_rows = []
    label_rows = []
    for trade_date in dates:
        for code, ret_5, result in (
            ("AAA", 0.9, 1.0),
            ("BBB", 0.1, -1.0),
        ):
            feature_rows.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "ret_5": ret_5,
                    "relay_score": 100.0 if code == "BBB" else 1.0,
                }
            )
            label_rows.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "entry_date": trade_date,
                    "exit_date": trade_date,
                    "forward_return_pct": result,
                    "live_adjusted_return_pct": result,
                    "model_target_pct": result,
                    "execution_blocked": False,
                    "research_evaluable": True,
                    "valuation_path": [
                        {"date": trade_date, "return_pct": result}
                    ],
                }
            )
    registered = {
        "model_version": relay.MODEL_VERSION,
        "fallback": False,
        "selected_factors": ["ret_5"],
        "coefficients": {"ret_5": 1.0},
        "normalized_weights": {"ret_5": 1.0},
        "feature_means": {"ret_5": 0.5},
        "feature_scales": {"ret_5": 0.25},
        "selected_market_gate": {"enabled": False},
        "validation_accepted": True,
        "validation_stable": True,
        "frozen_model_candidate_eligible": True,
    }
    rolling = {
        **registered,
        "coefficients": {"ret_5": -1.0},
        "normalized_weights": {"ret_5": -1.0},
    }
    registration = _eligible_registration(
        top_k=1,
        test_days=2,
    )
    registered_sha256 = relay.factor_model_digest(registered)
    registration["record"]["executable_model"] = dict(registered)
    registration["record"][
        "executable_model_sha256"
    ] = registered_sha256
    registration["model_sha256"] = registered_sha256
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    monkeypatch.setattr(
        relay,
        "_fit_preregistered_frozen_model",
        lambda *_args, **_kwargs: dict(registered),
    )
    monkeypatch.setattr(
        relay,
        "fit_factor_model",
        lambda *_args, **_kwargs: dict(rolling),
    )
    monkeypatch.setattr(
        relay,
        "select_factor_model",
        lambda *_args, **_kwargs: {
            "model": dict(rolling),
            "selection_source": "test_rolling_model",
            "selected_parameters": dict(relay.FIXED_MODEL_PARAMETERS),
            "model_spec_protocol_version": relay.MODEL_SPEC_PROTOCOL_VERSION,
            "model_spec_calibrated_through": (
                relay.MODEL_SPEC_CALIBRATED_THROUGH
            ),
            "validation_accepted": True,
            "validation_rejection_reason": None,
            "validation_stable": True,
            "validation_stability_reason": "ok",
            "validation_diagnostics": {},
            "candidates": [],
        },
    )

    result = relay.walk_forward_relay(
        pd.DataFrame(feature_rows),
        pd.DataFrame(label_rows),
        train_days=4,
        test_days=2,
        top_k=1,
        market_gate={"enabled": False},
        nested_gate_selection=False,
        locking_protocol=_locking_protocol_fixture(
            top_k=1,
            test_days=2,
        ),
        data_quality_assessment=(
            _data_quality_assessment_fixture()
        ),
        feature_freshness={
            "policy_version": (
                "authoritative_completed_session_v2"
            ),
            "clock_source": "system",
            "as_of": "2026-08-13T17:10:00+08:00",
            "latest_feature_date": "2026-08-13",
            "expected_latest_feature_date": "2026-08-13",
            "maturity_lag_sessions": 0,
        },
    )

    oos = result["preregistered_oos"]
    assert oos["model_sha256"] == relay.factor_model_digest(registered)
    assert oos["session_dates"][0] == "2026-08-03"
    assert oos["observation_cutoff"] == "2026-08-13"
    assert oos["pending_session_dates"] == []
    assert oos["session_dates"][-1] == "2026-08-13"
    assert oos["windows"][-1]["test_start"] == "2026-08-13"
    assert oos["windows"][-1]["test_end"] == "2026-08-13"
    assert oos["trades"][0]["codes"] == ["AAA"]
    assert oos["trades"]
    assert {
        code
        for trade in oos["trades"]
        for code in trade["codes"]
    } == {"AAA"}
    assert oos["diagnostic_policy_version"] == (
        relay.OOS_DIAGNOSTIC_POLICY_VERSION
    )
    assert oos["benchmark_policy_version"] == (
        relay.BENCHMARK_POLICY_VERSION
    )
    assert len(oos["session_diagnostics"]) == len(dates)
    assert all(
        diagnostic["rank_ic"] == 1.0
        for diagnostic in oos["session_diagnostics"]
    )
    assert len(oos["benchmark_attempts"]) == len(dates)
    benchmark_codes_by_date = {
        diagnostic["trade_date"]: diagnostic["benchmark_codes"]
        for diagnostic in oos["session_diagnostics"]
    }
    assert all(
        [attempt["ts_code"]]
        == benchmark_codes_by_date[attempt["trade_date"]]
        for attempt in oos["benchmark_attempts"]
    )


def test_research_assessment_rejects_unstable_validation_windows(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    for diagnostic in walk_forward["preregistered_oos"][
        "session_diagnostics"
    ]:
        diagnostic.update(
            {
                "rank_ic": -0.05,
                "bottom_quantile_return_pct": 1.0,
                "top_quantile_return_pct": -0.5,
                "top_bottom_spread_pct": -1.5,
            }
        )
    _rebuild_and_seal_preregistered_oos(walk_forward)

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert assessment["diagnostic_window_acceptance_rate"] == 0.0
    assert assessment["oos_mean_rank_ic"] == -0.05
    assert (
        "diagnostic_window_acceptance_rate_below_minimum"
        in assessment["failure_reasons"]
    )


def test_research_assessment_rejects_unevaluable_benchmark_session(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    missing_date = preregistered["session_dates"][0]
    preregistered["session_diagnostics"][0][
        "benchmark_evaluable"
    ] = False
    preregistered["benchmark_attempts"] = [
        attempt
        for attempt in preregistered["benchmark_attempts"]
        if attempt["trade_date"] != missing_date
    ]
    _rebuild_and_seal_preregistered_oos(walk_forward)

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["preregistered_oos_evidence_verified"] is True
    assert assessment["lock_eligible"] is False
    assert assessment["benchmark_unevaluable_oos_days"] == 1
    assert (
        "incomplete_benchmark_outcomes"
        in assessment["failure_reasons"]
    )


def test_research_assessment_rejects_any_unevaluable_oos_slot(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    walk_forward = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    preregistered = walk_forward["preregistered_oos"]
    missing_date = preregistered["session_dates"][0]
    preregistered["attempts"] = [
        attempt
        for attempt in preregistered["attempts"]
        if attempt["trade_date"] != missing_date
    ]
    missing_trade = next(
        trade
        for trade in preregistered["trades"]
        if trade["trade_date"] == missing_date
    )
    missing_trade["codes"] = []
    missing_trade.update(
        {
            "return_pct": None,
            "live_adjusted_return_pct": None,
            "research_evaluable": False,
            "gate_pass": False,
            "skip_reason": "incomplete_oos_outcomes",
        }
    )
    missing_trade.pop("gate_reason")
    _rebuild_and_seal_preregistered_oos(walk_forward)

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert assessment["unevaluable_oos_days"] == 1
    assert assessment["unevaluable_oos_slots"] == 5
    assert assessment["preregistered_oos_evidence_verified"] is True
    assert "incomplete_oos_outcomes" in assessment["failure_reasons"]


def test_research_assessment_rejects_history_at_or_before_model_cutoff():
    walk_forward = _assessment_fixture(0.001, window_return=1.0)
    for window in walk_forward["preregistered_oos"]["windows"]:
        window["test_start"] = "2026-07-30"
        window["test_end"] = "2026-07-30"

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert assessment["oos_window_count"] == 0
    assert assessment["model_spec_calibrated_through"] == "2026-07-30"
    assert "no_preregistered_oos_windows" in assessment["failure_reasons"]


def test_research_assessment_fails_closed_on_malformed_preregistered_window_count():
    walk_forward = _assessment_fixture(0.001, window_return=1.0)
    walk_forward["preregistered_oos"] = {
        "protocol_version": relay.MODEL_SPEC_PROTOCOL_VERSION,
        "calibrated_through": relay.MODEL_SPEC_CALIBRATED_THROUGH,
        "window_count": "not-an-integer",
        "portfolio": walk_forward["portfolio"],
        "gated_portfolio": walk_forward["gated_portfolio"],
    }

    assessment = relay.assess_research_result(walk_forward)

    assert assessment["lock_eligible"] is False
    assert assessment["oos_total_return_pct"] == 0.0
    assert "preregistered_oos_evidence_mismatch" in assessment["failure_reasons"]


def test_frozen_experiment_manifest_uses_only_a_lock_eligible_server_report(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    report = _lockable_report_fixture(registration)

    manifest = _build_manifest_for_test(report)

    assert manifest["schema_version"] == 4
    assert manifest["strategy_key"] == relay.STRATEGY_KEY
    assert manifest["protocol_binding"]["binding_version"] == (
        "relay_frozen_protocol_binding_v3"
    )
    assert manifest["protocol_binding"]["source_hash_policy"] == (
        "text_lf_sha256_v1"
    )
    assert manifest["protocol_binding"]["protocol_versions"] == {
        "candidate_selection": "relay_candidate_selection_v3",
        "control_selection": (
            "same_pit_universe_sha256_equal_weight_v1"
        ),
        "readiness": "relay_prospective_readiness_v3",
        "position_sizing": "relay_shadow_position_sizing_v1",
        "trade_path_settlement": (
            "relay_real_bar_atomic_dual_track_settlement_v4"
        ),
    }
    assert set(manifest["protocol_binding"]["source_sha256"]) == {
        "analytics/data_manager.py",
        "analytics/factors_zoo/scorer.py",
        "analytics/market_signal_quality.py",
        "analytics/preregistrations/relay_fixed_ridge_preregistered_v5.json",
        "analytics/relay_factor_experiment.py",
        "analytics/relay_factor_mining.py",
        "analytics/relay_factor_model.py",
        "analytics/relay_portfolio.py",
        "analytics/st_status_history.py",
        "analytics/tushare_provider.py",
        "market/calendar_commitments/tushare_sse_2026_v1.json",
        "market/trading_calendar.py",
    }
    assert all(
        len(digest) == 64
        for digest in manifest["protocol_binding"]["source_sha256"].values()
    )
    assert len(manifest["protocol_binding"]["bundle_sha256"]) == 64
    assert manifest["model"] == report["model"]
    assert manifest["model_specification"] == {
        "protocol_version": "relay_fixed_ridge_preregistered_v5",
        "registered_at": registration["record"]["registered_at"],
        "calibrated_through": "2026-07-30",
        "oos_not_before": "2026-08-03",
        "registration_sha256": registration["sha256"],
        "model_sha256": registration["model_sha256"],
        "calibration_sha256": registration["record"][
            "calibration_evidence"
        ]["sha256"],
        "parameters": {"max_factors": 6, "ridge_alpha": 5.0},
    }
    assert manifest["preregistration"] == {
        **registration["record"],
        "record_sha256": registration["sha256"],
    }
    assert manifest["config"]["hold_days"] == 2
    assert manifest["config"]["top_k"] == 5
    assert manifest["execution_rules"] == {
        **relay.LIVE_EXECUTION_RULES,
        "path_policy": "t_plus_1_stop_first_on_same_bar_v1",
        "limit_threshold_policy": "board_and_point_in_time_st_v1",
    }
    assert manifest["market_gate"] == {"enabled": False}
    assert manifest["acceptance_rules"] == {
        "protocol_version": (
            "prospective_60_session_paired_control_v3"
        ),
        "required_sessions": 60,
        "minimum_total_return_pct": 2.0,
        "minimum_excess_return_pct": 0.0,
        "maximum_drawdown_pct": -10.0,
        "minimum_executed_trades": 60,
        "minimum_bootstrap_lower_return_pct": 0.0,
        "minimum_excess_bootstrap_lower_return_pct": 0.0,
        "maximum_protocol_violations": 0,
        "preregistered_at": relay.MODEL_SPEC_REGISTERED_AT,
        "calibrated_through": "2026-07-30",
    }
    assert manifest["historical_evidence"]["generated_at"] == (
        report["generated_at"]
    )
    assert manifest["historical_evidence"]["scope"] == (
        "preregistered_frozen_model_oos"
    )
    assert manifest["historical_evidence"]["latest_trade_date"] == (
        report["preregistered_oos"]["observation_cutoff"]
    )


def test_frozen_manifest_rejects_stale_report_selected_after_new_session(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    report = _lockable_report_fixture(registration)
    as_of = pd.Timestamp(
        report["preregistered_oos"]["feature_freshness"]["as_of"]
    )
    validation_now = (
        as_of + pd.offsets.BDay(1)
    ).to_pydatetime()

    with pytest.raises(
        ValueError,
        match="current completed trading session",
    ):
        relay.build_frozen_experiment_manifest(
            report,
            _validation_now=validation_now,
        )


def test_frozen_manifest_rejects_latest_oos_date_not_bound_to_signed_cutoff(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    report = _lockable_report_fixture(registration)
    report["summary"]["latest_oos_observation_date"] = "2099-12-31"

    with pytest.raises(ValueError, match="latest OOS observation date"):
        _build_manifest_for_test(report)


def test_frozen_manifest_rejects_unsigned_locking_protocol_change(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    report = _lockable_report_fixture(registration)
    report["config"]["top_k"] = 10

    with pytest.raises(ValueError, match="locking protocol"):
        _build_manifest_for_test(report)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount_data_coverage", 0.0),
        ("latest_amount_data_coverage", 0.0),
        ("st_status_coverage", 0.0),
        ("latest_st_status_coverage", 0.0),
        ("universe_eligibility_coverage", 0.0),
        ("latest_universe_eligibility_coverage", 0.0),
        ("panel_max_abs_close_return", 99.0),
    ],
)
def test_frozen_manifest_rejects_tampered_data_quality_result(
    monkeypatch,
    field,
    value,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    report = _lockable_report_fixture(registration)
    report["summary"][field] = value

    with pytest.raises(ValueError, match="data quality"):
        _build_manifest_for_test(report)


def test_frozen_manifest_ignores_unsigned_backtest_policy_change(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    report = _lockable_report_fixture(registration)
    report["summary"]["backtest_policy"] = {
        "entry": "same_day_close",
    }

    manifest = _build_manifest_for_test(report)

    assert manifest["backtest_policy"] == {
        "purge": "exit_date_before_test_start",
        "validation": "chronological_tail_20pct",
        "entry": "next_session_open",
        "same_bar_stop_target": "stop_first_conservative",
        "blocked_slots": "remain_cash_without_redistribution",
        "capital": "cash_aware_overlapping_positions",
        "maximum_deferred_exit_sessions": 20,
        "compounding": True,
    }


def test_frozen_manifest_rejects_non_boolean_assessment_flags(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    report = _lockable_report_fixture(registration)
    report["research_assessment"]["metric_gate_passed"] = 1

    with pytest.raises(ValueError):
        _build_manifest_for_test(report)


def test_frozen_manifest_rejects_malformed_locking_config(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    report = _lockable_report_fixture(registration)
    report["config"]["top_k"] = {"malformed": True}

    with pytest.raises(ValueError, match="locking protocol"):
        _build_manifest_for_test(report)


@pytest.mark.parametrize(
    "invalid_protocol",
    [
        {
            "execution_rules": {
                **relay.normalize_execution_rules(),
                "round_trip_cost_pct": -0.1,
            },
        },
        {
            "mining_rules": {
                "weights": {
                    **relay.normalize_mining_rules(None)["weights"],
                    "ret_5": "0.18",
                },
            },
        },
    ],
)
def test_locking_protocol_rejects_malformed_or_weakened_rules(
    invalid_protocol,
):
    protocol = _locking_protocol_fixture()
    protocol.update(invalid_protocol)

    with pytest.raises(ValueError, match="locking protocol"):
        relay._build_locking_protocol(
            config=protocol["config"],
            mining_rules=protocol["mining_rules"],
            execution_rules=protocol["execution_rules"],
        )


def test_locking_protocol_rejects_malformed_data_quality_policy():
    protocol = _locking_protocol_fixture()
    protocol["config"]["data_quality_policy"].update(
        {
            "discontinuous_price_history": (
                "quarantine_from_first_detectable_break"
            ),
            "max_abs_daily_return": "0.35",
            "amount_without_real_cache": (
                "proxy_for_legacy_rules_excluded_from_v2_model"
            ),
            "st_status_source": (
                "point_in_time_cache_is_st_then_name"
            ),
            "st_status_unknown": (
                "display_only_zero_position_and_reject_execution"
            ),
            "universe_eligibility_missing": (
                "research_data_status_degraded"
            ),
        }
    )

    with pytest.raises(ValueError, match="data_quality_policy"):
        relay._build_locking_protocol(
            config=protocol["config"],
            mining_rules=protocol["mining_rules"],
            execution_rules=protocol["execution_rules"],
        )


def test_calendar_snapshot_rejects_self_consistent_dates_not_matching_authority():
    forged = _calendar_snapshot_fixture(
        "2026-07-31",
        "2026-08-12",
    )
    closed_days = set(forged["closed_days"]["2026"])
    closed_days.remove("2026-01-01")
    closed_days.add("2026-07-31")
    forged["closed_days"]["2026"] = sorted(closed_days)
    open_days = relay._calendar_year_open_days(2026, closed_days)
    forged["provenance"]["2026"]["open_days_count"] = len(open_days)
    forged["provenance"]["2026"]["open_days_sha256"] = (
        relay._calendar_open_days_sha256(open_days)
    )
    material = relay._calendar_snapshot_material(forged)
    forged["snapshot_sha256"] = hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert relay._validate_evidence_calendar_snapshot(forged) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("days", 10**400),
        ("take_profit", 10**400),
    ],
)
def test_locking_protocol_rejects_unbounded_numeric_config(
    field,
    value,
):
    protocol = _locking_protocol_fixture()
    protocol["config"][field] = value

    with pytest.raises(ValueError, match=field):
        relay._build_locking_protocol(
            config=protocol["config"],
            mining_rules=protocol["mining_rules"],
            execution_rules=protocol["execution_rules"],
        )


def test_frozen_experiment_manifest_rejects_untrusted_or_unvalidated_report():
    registration = relay.load_model_preregistration()
    assert registration["ok"] is True
    evidence = _assessment_fixture(0.001, window_return=1.0)
    assessment = relay.assess_research_result(evidence)
    base = {
        "status": "ok",
        "data_status": "ok",
        "research_assessment": assessment,
        "model": evidence["registered_model"],
        "config": {},
        "mining_rules": {},
        "windows": evidence["windows"],
        "portfolio": evidence["portfolio"],
        "gated_portfolio": evidence["gated_portfolio"],
        "preregistered_oos": evidence["preregistered_oos"],
    }
    for patch in (
        {"data_status": "degraded"},
        {"research_assessment": {"metric_gate_passed": False, "lock_eligible": False}},
        {"model": {"fallback": True, "validation_accepted": True}},
        {"model": {"fallback": False, "validation_accepted": False}},
        {
            "model": {
                "fallback": False,
                "validation_accepted": True,
                "validation_stable": False,
                "selected_factors": ["ret_5"],
            }
        },
    ):
        report = {**base, **patch}
        with pytest.raises(ValueError):
            _build_manifest_for_test(report)


def test_frozen_manifest_rejects_unbound_latest_rolling_model():
    report = {
        "status": "ok",
        "data_status": "ok",
        "research_assessment": {
            "metric_gate_passed": True,
            "lock_eligible": True,
        },
        "model": {
            "model_version": relay.MODEL_VERSION,
            "fallback": False,
            "validation_accepted": True,
            "validation_stable": True,
            "model_spec_protocol_version": relay.MODEL_SPEC_PROTOCOL_VERSION,
            "model_spec_calibrated_through": (
                relay.MODEL_SPEC_CALIBRATED_THROUGH
            ),
            "selected_factors": ["ret_5"],
            "coefficients": {"ret_5": 1.0},
        },
        "config": {},
        "mining_rules": {},
    }

    with pytest.raises(
        ValueError,
        match="preregistered frozen factor model",
    ):
        _build_manifest_for_test(report)


def test_frozen_manifest_recomputes_evidence_instead_of_trusting_flags(
    monkeypatch,
):
    registration = _eligible_registration()
    monkeypatch.setattr(
        relay,
        "load_model_preregistration",
        lambda: registration,
    )
    evidence = _assessment_fixture(
        0.001,
        window_return=1.0,
        registration=registration,
    )
    report = {
        "status": "ok",
        "data_status": "ok",
        "research_assessment": {
            "metric_gate_passed": True,
            "lock_eligible": True,
        },
        "model": evidence["registered_model"],
        "config": {},
        "mining_rules": {},
    }

    with pytest.raises(
        ValueError,
        match="research evidence did not pass",
    ):
        _build_manifest_for_test(report)


def test_cli_passes_market_signal_flag(monkeypatch, tmp_path):
    captured = {}

    def fake_run_relay_research(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "summary": {}}

    monkeypatch.setattr(relay, "run_relay_research", fake_run_relay_research)

    code = relay.main(["--with-market-signals", "--out-dir", str(tmp_path)])

    assert code == 0
    assert captured["include_market_signals"] is True


# ── 市场状态门（2026-07-27 引入）──────────────────────────────────

def _gate_features_labels(n_days: int = 30, emotions=None):
    """构造带 market_emotion_score 的最小 features/labels 对。"""
    dates = [
        d.strftime("%Y-%m-%d")
        for d in pd.bdate_range("2026-08-03", periods=n_days)
    ]
    if emotions is None:
        emotions = [40.0] * n_days
    rows_f, rows_l = [], []
    for i, d in enumerate(dates):
        for j, code in enumerate(["AAA", "BBB", "CCC"]):
            rows_f.append({
                "trade_date": d, "ts_code": code,
                "relay_score": float(10 - j + i * 0.01),
                "ret_5": float(3 - j),
                "market_emotion_score": float(emotions[i]),
            })
            rows_l.append({
                "trade_date": d, "ts_code": code,
                "forward_return_pct": 1.0 if j == 0 else -0.5,
            })
    return pd.DataFrame(rows_f), pd.DataFrame(rows_l)


def test_gate_check_reasons():
    gate = {"enabled": True, "emo_min": 20.0, "emo_max": 70.0, "require_cooling": True}
    assert relay._gate_check(gate, 40.0, -1.0) == (True, "pass")
    assert relay._gate_check(gate, 40.0, 0.0) == (True, "pass")
    assert relay._gate_check(gate, 10.0, -1.0) == (False, "emotion_too_cold")
    assert relay._gate_check(gate, 75.0, -1.0) == (False, "emotion_overheated")
    assert relay._gate_check(gate, 40.0, 3.0) == (False, "emotion_heating_up")
    assert relay._gate_check(gate, None, None) == (False, "gate_data_missing")
    assert relay._gate_check(gate, 40.0, None) == (False, "gate_data_missing")
    assert relay._GATE_REASON_TEXT["gate_data_missing"] == "市场情绪数据不足，状态门阻断"
    # 门关闭 → 全放行
    assert relay._gate_check({"enabled": False}, 5.0, 9.0) == (True, "gate_disabled")
    assert relay._gate_check(None, 5.0, 9.0) == (True, "gate_disabled")


def test_latest_emotion_delta_requires_current_and_previous_trusted_days():
    features = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-28",
                "ts_code": "000001.SZ",
                "market_emotion_score": 80.0,
                "market_data_trusted": False,
            },
            {
                "trade_date": "2026-07-29",
                "ts_code": "000001.SZ",
                "market_emotion_score": 40.0,
                "market_data_trusted": True,
            },
        ]
    )

    assert relay._latest_emotion_delta(features) is None


def _gate_selection_frame(*, gate_helps: bool) -> tuple[pd.DataFrame, dict]:
    rows = []
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-02-02", periods=10)]
    for index, trade_date in enumerate(dates):
        good_day = index % 2 == 0
        emotion = 40.0 if good_day == gate_helps else 80.0
        for code, signal in (("AAA", 2.0), ("BBB", 1.0)):
            target = (2.0 if good_day else -5.0) if code == "AAA" else 0.0
            rows.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "ret_5": signal,
                    "model_target_pct": target,
                    "forward_return_pct": target,
                    "live_adjusted_return_pct": target,
                    "entry_date": trade_date,
                    "exit_date": trade_date,
                    "execution_blocked": False,
                    "market_emotion_score": emotion,
                }
            )
    model = {
        "fallback": False,
        "selected_factors": ["ret_5"],
        "coefficients": {"ret_5": 1.0},
        "feature_means": {"ret_5": 0.5},
        "feature_scales": {"ret_5": 0.25},
    }
    return pd.DataFrame(rows), model


def test_market_gate_selection_uses_validation_when_gate_improves_net_portfolio():
    validation, model = _gate_selection_frame(gate_helps=True)
    gate = {"enabled": True, "emo_min": 20.0, "emo_max": 70.0, "require_cooling": False}

    selection = relay._select_market_gate(
        validation,
        model,
        configured_gate=gate,
        top_k=1,
        holding_sessions=1,
    )

    assert selection["selection_source"] == "chronological_validation"
    assert selection["selected_gate"] == gate
    assert selection["selection_reason"] == "validation_improvement"
    assert selection["candidates"][1]["total_return_pct"] > selection["candidates"][0]["total_return_pct"]


def test_market_gate_selection_disables_gate_when_validation_does_not_improve():
    validation, model = _gate_selection_frame(gate_helps=False)
    gate = {"enabled": True, "emo_min": 20.0, "emo_max": 70.0, "require_cooling": False}

    selection = relay._select_market_gate(
        validation,
        model,
        configured_gate=gate,
        top_k=1,
        holding_sessions=1,
    )

    assert selection["selected_gate"]["enabled"] is False
    assert selection["selection_reason"] == "configured_gate_not_better"


def test_walk_forward_gate_dual_track():
    # 30 天：前 20 训练，后 10 测试。测试段情绪交替 40（过门）/ 80（过热不进）
    emotions = [40.0] * 20 + [40.0, 80.0] * 5
    features, labels = _gate_features_labels(30, emotions)
    gate = {"enabled": True, "emo_min": 20.0, "emo_max": 70.0, "require_cooling": False}
    wf = relay.walk_forward_relay(
        features,
        labels,
        train_days=20,
        test_days=10,
        top_k=1,
        market_gate=gate,
        nested_gate_selection=False,
    )
    trades = wf["trades"]
    assert len(trades) == 10
    passed = [t for t in trades if t["gate_pass"]]
    blocked = [t for t in trades if not t["gate_pass"]]
    assert len(passed) == 5 and len(blocked) == 5
    assert all(t["gate_reason"] == "emotion_overheated" for t in blocked)
    # 双轨：原轨统计全部 10 天，门轨只统计 5 天
    s = wf["summary"]
    assert s["trade_count"] == 10
    assert s["gated_trade_count"] == 5
    assert s["gated_skipped_days"] == 5
    assert s["market_gate_applied"] is True
    assert s["ungated_positive_window_rate"] == s["positive_window_rate"]
    assert s["gated_positive_window_rate"] == 1.0
    assert wf["windows"][0]["gate_selection"]["selection_reason"] == "nested_selection_disabled"
    # 每笔 trade 带情绪归因字段
    assert trades[0]["market_emotion_score"] == 40.0
    assert "emotion_delta" in trades[0]


def test_walk_forward_gate_missing_emotion_column_is_inactive():
    features, labels = _gate_features_labels(30)
    features = features.drop(columns=["market_emotion_score"])
    gate = {"enabled": True, "emo_min": 20.0, "emo_max": 70.0, "require_cooling": True}
    wf = relay.walk_forward_relay(features, labels, train_days=20, test_days=10, top_k=1, market_gate=gate)
    s = wf["summary"]
    assert s["market_gate_applied"] is False
    assert s["gated_trade_count"] == s["trade_count"]  # 门失效=不减少样本
    assert "market_gate_note" in s


def test_walk_forward_nested_gate_selection_does_not_read_test_labels():
    features, labels = _gate_features_labels(40, [40.0, 80.0] * 20)
    gate = {"enabled": True, "emo_min": 20.0, "emo_max": 70.0, "require_cooling": False}
    baseline = relay.walk_forward_relay(
        features,
        labels,
        train_days=30,
        test_days=10,
        top_k=1,
        market_gate=gate,
    )
    mutated = labels.copy()
    test_dates = sorted(features["trade_date"].unique().tolist())[30:40]
    mutated.loc[mutated["trade_date"].isin(test_dates), "forward_return_pct"] *= -1000.0

    changed = relay.walk_forward_relay(
        features,
        mutated,
        train_days=30,
        test_days=10,
        top_k=1,
        market_gate=gate,
    )

    assert baseline["windows"][0]["gate_selection"] == changed["windows"][0]["gate_selection"]


def test_research_assessment_ignores_retuned_rolling_gate_for_frozen_oos():
    features, labels = _gate_features_labels(30, [40.0, 80.0] * 15)
    gate = {
        "enabled": True,
        "emo_min": 20.0,
        "emo_max": 70.0,
        "require_cooling": False,
        "calibrated_through": "2026-12-31",
        "protocol_version": "market_emotion_gate_v1",
    }

    walk_forward = relay.walk_forward_relay(
        features,
        labels,
        train_days=20,
        test_days=10,
        top_k=1,
        market_gate=gate,
        nested_gate_selection=False,
    )
    assessment = relay.assess_research_result(walk_forward)

    assert walk_forward["windows"][0]["market_gate_evidence_eligible"] is False
    assert (
        walk_forward["windows"][0]["market_gate_evidence_reason"]
        == "calibration_overlaps_test_history"
    )
    assert walk_forward["summary"]["market_gate_evidence_eligible"] is False
    assert assessment["assessment_portfolio_source"] == "gated"
    assert assessment["market_gate_evidence_eligible"] is True
    assert (
        "market_gate_historical_calibration_overlap"
        not in assessment["failure_reasons"]
    )
    assert assessment["lock_eligible"] is False


def test_normalize_rules_market_gate_bounds():
    rules = relay.normalize_mining_rules({"market_gate": {"emo_min": 80, "emo_max": 30}})
    # min>=max 非法 → 回落默认
    assert rules["market_gate"]["emo_min"] == 20.0
    assert rules["market_gate"]["emo_max"] == 70.0
    rules2 = relay.normalize_mining_rules({"market_gate": {"enabled": False, "emo_min": 25, "emo_max": 65, "require_cooling": False}})
    assert rules2["market_gate"] == {
        "enabled": False,
        "emo_min": 25.0,
        "emo_max": 65.0,
        "require_cooling": False,
        "calibrated_through": None,
        "protocol_version": None,
        "calibration_source": None,
    }
    # condition_explain 包含门说明
    lines = relay.condition_explain(relay.normalize_mining_rules(None))
    assert any("market gate" in line for line in lines)


def test_condition_explain_uses_explicit_selected_market_gate():
    rules = relay.normalize_mining_rules(
        {
            "market_gate": {
                "enabled": True,
                "emo_min": 25,
                "emo_max": 65,
                "require_cooling": True,
            }
        }
    )

    lines = relay.condition_explain(
        rules,
        selected_market_gate={"enabled": False},
    )

    assert lines[-1] == "market gate: disabled (trades every test day)"
