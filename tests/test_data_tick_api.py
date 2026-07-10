from flask import Flask


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


def test_api_second_kline_defaults_to_quote_trade_date(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(data_web.bp)
    calls = []

    monkeypatch.setattr(data_web.md, "get_order_book", lambda code: {"trade_time": "2026-07-09 15:00:00"})

    def fake_ticks(code, date):
        calls.append((code, date))
        return {
            "datetime": ["2026-07-09 09:30:01"],
            "price": [10.50],
            "volume": [800.0],
            "amount": [8400.0],
            "prev_price": [10.49],
            "kind": ["U"],
        }

    monkeypatch.setattr(data_web.md, "get_second_ticks", fake_ticks)

    resp = app.test_client().get("/api/kline/000001/seconds")

    assert resp.status_code == 200
    assert calls == [("000001", "20260709")]


def test_api_minute_kline_defaults_to_quote_trade_date(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(data_web.bp)
    calls = []

    monkeypatch.setattr(data_web.md, "get_order_book", lambda code: {"trade_time": "2026-07-09 15:00:00"})

    def fake_minute(code, date):
        calls.append((code, date))
        return {
            "datetime": ["2026-07-09 10:01:00"],
            "open": [10.4],
            "high": [10.5],
            "low": [10.3],
            "close": [10.45],
            "volume": [1000.0],
        }

    monkeypatch.setattr(data_web.md, "get_minute_kline", fake_minute)

    resp = app.test_client().get("/api/kline/000001/minute")

    assert resp.status_code == 200
    assert calls == [("000001", "20260709")]
