from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
ANALYTICS = ROOT / "analytics"
if str(ANALYTICS) not in sys.path:
    sys.path.insert(0, str(ANALYTICS))

from analytics import data_manager as dm  # noqa: E402
from analytics import st_status_history  # noqa: E402
from analytics.factors_zoo import scorer  # noqa: E402


def _daily_frame(start: str, periods: int) -> pd.DataFrame:
    dates = pd.bdate_range(start=start, periods=periods)
    frame = pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "open": range(10, 10 + periods),
            "close": range(11, 11 + periods),
            "high": range(12, 12 + periods),
            "low": range(9, 9 + periods),
            "volume": range(1000, 1000 + periods),
        }
    )
    frame["amount"] = frame["close"] * frame["volume"] * 100 + 123
    return frame


def test_download_one_does_not_overwrite_newer_cache_with_stale_source(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dm, "_load_meta", lambda: {})
    monkeypatch.setattr(dm, "_save_meta", lambda meta: None)
    monkeypatch.setattr(dm, "get_stock_name", lambda code: code)
    monkeypatch.setattr(dm.datasource_config, "get_internal_config", lambda: {
        "provider": "tushare",
        "tushare_enabled": True,
        "tushare_token": "token",
        "tushare_mode": "replay",
        "fallback_enabled": False,
    })
    monkeypatch.setattr(dm, "datetime", type("FixedDatetime", (), {
        "today": staticmethod(lambda: datetime(2026, 7, 9)),
        "now": staticmethod(lambda: datetime(2026, 7, 9, 16, 0)),
    }))

    cached = _daily_frame("2026-06-01", 28)
    cached.loc[len(cached) - 1, "date"] = "2026-07-08"
    cached.to_csv(tmp_path / "000002.csv", index=False)

    stale = _daily_frame("2026-06-01", 12)
    stale.loc[len(stale) - 1, "date"] = "2026-06-16"
    monkeypatch.setattr(dm.tushare_provider, "fetch_kline_qfq", lambda *args, **kwargs: stale)

    result = dm._download_one("000002", "2026-06-01")
    written = pd.read_csv(tmp_path / "000002.csv")

    assert result["ok"] is False
    assert "早于现有缓存" in result["msg"]
    assert str(written["date"].max()) == "2026-07-08"


def test_build_panel_from_cache_prefers_freshest_files_before_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)

    for code in ("000001", "000002"):
        old = _daily_frame("2026-01-01", 70)
        old.loc[len(old) - 1, "date"] = "2026-07-07"
        old.to_csv(tmp_path / f"{code}.csv", index=False)

    for code in ("300001", "600001"):
        fresh = _daily_frame("2026-01-01", 70)
        fresh.loc[len(fresh) - 1, "date"] = "2026-07-09"
        fresh.to_csv(tmp_path / f"{code}.csv", index=False)

    panel, codes = scorer.build_panel_from_cache(n_stocks=2, days=60, min_history=60)

    assert codes == ["300001", "600001"]
    assert set(panel["close"].columns) == {"300001", "600001"}


def test_build_panel_from_cache_require_latest_rejects_stale_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)

    for code in ("000001", "000002"):
        old = _daily_frame("2026-01-01", 70)
        old.loc[len(old) - 1, "date"] = "2026-07-07"
        old.to_csv(tmp_path / f"{code}.csv", index=False)

    fresh = _daily_frame("2026-01-01", 70)
    fresh.loc[len(fresh) - 1, "date"] = "2026-07-09"
    fresh.to_csv(tmp_path / "300001.csv", index=False)

    with pytest.raises(ValueError, match="Refresh data before generating live signals"):
        scorer.build_panel_from_cache(n_stocks=2, days=60, min_history=60, require_latest=True)


def test_build_panel_from_cache_uses_stable_sample_instead_of_longest_histories(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)

    for index, code in enumerate(("000001", "000002", "000003", "000004", "000005", "000006")):
        frame = _daily_frame("2026-01-01", 60 + index * 10)
        frame.loc[len(frame) - 1, "date"] = "2026-07-29"
        frame.to_csv(tmp_path / f"{code}.csv", index=False)

    panel, codes = scorer.build_panel_from_cache(n_stocks=2, days=60, min_history=60, require_latest=True)

    assert codes == ["000004", "000006"]
    assert set(panel["close"].columns) == {"000004", "000006"}


