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
        return {
            "ok": True,
            "date": "2026-07-08",
            "stocks": [{"code": "000001", "branches": [{"name": "机构专用"}]}],
        }

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


def test_preload_today_keeps_retrying_until_seat_details_are_ready(monkeypatch):
    monkeypatch.setattr(
        lhb_engine,
        "get_lhb_data",
        lambda date=None, force_refresh=False: {
            "ok": True,
            "date": "2026-07-08",
            "stocks": [{"code": "000001", "branches": []}, {"code": "000002"}],
        },
    )

    assert lhb_engine.preload_today() is False


def test_fetch_lhb_stocks_merges_duplicate_akshare_rows(monkeypatch):
    class FakeDf:
        empty = False
        columns = [
            "代码",
            "名称",
            "涨跌幅",
            "净额汇总",
            "买入额汇总",
            "卖出额汇总",
            "收盘价",
            "换手率",
            "上榜原因",
            "成交额",
        ]

        def iterrows(self):
            rows = [
                {
                    "代码": "000001",
                    "名称": "平安银行",
                    "涨跌幅": 10.01,
                    "净额汇总": 1200000,
                    "买入额汇总": 5000000,
                    "卖出额汇总": 3800000,
                    "收盘价": 12.34,
                    "换手率": 8.9,
                    "上榜原因": "日涨幅偏离值达7%",
                    "成交额": 8800000,
                },
                {
                    "代码": "000001",
                    "名称": "平安银行",
                    "涨跌幅": 10.01,
                    "净额汇总": 900000,
                    "买入额汇总": 4200000,
                    "卖出额汇总": 3300000,
                    "收盘价": 12.34,
                    "换手率": 8.9,
                    "上榜原因": "连续三个交易日内涨幅偏离值累计达20%",
                    "成交额": 7600000,
                },
            ]
            for idx, row in enumerate(rows):
                yield idx, row

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        types.SimpleNamespace(stock_lhb_detail_em=lambda *args, **kwargs: FakeDf()),
    )
    monkeypatch.setattr(lhb_engine, "no_proxy", lambda: nullcontext())

    rows = lhb_engine._fetch_lhb_stocks("20260708")

    assert len(rows) == 1
    assert rows[0]["code"] == "000001"
    assert rows[0]["net_amt"] == 120.0
    assert "日涨幅偏离值达7%" in rows[0]["reason"]
    assert "连续三个交易日" in rows[0]["reason"]


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
