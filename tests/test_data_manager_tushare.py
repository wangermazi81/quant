import pandas as pd

import analytics.data_manager as dm


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
        "provider": "tushare", "tushare_enabled": True, "fallback_enabled": True, "tushare_token": "tok"
    })
    monkeypatch.setattr(dm.tushare_provider, "fetch_kline_qfq", lambda code, start, end, token=None: _frame())
    monkeypatch.setattr(dm.ak, "stock_zh_a_hist_tx", lambda **kwargs: (_ for _ in ()).throw(AssertionError("current source should not be used")))

    result = dm._download_one("000001", "2026-07-01")

    assert result["ok"] is True
    saved = pd.read_csv(tmp_path / "000001.csv")
    assert len(saved) == 2


def test_download_one_falls_back_when_tushare_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(dm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dm, "META_FILE", tmp_path / "metadata.json")
    monkeypatch.setattr(dm, "get_stock_name", lambda code: "平安银行")
    monkeypatch.setattr(dm.datasource_config, "get_internal_config", lambda: {
        "provider": "tushare", "tushare_enabled": True, "fallback_enabled": True, "tushare_token": "tok"
    })
    monkeypatch.setattr(dm.tushare_provider, "fetch_kline_qfq", lambda code, start, end, token=None: None)
    monkeypatch.setattr(dm.ak, "stock_zh_a_hist_tx", lambda **kwargs: _frame(20.0))

    result = dm._download_one("000001", "2026-07-01")

    assert result["ok"] is True
    saved = pd.read_csv(tmp_path / "000001.csv")
    assert float(saved["close"].iloc[-1]) == 20.1

