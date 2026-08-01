import pandas as pd

from analytics import data_gateway as gateway


def _config(**overrides):
    data = {
        "provider": "agudata",
        "agudata_enabled": True,
        "agudata_api_key": "key",
        "agudata_base_url": "https://data.example/openapi/v1",
        "tushare_enabled": True,
        "tushare_token": "old-key",
        "tushare_mode": "replay",
        "fallback_enabled": True,
    }
    data.update(overrides)
    return data


def test_agudata_is_primary_and_current_is_fallback(monkeypatch):
    monkeypatch.setattr(
        gateway.datasource_config,
        "get_internal_config",
        lambda: _config(),
    )

    assert gateway.get_source_order() == ["agudata", "current"]


def test_realtime_quotes_use_agudata_primary(monkeypatch):
    calls = []
    monkeypatch.setattr(
        gateway.datasource_config,
        "get_internal_config",
        lambda: _config(),
    )
    monkeypatch.setattr(
        gateway.agudata_provider,
        "fetch_realtime_quotes",
        lambda codes, **kwargs: calls.append((codes, kwargs))
        or [{"code": "000001", "price": 12.0}],
    )
    monkeypatch.setattr(
        gateway,
        "_current_quotes",
        lambda codes: (_ for _ in ()).throw(
            AssertionError("fallback should not run")
        ),
    )

    result = gateway.get_realtime_quotes_result(["000001"])

    assert result["ok"] is True
    assert result["source"] == "agudata"
    assert result["fallback_used"] is False
    assert calls[0][0] == ["000001"]
    assert calls[0][1]["api_key"] == "key"


def test_order_book_skips_unsupported_agudata_and_falls_back(monkeypatch):
    monkeypatch.setattr(
        gateway.datasource_config,
        "get_internal_config",
        lambda: _config(),
    )
    monkeypatch.setattr(
        gateway,
        "_current_quote",
        lambda code: {
            "code": code,
            "price": 12.0,
            "bids": [{"price": 11.99, "volume": 100}],
            "asks": [{"price": 12.01, "volume": 200}],
        },
    )

    result = gateway.get_order_book_result("000001")

    assert result["ok"] is True
    assert result["source"] == "current"
    assert result["fallback_used"] is True


def test_minute_kline_uses_agudata(monkeypatch):
    monkeypatch.setattr(
        gateway.datasource_config,
        "get_internal_config",
        lambda: _config(),
    )
    expected = pd.DataFrame(
        {
            "datetime": pd.to_datetime(["2026-07-31 09:30:00"]),
            "close": [12.0],
        }
    )
    monkeypatch.setattr(
        gateway.agudata_provider,
        "fetch_minute_kline",
        lambda *args, **kwargs: expected,
    )

    result = gateway.get_minute_kline_result(
        "000001",
        trade_date="20260731",
    )

    assert result["ok"] is True
    assert result["source"] == "agudata"
    assert result["df"].equals(expected)
