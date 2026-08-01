from __future__ import annotations

import json

import pandas as pd

from analytics import short_factor_st_status as research_history
from analytics import st_status_history as history


def test_plain_code_accepts_baostock_exchange_prefix():
    assert research_history._plain_code("sz.000001") == "000001"
    assert research_history._plain_code("sh.600000") == "600000"


def test_status_series_never_uses_name_change_before_announcement():
    dates = pd.to_datetime(["2026-07-10", "2026-07-11", "2026-07-12", "2026-07-13"])
    cache = {
        "codes": {
            "000010": {
                "records": [
                    {
                        "ts_code": "000010.SZ",
                        "name": "美丽生态",
                        "start_date": "20260101",
                        "end_date": "20260710",
                        "ann_date": "20251231",
                    },
                    {
                        "ts_code": "000010.SZ",
                        "name": "*ST美丽",
                        "start_date": "20260711",
                        "end_date": "",
                        "ann_date": "20260712",
                    },
                ]
            }
        }
    }

    is_st, known = history.status_series("000010", dates, cache=cache)

    assert known.tolist() == [True, False, True, True]
    assert is_st.tolist() == [False, False, True, True]


def test_status_series_prefers_exact_baostock_daily_status_without_filling_gaps():
    dates = pd.to_datetime(
        ["2026-07-10", "2026-07-11", "2026-07-12"]
    )
    cache = {
        "codes": {
            "000010": {
                "records": [
                    {
                        "ts_code": "000010.SZ",
                        "name": "Normal Corp",
                        "start_date": "20200101",
                        "end_date": "",
                        "ann_date": "20200101",
                    }
                ],
                "daily_status": {
                    "20260710": False,
                    "20260712": True,
                },
            }
        }
    }

    is_st, known = research_history.status_series(
        "000010",
        dates,
        cache=cache,
    )

    assert known.tolist() == [True, True, True]
    assert is_st.tolist() == [False, False, True]


def test_daily_status_without_name_history_does_not_fill_missing_trade_date():
    dates = pd.to_datetime(
        ["2026-07-10", "2026-07-11", "2026-07-12"]
    )
    cache = {
        "codes": {
            "000010": {
                "daily_status": {
                    "20260710": False,
                    "20260712": True,
                },
            }
        }
    }

    is_st, known = research_history.status_series(
        "000010",
        dates,
        cache=cache,
    )

    assert known.tolist() == [True, False, True]
    assert is_st.tolist() == [False, False, True]


def test_refresh_daily_status_cache_only_fetches_missing_ranges(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        research_history,
        "CACHE_PATH",
        tmp_path / "st_namechange_cache.json",
    )
    research_history.CACHE_PATH.write_text(
        json.dumps(
            {
                "codes": {
                    "000001": {
                        "daily_status": {
                            "20260709": False,
                            "20260710": False,
                        },
                        "daily_coverage": [
                            {
                                "start": "20260709",
                                "end": "20260710",
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def fake_fetch(codes, start, end):
        calls.append((list(codes), start, end))
        return {
            "statuses": {
                "000001": {"20260711": True},
                "000002": {
                    "20260709": False,
                    "20260710": False,
                    "20260711": False,
                },
            },
            "successful_codes": list(codes),
            "errors": [],
        }

    monkeypatch.setattr(
        research_history,
        "_fetch_baostock_daily_status",
        fake_fetch,
    )

    result = research_history.refresh_daily_status_cache(
        ["000001", "000002"],
        "20260709",
        "20260711",
        batch_size=20,
    )

    assert result["ok"] is True
    assert result["coverage"] == 1.0
    assert calls == [
        (["000002"], "20260709", "20260711"),
        (["000001"], "20260711", "20260711"),
    ]
    stored = json.loads(
        research_history.CACHE_PATH.read_text(encoding="utf-8")
    )
    assert stored["codes"]["000001"]["daily_status"] == {
        "20260709": False,
        "20260710": False,
        "20260711": True,
    }


def test_enrich_panel_reports_only_eligible_close_cell_coverage():
    dates = pd.to_datetime(["2026-07-10", "2026-07-11"])
    close = pd.DataFrame(
        {
            "000001": [10.0, 10.1],
            "000002": [20.0, 20.1],
        },
        index=dates,
    )
    panel = {
        "close": close,
        "universe_eligible": pd.DataFrame(
            {
                "000001": [1.0, 1.0],
                "000002": [0.0, 1.0],
            },
            index=dates,
        ),
        "security_master_eligible": pd.DataFrame(
            1.0,
            index=dates,
            columns=close.columns,
        ),
    }
    cache = {
        "codes": {
            "000001": {
                "daily_status": {
                    "20260710": False,
                    "20260711": True,
                }
            },
            "000002": {
                "daily_status": {
                    "20260711": False,
                }
            },
        }
    }

    enriched, quality = research_history.enrich_panel_daily_status(
        panel,
        cache=cache,
    )

    assert quality["eligible_cells"] == 3
    assert quality["known_cells"] == 3
    assert quality["coverage"] == 1.0
    assert enriched["st_status_known"].loc[
        dates[0], "000002"
    ] == 0.0
    assert enriched["is_st"].loc[dates[1], "000001"] == 1.0


def test_refresh_cache_reuses_fresh_records(monkeypatch, tmp_path):
    monkeypatch.setattr(history, "CACHE_PATH", tmp_path / "st_namechange_cache.json")
    calls = []

    def fake_fetch(code, token=None, mode=None):
        calls.append(code)
        return [
            {
                "ts_code": f"{code}.SZ",
                "name": "测试股份",
                "start_date": "20200101",
                "end_date": "",
                "ann_date": "20200101",
                "change_reason": "其他",
            }
        ]

    monkeypatch.setattr(history.tushare_provider, "fetch_namechange_history", fake_fetch)

    first = history.refresh_cache(["000001", "000002"], max_workers=1)
    second = history.refresh_cache(["000001", "000002"], max_workers=1)

    assert first["ok"] is True
    assert first["fetched"] == 2
    assert second["ok"] is True
    assert second["fetched"] == 0
    assert second["fresh"] == 2
    assert calls == ["000001", "000002"]


def test_refresh_cache_does_not_treat_stale_records_as_fresh_when_refresh_fails(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(history, "CACHE_PATH", tmp_path / "st_namechange_cache.json")
    history.CACHE_PATH.write_text(
        json.dumps(
            {
                "codes": {
                    "000001": {
                        "fetched_at": "2000-01-01 00:00:00",
                        "records": [
                            {
                                "ts_code": "000001.SZ",
                                "name": "Ping An Bank",
                                "start_date": "19910403",
                                "end_date": "",
                                "ann_date": "19910403",
                                "change_reason": "other",
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        history.tushare_provider,
        "fetch_namechange_history",
        lambda code, token=None, mode=None: [],
    )

    result = history.refresh_cache(["000001"], max_workers=1)

    assert result["ok"] is False
    assert result["fresh_coverage"] == 0.0
    assert result["usable_coverage"] == 1.0
    assert result["coverage"] == 0.0
    assert result["usable"] == 1
    assert result["failed"] == 1
