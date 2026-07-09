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
VALID_MINUTE_FREQS = {"1MIN", "5MIN", "15MIN", "30MIN", "60MIN"}
REALTIME_REPLAY_TIMEOUT = 4


def _ts_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    suffix = ""
    if "." in raw:
        raw_code, raw_suffix = raw.rsplit(".", 1)
        if raw_suffix in {"SZ", "SH", "BJ"}:
            suffix = raw_suffix
        raw = raw_code
    elif raw[:2] in {"SZ", "SH", "BJ"}:
        suffix = raw[:2]

    digits = "".join(ch for ch in raw if ch.isdigit())
    c = (digits[-6:] if digits else raw[-6:]).zfill(6)
    if suffix:
        return f"{c}.{suffix}"
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




def _num(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _plain_code(ts_code: str) -> str:
    raw = str(ts_code or "").strip()
    if "." in raw:
        raw = raw.split(".", 1)[0]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[-6:] if digits else raw[-6:]

def _replay_get(
    api_name: str,
    token: str,
    params: dict | None = None,
    timeout: int = 25,
    max_attempts: int | None = None,
) -> list[dict]:
    headers = {"X-API-Key": token, "Accept": "application/json"}
    errors = []
    base_urls = _replay_base_urls()
    if max_attempts is not None:
        base_urls = base_urls[:max(1, int(max_attempts))]
    for base_url in base_urls:
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




def _normalize_minute_frame(df: pd.DataFrame | None) -> pd.DataFrame:
    columns = ["datetime", "open", "high", "low", "close", "volume", "amount", "is_complete"]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)
    rename = {
        "time": "datetime",
        "trade_time": "datetime",
        "vol": "volume",
    }
    data = df.rename(columns={k: v for k, v in rename.items() if k in df.columns}).copy()
    data = data.loc[:, ~data.columns.duplicated()]
    required = ["datetime", "open", "high", "low", "close", "volume"]
    if any(c not in data.columns for c in required):
        return pd.DataFrame(columns=columns)
    data["datetime"] = pd.to_datetime(data["datetime"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce")
    if "amount" not in data.columns:
        data["amount"] = 0.0
    if "is_complete" not in data.columns:
        data["is_complete"] = True
    data = data.dropna(subset=["datetime", "close"]).sort_values("datetime").reset_index(drop=True)
    return data[columns]



def _normalize_replay_quote(row: dict) -> dict:
    ts_code = str(row.get("ts_code") or row.get("code") or "")
    price = _num(row.get("price", row.get("close", row.get("close_price"))))
    last_close = _num(row.get("pre_price", row.get("pre_close", row.get("last_close"))))
    change_pct = _num(row.get("change_pct"))
    if not change_pct and last_close:
        change_pct = round((price - last_close) / last_close * 100, 4)
    data = {
        "code": _plain_code(ts_code),
        "ts_code": ts_code or _ts_code(str(row.get("code") or "")),
        "name": str(row.get("name") or ""),
        "trade_time": str(row.get("trade_time") or row.get("time") or ""),
        "price": price,
        "last_close": last_close,
        "open": _num(row.get("open_price", row.get("open"))),
        "high": _num(row.get("high_price", row.get("high"))),
        "low": _num(row.get("low_price", row.get("low"))),
        "vol": _num(row.get("volume", row.get("vol"))),
        "amount": _num(row.get("amount")),
        "change_pct": change_pct,
        "source": "tushare_rt_tick",
        "level": "L1-5",
    }
    for level in range(1, 6):
        data[f"bid{level}"] = _num(row.get(f"bid_price{level}"))
        data[f"bid{level}_vol"] = _num(row.get(f"bid_volume{level}"))
        data[f"ask{level}"] = _num(row.get(f"ask_price{level}"))
        data[f"ask{level}_vol"] = _num(row.get(f"ask_volume{level}"))
    return data


def _normalize_tick_frame(df: pd.DataFrame | None) -> pd.DataFrame:
    columns = ["datetime", "price", "volume", "amount", "prev_price", "kind"]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)
    rename = {"time": "datetime", "vol": "volume"}
    data = df.rename(columns={k: v for k, v in rename.items() if k in df.columns}).copy()
    if "datetime" not in data.columns or "price" not in data.columns:
        return pd.DataFrame(columns=columns)
    data["datetime"] = pd.to_datetime(data["datetime"], errors="coerce")
    for col in ("price", "volume", "amount", "prev_price"):
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce")
        else:
            data[col] = 0.0
    if "kind" not in data.columns:
        data["kind"] = ""
    data = data.dropna(subset=["datetime", "price"]).sort_values("datetime").reset_index(drop=True)
    return data[columns]


def _five_level_side(row: dict, side: str) -> list[dict]:
    prefix = "bid" if side == "bid" else "ask"
    return [
        {
            "level": level,
            "price": _num(row.get(f"{prefix}{level}")),
            "volume": _num(row.get(f"{prefix}{level}_vol")),
        }
        for level in range(1, 6)
    ]


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


def fetch_realtime_quotes(
    codes: list[str],
    token: str | None = None,
    mode: str | None = None,
) -> list[dict]:
    resolved_mode = _mode(mode)
    token = token or datasource_config.resolve_tushare_token(mode=resolved_mode)
    if not token or resolved_mode != "replay":
        return []
    ts_codes = ",".join(_ts_code(code) for code in codes if str(code or "").strip())
    if not ts_codes:
        return []
    rows = _replay_get("rt_tick", token, {"ts_code": ts_codes}, timeout=REALTIME_REPLAY_TIMEOUT, max_attempts=1)
    return [_normalize_replay_quote(row) for row in rows]



def fetch_tick_series(
    code: str,
    trade_date: str | None = None,
    limit: int | None = 240,
    token: str | None = None,
    mode: str | None = None,
) -> pd.DataFrame:
    resolved_mode = _mode(mode)
    token = token or datasource_config.resolve_tushare_token(mode=resolved_mode)
    if not token or resolved_mode != "replay":
        return _normalize_tick_frame(None)
    params: dict[str, Any] = {"ts_code": _ts_code(code)}
    if trade_date:
        params["trade_date"] = _date_text(trade_date)
    if limit:
        params["limit"] = int(limit)
    rows = _replay_get("a_share_ticks", token, params, timeout=REALTIME_REPLAY_TIMEOUT, max_attempts=1)
    return _normalize_tick_frame(pd.DataFrame(rows))


def fetch_order_book(
    code: str,
    token: str | None = None,
    mode: str | None = None,
) -> dict:
    rows = fetch_realtime_quotes([code], token=token, mode=mode)
    if not rows:
        return {}
    row = rows[0]
    return {
        "code": row.get("code", _plain_code(str(row.get("ts_code") or code))),
        "ts_code": row.get("ts_code") or _ts_code(code),
        "name": row.get("name", ""),
        "trade_time": row.get("trade_time", ""),
        "price": _num(row.get("price")),
        "last_close": _num(row.get("last_close")),
        "open": _num(row.get("open")),
        "high": _num(row.get("high")),
        "low": _num(row.get("low")),
        "amount": _num(row.get("amount")),
        "change_pct": _num(row.get("change_pct")),
        "bids": _five_level_side(row, "bid"),
        "asks": _five_level_side(row, "ask"),
        "source": row.get("source", "tushare_rt_tick"),
        "level": row.get("level", "L1-5"),
    }


def fetch_minute_kline(
    code: str,
    freq: str = "1MIN",
    limit: int | None = None,
    token: str | None = None,
    mode: str | None = None,
) -> pd.DataFrame:
    resolved_mode = _mode(mode)
    token = token or datasource_config.resolve_tushare_token(mode=resolved_mode)
    if not token:
        return _normalize_minute_frame(None)
    freq_key = str(freq or "1MIN").strip().upper()
    if freq_key not in VALID_MINUTE_FREQS:
        freq_key = "1MIN"
    ts_code = _ts_code(code)
    if resolved_mode == "replay":
        params: dict[str, Any] = {"ts_code": ts_code, "freq": freq_key}
        rows = _replay_get("rt_min", token, params, timeout=REALTIME_REPLAY_TIMEOUT, max_attempts=1)
        return _normalize_minute_frame(pd.DataFrame(rows))

    ts = _load_tushare()
    api = ts.pro_api(token)
    sdk_freq = freq_key.lower().replace("min", "min")
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
    df = ts.pro_bar(ts_code=ts_code, api=api, start_date=start, end_date=end, freq=sdk_freq, asset="E")
    if limit and df is not None and not df.empty:
        df = df.tail(int(limit))
    return _normalize_minute_frame(df)


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
