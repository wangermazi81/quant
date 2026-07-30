from __future__ import annotations

import json

import pandas as pd

from analytics import st_status_history as history


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
