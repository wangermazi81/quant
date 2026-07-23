from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics.factors_zoo.registry import Registry


def _panel() -> dict[str, pd.DataFrame]:
    dates = pd.date_range("2026-01-01", periods=45, freq="B")
    base = pd.Series(np.linspace(10.0, 14.4, len(dates)), index=dates)
    close = pd.DataFrame(
        {
            "strong": base,
            "weak": pd.Series(np.linspace(12.0, 10.5, len(dates)), index=dates),
            "choppy": pd.Series(11.0 + np.sin(np.arange(len(dates))) * 0.8, index=dates),
        }
    )
    high = close * 1.02
    low = close * 0.98
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.DataFrame(
        {
            "strong": np.r_[np.full(35, 100.0), np.full(10, 240.0)],
            "weak": np.full(len(dates), 110.0),
            "choppy": np.r_[np.full(35, 120.0), np.full(10, 80.0)],
        },
        index=dates,
    )
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume}


def test_jw_relay_heat_v1_is_registered_and_computable():
    reg = Registry()

    assert "jw_relay_heat_v1" in reg.list(zoo="jianwei")

    result = reg.compute("jw_relay_heat_v1", _panel())

    assert result.shape == _panel()["close"].shape
    assert np.isfinite(result.dropna(how="all").to_numpy()).all()
    assert result.iloc[-1]["strong"] > result.iloc[-1]["weak"]


def test_jw_relay_heat_v1_does_not_use_future_rows():
    reg = Registry()
    panel = _panel()
    baseline = reg.compute("jw_relay_heat_v1", panel)

    shocked = {k: v.copy() for k, v in panel.items()}
    for frame in shocked.values():
        frame.iloc[-1] = frame.iloc[-1] * 100
    changed = reg.compute("jw_relay_heat_v1", shocked)

    compare_date = baseline.index[-2]
    pd.testing.assert_series_equal(
        baseline.loc[compare_date],
        changed.loc[compare_date],
        check_names=False,
    )


def test_jw_relay_pullback_v1_is_registered_and_computable():
    reg = Registry()

    assert "jw_relay_pullback_v1" in reg.list(zoo="jianwei")

    result = reg.compute("jw_relay_pullback_v1", _panel())

    assert result.shape == _panel()["close"].shape
    assert np.isfinite(result.dropna(how="all").to_numpy()).all()
    assert result.iloc[-1]["strong"] > result.iloc[-1]["weak"]


def test_jw_relay_pullback_v1_does_not_use_future_rows():
    reg = Registry()
    panel = _panel()
    baseline = reg.compute("jw_relay_pullback_v1", panel)

    shocked = {k: v.copy() for k, v in panel.items()}
    for frame in shocked.values():
        frame.iloc[-1] = frame.iloc[-1] * 100
    changed = reg.compute("jw_relay_pullback_v1", shocked)

    compare_date = baseline.index[-2]
    pd.testing.assert_series_equal(
        baseline.loc[compare_date],
        changed.loc[compare_date],
        check_names=False,
    )


def test_jw_relay_pullback_v1_penalizes_short_term_overextension():
    reg = Registry()
    dates = pd.date_range("2026-01-01", periods=45, freq="B")
    steady = pd.Series(np.linspace(10.0, 14.4, len(dates)), index=dates)
    overheated = steady.copy()
    overheated.iloc[-3:] = [15.8, 17.4, 19.0]
    weak = pd.Series(np.linspace(12.0, 10.5, len(dates)), index=dates)
    close = pd.DataFrame({"steady": steady, "overheated": overheated, "weak": weak})
    panel = {
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": pd.DataFrame(
            {
                "steady": np.r_[np.full(35, 100.0), np.full(10, 220.0)],
                "overheated": np.r_[np.full(35, 100.0), np.full(10, 220.0)],
                "weak": np.full(len(dates), 110.0),
            },
            index=dates,
        ),
    }

    result = reg.compute("jw_relay_pullback_v1", panel)

    assert result.iloc[-1]["steady"] > result.iloc[-1]["overheated"]


