import sys
import types
from datetime import datetime

import pandas as pd
import pytest

import analytics.data_manager as dm


def _trusted_security_master_rows() -> list[dict]:
    rows = [
        {
            "code": "000001",
            "ts_code": "000001.SZ",
            "list_date": "19910403",
            "delist_date": "",
            "list_status": "L",
        },
        {
            "code": "000002",
            "ts_code": "000002.SZ",
            "list_date": "19910129",
            "delist_date": "20200101",
            "list_status": "D",
        },
        {
            "code": "600000",
            "ts_code": "600000.SH",
            "list_date": "19991110",
            "delist_date": "",
            "list_status": "L",
        },
        {
            "code": "600001",
            "ts_code": "600001.SH",
            "list_date": "19980122",
            "delist_date": "20091229",
            "list_status": "D",
        },
    ]
    for row in rows:
        row.update(
            {
                "lifecycle_source": "baostock_stock_basic",
                "lifecycle_snapshot_complete": True,
                "universe_scope": "sh_sz_a_share",
                "snapshot_at": "2026-07-30T17:10:00",
                "security_master_pagination_complete": True,
                "security_master_page_requests": 1,
                "security_master_row_count": 4,
                "security_master_snapshot_complete": True,
                "security_master_snapshot_trade_date": "20260730",
            }
        )
    return rows


@pytest.mark.parametrize(
    "case",
    [
        "provider_error",
        "duplicate_code",
        "missing_delist_date",
    ],
)
def test_refresh_security_master_fails_closed_before_overwrite(
    case,
    monkeypatch,
    tmp_path,
):
    master_path = tmp_path / "security_master.csv"
    master_path.write_text("sentinel", encoding="utf-8")
    rows = _trusted_security_master_rows()
    if case == "duplicate_code":
        rows.append(dict(rows[0]))
    elif case == "missing_delist_date":
        rows[1]["delist_date"] = ""

    def fake_fetch_security_master(**kwargs):
        if case == "provider_error":
            raise RuntimeError("baostock lifecycle unavailable")
        return rows

    monkeypatch.setattr(dm, "DATA_DIR", tmp_path / "daily")
    monkeypatch.setattr(dm, "SECURITY_MASTER_FILE", master_path)
    monkeypatch.setattr(
        dm.datasource_config,
        "get_internal_config",
        lambda: {
            "tushare_token": "tok",
            "tushare_mode": "replay",
        },
    )
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_security_master",
        fake_fetch_security_master,
    )
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid master must fail before history fetch")
        ),
    )

    result = dm.refresh_factor_security_master_history(
        days=120,
        n_stocks=0,
        now=datetime(2026, 7, 30, 17, 10),
        token="tok",
        mode="replay",
    )

    assert result["ok"] is False
    assert "security master" in result["error"].lower()
    assert master_path.read_text(encoding="utf-8") == "sentinel"


def _frame(close=10.0):
    return pd.DataFrame({
        "date": pd.to_datetime(["2026-07-07", "2026-07-08"]),
        "open": [close - 0.2, close],
        "high": [close + 0.2, close + 0.3],
        "low": [close - 0.4, close - 0.1],
        "close": [close, close + 0.1],
        "volume": [1000, 1100],
    })


def test_download_one_uses_tushare_when_selected(monkeypatch, tmp_path):
    monkeypatch.setattr(dm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    monkeypatch.setattr(dm, "get_stock_name", lambda code: "平安银行")
    monkeypatch.setattr(dm.datasource_config, "get_internal_config", lambda: {
        "provider": "tushare", "tushare_mode": "replay", "tushare_enabled": True, "fallback_enabled": True, "tushare_token": "tok"
    })
    monkeypatch.setattr(dm.tushare_provider, "fetch_kline_qfq", lambda code, start, end, token=None, mode=None: _frame())
    monkeypatch.setattr(dm.ak, "stock_zh_a_hist_tx", lambda **kwargs: (_ for _ in ()).throw(AssertionError("current source should not be used")))

    result = dm._download_one("000001", "2026-07-01")

    assert result["ok"] is True
    saved = pd.read_csv(tmp_path / "000001.csv")
    assert len(saved) == 2


def test_download_one_preserves_tushare_turnover_amount(monkeypatch, tmp_path):
    monkeypatch.setattr(dm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    monkeypatch.setattr(dm, "get_stock_name", lambda code: "Ping An Bank")
    monkeypatch.setattr(
        dm.datasource_config,
        "get_internal_config",
        lambda: {
            "provider": "tushare",
            "tushare_mode": "replay",
            "tushare_enabled": True,
            "fallback_enabled": True,
            "tushare_token": "tok",
        },
    )
    source = _frame()
    source["amount"] = [123456.7, 234567.8]
    monkeypatch.setattr(
        dm.tushare_provider,
        "fetch_kline_qfq",
        lambda code, start, end, token=None, mode=None: source,
    )
    monkeypatch.setattr(
        dm.ak,
        "stock_zh_a_hist_tx",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("current source should not be used")),
    )

    result = dm._download_one("000001", "2026-07-01")
    saved = pd.read_csv(tmp_path / "000001.csv")

    assert result["ok"] is True
    assert saved["volume"].tolist() == [1000, 1100]
    assert saved["amount"].tolist() == [123456.7, 234567.8]


def test_baostock_qfq_normalizes_volume_and_amount_to_tushare_units(monkeypatch):
    captured = {}

    class FakeResult:
        error_code = "0"

        def __init__(self):
            self.rows = [
                [
                    "2026-07-30",
                    "10.00",
                    "10.50",
                    "9.92",
                    "9.98",
                    "12734608",
                    "129687873.56",
                ]
            ]
            self.index = -1

        def next(self):
            self.index += 1
            return self.index < len(self.rows)

        def get_row_data(self):
            return self.rows[self.index]

    def fake_query(code, fields, **kwargs):
        captured.update(
            {
                "code": code,
                "fields": fields,
                **kwargs,
            }
        )
        return FakeResult()

    fake_bs = types.SimpleNamespace(
        login=lambda: types.SimpleNamespace(error_code="0"),
        logout=lambda: None,
        query_history_k_data_plus=fake_query,
    )
    monkeypatch.setitem(sys.modules, "baostock", fake_bs)

    frame = dm._fetch_kline_baostock(
        "000014",
        "2026-07-01",
        "2026-07-30",
    )

    assert captured["code"] == "sz.000014"
    assert captured["adjustflag"] == "2"
    assert captured["fields"] == "date,open,high,low,close,volume,amount"
    assert frame["volume"].tolist() == pytest.approx([127346.08])
    assert frame["amount"].tolist() == pytest.approx([129687.87356])


def test_download_one_falls_back_when_tushare_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(dm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    monkeypatch.setattr(dm, "get_stock_name", lambda code: "平安银行")
    monkeypatch.setattr(dm.datasource_config, "get_internal_config", lambda: {
        "provider": "tushare", "tushare_mode": "replay", "tushare_enabled": True, "fallback_enabled": True, "tushare_token": "tok"
    })
    monkeypatch.setattr(dm.tushare_provider, "fetch_kline_qfq", lambda code, start, end, token=None, mode=None: None)
    monkeypatch.setattr(dm.ak, "stock_zh_a_hist_tx", lambda **kwargs: _frame(20.0))

    result = dm._download_one("000001", "2026-07-01")

    assert result["ok"] is True
    saved = pd.read_csv(tmp_path / "000001.csv")
    assert float(saved["close"].iloc[-1]) == 20.1