def test_build_panel_from_cache_skips_discontinuous_price_history_and_refills_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)

    for code in ("000001", "000002", "000003", "000004", "000005", "000006"):
        frame = _daily_frame("2026-01-01", 70)
        frame.loc[len(frame) - 1, "date"] = "2026-07-29"
        if code == "000004":
            spike = len(frame) - 3
            frame.loc[spike, ["open", "close", "high", "low"]] *= 30
        frame.to_csv(tmp_path / f"{code}.csv", index=False)

    panel, codes = scorer.build_panel_from_cache(
        n_stocks=2,
        days=60,
        min_history=60,
        require_latest=True,
    )

    assert codes == ["000003", "000006"]
    assert set(panel["close"].columns) == {"000003", "000006"}


def test_build_panel_from_cache_preserves_real_turnover_amount(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)
    frame = _daily_frame("2026-01-01", 70)
    frame.to_csv(tmp_path / "000001.csv", index=False)

    panel, _codes = scorer.build_panel_from_cache(n_stocks=1, days=60, min_history=60)

    expected = float(frame.tail(60).iloc[-1]["amount"])
    assert float(panel["amount"].iloc[-1]["000001"]) == expected


def test_build_panel_from_cache_keeps_stocks_with_missing_amount_as_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)
    real = _daily_frame("2026-01-01", 70)
    proxy = _daily_frame("2026-01-01", 70).drop(columns=["amount"])
    real.to_csv(tmp_path / "000001.csv", index=False)
    proxy.to_csv(tmp_path / "000002.csv", index=False)

    panel, codes = scorer.build_panel_from_cache(n_stocks=2, days=60, min_history=60)

    assert codes == ["000001", "000002"]
    assert set(panel["close"].columns) == {"000001", "000002"}
    assert set(panel["amount"].columns) == {"000001", "000002"}
    assert bool(panel["amount_real"]["000001"].all()) is True
    assert bool(panel["amount_real"]["000002"].any()) is False
    expected_proxy = panel["close"]["000002"] * panel["volume"]["000002"]
    pd.testing.assert_series_equal(
        panel["amount"]["000002"],
        expected_proxy,
        check_names=False,
    )


def test_point_in_time_panel_quarantines_only_from_first_price_break(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)
    stable = _daily_frame("2026-01-01", 70)
    broken = _daily_frame("2026-01-01", 70)
    break_index = len(broken) - 5
    broken.loc[break_index, ["open", "close", "high", "low"]] *= 30
    stable.to_csv(tmp_path / "000001.csv", index=False)
    broken.to_csv(tmp_path / "000002.csv", index=False)

    panel, codes = scorer.build_panel_from_cache(
        n_stocks=None,
        days=60,
        min_history=10,
        point_in_time=True,
    )

    break_date = pd.Timestamp(broken.loc[break_index, "date"])
    prior_date = pd.Timestamp(broken.loc[break_index - 1, "date"])
    assert "000002" in codes
    assert bool(panel["universe_eligible"].loc[prior_date, "000002"]) is True
    assert bool(panel["universe_eligible"].loc[break_date, "000002"]) is False
    assert pd.isna(panel["close"].loc[break_date, "000002"])


def test_build_panel_from_cache_preserves_point_in_time_st_status_without_filling(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)
    explicit = _daily_frame("2026-01-01", 70)
    explicit["is_st"] = 0
    explicit.loc[len(explicit) - 2, "is_st"] = 1
    explicit.loc[len(explicit) - 1, "is_st"] = None
    named = _daily_frame("2026-01-01", 70)
    named["name"] = "平安银行"
    named.loc[len(named) - 2, "name"] = "*ST测试"
    named.loc[len(named) - 1, "name"] = ""
    explicit.to_csv(tmp_path / "000001.csv", index=False)
    named.to_csv(tmp_path / "000002.csv", index=False)

    panel, codes = scorer.build_panel_from_cache(
        n_stocks=None,
        days=60,
        min_history=10,
        point_in_time=True,
    )

    st_date = pd.Timestamp(explicit.loc[len(explicit) - 2, "date"])
    unknown_date = pd.Timestamp(explicit.loc[len(explicit) - 1, "date"])
    assert codes == ["000001", "000002"]
    assert bool(panel["is_st"].loc[st_date, "000001"]) is True
    assert bool(panel["is_st"].loc[st_date, "000002"]) is True
    assert bool(panel["st_status_known"].loc[st_date, "000001"]) is True
    assert bool(panel["st_status_known"].loc[st_date, "000002"]) is True
    assert bool(panel["st_status_known"].loc[unknown_date, "000001"]) is False
    assert bool(panel["st_status_known"].loc[unknown_date, "000002"]) is False


