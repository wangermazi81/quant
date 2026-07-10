# -*- coding: utf-8 -*-
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

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


def test_run_research_writes_report_and_latest_candidates(tmp_path):
    result = relay.run_relay_research(
        panel=_panel(days=55),
        out_dir=tmp_path,
        train_days=25,
        test_days=10,
        hold_days=2,
        top_k=2,
    )

    assert result["status"] in {"ok", "degraded"}
    assert result["summary"]["feature_rows"] > 0
    assert result["summary"]["label_rows"] > 0
    assert result["summary"]["lookahead_policy"] == "features_current_or_past_labels_t_plus_1"
    assert (tmp_path / "features.csv").exists()
    assert (tmp_path / "labels.csv").exists()
    assert (tmp_path / "walkforward_report.json").exists()
    assert (tmp_path / "latest_candidates.json").exists()

    candidates = json.loads((tmp_path / "latest_candidates.json").read_text(encoding="utf-8"))
    assert candidates
    assert {"ts_code", "trade_date", "relay_score", "confidence", "entry_mode", "stop_loss", "take_profit"}.issubset(
        candidates[0]
    )


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

    assert explanation["factor_version"] == "relay_factor_mining_v1"
    assert "二三板接力" in explanation["summary"]
    assert explanation["matched_factors"]
    assert explanation["risk_controls"]
    assert explanation["trading_plan"]["hold_days"] == 2
    assert explanation["update_policy"]["signal_refresh"] == "每个交易日收盘后刷新候选池"
    assert explanation["feature_snapshot"]["volume_ratio_20"] == 1.9


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


def test_run_research_degrades_when_samples_are_insufficient(tmp_path):
    result = relay.run_relay_research(panel=_panel(days=12), out_dir=tmp_path, train_days=20, test_days=5)

    assert result["status"] == "degraded"
    assert result["summary"]["window_count"] == 0
    assert "insufficient" in result["reason"]


def test_cli_passes_market_signal_flag(monkeypatch, tmp_path):
    captured = {}

    def fake_run_relay_research(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "summary": {}}

    monkeypatch.setattr(relay, "run_relay_research", fake_run_relay_research)

    code = relay.main(["--with-market-signals", "--out-dir", str(tmp_path)])

    assert code == 0
    assert captured["include_market_signals"] is True
