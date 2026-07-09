# -*- coding: utf-8 -*-
"""Tushare Pro adapter used by the selectable datasource layer."""

from __future__ import annotations

import importlib
import os
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests

try:
    import datasource_config
except Exception:  # pragma: no cover - package import fallback for tests/tools
    from . import datasource_config  # type: ignore


DEFAULT_REPLAY_BASE_URLS = (
    "https://ai-tool.indevs.in/tushare/pro",
    "https://tushare.indevs.in/tushare/pro",
    "https://ai-tool.indevs.in/quant/tushare/pro",
)
REPLAY_BASE_URL_ENV = "TUSHARE_REPLAY_BASE_URL"
VALID_MODES = {"replay", "sdk"}


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
        raise RuntimeError("Tushare SDK not installed; install tushare from requirements.txt") from exc


def _mode(value: str | None = None) -> str:
    raw = value or datasource_config.get_internal_config().get("tushare_mode") or "replay"
    mode = str(raw).strip().lower()
    return mode if mode in VALID_MODES else "replay"


def _replay_base_urls() -> tuple[str, ...]:
    configured = os.getenv(REPLAY_BASE_URL_ENV, "").strip().rstrip("/")
    if configured:
        return (configured,)
    return DEFAULT_REPLAY_BASE_URLS


def _extract_rows(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        rows = data.get("items") or data.get("rows") or data.get("data") or []
        fields = data.get("fields") or []
        if rows and fields and all(not isinstance(row, dict) for row in rows):
            return [dict(zip(fields, row)) for row in rows]
        return [row for row in rows if isinstance(row, dict)]
    return []


def _replay_get(api_name: str, token: str, params: dict | None = None, timeout: int = 25) -> list[dict]:
    headers = {"X-API-Key": token, "Accept": "application/json"}
    errors = []
    for base_url in _replay_base_urls():
        url = f"{base_url.rstrip('/')}/{api_name}"
        try:
            resp = requests.get(url, params=params or {}, headers=headers, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict) and int(payload.get("code") or 0) not in (0, 200):
                raise RuntimeError(str(payload.get("msg") or payload.get("message") or payload.get("error") or payload))
            return _extract_rows(payload)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("; ".join(errors))


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


def _apply_qfq(daily: pd.DataFrame, factors: pd.DataFrame | None) -> pd.DataFrame:
    if factors is None or factors.empty or "adj_factor" not in factors.columns:
        return daily
    data = daily.copy()
    fac = factors[["trade_date", "adj_factor"]].copy()
    fac["adj_factor"] = pd.to_numeric(fac["adj_factor"], errors="coerce")
    fac = fac.dropna(subset=["trade_date", "adj_factor"])
    if fac.empty:
        return data
    data = data.merge(fac, on="trade_date", how="left")
    data = data.sort_values("trade_date").reset_index(drop=True)
    latest = data["adj_factor"].dropna().iloc[-1] if not data["adj_factor"].dropna().empty else None
    if not latest or latest <= 0:
        return data.drop(columns=["adj_factor"], errors="ignore")
    ratio = data["adj_factor"] / latest
    for col in ("open", "high", "low", "close"):
        data[col] = pd.to_numeric(data[col], errors="coerce") * ratio
    return data.drop(columns=["adj_factor"], errors="ignore")


def _fetch_replay_kline_qfq(code: str, start: str, end: str, token: str) -> pd.DataFrame | None:
    ts_code = _ts_code(code)
    start_date = _date_text(start)
    end_date = _date_text(end)
    daily_rows = _replay_get(
        "daily",
        token,
        {
            "ts_code": ts_code,
            "start_date": start_date,
            "end_date": end_date,
            "fields": "ts_code,trade_date,open,high,low,close,vol",
        },
    )
    if not daily_rows:
        return None
    daily = pd.DataFrame(daily_rows)
    try:
        factor_rows = _replay_get(
            "adj_factor",
            token,
            {
                "ts_code": ts_code,
                "start_date": start_date,
                "end_date": end_date,
                "fields": "ts_code,trade_date,adj_factor",
            },
        )
        daily = _apply_qfq(daily, pd.DataFrame(factor_rows))
    except Exception:
        pass
    return _normalize_daily_frame(daily)


def fetch_kline_qfq(
    code: str,
    start: str,
    end: str,
    token: str | None = None,
    mode: str | None = None,
) -> pd.DataFrame | None:
    resolved_mode = _mode(mode)
    token = token or datasource_config.resolve_tushare_token(mode=resolved_mode)
    if not token:
        return None
    if resolved_mode == "replay":
        return _fetch_replay_kline_qfq(code, start, end, token)

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


def test_connection(token: str | None = None, mode: str | None = None) -> dict:
    resolved_mode = _mode(mode)
    token = token or datasource_config.resolve_tushare_token(mode=resolved_mode)
    if not token:
        return {"ok": False, "error": "Tushare Token/API Key is not configured"}
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=45)).strftime("%Y%m%d")
        if resolved_mode == "replay":
            rows = _replay_get(
                "daily",
                token,
                {
                    "ts_code": "000001.SZ",
                    "start_date": start,
                    "end_date": end,
                    "fields": "ts_code,trade_date,open,high,low,close,vol",
                    "limit": 5,
                },
            )
            return {"ok": True, "rows": len(rows), "sample": "000001.SZ", "mode": "replay"}

        ts = _load_tushare()
        pro = ts.pro_api(token)
        df = pro.daily(ts_code="000001.SZ", start_date=start, end_date=end)
        rows = 0 if df is None else len(df)
        return {"ok": True, "rows": rows, "sample": "000001.SZ", "mode": "sdk"}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "mode": resolved_mode}