def test_build_panel_from_cache_uses_historical_name_intervals(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "_DATA_DIR", tmp_path)
    frame = _daily_frame("2026-01-01", 70)
    frame["is_st"] = 1
    frame["name"] = "*ST current snapshot"
    frame.to_csv(tmp_path / "000010.csv", index=False)
    st_date = pd.Timestamp(frame.loc[len(frame) - 2, "date"])
    previous_date = pd.Timestamp(frame.loc[len(frame) - 3, "date"])
    monkeypatch.setattr(
        st_status_history,
        "load_cache",
        lambda: {
            "codes": {
                "000010": {
                    "records": [
                        {
                            "name": "美丽生态",
                            "start_date": "20260101",
                            "end_date": previous_date.strftime("%Y%m%d"),
                            "ann_date": "20260101",
                        },
                        {
                            "name": "*ST美丽",
                            "start_date": st_date.strftime("%Y%m%d"),
                            "end_date": "",
                            "ann_date": st_date.strftime("%Y%m%d"),
                        },
                    ]
                }
            }
        },
    )

    panel, _codes = scorer.build_panel_from_cache(
        n_stocks=1,
        days=60,
        min_history=10,
        point_in_time=True,
    )

    assert bool(panel["is_st"].loc[previous_date, "000010"]) is False
    assert bool(panel["is_st"].loc[st_date, "000010"]) is True
    assert bool(panel["st_status_known"].loc[previous_date, "000010"]) is True
    assert bool(panel["st_status_known"].loc[st_date, "000010"]) is True


def test_historical_st_unknown_is_not_overwritten_by_current_csv_name():
    dates = pd.to_datetime(["2026-07-10", "2026-07-11", "2026-07-12"])
    frame = pd.DataFrame(
        {
            "name": ["*ST current snapshot"] * 3,
            "is_st": [1, 1, 1],
        },
        index=dates,
    )
    history_cache = {
        "codes": {
            "000010": {
                "records": [
                    {
                        "name": "*ST美丽",
                        "start_date": "20260711",
                        "end_date": "",
                        "ann_date": "20260712",
                    }
                ]
            }
        }
    }

    is_st, known = scorer._st_status_from_cache(
        frame,
        code="000010",
        history_cache=history_cache,
    )

    assert known.astype(bool).tolist() == [False, False, True]
    assert is_st.astype(bool).tolist() == [False, False, True]


def test_merge_kline_cache_preserves_amount_and_st_fields_on_ohlcv_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, "DATA_DIR", tmp_path)
    initial = _daily_frame("2026-07-01", 2)
    initial["name"] = ["测试股份", "*ST测试"]
    initial["is_st"] = [0, 1]
    first = dm.merge_kline_cache("000001", initial, update_metadata=False)
    refresh = initial.tail(1).drop(columns=["amount", "name", "is_st"]).copy()
    refresh["close"] = refresh["close"].astype(float) + 0.1

    merged = dm.merge_kline_cache("000001", refresh, update_metadata=False)
    saved = pd.read_csv(tmp_path / "000001.csv")

    assert {"amount", "name", "is_st"}.issubset(merged.columns)
    assert float(merged.iloc[-1]["amount"]) == float(first.iloc[-1]["amount"])
    assert merged.iloc[-1]["name"] == "*ST测试"
    assert int(merged.iloc[-1]["is_st"]) == 1
    assert {"amount", "name", "is_st"}.issubset(saved.columns)
