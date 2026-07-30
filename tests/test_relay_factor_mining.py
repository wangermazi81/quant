# -*- coding: utf-8 -*-
from __future__ import annotations
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics import relay_factor_mining as relay


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
    }


def test_feature_frame_is_point_in_time_and_has_required_fields():
    panel = _panel()
    baseline = relay.build_feature_frame(panel)

    shocked = {key: value.copy() for key, value in panel.items()}
    for frame in shocked.values():
        frame.iloc[-1] = frame.iloc[-1] * 100
    changed = relay.build_feature_frame(shocked)

    compare_date = baseline["trade_date"].sort_values().unique()[-2]
    before = baseline[baseline["trade_date"] == compare_date].sort_values("ts_code").reset_index(drop=True)
    after = changed[changed["trade_date"] == compare_date].sort_values("ts_code").reset_index(drop=True)
    pd.testing.assert_series_equal(before["relay_score"], after["relay_score"], check_names=False)

    latest = baseline.sort_values(["trade_date", "relay_score"]).iloc[-1]
    assert latest["available_at"] == f"{latest['trade_date']} 15:00:00"
    assert {"ret_5", "volume_ratio_20", "near_high_20", "relay_score", "risk_flags"}.issubset(baseline.columns)


def test_feature_frame_marks_amount_proxy_when_cache_has_no_real_amount():
    panel = _panel()
    panel.pop("amount")

    features = relay.build_feature_frame(panel)

    assert set(features["amount_data_source"]) == {"close_times_volume_proxy"}
    assert not features["amount_data_real"].astype(bool).any()


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
        == "assume_5pct_limit_for_execution_and_cap_candidate_at_watch"
    )
    assert (tmp_path / "features.csv").exists()
    assert (tmp_path / "labels.csv").exists()
    assert (tmp_path / "walkforward_report.json").exists()
    assert (tmp_path / "latest_candidates.json").exists()
    assert (tmp_path / "factor_model.json").exists()
    assert result["model"]["model_version"] == "relay_factor_mining_v3_nested_ridge"
    assert result["summary"]["backtest_policy"]["purge"] == "exit_date_before_test_start"

    candidates = json.loads((tmp_path / "latest_candidates.json").read_text(encoding="utf-8"))
    assert candidates
    assert {"ts_code", "trade_date", "relay_score", "confidence", "entry_mode", "stop_loss", "take_profit"}.issubset(
        candidates[0]
    )


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


