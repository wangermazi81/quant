import sys
import types
from pathlib import Path
from contextlib import nullcontext

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

import lhb_engine


def test_lhb_api_refresh_forwards_force_refresh(monkeypatch):
    from flask import Flask
    import web.lhb as lhb_web

    calls = []

    def fake_get_lhb_data(date=None, force_refresh=False):
        calls.append((date, force_refresh))
        return {"ok": True, "date": date, "stocks": [], "total": 0}

    fake_engine = types.SimpleNamespace(
        get_lhb_data=fake_get_lhb_data,
        get_lhb_dates=lambda: ["2026-07-08"],
    )
    monkeypatch.setitem(sys.modules, "lhb_engine", fake_engine)

    app = Flask(__name__)
    app.register_blueprint(lhb_web.bp)

    payload = app.test_client().get("/api/lhb/data?date=2026-07-08&refresh=1").get_json()

    assert payload["ok"] is True
    assert calls == [("2026-07-08", True)]


def test_preload_today_force_refreshes_and_reports_success(monkeypatch):
    calls = []

    def fake_get_lhb_data(date=None, force_refresh=False):
        calls.append((date, force_refresh))
        return {"ok": True, "date": "2026-07-08", "stocks": [{"code": "000001"}]}

    monkeypatch.setattr(lhb_engine, "get_lhb_data", fake_get_lhb_data)

    assert lhb_engine.preload_today() is True
    assert calls == [(None, True)]


def test_preload_today_reports_pending_when_not_published(monkeypatch):
    monkeypatch.setattr(
        lhb_engine,
        "get_lhb_data",
        lambda date=None, force_refresh=False: {"ok": False, "date": "2026-07-08", "stocks": []},
    )

    assert lhb_engine.preload_today() is False


def test_fetch_lhb_stocks_falls_back_to_eastmoney_when_akshare_breaks(monkeypatch):
    def broken_lhb_detail(*args, **kwargs):
        raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        types.SimpleNamespace(stock_lhb_detail_em=broken_lhb_detail),
    )
    monkeypatch.setattr(lhb_engine, "no_proxy", lambda: nullcontext())

    class FakeResponse:
        def json(self):
            return {
                "result": {
                    "pages": 1,
                    "data": [
                        {
                            "SECURITY_CODE": "1",
                            "SECURITY_NAME_ABBR": "平安银行",
                            "CHANGE_RATE": 10.01,
                            "BILLBOARD_NET_AMT": 1200000,
                            "BILLBOARD_BUY_AMT": 5000000,
                            "BILLBOARD_SELL_AMT": 3800000,
                            "CLOSE_PRICE": 12.34,
                            "TURNOVERRATE": 8.9,
                            "EXPLANATION": "日涨幅偏离值达7%",
                            "BILLBOARD_DEAL_AMT": 8800000,
                        }
                    ],
                }
            }

    def fake_get(url, params=None, headers=None, timeout=None):
        assert params["reportName"] == "RPT_DAILYBILLBOARD_DETAILSNEW"
        assert "(TRADE_DATE<='2026-07-08')" in params["filter"]
        return FakeResponse()

    import requests

    monkeypatch.setattr(requests, "get", fake_get)

    rows = lhb_engine._fetch_lhb_stocks("20260708")

    assert rows == [
        {
            "code": "000001",
            "name": "平安银行",
            "chg": 10.01,
            "net_amt": 120.0,
            "buy_amt": 500.0,
            "sell_amt": 380.0,
            "close": 12.34,
            "turnover": 8.9,
            "reason": "日涨幅偏离值达7%",
            "vol": 0.09,
            "youzai": [],
            "branches": [],
        }
    ]
