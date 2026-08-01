# -*- coding: utf-8 -*-
"""Agudata HTTP market-data adapter.

API keys stay in server-side datasource configuration or environment
variables and are never included in exceptions.
"""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests

try:
    from analytics import datasource_config
except Exception:  # pragma: no cover - package import fallback
    from . import datasource_config  # type: ignore

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api-hk.agudata.cn/app-api/openapi/v1"
VALID_MINUTE_FREQS = {"1MIN", "5MIN", "15MIN", "30MIN", "60MIN"}
DEFAULT_PAGE_SIZE = 1000
DEFAULT_MAX_ROWS = 10000
FULL_MARKET_DAILY_PAGE_SIZE = 500
RATE_LIMIT_BACKOFF_SECONDS = (3.0, 10.0)


class AgudataError(RuntimeError):
    """Base error for Agudata requests."""


class AgudataPermissionError(AgudataError):
    """The API key lacks permission for an endpoint."""


class AgudataRateLimitError(AgudataError):
    """The request exceeded the provider rate limit."""


def _ts_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    suffix = ""
    if "." in raw:
        raw, suffix = raw.rsplit(".", 1)
        suffix = suffix if suffix in {"SZ", "SH", "BJ"} else ""
    elif raw[:2] in {"SZ", "SH", "BJ"}:
        suffix, raw = raw[:2], raw[2:]
    digits = "".join(ch for ch in raw if ch.isdigit())
    clean = (digits[-6:] if digits else raw[-6:]).zfill(6)
    if suffix:
        return f"{clean}.{suffix}"
    if clean.startswith(("4", "8", "920")):
        return f"{clean}.BJ"
    if clean.startswith(("5", "6", "9")):
        return f"{clean}.SH"
    return f"{clean}.SZ"


def _plain_code(value: str) -> str:
    raw = str(value or "").strip().upper()
    if "." in raw:
        raw = raw.split(".", 1)[0]
    if raw[:2] in {"SZ", "SH", "BJ"}:
        raw = raw[2:]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[-6:] if digits else raw[-6:]


def _date_text(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


def _num(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _resolved_key(api_key: str | None = None) -> str:
    return str(
        api_key
        or datasource_config.resolve_agudata_api_key()
        or os.getenv("AGUDATA_API_KEY", "")
    ).strip()


def _resolved_base_url(base_url: str | None = None) -> str:
    return str(
        base_url
        or os.getenv("AGUDATA_BASE_URL", "")
        or DEFAULT_BASE_URL
    ).strip().rstrip("/")


def _payload_error(payload: Any) -> tuple[int | None, str]:
    current = payload
    for _ in range(8):
        if not isinstance(current, dict):
            break
        raw_code = current.get("code")
        if raw_code not in (None, "", 0, "0", 200, "200"):
            try:
                code = int(raw_code)
            except (TypeError, ValueError):
                code = None
            message = str(
                current.get("message")
                or current.get("msg")
                or current.get("error")
                or f"Agudata business error {raw_code}"
            )
            return code, message
        current = current.get("data")
    return None, ""


def _extract_rows(payload: Any) -> list[dict]:
    current = payload
    for _ in range(10):
        if isinstance(current, list):
            return [row for row in current if isinstance(row, dict)]
        if not isinstance(current, dict):
            return []
        for key in ("items", "rows"):
            rows = current.get(key)
            if not isinstance(rows, list):
                continue
            fields = current.get("fields") or []
            if rows and fields and all(
                not isinstance(row, dict) for row in rows
            ):
                return [
                    dict(zip(fields, row))
                    for row in rows
                    if isinstance(row, (list, tuple))
                ]
            return [row for row in rows if isinstance(row, dict)]
        current = current.get("data")
    return []


def query_api(
    endpoint: str,
    params: dict | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: int = 30,
    session: Any | None = None,
) -> list[dict]:
    key = _resolved_key(api_key)
    if not key:
        raise AgudataPermissionError("Agudata API key is not configured")
    slug = str(endpoint or "").strip().strip("/")
    if not slug or any(part in slug for part in ("..", "?", "#")):
        raise ValueError("invalid Agudata endpoint")
    url = f"{_resolved_base_url(base_url)}/tushare/{slug}"
    try:
        get = session.get if session is not None else requests.get
        response = get(
            url,
            params=params or {},
            headers={"X-API-Key": key, "Accept": "application/json"},
            timeout=max(1, int(timeout)),
        )
    except requests.RequestException as exc:
        raise AgudataError(
            f"Agudata {slug} request failed: {exc}"
        ) from exc
    try:
        payload = response.json()
    except Exception as exc:
        raise AgudataError(
            f"Agudata {slug} returned non-JSON content "
            f"(HTTP {response.status_code})"
        ) from exc

    code, message = _payload_error(payload)
    http_status = int(getattr(response, "status_code", 0) or 0)
    effective_code = code or (http_status if http_status >= 400 else None)
    if effective_code in {401, 403}:
        raise AgudataPermissionError(message or "Agudata permission denied")
    if effective_code == 429:
        raise AgudataRateLimitError(message or "Agudata rate limit exceeded")
    if effective_code:
        raise AgudataError(
            message or f"Agudata {slug} failed with code {effective_code}"
        )
    return _extract_rows(payload)


def _paged_query(
    endpoint: str,
    params: dict | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: int = 60,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> list[dict]:
    size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), 1000))
    cap = max(size, int(max_rows or DEFAULT_MAX_ROWS))
    offset = 0
    result: list[dict] = []
    with requests.Session() as session:
        while offset < cap:
            query = dict(params or {})
            query["limit"] = min(size, cap - offset)
            query["offset"] = offset
            page = None
            for attempt in range(3):
                try:
                    page = query_api(
                        endpoint,
                        query,
                        api_key=api_key,
                        base_url=base_url,
                        timeout=timeout,
                        session=session,
                    )
                    break
                except AgudataRateLimitError:
                    if attempt >= len(RATE_LIMIT_BACKOFF_SECONDS):
                        raise
                    time.sleep(RATE_LIMIT_BACKOFF_SECONDS[attempt])
            if not page:
                break
            result.extend(page)
            if len(page) < query["limit"]:
                break
            offset += len(page)
    return result[:cap]


