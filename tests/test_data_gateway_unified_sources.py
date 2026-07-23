import pandas as pd

from analytics import data_gateway as dg


def _tushare_cfg():
    return {
        "provider": "tushare",
        "tushare_enabled": True,
        "tushare_mode": "replay",
        "tushare_token": "tok",
        "fallback_enabled": True,
    }


def test_unified_gateway_routes_extended_sources_to_tushare(monkeypatch):
    calls = []
    monkeypatch.setattr(dg.datasource_config, "get_internal_config", lambda: _tushare_cfg())
    monkeypatch.setattr(dg.tushare_provider, "fetch_stock_sectors_bulk", lambda codes, **kwargs: calls.append("sectors") or {"000001": {"industry": "银行", "concepts": ["金融科技"]}})
    monkeypatch.setattr(dg.tushare_provider, "fetch_lhb_stocks", lambda trade_date, **kwargs: calls.append("lhb") or [{"code": "000001", "name": "平安银行"}])
    monkeypatch.setattr(dg.tushare_provider, "fetch_lhb_seats", lambda code, trade_date, **kwargs: calls.append("seats") or [{"name": "测试席位", "net": 1}])
    monkeypatch.setattr(dg.tushare_provider, "fetch_index_kline", lambda code, **kwargs: calls.append("index_kline") or pd.DataFrame({"date": ["2026-07-17"], "close": [1.0]}))
    monkeypatch.setattr(dg.tushare_provider, "fetch_index_quotes", lambda pairs, **kwargs: calls.append("index_quotes") or [{"code": "000001", "price": 1.0}])
    monkeypatch.setattr(dg.tushare_provider, "fetch_finance_info", lambda code, **kwargs: calls.append("finance") or {"code": code, "pe_ttm": 12.0})

    assert dg.get_stock_sectors_bulk(["000001"])["000001"]["industry"] == "银行"
    assert dg.get_lhb_stocks_result("20260717")["source"] == "tushare"
    assert dg.get_lhb_seats("000001", "2026-07-17")[0]["name"] == "测试席位"
    assert dg.get_index_kline_result("000001", days=1)["source"] == "tushare"
    assert dg.get_index_quotes_result([(1, "000001")])["source"] == "tushare"
    assert dg.get_finance_info("000001")["pe_ttm"] == 12.0
    assert calls == ["sectors", "lhb", "seats", "index_kline", "index_quotes", "finance"]


def test_unified_gateway_routes_limit_pools_and_sector_rankings_to_tushare(monkeypatch):
    calls = []
    monkeypatch.setattr(dg.datasource_config, "get_internal_config", lambda: _tushare_cfg())
    monkeypatch.setattr(
        dg.tushare_provider,
        "fetch_limit_pools",
        lambda trade_date, **kwargs: calls.append(("limit", trade_date)) or {"zt_count": 2, "dt_count": 1, "zb_count": 0, "ladder": [{"height": 1, "stocks": []}]},
    )
    monkeypatch.setattr(
        dg.tushare_provider,
        "fetch_sector_rankings",
        lambda trade_date=None, **kwargs: calls.append(("sector", trade_date)) or {"industry_top": [{"name": "bank", "pct": 1.2}], "concept_top": []},
    )

    limit_result = dg.get_limit_pools_result("2026-07-17")
    sector_result = dg.get_sector_rankings_result("20260717")

    assert limit_result["ok"] is True
    assert limit_result["source"] == "tushare"
    assert limit_result["data"]["zt_count"] == 2
    assert sector_result["ok"] is True
    assert sector_result["source"] == "tushare"
    assert sector_result["data"]["industry_top"][0]["name"] == "bank"
    assert calls == [("limit", "20260717"), ("sector", "20260717")]


def test_unified_gateway_uses_current_only_when_config_selects_current(monkeypatch):
    monkeypatch.setattr(
        dg.datasource_config,
        "get_internal_config",
        lambda: {**_tushare_cfg(), "provider": "current", "fallback_enabled": False},
    )
    monkeypatch.setattr(
        dg.tushare_provider,
        "fetch_limit_pools",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("tushare must not be used when current-only is selected")),
    )
    monkeypatch.setattr(dg, "_current_limit_pools", lambda trade_date: {"zt_count": 1, "source": "current_akshare"})

    result = dg.get_limit_pools_result("20260717")

    assert result["ok"] is True
    assert result["source"] == "current"
    assert result["data"]["zt_count"] == 1
