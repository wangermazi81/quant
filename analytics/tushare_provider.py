# -*- coding: utf-8 -*-
"""Tushare Pro adapter used by the selectable datasource layer."""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta

import pandas as pd

try:
    import datasource_config
except Exception:  # pragma: no cover - package import fallback for tests/tools
    from . import datasource_config  # type: ignore


def _ts_code(code: str) -> str:
    c = str(code or "").strip().zfill(6)
    if c.startswith(("4", "8", "920")):
        return f"{c}.BJ"
    if c.startswith(("5", "6", "9")):
        return f"{c}.SH"
    return f"{c}.SZ"


def _date_text(value: str) -> str:
    return str(value or "").replace("-", "")[:8]


def _load_tushare():
    try:
        return importlib.import_module("tushare")
    except Exception as exc:
        raise RuntimeError("Tushare SDK 未安装，请先安装 requirements.txt 中的 tushare") from exc


def _normalize_daily_frame(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    rename = {
        "trade_date": "date",
        "vol": "volume",
        "turnover_vol": "volume",
    }
    data = df.rename(columns={k: v for k, v in rename.items() if k in df.columns}).copy()
    required = ["date", "open", "high", "low", "close", "volume"]
    if any(c not in data.columns for c in required):
        return None
    data = data[required]
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume"):
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    return data if not data.empty else None


def fetch_kline_qfq(code: str, start: str, end: str, token: str | None = None) -> pd.DataFrame | None:
    token = token or datasource_config.resolve_tushare_token()
    if not token:
        return None
    ts = _load_tushare()
    api = ts.pro_api(token)
    df = ts.pro_bar(
        ts_code=_ts_code(code),
        api=api,
        start_date=_date_text(start),
        end_date=_date_text(end),
        adj="qfq",
        freq="D",
        asset="E",
    )
    return _normalize_daily_frame(df)


def test_connection(token: str | None = None) -> dict:
    token = token or datasource_config.resolve_tushare_token()
    if not token:
        return {"ok": False, "error": "未配置 Tushare Token"}
    try:
        ts = _load_tushare()
        pro = ts.pro_api(token)
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=45)).strftime("%Y%m%d")
        df = pro.daily(ts_code="000001.SZ", start_date=start, end_date=end)
        rows = 0 if df is None else len(df)
        return {"ok": True, "rows": rows, "sample": "000001.SZ"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
