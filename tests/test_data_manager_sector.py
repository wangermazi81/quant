import types
import json
import time
import sys

import pytest

import analytics.data_manager as dm


@pytest.fixture(autouse=True)
def _use_current_sector_source(monkeypatch):
    monkeypatch.setattr(dm, "get_stock_sectors_bulk", dm.get_stock_sectors_bulk_current)


def test_get_stock_sector_falls_back_to_eastmoney_quote(monkeypatch, tmp_path):
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    monkeypatch.setattr(
        dm.ak,
        "stock_individual_info_em",
        lambda symbol: (_ for _ in ()).throw(ConnectionError("akshare down")),
    )
    monkeypatch.setattr(
        dm.ak,
        "stock_board_concept_name_em",
        lambda: (_ for _ in ()).throw(ConnectionError("akshare down")),
    )

    class FakeResponse:
        def json(self):
            return {
                "rc": 0,
                "data": {
                    "diff": [{
                        "f12": "000001",
                        "f100": "银行Ⅱ",
                        "f103": "深圳特区,互联网金融,区块链,跨境支付",
                    }],
                },
            }

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(dm, "_requests", types.SimpleNamespace(get=fake_get))

    assert dm.get_stock_sector("000001") == {
        "industry": "银行Ⅱ",
        "concepts": ["深圳特区", "互联网金融", "区块链", "跨境支付"],
    }
    assert calls


def test_get_stock_sector_failure_cache_does_not_block_empty_fallback(monkeypatch, tmp_path):
    meta_file = tmp_path / "metadata.json"
    meta_file.write_text(json.dumps({"000001": {"sector_error_at": time.time()}}), encoding="utf-8")
    monkeypatch.setattr(dm, "META_FILE", meta_file)
    monkeypatch.setattr(
        dm.ak,
        "stock_individual_info_em",
        lambda symbol: (_ for _ in ()).throw(ConnectionError("akshare down")),
    )
    monkeypatch.setattr(
        dm.ak,
        "stock_board_concept_name_em",
        lambda: (_ for _ in ()).throw(ConnectionError("akshare down")),
    )

    class FakeResponse:
        def json(self):
            return {"rc": 0, "data": {"diff": [{"f12": "000001", "f100": "银行Ⅱ", "f103": "互联网金融"}]}}

    monkeypatch.setattr(dm, "_requests", types.SimpleNamespace(get=lambda *a, **k: FakeResponse()))

    assert dm.get_stock_sector("000001") == {"industry": "银行Ⅱ", "concepts": ["互联网金融"]}


def test_get_stock_sectors_bulk_reads_industry_and_concepts(monkeypatch):
    class FakeResponse:
        def json(self):
            return {
                "rc": 0,
                "data": {
                    "diff": [
                        {"f12": "000001", "f100": "银行Ⅱ", "f103": "互联网金融,区块链"},
                        {"f12": "600519", "f100": "白酒Ⅱ", "f103": "超级品牌,白酒"},
                    ]
                },
            }

    monkeypatch.setattr(dm, "_requests", types.SimpleNamespace(get=lambda *a, **k: FakeResponse()))

    assert dm.get_stock_sectors_bulk(["000001", "600519"]) == {
        "000001": {"industry": "银行Ⅱ", "concepts": ["互联网金融", "区块链"]},
        "600519": {"industry": "白酒Ⅱ", "concepts": ["超级品牌", "白酒"]},
    }


def test_get_stock_sectors_bulk_respects_tushare_only_when_gateway_fails(monkeypatch):
    from analytics import data_gateway as dg

    monkeypatch.setattr(dg, "get_source_order", lambda: ["tushare"])
    monkeypatch.setattr(dg, "get_stock_sectors_bulk", lambda codes: (_ for _ in ()).throw(RuntimeError("gateway down")))
    monkeypatch.setattr(
        dm,
        "get_stock_sectors_bulk_current",
        lambda codes: (_ for _ in ()).throw(AssertionError("current sector fallback must not run")),
    )

    assert dm.get_stock_sectors_bulk(["000001"]) == {}


def test_get_stock_sector_uses_gateway_before_legacy_sector_scan(monkeypatch, tmp_path):
    from analytics import data_gateway as dg

    legacy_calls = []
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    monkeypatch.setattr(dg, "get_stock_sectors_bulk", lambda codes: {"000001": {"industry": "bank", "concepts": ["finance"]}})
    monkeypatch.setattr(
        dm.ak,
        "stock_individual_info_em",
        lambda symbol: legacy_calls.append("industry") or (_ for _ in ()).throw(AssertionError("legacy industry scan must not run")),
    )
    monkeypatch.setattr(
        dm.ak,
        "stock_board_concept_name_em",
        lambda: legacy_calls.append("concept") or (_ for _ in ()).throw(AssertionError("legacy concept scan must not run")),
    )

    assert dm.get_stock_sector("000001") == {"industry": "bank", "concepts": ["finance"]}
    assert legacy_calls == []


def test_get_stock_sectors_bulk_falls_back_to_baostock_industry(monkeypatch):
    monkeypatch.setattr(
        dm,
        "_requests",
        types.SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(ConnectionError("eastmoney down"))),
    )

    class FakeLogin:
        error_code = "0"

    class FakeRows:
        fields = ["updateDate", "code", "code_name", "industry", "industryClassification"]

        def __init__(self):
            self._done = False

        def next(self):
            if self._done:
                return False
            self._done = True
            return True

        def get_row_data(self):
            return ["2026-07-06", "sz.000001", "平安银行", "J66货币金融服务", "证监会行业分类"]

    fake_bs = types.SimpleNamespace(
        login=lambda: FakeLogin(),
        logout=lambda: None,
        query_stock_industry=lambda code: FakeRows(),
    )
    monkeypatch.setitem(sys.modules, "baostock", fake_bs)

    assert dm.get_stock_sectors_bulk(["000001"]) == {
        "000001": {"industry": "J66货币金融服务", "concepts": []}
    }


def test_get_stock_sectors_bulk_infers_special_market_codes(monkeypatch):
    monkeypatch.setattr(
        dm,
        "_requests",
        types.SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(ConnectionError("eastmoney down"))),
    )
    monkeypatch.setattr(dm, "_fetch_stock_sectors_baostock", lambda codes: {})

    assert dm.get_stock_sectors_bulk(["920857", "123273"]) == {
        "920857": {"industry": "北交所", "concepts": []},
        "123273": {"industry": "可转债", "concepts": []},
    }