def _normalize_daily_frame(
    frame: pd.DataFrame | None,
    *,
    include_code: bool = False,
) -> pd.DataFrame:
    columns = (
        ["code", "date", "open", "high", "low", "close", "volume", "amount"]
        if include_code
        else ["date", "open", "high", "low", "close", "volume", "amount"]
    )
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    data = frame.rename(
        columns={"trade_date": "date", "vol": "volume"}
    ).copy()
    if include_code and "code" not in data.columns:
        if "ts_code" not in data.columns:
            return pd.DataFrame(columns=columns)
        data["code"] = data["ts_code"].map(_plain_code)
    required = ["date", "open", "high", "low", "close", "volume"]
    if any(column not in data.columns for column in required):
        return pd.DataFrame(columns=columns)
    if "amount" not in data.columns:
        data["amount"] = pd.NA
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(
        subset=["date", "open", "high", "low", "close", "volume"]
    )
    if include_code:
        data["code"] = data["code"].astype(str).map(_plain_code)
        data = data[data["code"].str.fullmatch(r"\d{6}")]
    dedupe = ["code", "date"] if include_code else ["date"]
    order = ["code", "date"] if include_code else ["date"]
    return (
        data[columns]
        .drop_duplicates(subset=dedupe, keep="last")
        .sort_values(order)
        .reset_index(drop=True)
    )


def _apply_qfq(daily: pd.DataFrame, factors: pd.DataFrame) -> pd.DataFrame:
    error = "Agudata QFQ requires one valid factor per daily row"
    if daily.empty or factors.empty:
        raise ValueError(error)
    data = daily.copy()
    fac = factors[["trade_date", "adj_factor"]].copy()
    data["_date"] = pd.to_datetime(
        data.get("trade_date"), errors="coerce"
    ).dt.normalize()
    fac["_date"] = pd.to_datetime(
        fac.get("trade_date"), errors="coerce"
    ).dt.normalize()
    fac["adj_factor"] = pd.to_numeric(
        fac["adj_factor"], errors="coerce"
    )
    invalid = data["_date"].isna() | data["_date"].duplicated()
    invalid_factor = (
        fac["_date"].isna()
        | fac["_date"].duplicated()
        | fac["adj_factor"].isna()
        | ~fac["adj_factor"].map(math.isfinite)
        | fac["adj_factor"].le(0)
    )
    if (
        invalid.any()
        or invalid_factor.any()
        or len(data) != len(fac)
        or set(data["_date"]) != set(fac["_date"])
    ):
        raise ValueError(error)
    data = data.merge(
        fac[["_date", "adj_factor"]],
        on="_date",
        how="left",
        validate="one_to_one",
    ).sort_values("_date")
    ratio = data["adj_factor"] / data["adj_factor"].iloc[-1]
    for column in ("open", "high", "low", "close"):
        data[column] = pd.to_numeric(
            data[column], errors="coerce"
        ) * ratio
    return data.drop(columns=["_date", "adj_factor"], errors="ignore")