def test_latest_candidates_cap_unknown_st_status_at_watch():
    features = relay.build_feature_frame(_panel())

    candidates = relay.build_latest_candidates(features, top_k=1, candidate_pool_size=1)

    assert candidates
    assert candidates[0]["st_status_known"] is False
    assert candidates[0]["live_readiness_status"] == "watch"
    assert candidates[0]["position_pct"] <= 0.05
    assert "st_status_unknown" in candidates[0]["live_warning_keys"]


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

    assert explanation["factor_version"] == "relay_factor_mining_v3_nested_ridge"
    assert "二三板接力" in explanation["summary"]
    assert explanation["matched_factors"]
    assert explanation["risk_controls"]
    assert explanation["trading_plan"]["hold_days"] == 2
    assert explanation["update_policy"]["signal_refresh"] == "每个交易日收盘后刷新候选池"
    assert explanation["feature_snapshot"]["volume_ratio_20"] == 1.9


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
        "model": {
            "model_version": "relay_factor_mining_v3_nested_ridge",
            "fallback": False,
            "validation_accepted": True,
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
        "model": {
            "model_version": "relay_factor_mining_v3_nested_ridge",
            "fallback": False,
            "validation_accepted": True,
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


def _assessment_fixture(daily_return: float, *, window_return: float) -> dict:
    equity = 1.0
    curve = [{"date": "initial", "equity": equity}]
    for index in range(80):
        equity *= 1.0 + daily_return
        curve.append({"date": f"2026-01-{index + 1:02d}", "equity": equity})
    windows = [
        {
            "total_return_pct": window_return,
            "model": {"validation_accepted": True},
        }
        for _ in range(12)
    ]
    portfolio = {
        "total_return_pct": (equity - 1.0) * 100.0,
        "max_drawdown_pct": -2.0 if daily_return > 0 else -20.0,
        "executed_trade_count": 180,
        "equity_curve": curve,
    }
    return {"windows": windows, "gated_portfolio": portfolio, "portfolio": portfolio}


def test_research_assessment_fails_negative_out_of_sample_result():
    assessment = relay.assess_research_result(
        _assessment_fixture(-0.001, window_return=-1.0)
    )

    assert assessment["research_status"] == "failed"
    assert assessment["deployment_eligible"] is False
    assert "non_positive_oos_return" in assessment["failure_reasons"]


def test_research_assessment_can_only_make_an_exploratory_run_lock_eligible():
    assessment = relay.assess_research_result(
        _assessment_fixture(0.001, window_return=1.0),
        prospective_sessions=20,
    )

    assert assessment["metric_gate_passed"] is True
    assert assessment["bootstrap_total_return_ci_pct"]["lower"] > 0.0
    assert assessment["research_status"] == "exploratory"
    assert assessment["lock_eligible"] is True
    assert assessment["prospective_sessions_required"] == 20
    assert assessment["prospective_sessions_observed"] == 0
    assert assessment["prospective_sessions_source"] == "frozen_experiment_store_only"
    assert assessment["deployment_eligible"] is False


def test_frozen_experiment_manifest_uses_only_a_lock_eligible_server_report():
    report = {
        "status": "ok",
        "data_status": "ok",
        "generated_at": "2026-07-30 17:10:00",
        "research_status": "exploratory",
        "research_assessment": {
            "metric_gate_passed": True,
            "lock_eligible": True,
            "oos_total_return_pct": 12.5,
            "oos_max_drawdown_pct": -6.0,
            "bootstrap_total_return_ci_pct": {
                "lower": 1.2,
                "median": 8.0,
                "upper": 15.0,
            },
        },
        "model": {
            "model_version": "relay_factor_mining_v3_nested_ridge",
            "strategy_key": relay.STRATEGY_KEY,
            "fallback": False,
            "validation_accepted": True,
            "selected_factors": ["ret_5"],
            "coefficients": {"ret_5": 1.5},
            "selected_market_gate": {"enabled": False},
        },
        "config": {
            "n_stocks": 800,
            "days": 180,
            "train_days": 60,
            "test_days": 10,
            "hold_days": 2,
            "stop_loss": -0.035,
            "take_profit": 0.07,
            "top_k": 10,
            "candidate_pool_size": 100,
            "include_market_signals": True,
            "universe_selection": "point_in_time_eligible_stable_sha256_sample",
            "data_quality_policy": {"minimum_real_amount_coverage": 0.95},
        },
        "mining_rules": relay.normalize_mining_rules(None),
        "summary": {
            "backtest_policy": {"entry": "next_session_open"},
            "latest_trade_date": "2026-07-30",
        },
    }

    manifest = relay.build_frozen_experiment_manifest(report)

    assert manifest["schema_version"] == 2
    assert manifest["strategy_key"] == relay.STRATEGY_KEY
    assert manifest["model"] == report["model"]
    assert manifest["config"]["hold_days"] == 2
    assert manifest["config"]["top_k"] == 10
    assert manifest["execution_rules"] == {
        **relay.LIVE_EXECUTION_RULES,
        "path_policy": "t_plus_1_stop_first_on_same_bar_v1",
        "limit_threshold_policy": "board_and_point_in_time_st_v1",
    }
    assert manifest["market_gate"] == {"enabled": False}
    assert manifest["acceptance_rules"] == {
        "required_sessions": 20,
        "minimum_total_return_pct": 0.0,
        "maximum_drawdown_pct": -10.0,
        "minimum_executed_trades": 20,
        "maximum_protocol_violations": 0,
    }
    assert manifest["historical_evidence"]["generated_at"] == "2026-07-30 17:10:00"


def test_frozen_experiment_manifest_rejects_untrusted_or_unvalidated_report():
    base = {
        "status": "ok",
        "data_status": "ok",
        "research_assessment": {
            "metric_gate_passed": True,
            "lock_eligible": True,
        },
        "model": {
            "fallback": False,
            "validation_accepted": True,
            "selected_factors": ["ret_5"],
        },
        "config": {},
        "mining_rules": {},
    }
    for patch in (
        {"data_status": "degraded"},
        {"research_assessment": {"metric_gate_passed": False, "lock_eligible": False}},
        {"model": {"fallback": True, "validation_accepted": True}},
        {"model": {"fallback": False, "validation_accepted": False}},
    ):
        report = {**base, **patch}
        with pytest.raises(ValueError):
            relay.build_frozen_experiment_manifest(report)


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
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=n_days)]
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


def test_normalize_rules_market_gate_bounds():
    rules = relay.normalize_mining_rules({"market_gate": {"emo_min": 80, "emo_max": 30}})
    # min>=max 非法 → 回落默认
    assert rules["market_gate"]["emo_min"] == 20.0
    assert rules["market_gate"]["emo_max"] == 70.0
    rules2 = relay.normalize_mining_rules({"market_gate": {"enabled": False, "emo_min": 25, "emo_max": 65, "require_cooling": False}})
    assert rules2["market_gate"] == {"enabled": False, "emo_min": 25.0, "emo_max": 65.0, "require_cooling": False}
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
