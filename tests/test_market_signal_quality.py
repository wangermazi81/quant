# -*- coding: utf-8 -*-
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics import market_signal_quality as msq
from analytics import relay_factor_mining as relay


def _trusted_snapshot(date="20260708"):
    return {
        "source": "tushare_replay_daily_info",
        "stat_date": "2026-07-08",
        "turnover": {
            "amount": 25681.87,
            "avg_60d": 24000,
            "source": "tushare_replay_daily_info",
            "degraded": False,
        },
        "breadth": {
            "up": 3200,
            "flat": 80,
            "down": 2100,
            "total": 5380,
            "listed_total": 5450,
            "coverage": 0.987,
            "limit_up": 92,
            "limit_dn": 18,
            "distribution": {"limit_up": 92, "limit_down": 18},
            "source": "tushare_replay_daily",
            "degraded": False,
        },
    }


def _degraded_snapshot():
    snap = _trusted_snapshot()
    snap["source"] = "none"
    snap["turnover"] = {"amount": 0, "source": "none", "degraded": True, "reason": "zero turnover"}
    snap["breadth"]["coverage"] = 0.91
    snap["breadth"]["degraded"] = True
    snap["breadth"]["reason"] = "coverage too low"
    return snap


def test_quality_row_accepts_trusted_snapshot_and_scores_emotion():
    row = msq.quality_row_from_snapshot(_trusted_snapshot())

    assert row["trade_date"] == "2026-07-08"
    assert row["market_data_trusted"] is True
    assert row["market_data_status"] == "ok"
    assert row["turnover_amount_yi"] == 25681.87
    assert row["up_down_ratio"] > 1
    assert 0 <= row["market_emotion_score"] <= 100
    assert row["market_emotion_score"] > 50


def test_quality_row_rejects_degraded_or_low_coverage_snapshot():
    row = msq.quality_row_from_snapshot(_degraded_snapshot())

    assert row["market_data_trusted"] is False
    assert row["market_data_status"] == "degraded"
    assert "breadth_degraded" in row["market_data_reason"]
    assert "turnover_degraded" in row["market_data_reason"]


def test_build_market_signal_frame_uses_provider_and_records_failures():
    def provider(date_text):
        if str(date_text).endswith("08"):
            return _trusted_snapshot(date_text)
        raise RuntimeError("missing official data")

    frame = msq.build_market_signal_frame(["2026-07-08", "2026-07-09"], snapshot_provider=provider)

    assert frame.shape[0] == 2
    assert frame.loc[frame["trade_date"] == "2026-07-08", "market_data_trusted"].iloc[0] is True
    bad = frame.loc[frame["trade_date"] == "2026-07-09"].iloc[0]
    assert bad["market_data_trusted"] is False
    assert "missing official data" in bad["market_data_reason"]


def test_build_market_signal_frame_accepts_numpy_date_array():
    frame = msq.build_market_signal_frame(np.array(["2026-07-08"]), snapshot_provider=lambda date_text: _trusted_snapshot(date_text))

    assert frame.shape[0] == 1
    assert frame.iloc[0]["trade_date"] == "2026-07-08"


def test_relay_research_excludes_degraded_market_dates(tmp_path):
    panel = relay_panel(days=45)
    features = relay.build_feature_frame(panel)
    dates = sorted(features["trade_date"].unique())
    market_frame = pd.DataFrame([
        {"trade_date": dt, "market_data_trusted": dt != dates[-1], "market_data_status": "ok" if dt != dates[-1] else "degraded", "market_emotion_score": 60.0, "market_data_reason": "" if dt != dates[-1] else "bad latest"}
        for dt in dates
    ])

    result = relay.run_relay_research(
        panel=panel,
        out_dir=tmp_path,
        market_signal_frame=market_frame,
        train_days=20,
        test_days=8,
        hold_days=2,
        top_k=2,
    )

    assert result["summary"]["market_signal_rows"] == len(dates)
    assert result["summary"]["trusted_feature_rows"] < result["summary"]["feature_rows"]
    assert result["summary"]["latest_market_data_status"] == "degraded"
    assert result["latest_candidates"] == []


def relay_panel(days: int = 45) -> dict[str, pd.DataFrame]:
    import numpy as np

    dates = pd.bdate_range("2026-01-02", periods=days)
    close = pd.DataFrame(
        {
            "AAA": np.linspace(10.0, 17.0, days),
            "BBB": np.linspace(10.0, 12.0, days),
            "CCC": np.linspace(12.0, 9.0, days),
        },
        index=dates,
        dtype=float,
    )
    volume = pd.DataFrame(
        {
            "AAA": np.r_[np.full(days - 8, 100.0), np.full(8, 260.0)],
            "BBB": np.full(days, 130.0),
            "CCC": np.full(days, 90.0),
        },
        index=dates,
        dtype=float,
    )
    return {"open": close.shift(1).fillna(close.iloc[0]), "high": close * 1.02, "low": close * 0.98, "close": close, "volume": volume, "amount": close * volume * 100.0}
