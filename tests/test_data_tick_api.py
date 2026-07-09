from flask import Flask

import _pathsetup  # noqa: F401

import web.data as data_web


def test_api_second_kline_returns_tick_series(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(data_web.bp)
    monkeypatch.setattr(data_web.md, "get_second_ticks", lambda code, date: {
        "datetime": ["2026-07-09 09:30:01", "2026-07-09 09:30:02"],
        "price": [10.50, 10.51],
        "volume": [800.0, 1200.0],
        "amount": [8400.0, 12612.0],
        "prev_price": [10.49, 10.50],
        "kind": ["D", "U"],
    })

    resp = app.test_client().get("/api/kline/000001/seconds?date=20260709")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["source"] == "tushare_ticks"
    assert payload["data"] == [
        {
            "date": "09:30:01",
            "datetime": "2026-07-09 09:30:01",
            "price": 10.50,
            "volume": 800.0,
            "amount": 8400.0,
            "prev_price": 10.49,
            "kind": "D",
        },
        {
            "date": "09:30:02",
            "datetime": "2026-07-09 09:30:02",
            "price": 10.51,
            "volume": 1200.0,
            "amount": 12612.0,
            "prev_price": 10.50,
            "kind": "U",
        },
    ]


def test_api_order_book_returns_five_levels(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(data_web.bp)
    monkeypatch.setattr(data_web.md, "get_order_book", lambda code: {
        "code": "000001",
        "ts_code": "000001.SZ",
        "name": "Ping An",
        "trade_time": "2026-07-09 10:01:02",
        "price": 10.49,
        "last_close": 10.60,
        "change_pct": -1.0377,
        "bids": [{"level": 1, "price": 10.49, "volume": 9100.0}],
        "asks": [{"level": 1, "price": 10.50, "volume": 6800.0}],
        "source": "tushare_rt_tick",
    })

    resp = app.test_client().get("/api/quote/000001/order_book")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["data"]["bids"][0]["price"] == 10.49
    assert payload["data"]["asks"][0]["volume"] == 6800.0