def test_jw_overheat_avoid_v1_is_registered_and_penalizes_overheated_names():
    reg = Registry()
    dates = pd.date_range("2026-01-01", periods=45, freq="B")
    calm = pd.Series(np.linspace(10.0, 11.0, len(dates)), index=dates)
    overheated = calm.copy()
    overheated.iloc[-20:] = np.linspace(11.0, 19.0, 20)
    overheated.iloc[-3:] = [20.8, 22.9, 25.2]
    quiet = pd.Series(np.linspace(9.5, 10.0, len(dates)), index=dates)
    close = pd.DataFrame({"calm": calm, "overheated": overheated, "quiet": quiet})
    panel = {
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": pd.DataFrame(
            {
                "calm": np.full(len(dates), 100.0),
                "overheated": np.r_[np.full(25, 100.0), np.full(20, 500.0)],
                "quiet": np.full(len(dates), 90.0),
            },
            index=dates,
        ),
    }

    assert "jw_overheat_avoid_v1" in reg.list(zoo="jianwei")

    result = reg.compute("jw_overheat_avoid_v1", panel)

    assert result.shape == close.shape
    assert np.isfinite(result.dropna(how="all").to_numpy()).all()
    assert result.iloc[-1]["calm"] > result.iloc[-1]["overheated"]


def test_jw_overheat_avoid_v1_does_not_use_future_rows():
    reg = Registry()
    panel = _panel()
    baseline = reg.compute("jw_overheat_avoid_v1", panel)

    shocked = {k: v.copy() for k, v in panel.items()}
    for frame in shocked.values():
        frame.iloc[-1] = frame.iloc[-1] * 100
    changed = reg.compute("jw_overheat_avoid_v1", shocked)

    compare_date = baseline.index[-2]
    pd.testing.assert_series_equal(
        baseline.loc[compare_date],
        changed.loc[compare_date],
        check_names=False,
    )


def test_jw_ai_voldiff_relay_v2_is_registered_and_computable():
    reg = Registry()

    assert "jw_ai_voldiff_relay_v2" in reg.list(zoo="jianwei")

    result = reg.compute("jw_ai_voldiff_relay_v2", _panel())

    assert result.shape == _panel()["close"].shape
    assert np.isfinite(result.dropna(how="all").to_numpy()).all()


def test_jw_ai_voldiff_relay_v2_does_not_use_future_rows():
    reg = Registry()
    panel = _panel()
    baseline = reg.compute("jw_ai_voldiff_relay_v2", panel)

    shocked = {k: v.copy() for k, v in panel.items()}
    for frame in shocked.values():
        frame.iloc[-1] = frame.iloc[-1] * 100
    changed = reg.compute("jw_ai_voldiff_relay_v2", shocked)

    compare_date = baseline.index[-2]
    pd.testing.assert_series_equal(
        baseline.loc[compare_date],
        changed.loc[compare_date],
        check_names=False,
    )

def test_jw_ai_voldiff_relay_v3_is_registered_and_computable():
    reg = Registry()

    assert "jw_ai_voldiff_relay_v3" in reg.list(zoo="jianwei")

    result = reg.compute("jw_ai_voldiff_relay_v3", _panel())

    assert result.shape == _panel()["close"].shape
    assert np.isfinite(result.dropna(how="all").to_numpy()).all()


def test_jw_ai_voldiff_relay_v3_does_not_use_future_rows():
    reg = Registry()
    panel = _panel()
    baseline = reg.compute("jw_ai_voldiff_relay_v3", panel)

    shocked = {k: v.copy() for k, v in panel.items()}
    for frame in shocked.values():
        frame.iloc[-1] = frame.iloc[-1] * 100
    changed = reg.compute("jw_ai_voldiff_relay_v3", shocked)

    compare_date = baseline.index[-2]
    pd.testing.assert_series_equal(
        baseline.loc[compare_date],
        changed.loc[compare_date],
        check_names=False,
    )