def fetch_kline_qfq(
    code: str,
    start: str,
    end: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> pd.DataFrame | None:
    params = {
        "ts_code": _ts_code(code),
        "start_date": _date_text(start),
        "end_date": _date_text(end),
        "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
    }
    daily_rows = _paged_query(
        "daily",
        params,
        api_key=api_key,
        base_url=base_url,
        max_rows=5000,
    )
    if not daily_rows:
        return None
    factor_rows = _paged_query(
        "adj-factor",
        {
            "ts_code": params["ts_code"],
            "start_date": params["start_date"],
            "end_date": params["end_date"],
            "fields": "ts_code,trade_date,adj_factor",
        },
        api_key=api_key,
        base_url=base_url,
        max_rows=5000,
    )
    try:
        adjusted = _apply_qfq(
            pd.DataFrame(daily_rows).drop_duplicates(
                "trade_date", keep="last"
            ),
            pd.DataFrame(factor_rows).drop_duplicates(
                "trade_date", keep="last"
            ),
        )
    except (KeyError, ValueError) as exc:
        logger.error(
            "[agudata] rejected incomplete QFQ history for %s: %s",
            _ts_code(code),
            exc,
        )
        return None
    normalized = _normalize_daily_frame(adjusted)
    if normalized.empty:
        return None
    if bool(
        normalized["close"]
        .pct_change(fill_method=None)
        .abs()
        .gt(0.35)
        .any()
    ):
        logger.error(
            "[agudata] rejected discontinuous QFQ history for %s",
            _ts_code(code),
        )
        return None
    return normalized


def fetch_full_market_daily(
    trade_date: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    page_size: int = FULL_MARKET_DAILY_PAGE_SIZE,
) -> pd.DataFrame:
    rows = _paged_query(
        "daily",
        {
            "trade_date": _date_text(trade_date),
            "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
        },
        api_key=api_key,
        base_url=base_url,
        timeout=60,
        page_size=page_size,
        max_rows=7000,
    )
    return _normalize_daily_frame(
        pd.DataFrame(rows), include_code=True
    )


def fetch_full_market_adj_factors(
    trade_date: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> pd.DataFrame:
    columns = ["code", "date", "adj_factor"]
    rows = _paged_query(
        "adj-factor",
        {
            "trade_date": _date_text(trade_date),
            "fields": "ts_code,trade_date,adj_factor",
        },
        api_key=api_key,
        base_url=base_url,
        timeout=60,
        max_rows=7000,
    )
    data = pd.DataFrame(rows)
    required = {"ts_code", "trade_date", "adj_factor"}
    if data.empty or not required.issubset(data.columns):
        return pd.DataFrame(columns=columns)
    data["code"] = data["ts_code"].map(_plain_code)
    data["date"] = pd.to_datetime(data["trade_date"], errors="coerce")
    data["adj_factor"] = pd.to_numeric(
        data["adj_factor"], errors="coerce"
    )
    data = data.dropna(subset=["date", "adj_factor"])
    data = data[
        data["code"].str.fullmatch(r"\d{6}")
        & data["adj_factor"].gt(0)
    ]
    return data[columns].drop_duplicates(
        ["code", "date"], keep="last"
    ).reset_index(drop=True)


def _normalize_quote(row: dict) -> dict:
    ts_code = str(row.get("ts_code") or row.get("code") or "")
    price = _num(row.get("close", row.get("price")))
    previous = _num(row.get("pre_close", row.get("last_close")))
    change_pct = _num(row.get("pct_chg", row.get("change_pct")))
    if not change_pct and previous:
        change_pct = (price / previous - 1) * 100
    return {
        "code": _plain_code(ts_code),
        "ts_code": ts_code or _ts_code(str(row.get("code") or "")),
        "name": str(row.get("name") or ""),
        "trade_time": str(row.get("trade_time") or row.get("time") or ""),
        "price": price,
        "last_close": previous,
        "open": _num(row.get("open")),
        "high": _num(row.get("high")),
        "low": _num(row.get("low")),
        "vol": _num(row.get("vol", row.get("volume"))),
        "amount": _num(row.get("amount")),
        "change_pct": change_pct,
        "source": "agudata_rt_k",
        "level": "KLINE",
    }


def fetch_realtime_quotes(
    codes: list[str],
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[dict]:
    clean = [_ts_code(code) for code in codes if str(code or "").strip()]
    if not clean:
        return []
    params = {
        "ts_code": ",".join(clean),
        "fields": (
            "ts_code,name,trade_time,close,pre_close,high,open,low,"
            "vol,amount"
        ),
    }
    try:
        rows = _paged_query(
            "rt-k",
            params,
            api_key=api_key,
            base_url=base_url,
            timeout=8,
            max_rows=max(1000, len(clean)),
        )
    except AgudataError:
        if len(clean) == 1:
            raise
        rows = []
        for ts_code in clean:
            rows.extend(
                _paged_query(
                    "rt-k",
                    {**params, "ts_code": ts_code},
                    api_key=api_key,
                    base_url=base_url,
                    timeout=8,
                    max_rows=10,
                )
            )
    return [_normalize_quote(row) for row in rows]


def fetch_full_market_realtime_daily(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> pd.DataFrame:
    rows = _paged_query(
        "rt-k",
        {
            "fields": (
                "ts_code,name,trade_time,close,pre_close,high,open,low,"
                "vol,amount"
            ),
        },
        api_key=api_key,
        base_url=base_url,
        timeout=60,
        max_rows=7000,
    )
    data = pd.DataFrame(rows)
    if data.empty:
        return _normalize_daily_frame(None, include_code=True)
    data["trade_date"] = data.get(
        "trade_time",
        pd.Series(index=data.index, dtype="string"),
    ).astype(str).str.slice(0, 10)
    if "vol" in data.columns:
        data["vol"] = pd.to_numeric(data["vol"], errors="coerce") / 100.0
    if "amount" in data.columns:
        data["amount"] = (
            pd.to_numeric(data["amount"], errors="coerce") / 1000.0
        )
    return _normalize_daily_frame(data, include_code=True)


def _normalize_minute_frame(
    rows: list[dict],
    *,
    historical: bool,
) -> pd.DataFrame:
    columns = [
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "is_complete",
    ]
    data = pd.DataFrame(rows)
    if data.empty:
        return pd.DataFrame(columns=columns)
    data = data.rename(
        columns={
            "trade_time": "datetime",
            "time": "datetime",
            "vol": "volume",
        }
    )
    required = {"datetime", "open", "high", "low", "close", "volume"}
    if not required.issubset(data.columns):
        return pd.DataFrame(columns=columns)
    data["datetime"] = pd.to_datetime(data["datetime"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column not in data.columns:
            data[column] = 0.0
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["datetime", "close"]).sort_values("datetime")
    data["is_complete"] = True
    if not historical and not data.empty:
        latest = data["datetime"].iloc[-1]
        now = pd.Timestamp(datetime.now())
        if latest.floor("min") >= now.floor("min"):
            data.loc[data.index[-1], "is_complete"] = False
    return data[columns].reset_index(drop=True)


def fetch_minute_kline(
    code: str,
    freq: str = "1MIN",
    limit: int | None = 240,
    trade_date: str | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> pd.DataFrame:
    freq_key = str(freq or "1MIN").strip().upper()
    if freq_key not in VALID_MINUTE_FREQS:
        freq_key = "1MIN"
    historical = bool(trade_date)
    params: dict[str, Any] = {
        "ts_code": _ts_code(code),
        "freq": freq_key.lower() if historical else freq_key,
    }
    if historical:
        compact = _date_text(str(trade_date))
        params.update(
            {
                "start_date": f"{compact} 09:30:00",
                "end_date": f"{compact} 15:00:00",
                "fields": (
                    "ts_code,trade_time,open,close,high,low,vol,amount"
                ),
            }
        )
        endpoint = "stk-mins"
    else:
        params["fields"] = "ts_code,time,open,close,high,low,vol,amount"
        endpoint = "rt-min"
    rows = _paged_query(
        endpoint,
        params,
        api_key=api_key,
        base_url=base_url,
        timeout=30,
        max_rows=max(1000, int(limit or 10000)),
    )
    frame = _normalize_minute_frame(rows, historical=historical)
    return frame.tail(int(limit)).reset_index(drop=True) if limit else frame


def fetch_auction_snapshot(
    code: str,
    trade_date: str | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> dict:
    params: dict[str, Any] = {
        "ts_code": _ts_code(code),
        "fields": (
            "ts_code,trade_date,vol,price,amount,pre_close,"
            "turnover_rate,volume_ratio,float_share"
        ),
    }
    if trade_date:
        params["trade_date"] = _date_text(trade_date)
    rows = _paged_query(
        "stk-auction",
        params,
        api_key=api_key,
        base_url=base_url,
        timeout=15,
        max_rows=20,
    )
    if not rows:
        return {}
    row = rows[-1]
    return {
        "code": _plain_code(str(row.get("ts_code") or code)),
        "ts_code": row.get("ts_code") or _ts_code(code),
        "trade_date": str(row.get("trade_date") or ""),
        "price": _num(row.get("price")),
        "vol": _num(row.get("vol")),
        "amount": _num(row.get("amount")),
        "pre_close": _num(row.get("pre_close")),
        "turnover_rate": _num(row.get("turnover_rate")),
        "volume_ratio": _num(row.get("volume_ratio")),
        "float_share": _num(row.get("float_share")),
        "source": "agudata_stk_auction",
    }


def fetch_stock_basic(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[dict]:
    rows = _paged_query(
        "stock-basic",
        {
            "list_status": "L",
            "fields": (
                "ts_code,symbol,name,area,industry,market,"
                "list_date,list_status"
            ),
        },
        api_key=api_key,
        base_url=base_url,
        max_rows=10000,
    )
    result = []
    for row in rows:
        code = _plain_code(
            str(row.get("ts_code") or row.get("symbol") or "")
        )
        if not code.isdigit() or len(code) != 6:
            continue
        result.append(
            {
                "code": code,
                "ts_code": row.get("ts_code") or _ts_code(code),
                "name": str(row.get("name") or "").strip(),
                "industry": str(row.get("industry") or "").strip(),
                "area": str(row.get("area") or "").strip(),
                "market": str(row.get("market") or "").strip(),
                "list_date": str(row.get("list_date") or "").strip(),
                "list_status": str(row.get("list_status") or "L").strip(),
                "source": "agudata_stock_basic",
            }
        )
    return result


def fetch_trade_calendar(
    start: str,
    end: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[str]:
    rows = _paged_query(
        "trade-cal",
        {
            "exchange": "SSE",
            "start_date": _date_text(start),
            "end_date": _date_text(end),
            "is_open": "1",
            "fields": "exchange,cal_date,is_open,pretrade_date",
        },
        api_key=api_key,
        base_url=base_url,
        page_size=100,
        max_rows=100,
    )
    dates = {
        _date_text(str(row.get("cal_date") or ""))
        for row in rows
        if str(row.get("is_open", "1")).strip() in {"1", "1.0", "True"}
    }
    return sorted(
        value
        for value in dates
        if len(value) == 8 and value.isdigit()
    )


def _fetch_trusted_security_lifecycle() -> list[dict]:
    # 生命周期继续使用独立 Baostock 快照，避免 Agudata 档案缺失导致幸存者偏差。
    from analytics import tushare_provider

    return tushare_provider._validate_baostock_security_lifecycle(
        tushare_provider._fetch_baostock_security_lifecycle()
    )


def fetch_security_master(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    statuses: tuple[str, ...] = ("L", "D", "P"),
) -> list[dict]:
    snapshot_now = datetime.now()
    completed_through = (
        snapshot_now
        if snapshot_now.hour >= 15
        else snapshot_now - timedelta(days=1)
    )
    calendar = fetch_trade_calendar(
        (completed_through - timedelta(days=31)).strftime("%Y%m%d"),
        completed_through.strftime("%Y%m%d"),
        api_key=api_key,
        base_url=base_url,
    )
    if not calendar:
        raise AgudataError(
            "cannot verify latest completed trade date "
            "for security master snapshot"
        )
    snapshot_trade_date = max(calendar)
    lifecycle_rows = _fetch_trusted_security_lifecycle()

    fields = (
        "ts_code,symbol,name,area,industry,market,"
        "list_date,delist_date,list_status"
    )
    profiles: dict[str, dict] = {}
    profile_requests = 0
    for raw_status in statuses:
        status = str(raw_status or "").strip().upper()
        if status not in {"L", "D", "P"}:
            continue
        rows = _paged_query(
            "stock-basic",
            {
                "list_status": status,
                "fields": fields,
            },
            api_key=api_key,
            base_url=base_url,
            page_size=500,
            max_rows=10000,
        )
        profile_requests += 1
        for row in rows:
            code = _plain_code(
                str(
                    row.get("ts_code")
                    or row.get("symbol")
                    or row.get("code")
                    or ""
                )
            )
            if len(code) != 6 or not code.isdigit() or code in profiles:
                continue
            profiles[code] = {
                "code": code,
                "ts_code": row.get("ts_code") or _ts_code(code),
                "name": str(row.get("name") or "").strip(),
                "industry": str(row.get("industry") or "").strip(),
                "area": str(row.get("area") or "").strip(),
                "market": str(row.get("market") or "").strip(),
                "source": "agudata_stock_basic",
            }

    normalized: dict[str, dict] = {}
    for lifecycle in lifecycle_rows:
        code = str(lifecycle.get("code") or "").strip()
        profile = profiles.get(code, {})
        normalized[code] = {
            **lifecycle,
            "name": str(
                profile.get("name") or lifecycle.get("name") or ""
            ).strip(),
            "industry": str(profile.get("industry") or "").strip(),
            "area": str(profile.get("area") or "").strip(),
            "market": str(profile.get("market") or "").strip(),
            "source": (
                "agudata_stock_basic+baostock_stock_basic"
                if profile
                else "baostock_stock_basic"
            ),
        }

    snapshot_at = snapshot_now.isoformat(timespec="seconds")
    row_count = len(normalized)
    for row in normalized.values():
        row.update(
            {
                "lifecycle_source": "baostock_stock_basic",
                "lifecycle_snapshot_complete": True,
                "universe_scope": "sh_sz_a_share",
                "snapshot_at": snapshot_at,
                "security_master_pagination_complete": True,
                "security_master_page_requests": 1,
                "security_master_row_count": row_count,
                "security_master_snapshot_complete": True,
                "security_master_snapshot_trade_date": snapshot_trade_date,
                "agudata_profile_snapshot_complete": True,
                "agudata_profile_status_requests": profile_requests,
                "agudata_profile_row_count": len(profiles),
            }
        )
    return [normalized[code] for code in sorted(normalized)]


def test_connection(
    api_key: str | None = None,
    base_url: str | None = None,
) -> dict:
    try:
        rows = query_api(
            "stock-basic",
            {
                "list_status": "L",
                "limit": 1,
                "offset": 0,
                "fields": "ts_code,symbol,name",
            },
            api_key=api_key,
            base_url=base_url,
            timeout=15,
        )
        if not rows:
            return {
                "ok": False,
                "provider": "agudata",
                "error": "Agudata connection succeeded but returned no rows",
            }
        sample = rows[0]
        return {
            "ok": True,
            "provider": "agudata",
            "rows": len(rows),
            "sample": str(
                sample.get("ts_code") or sample.get("symbol") or ""
            ),
            "capabilities": [
                "stock_basic",
                "security_master",
                "trade_calendar",
                "daily_qfq",
                "realtime_k",
                "minute",
                "auction_summary",
            ],
            "unsupported": [
                "historical_order_book",
                "tick_series",
                "dynamic_auction_ticks",
            ],
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": "agudata",
            "error": str(exc)[:300],
        }


def clear_cache() -> None:
    """Reserved for parity with other providers."""
