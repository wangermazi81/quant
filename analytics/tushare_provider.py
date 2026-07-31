# -*- coding: utf-8 -*-
"""Tushare Pro adapter used by the selectable datasource layer."""

from __future__ import annotations

import importlib
import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Mapping

import pandas as pd
import requests

logger = logging.getLogger(__name__)

try:
    from analytics import datasource_config
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
REPLAY_DAILY_CHUNK_DAYS = 366
STOCK_BASIC_PAGE_SIZE = 1000
STOCK_BASIC_MAX_LIMIT = 10000
STOCK_BASIC_MAX_PAGE_REQUESTS = 100
_BAOSTOCK_MASTER_LOCK = threading.Lock()


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

def _replay_request(
    api_name: str,
    token: str,
    params: dict | None = None,
    timeout: int = 25,
    max_attempts: int | None = None,
) -> Any:
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
            return payload
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("; ".join(errors))


def _replay_get(
    api_name: str,
    token: str,
    params: dict | None = None,
    timeout: int = 25,
    max_attempts: int | None = None,
) -> list[dict]:
    payload = _replay_request(
        api_name,
        token,
        params=params,
        timeout=timeout,
        max_attempts=max_attempts,
    )
    return _extract_rows(payload)


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
    columns = required + (["amount"] if "amount" in data.columns else [])
    data = data[columns]
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in data.columns:
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
    error = "QFQ requires exactly one valid adjustment factor per daily trade date"
    if (
        daily is None
        or daily.empty
        or "trade_date" not in daily.columns
        or factors is None
        or factors.empty
        or "trade_date" not in factors.columns
        or "adj_factor" not in factors.columns
    ):
        raise ValueError(error)
    data = daily.copy()
    fac = factors[["trade_date", "adj_factor"]].copy()
    data["_qfq_trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce").dt.normalize()
    fac["_qfq_trade_date"] = pd.to_datetime(fac["trade_date"], errors="coerce").dt.normalize()
    fac["adj_factor"] = pd.to_numeric(fac["adj_factor"], errors="coerce")
    invalid_factor = (
        fac["adj_factor"].isna()
        | ~fac["adj_factor"].map(math.isfinite)
        | fac["adj_factor"].le(0)
    )
    if (
        data["_qfq_trade_date"].isna().any()
        or data["_qfq_trade_date"].duplicated().any()
        or fac["_qfq_trade_date"].isna().any()
        or fac["_qfq_trade_date"].duplicated().any()
        or invalid_factor.any()
        or len(data) != len(fac)
        or set(data["_qfq_trade_date"]) != set(fac["_qfq_trade_date"])
    ):
        raise ValueError(error)
    data = data.merge(
        fac[["_qfq_trade_date", "adj_factor"]],
        on="_qfq_trade_date",
        how="left",
        validate="one_to_one",
    )
    data = data.sort_values("_qfq_trade_date").reset_index(drop=True)
    latest = data["adj_factor"].iloc[-1]
    ratio = data["adj_factor"] / latest
    for col in ("open", "high", "low", "close"):
        data[col] = pd.to_numeric(data[col], errors="coerce") * ratio
    return data.drop(columns=["_qfq_trade_date", "adj_factor"], errors="ignore")


def _fetch_replay_kline_qfq(code: str, start: str, end: str, token: str) -> pd.DataFrame | None:
    ts_code = _ts_code(code)
    start_date = _date_text(start)
    end_date = _date_text(end)
    start_dt = datetime.strptime(start_date, "%Y%m%d")
    end_dt = datetime.strptime(end_date, "%Y%m%d")
    if start_dt > end_dt:
        return None

    chunks: list[tuple[str, str]] = []
    chunk_start = start_dt
    while chunk_start <= end_dt:
        chunk_end = min(
            chunk_start + timedelta(days=REPLAY_DAILY_CHUNK_DAYS),
            end_dt,
        )
        chunks.append(
            (
                chunk_start.strftime("%Y%m%d"),
                chunk_end.strftime("%Y%m%d"),
            )
        )
        chunk_start = chunk_end + timedelta(days=1)

    daily_rows: list[dict] = []
    factor_rows: list[dict] = []
    factor_error: Exception | None = None
    for chunk_start, chunk_end in chunks:
        daily_rows.extend(
            _replay_get(
                "daily",
                token,
                {
                    "ts_code": ts_code,
                    "start_date": chunk_start,
                    "end_date": chunk_end,
                    "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
                },
            )
        )
        try:
            factor_rows.extend(
                _replay_get(
                    "adj_factor",
                    token,
                    {
                        "ts_code": ts_code,
                        "start_date": chunk_start,
                        "end_date": chunk_end,
                        "fields": "ts_code,trade_date,adj_factor",
                    },
                )
            )
        except Exception as exc:
            factor_error = exc
    if not daily_rows:
        return None
    daily = pd.DataFrame(daily_rows).drop_duplicates("trade_date", keep="last")
    expected_daily_rows = len(daily)
    if factor_error is not None:
        logger.error(
            "[tushare] rejected replay daily history without adjustment factors: %s",
            factor_error,
        )
        return None
    if not factor_rows:
        logger.error(
            "[tushare] rejected replay daily history with empty adjustment factors for %s",
            ts_code,
        )
        return None
    try:
        daily = _apply_qfq(daily, pd.DataFrame(factor_rows))
    except ValueError as exc:
        logger.error(
            "[tushare] rejected replay QFQ history for %s: %s",
            ts_code,
            exc,
        )
        return None
    normalized = _normalize_daily_frame(daily)
    if (
        normalized is None
        or normalized.empty
        or len(normalized) != expected_daily_rows
    ):
        logger.error(
            "[tushare] rejected incomplete replay QFQ history for %s: expected %s rows",
            ts_code,
            expected_daily_rows,
        )
        return None
    close = pd.to_numeric(normalized["close"], errors="coerce")
    if bool(close.pct_change(fill_method=None).abs().gt(0.35).any()):
        logger.error(
            "[tushare] rejected discontinuous replay QFQ history for %s",
            ts_code,
        )
        return None
    return normalized


# ── 日线进程内 TTL 缓存 ───────────────────────────────────────────────
# 本模块此前**完全没有缓存**：默认配置就是 provider=tushare/mode=replay，
# 于是一轮 5000 只的全市场扫描 = 5000+ 次外网 HTTP（replay 分支每只还要
# daily + adj_factor 两次），代理降级时单只最坏 3 个域名 × 25s = 75s，
# 扫描线程可挂起数小时，对方限流时选股功能间歇性「返回 0」。
# 日线是收盘后就不再变的数据，按 (code, start, end, mode) 缓存完全安全。
_KLINE_CACHE: dict[tuple, tuple[float, Any]] = {}
_KLINE_CACHE_LOCK = threading.Lock()
_KLINE_CACHE_TTL = float(os.environ.get("TUSHARE_KLINE_CACHE_TTL", "900") or "900")
_KLINE_CACHE_MAX = int(os.environ.get("TUSHARE_KLINE_CACHE_MAX", "6000") or "6000")


def clear_kline_cache() -> None:
    """清空日线缓存（测试与「强制刷新」入口用）。"""
    with _KLINE_CACHE_LOCK:
        _KLINE_CACHE.clear()


def _kline_cache_get(key: tuple):
    with _KLINE_CACHE_LOCK:
        hit = _KLINE_CACHE.get(key)
        if not hit:
            return None
        ts, value = hit
        if time.time() - ts > _KLINE_CACHE_TTL:
            _KLINE_CACHE.pop(key, None)
            return None
        # 必须回拷副本：调用方拿到 DataFrame 后普遍会原地改列/排序，
        # 直接共享同一对象会让缓存内容被下游污染。
        return value.copy() if hasattr(value, "copy") else value


def _kline_cache_put(key: tuple, value) -> None:
    with _KLINE_CACHE_LOCK:
        if len(_KLINE_CACHE) >= _KLINE_CACHE_MAX:
            # 简单容量控制：按写入时间淘汰最旧的一批，避免无界增长
            for stale in sorted(_KLINE_CACHE, key=lambda k: _KLINE_CACHE[k][0])[:_KLINE_CACHE_MAX // 4]:
                _KLINE_CACHE.pop(stale, None)
        _KLINE_CACHE[key] = (time.time(), value.copy() if hasattr(value, "copy") else value)


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

    cache_key = (str(code), _date_text(start), _date_text(end), resolved_mode)
    cached = _kline_cache_get(cache_key)
    if cached is not None:
        return cached

    if resolved_mode == "replay":
        result = _fetch_replay_kline_qfq(code, start, end, token)
        # 只缓存有效结果：None/空表往往是限流或代理抖动，缓存下来会把
        # 一次偶发失败放大成 15 分钟内持续选不出票。
        if result is not None and not result.empty:
            _kline_cache_put(cache_key, result)
        return result

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
    result = _normalize_daily_frame(df)
    if result is not None and not result.empty:
        _kline_cache_put(cache_key, result)
    return result


def _normalize_full_market_daily_frame(df: pd.DataFrame | None) -> pd.DataFrame:
    columns = ["code", "date", "open", "high", "low", "close", "volume", "amount"]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)
    data = df.copy()
    if "trade_date" in data.columns and "date" not in data.columns:
        data = data.rename(columns={"trade_date": "date"})
    if "vol" in data.columns and "volume" not in data.columns:
        data = data.rename(columns={"vol": "volume"})
    if "code" not in data.columns:
        if "ts_code" not in data.columns:
            return pd.DataFrame(columns=columns)
        data["code"] = data["ts_code"].map(_plain_code)
    required = ["code", "date", "open", "high", "low", "close", "volume"]
    if any(column not in data.columns for column in required):
        return pd.DataFrame(columns=columns)
    data["code"] = data["code"].astype(str).map(_plain_code)
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column not in data.columns:
            data[column] = pd.NA
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    data = data[(data["code"].str.len() == 6) & (data["open"] > 0) & (data["close"] > 0)]
    return data[columns].drop_duplicates(subset=["code", "date"], keep="last").reset_index(drop=True)


def fetch_trade_calendar(
    start_date: str,
    end_date: str,
    token: str | None = None,
    mode: str | None = None,
) -> list[str]:
    """取区间内的 A 股**开市日**列表（YYYY-MM-DD）。供 market/trading_calendar 用。"""
    resolved_mode = _mode(mode)
    token = token or datasource_config.resolve_tushare_token(mode=resolved_mode)
    if not token:
        return []
    rows = _replay_get(
        "trade_cal",
        token,
        {
            "exchange": "SSE",
            "start_date": _date_text(start_date),
            "end_date": _date_text(end_date),
            "is_open": "1",
        },
    )
    out = []
    for row in rows or []:
        raw = str(row.get("cal_date") or row.get("trade_date") or "").strip()
        # ★ 必须在本地按 is_open 字段过滤，不能信任请求参数：
        #   replay 端点**无视 is_open=1**，会把区间内全部日历日返回（实测 2026 年
        #   返回 365 条、含 104 个周末），只是每行带了正确的 is_open 标记。
        #   若照单全收，「开市日」里就包含了周末与法定假日 → 反推出的休市日为 0 条，
        #   写出一份「零休市日」的日历。那比没有日历更糟：has_calendar() 变成 True，
        #   「缺日历」的告警被抑制，而节假日一天都拦不住。
        if "is_open" in row:
            try:
                if int(row.get("is_open") or 0) != 1:
                    continue
            except (TypeError, ValueError):
                continue
        if len(raw) == 8 and raw.isdigit():
            out.append(f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")
        elif len(raw) == 10:
            out.append(raw)
    return out


def fetch_full_market_daily(
    trade_date: str,
    token: str | None = None,
    mode: str | None = None,
) -> pd.DataFrame:
    """Fetch one completed A-share trading day in a single Tushare request."""
    resolved_mode = _mode(mode)
    token = token or datasource_config.resolve_tushare_token(mode=resolved_mode)
    if not token:
        return _normalize_full_market_daily_frame(None)
    compact_date = _date_text(trade_date)
    fields = "ts_code,trade_date,open,high,low,close,vol,amount"
    if resolved_mode == "replay":
        rows = _replay_get(
            "daily",
            token,
            {"trade_date": compact_date, "limit": 7000, "fields": fields},
            timeout=60,
            max_attempts=1,
        )
        return _normalize_full_market_daily_frame(pd.DataFrame(rows))

    ts = _load_tushare()
    api = ts.pro_api(token)
    frame = api.daily(trade_date=compact_date, fields=fields)
    return _normalize_full_market_daily_frame(frame)


def fetch_full_market_realtime_daily(
    token: str | None = None,
    mode: str | None = None,
) -> pd.DataFrame:
    """Fetch the latest full-market realtime daily bar after market close."""
    resolved_mode = _mode(mode)
    token = token or datasource_config.resolve_tushare_token(mode=resolved_mode)
    if not token or resolved_mode != "replay":
        return _normalize_full_market_daily_frame(None)
    payload = _replay_request(
        "rt_k",
        token,
        {},
        timeout=60,
        max_attempts=1,
    )
    rows = _extract_rows(payload)
    params = payload.get("params") if isinstance(payload, dict) else {}
    source_trade_date = str((params or {}).get("source_trade_date") or "").strip()
    if len(_date_text(source_trade_date)) != 8:
        raise RuntimeError("Tushare rt_k payload has no source_trade_date")
    data = pd.DataFrame(rows)
    if data.empty:
        return _normalize_full_market_daily_frame(None)
    # rt_k reports shares/yuan; daily reports lots/thousand-yuan.
    volume_column = "volume" if "volume" in data.columns else "vol"
    if volume_column in data.columns:
        data[volume_column] = pd.to_numeric(
            data[volume_column],
            errors="coerce",
        ) / 100.0
    if "amount" in data.columns:
        data["amount"] = pd.to_numeric(data["amount"], errors="coerce") / 1000.0
    data["trade_date"] = _date_text(source_trade_date)
    return _normalize_full_market_daily_frame(data)


def fetch_full_market_adj_factors(
    trade_date: str,
    token: str | None = None,
    mode: str | None = None,
) -> pd.DataFrame:
    """Fetch one trading day's adjustment factors for the full A-share market."""
    columns = ["code", "date", "adj_factor"]
    resolved_mode = _mode(mode)
    token = token or datasource_config.resolve_tushare_token(mode=resolved_mode)
    if not token:
        return pd.DataFrame(columns=columns)
    compact_date = _date_text(trade_date)
    fields = "ts_code,trade_date,adj_factor"
    if resolved_mode == "replay":
        rows = _replay_get(
            "adj_factor",
            token,
            {"trade_date": compact_date, "limit": 7000, "fields": fields},
            timeout=60,
            max_attempts=1,
        )
        data = pd.DataFrame(rows)
    else:
        ts = _load_tushare()
        api = ts.pro_api(token)
        data = api.adj_factor(trade_date=compact_date, fields=fields)
    if data is None or data.empty or "ts_code" not in data.columns or "adj_factor" not in data.columns:
        return pd.DataFrame(columns=columns)
    data = data.copy()
    data["code"] = data["ts_code"].map(_plain_code)
    data["date"] = pd.to_datetime(data.get("trade_date", compact_date), errors="coerce")
    data["adj_factor"] = pd.to_numeric(data["adj_factor"], errors="coerce")
    data = data.dropna(subset=["date", "adj_factor"])
    data = data[(data["code"].str.len() == 6) & (data["adj_factor"] > 0)]
    return data[columns].drop_duplicates(subset=["code", "date"], keep="last").reset_index(drop=True)


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
    if limit is None and trade_date:
        params["limit"] = 10000
    elif limit:
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
    trade_date: str | None = None,
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
        if trade_date:
            params["trade_date"] = _date_text(trade_date)
        rows = _replay_get("rt_min", token, params, timeout=REALTIME_REPLAY_TIMEOUT, max_attempts=1)
        df = _normalize_minute_frame(pd.DataFrame(rows))
        if limit and df is not None and not df.empty:
            df = df.tail(int(limit))
        return df

    ts = _load_tushare()
    api = ts.pro_api(token)
    sdk_freq = freq_key.lower().replace("min", "min")
    end = _date_text(trade_date) if trade_date else datetime.now().strftime("%Y%m%d")
    start = end if trade_date else (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
    df = ts.pro_bar(ts_code=ts_code, api=api, start_date=start, end_date=end, freq=sdk_freq, asset="E")
    if limit and df is not None and not df.empty:
        df = df.tail(int(limit))
    return _normalize_minute_frame(df)


def query_replay_api(
    api_name: str,
    params: dict | None = None,
    token: str | None = None,
    mode: str | None = None,
    timeout: int = 10,
    max_attempts: int = 2,
) -> list[dict]:
    """Best-effort access to a Tushare Pro API through the configured source."""
    resolved_mode = _mode(mode)
    token = token or datasource_config.resolve_tushare_token(mode=resolved_mode)
    if not token or not api_name:
        return []
    if resolved_mode == "replay":
        return _replay_get(api_name, token, params or {}, timeout=timeout, max_attempts=max_attempts)
    ts = _load_tushare()
    api = ts.pro_api(token)
    try:
        fn = getattr(api, api_name)
        df = fn(**(params or {}))
    except AttributeError:
        df = api.query(api_name, **(params or {}))
    if df is None or getattr(df, "empty", True):
        return []
    return df.to_dict("records")


def _strict_security_master_date(
    value: Any,
    *,
    field: str,
    allow_empty: bool = False,
) -> str:
    text = str(value or "").strip().replace("-", "")
    if not text and allow_empty:
        return ""
    if len(text) != 8 or not text.isdigit():
        raise RuntimeError(
            f"baostock lifecycle returned invalid {field}"
        )
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise RuntimeError(
            f"baostock lifecycle returned invalid {field}"
        ) from exc
    return text


def _validate_baostock_security_lifecycle(
    rows: list[dict],
) -> list[dict]:
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("baostock lifecycle is empty")

    normalized: list[dict] = []
    seen_codes: set[str] = set()
    seen_ts_codes: set[str] = set()
    coverage: set[tuple[str, str]] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise RuntimeError("baostock lifecycle returned invalid row")
        code = str(raw.get("code") or "").strip()
        ts_code = str(raw.get("ts_code") or "").strip().upper()
        exchange = str(raw.get("exchange") or "").strip().upper()
        if not exchange and "." in ts_code:
            exchange = ts_code.rsplit(".", 1)[-1]
        expected_ts_code = f"{code}.{exchange}"
        if (
            len(code) != 6
            or not code.isdigit()
            or exchange not in {"SH", "SZ"}
            or ts_code != expected_ts_code
            or code in seen_codes
            or ts_code in seen_ts_codes
        ):
            raise RuntimeError(
                "baostock lifecycle returned invalid or repeated security"
            )

        list_status = str(
            raw.get("list_status") or ""
        ).strip().upper()
        if list_status not in {"L", "D"}:
            raise RuntimeError(
                "baostock lifecycle returned invalid list status"
            )
        list_date = _strict_security_master_date(
            raw.get("list_date"),
            field="list_date",
        )
        delist_date = _strict_security_master_date(
            raw.get("delist_date"),
            field="delist_date",
            allow_empty=list_status == "L",
        )
        if (
            (list_status == "L" and delist_date)
            or (
                list_status == "D"
                and (
                    not delist_date
                    or delist_date < list_date
                )
            )
        ):
            raise RuntimeError(
                "baostock lifecycle returned inconsistent lifecycle dates"
            )

        seen_codes.add(code)
        seen_ts_codes.add(ts_code)
        coverage.add((exchange, list_status))
        normalized.append(
            {
                "code": code,
                "ts_code": ts_code,
                "name": str(raw.get("name") or "").strip(),
                "list_date": list_date,
                "delist_date": delist_date,
                "list_status": list_status,
                "exchange": exchange,
            }
        )

    required_coverage = {
        ("SH", "L"),
        ("SH", "D"),
        ("SZ", "L"),
        ("SZ", "D"),
    }
    if not required_coverage.issubset(coverage):
        raise RuntimeError(
            "baostock lifecycle is missing SH/SZ listed or delisted coverage"
        )
    return sorted(normalized, key=lambda row: row["ts_code"])


def _fetch_baostock_security_lifecycle() -> list[dict]:
    try:
        import baostock as bs
    except Exception as exc:
        raise RuntimeError(
            "baostock lifecycle provider is unavailable"
        ) from exc

    with _BAOSTOCK_MASTER_LOCK:
        try:
            login = bs.login()
        except Exception as exc:
            raise RuntimeError(
                "baostock lifecycle login failed"
            ) from exc
        if str(getattr(login, "error_code", "")) != "0":
            raise RuntimeError(
                "baostock lifecycle login failed: "
                f"{getattr(login, 'error_msg', '')}"
            )
        try:
            result = bs.query_stock_basic()
            if str(getattr(result, "error_code", "")) != "0":
                raise RuntimeError(
                    "baostock lifecycle query failed: "
                    f"{getattr(result, 'error_msg', '')}"
                )
            rows: list[dict] = []
            while True:
                if str(getattr(result, "error_code", "")) != "0":
                    raise RuntimeError(
                        "baostock lifecycle cursor failed: "
                        f"{getattr(result, 'error_msg', '')}"
                    )
                if not result.next():
                    break
                raw = dict(zip(result.fields, result.get_row_data()))
                if str(raw.get("type") or "") != "1":
                    continue
                raw_code = str(raw.get("code") or "").strip().lower()
                if not raw_code.startswith(("sh.", "sz.")):
                    continue
                exchange_text, code = raw_code.split(".", 1)
                if len(code) != 6 or not code.isdigit():
                    raise RuntimeError(
                        "baostock lifecycle returned invalid security code"
                    )
                raw_status = str(raw.get("status") or "").strip()
                if raw_status not in {"0", "1"}:
                    raise RuntimeError(
                        "baostock lifecycle returned invalid status"
                    )
                exchange = exchange_text.upper()
                rows.append(
                    {
                        "code": code,
                        "ts_code": f"{code}.{exchange}",
                        "name": str(raw.get("code_name") or "").strip(),
                        "list_date": raw.get("ipoDate"),
                        "delist_date": raw.get("outDate"),
                        "list_status": "D" if raw_status == "0" else "L",
                        "exchange": exchange,
                    }
                )
            if str(getattr(result, "error_code", "")) != "0":
                raise RuntimeError(
                    "baostock lifecycle cursor failed: "
                    f"{getattr(result, 'error_msg', '')}"
                )
            return _validate_baostock_security_lifecycle(rows)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "baostock lifecycle query failed"
            ) from exc
        finally:
            try:
                bs.logout()
            except Exception:
                pass


def _latest_completed_trade_date(
    snapshot_now: datetime,
    token: str | None,
    mode: str | None,
) -> str:
    completed_through = (
        snapshot_now
        if snapshot_now.hour >= 15
        else snapshot_now - timedelta(days=1)
    )
    calendar_start = completed_through - timedelta(days=31)
    trade_dates = fetch_trade_calendar(
        calendar_start.strftime("%Y%m%d"),
        completed_through.strftime("%Y%m%d"),
        token=token,
        mode=mode,
    )
    completed_limit = completed_through.strftime("%Y%m%d")
    completed_dates = {
        _date_text(value)
        for value in trade_dates
        if len(_date_text(value)) == 8
        and _date_text(value).isdigit()
        and _date_text(value) <= completed_limit
    }
    if not completed_dates:
        raise RuntimeError(
            "cannot verify latest completed trade date for security master snapshot"
        )
    return max(completed_dates)


def _fetch_stock_basic_pages(
    status: str,
    fields: str,
    token: str | None,
    mode: str | None,
) -> tuple[list[dict], int]:
    limit = STOCK_BASIC_PAGE_SIZE
    max_limit = max(STOCK_BASIC_PAGE_SIZE, STOCK_BASIC_MAX_LIMIT)
    previous_rows: dict[str, dict] | None = None
    previous_was_full = False
    for request_count in range(1, STOCK_BASIC_MAX_PAGE_REQUESTS + 1):
        page = query_replay_api(
            "stock_basic",
            {
                "list_status": status,
                "fields": fields,
                "limit": limit,
                "offset": 0,
            },
            token=token,
            mode=mode,
            timeout=15,
        )
        if len(page) > limit:
            raise RuntimeError(
                f"stock_basic pagination exceeded requested page size for {status}"
            )

        current_rows: dict[str, dict] = {}
        for row in page:
            code = _plain_code(
                str(
                    row.get("ts_code")
                    or row.get("symbol")
                    or row.get("code")
                    or ""
                )
            )
            if (
                len(code) != 6
                or not code.isdigit()
                or code in current_rows
            ):
                raise RuntimeError(
                    f"stock_basic pagination returned invalid or repeated rows for {status}"
                )
            current_rows[code] = dict(row)

        if previous_rows is not None:
            previous_codes = set(previous_rows)
            current_codes = set(current_rows)
            if (
                len(current_rows) < len(previous_rows)
                or not previous_codes.issubset(current_codes)
                or any(
                    current_rows[code] != previous_rows[code]
                    for code in previous_codes & current_codes
                )
            ):
                raise RuntimeError(
                    f"stock_basic pagination returned unstable rows for {status}"
                )
            if current_rows == previous_rows:
                if previous_was_full:
                    raise RuntimeError(
                        f"stock_basic pagination appears server-capped for {status}"
                    )
                return page, request_count

        current_was_full = len(page) == limit
        if current_was_full and limit >= max_limit:
            raise RuntimeError(
                f"stock_basic pagination reached maximum limit for {status}"
            )

        previous_rows = current_rows
        previous_was_full = current_was_full
        limit = min(limit * 2, max_limit)

    raise RuntimeError(
        f"stock_basic pagination did not terminate for {status}"
    )


def fetch_security_master(
    token: str | None = None,
    mode: str | None = None,
    statuses: tuple[str, ...] = ("L", "D", "P"),
) -> list[dict]:
    fields = (
        "ts_code,symbol,name,area,industry,market,"
        "list_date,delist_date,list_status"
    )
    snapshot_now = datetime.now()
    snapshot_trade_date = _latest_completed_trade_date(
        snapshot_now,
        token,
        mode,
    )
    lifecycle_rows = _validate_baostock_security_lifecycle(
        _fetch_baostock_security_lifecycle()
    )
    profiles: dict[str, dict] = {}
    page_requests = 0
    for status in statuses:
        try:
            rows, status_page_requests = _fetch_stock_basic_pages(
                status,
                fields,
                token,
                mode,
            )
        except RuntimeError as exc:
            logger.warning(
                "[security-master] optional tushare profile failed "
                "for %s: %s",
                status,
                exc,
            )
            continue
        page_requests += status_page_requests
        for row in rows:
            code = _plain_code(
                str(row.get("ts_code") or row.get("symbol") or row.get("code") or "")
            )
            if len(code) != 6 or not code.isdigit():
                continue
            if code in profiles:
                logger.warning(
                    "[security-master] ignored repeated optional "
                    "tushare profile for %s",
                    code,
                )
                continue
            profiles[code] = {
                "code": code,
                "ts_code": row.get("ts_code") or _ts_code(code),
                "name": str(row.get("name") or "").strip(),
                "industry": str(row.get("industry") or "").strip(),
                "area": str(row.get("area") or "").strip(),
                "market": str(row.get("market") or "").strip(),
                "list_date": str(row.get("list_date") or "").strip(),
                "delist_date": str(row.get("delist_date") or "").strip(),
                "list_status": str(row.get("list_status") or status).strip().upper(),
                "source": "tushare_stock_basic",
            }

    normalized: dict[str, dict] = {}
    for lifecycle in lifecycle_rows:
        code = str(lifecycle["code"])
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
                "tushare_stock_basic+baostock_stock_basic"
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
                "tushare_profile_snapshot_complete": False,
                "tushare_profile_page_requests": page_requests,
                "tushare_profile_row_count": len(profiles),
            }
        )
    return [normalized[code] for code in sorted(normalized)]


def fetch_stock_basic(
    token: str | None = None,
    mode: str | None = None,
) -> list[dict]:
    fields = "ts_code,symbol,name,area,industry,market,list_date,list_status"
    rows = query_replay_api(
        "stock_basic",
        {"list_status": "L", "fields": fields},
        token=token,
        mode=mode,
        timeout=15,
    )
    out: list[dict] = []
    for row in rows:
        code = _plain_code(str(row.get("ts_code") or row.get("symbol") or row.get("code") or ""))
        if not code or len(code) != 6:
            continue
        out.append({
            "code": code,
            "ts_code": row.get("ts_code") or _ts_code(code),
            "name": str(row.get("name") or "").strip(),
            "industry": str(row.get("industry") or "").strip(),
            "area": str(row.get("area") or "").strip(),
            "market": str(row.get("market") or "").strip(),
            "list_date": str(row.get("list_date") or "").strip(),
            "source": "tushare_stock_basic",
        })
    return out


def fetch_namechange_history(
    code: str,
    token: str | None = None,
    mode: str | None = None,
) -> list[dict]:
    ts_code = _ts_code(code)
    fields = "ts_code,name,start_date,end_date,ann_date,change_reason"
    rows = query_replay_api(
        "namechange",
        {"ts_code": ts_code, "fields": fields},
        token=token,
        mode=mode,
        timeout=15,
        max_attempts=1,
    )
    out: list[dict] = []
    for row in rows:
        row_code = _ts_code(str(row.get("ts_code") or code))
        name = str(row.get("name") or "").strip()
        dates = {}
        for key in ("start_date", "end_date", "ann_date"):
            digits = "".join(ch for ch in str(row.get(key) or "") if ch.isdigit())
            dates[key] = digits[-8:] if len(digits) >= 8 else ""
        if row_code != ts_code or not name or not dates["start_date"]:
            continue
        out.append(
            {
                "ts_code": row_code,
                "name": name,
                **dates,
                "change_reason": str(row.get("change_reason") or "").strip(),
            }
        )
    return sorted(out, key=lambda row: (row["start_date"], row["ann_date"], row["name"]))


def fetch_stock_sectors_bulk(
    codes: list[str],
    token: str | None = None,
    mode: str | None = None,
    max_concepts_per_code: int = 5,
) -> dict[str, dict]:
    clean = []
    for code in codes or []:
        c = _plain_code(str(code or ""))
        if c and c not in clean:
            clean.append(c)
    if not clean:
        return {}
    wanted = set(clean)
    result: dict[str, dict] = {}
    for row in fetch_stock_basic(token=token, mode=mode):
        code = row.get("code")
        if code in wanted and row.get("industry"):
            result[code] = {"industry": row.get("industry", ""), "concepts": [], "source": row.get("source")}

    # Concepts are optional and can be permission/rate limited, so fetch them per-code only after industry fill.
    for code in clean:
        concepts: list[str] = []
        ts_code = _ts_code(code)
        for api_name in ("ths_member", "concept_detail"):
            try:
                rows = query_replay_api(
                    api_name,
                    {"ts_code": ts_code, "fields": "ts_code,con_code,concept_code,concept_name,name"},
                    token=token,
                    mode=mode,
                    timeout=8,
                    max_attempts=1,
                )
            except Exception as exc:
                logger.debug(f"[tushare] concept {api_name} failed for {code}: {exc}")
                rows = []
            for row in rows:
                name = str(row.get("concept_name") or row.get("name") or "").strip()
                if name and name not in concepts:
                    concepts.append(name)
                if len(concepts) >= max_concepts_per_code:
                    break
            if concepts:
                break
        if concepts or code in result:
            item = result.setdefault(code, {"industry": "", "concepts": [], "source": "tushare_stock_basic"})
            item["concepts"] = concepts[:max_concepts_per_code]
            item["source"] = "tushare_sector"
    return result


def fetch_limit_pools(
    trade_date: str,
    token: str | None = None,
    mode: str | None = None,
) -> dict:
    compact = _date_text(trade_date)
    if not compact:
        compact = datetime.now().strftime("%Y%m%d")
    fields = "trade_date,ts_code,name,close,pct_chg,limit,open_times,up_stat,limit_times,industry"
    rows: list[dict] = []
    for api_name in ("limit_list_d", "limit_list"):
        try:
            rows = query_replay_api(
                api_name,
                {"trade_date": compact, "fields": fields},
                token=token,
                mode=mode,
                timeout=15,
                max_attempts=1,
            )
        except Exception as exc:
            logger.debug(f"[tushare] {api_name} failed for {compact}: {exc}")
            rows = []
        if rows:
            break
    if not rows:
        return {}

    out = {
        "zt_count": 0,
        "dt_count": 0,
        "zb_count": 0,
        "zb_rate": None,
        "max_height": 0,
        "ladder": [],
        "prev_zt_avg": None,
        "prev_zt_red_rate": None,
        "source": "tushare_limit_list",
    }
    ladder: dict[int, list[dict]] = {}
    for row in rows:
        marker = str(row.get("limit") or row.get("limit_type") or "").strip().upper()
        pct = _num(row.get("pct_chg", row.get("pct_change")))
        if marker.startswith("D") or marker in {"DOWN", "LIMIT_DOWN"} or pct <= -9.8:
            out["dt_count"] += 1
            continue
        if marker.startswith("Z") or marker in {"OPEN", "BROKEN"}:
            out["zb_count"] += 1
            continue
        if marker and not marker.startswith("U") and pct < 9.0:
            continue
        code = _plain_code(str(row.get("ts_code") or row.get("code") or ""))
        name = str(row.get("name") or row.get("stock_name") or "").strip()
        if not code or not name:
            continue
        height = int(_num(row.get("limit_times") or row.get("up_stat") or 1)) or 1
        height = max(1, height)
        ladder.setdefault(height, []).append({
            "code": code,
            "name": name,
            "industry": str(row.get("industry") or ""),
        })
        out["zt_count"] += 1
    if ladder:
        out["max_height"] = max(ladder.keys())
        out["ladder"] = [{"height": h, "stocks": ladder[h][:30]} for h in sorted(ladder.keys(), reverse=True)]
    if out["zt_count"] + out["zb_count"] > 0:
        out["zb_rate"] = round(out["zb_count"] * 100.0 / (out["zt_count"] + out["zb_count"]), 1)
    return out if out["zt_count"] or out["dt_count"] or out["zb_count"] else {}


def fetch_sector_rankings(
    trade_date: str | None = None,
    token: str | None = None,
    mode: str | None = None,
) -> dict:
    compact = _date_text(trade_date or datetime.now().strftime("%Y%m%d"))
    fields = "ts_code,trade_date,close,pct_change,pct_chg,vol,amount"
    rows = query_replay_api(
        "ths_daily",
        {"trade_date": compact, "fields": fields},
        token=token,
        mode=mode,
        timeout=15,
        max_attempts=1,
    )
    if not rows:
        return {}
    basics = query_replay_api(
        "ths_index",
        {"fields": "ts_code,name,type,exchange,list_date"},
        token=token,
        mode=mode,
        timeout=15,
        max_attempts=1,
    )
    meta = {str(row.get("ts_code") or ""): row for row in basics or []}

    industry: list[dict] = []
    concept: list[dict] = []
    for row in rows:
        ts_code = str(row.get("ts_code") or "")
        info = meta.get(ts_code, {})
        name = str(row.get("name") or info.get("name") or ts_code).strip()
        chg = _num(row.get("pct_change", row.get("pct_chg")))
        item = {"code": ts_code, "name": name, "chg": round(chg, 2)}
        typ = str(info.get("type") or "").strip().upper()
        if typ in {"I", "INDUSTRY", "HY"}:
            industry.append(item)
        elif typ in {"N", "C", "CONCEPT", "GN"}:
            concept.append(item)
        else:
            concept.append(item)
    industry = sorted(industry, key=lambda x: x.get("chg", 0), reverse=True)
    concept = sorted(concept, key=lambda x: x.get("chg", 0), reverse=True)
    return {
        "industry_top": industry[:10],
        "industry_bottom": industry[-5:] if industry else [],
        "concept_top": concept[:10],
        "source": "tushare_ths_daily",
    }


def fetch_lhb_stocks(
    trade_date: str,
    token: str | None = None,
    mode: str | None = None,
) -> list[dict]:
    compact = _date_text(trade_date)
    fields = "ts_code,name,trade_date,close,pct_change,turnover_rate,amount,l_sell,l_buy,l_amount,net_amount,reason"
    rows = query_replay_api("top_list", {"trade_date": compact, "fields": fields}, token=token, mode=mode, timeout=15)
    out: list[dict] = []
    for row in rows:
        code = _plain_code(str(row.get("ts_code") or row.get("code") or ""))
        name = str(row.get("name") or row.get("stock_name") or "").strip()
        if not code or not name:
            continue
        net = _num(row.get("net_amount", row.get("net_amt")))
        buy = _num(row.get("l_buy", row.get("buy", row.get("buy_amount"))))
        sell = _num(row.get("l_sell", row.get("sell", row.get("sell_amount"))))
        amount = _num(row.get("l_amount", row.get("amount")))
        out.append({
            "code": code,
            "name": name,
            "chg": _num(row.get("pct_change", row.get("change_rate"))),
            "net_amt": round(net / 10000, 2) if net and abs(net) > 1_000_000 else round(net, 2) if net else None,
            "buy_amt": round(buy / 10000, 2) if buy and abs(buy) > 1_000_000 else round(buy, 2) if buy else None,
            "sell_amt": round(sell / 10000, 2) if sell and abs(sell) > 1_000_000 else round(sell, 2) if sell else None,
            "close": _num(row.get("close")),
            "turnover": _num(row.get("turnover_rate")),
            "reason": str(row.get("reason") or "").strip()[:60],
            "vol": round(amount / 100000000, 2) if amount and abs(amount) > 100000000 else round(amount / 10000, 2) if amount else None,
            "youzai": [],
            "branches": [],
            "source": "tushare_top_list",
        })
    return out


def fetch_lhb_seats(
    code: str,
    trade_date: str,
    token: str | None = None,
    mode: str | None = None,
) -> list[dict]:
    compact = _date_text(trade_date)
    fields = "ts_code,trade_date,exalter,buy,sell,net_buy"
    rows = query_replay_api("top_inst", {"ts_code": _ts_code(code), "trade_date": compact, "fields": fields}, token=token, mode=mode, timeout=12)
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        name = str(row.get("exalter") or row.get("name") or row.get("seat") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        buy = _num(row.get("buy"))
        sell = _num(row.get("sell"))
        net = _num(row.get("net_buy", row.get("net")))
        if net == 0 and (buy or sell):
            net = buy - sell
        out.append({
            "name": name,
            "buy": round(buy / 10000, 2) if abs(buy) > 1_000_000 else round(buy, 2),
            "sell": round(sell / 10000, 2) if abs(sell) > 1_000_000 else round(sell, 2),
            "net": round(net / 10000, 2) if abs(net) > 1_000_000 else round(net, 2),
            "dir": "B" if net >= 0 else "S",
            "source": "tushare_top_inst",
        })
    return sorted(out, key=lambda x: x.get("net", 0), reverse=True)


def _index_ts_code(code: str, market: int | None = None) -> str:
    clean = _plain_code(str(code or ""))
    if "." in str(code or ""):
        return str(code).strip().upper()
    if market == 1 or clean.startswith(("0", "3")) and not clean.startswith("399"):
        return f"{clean}.SH"
    if market == 0 or clean.startswith("399"):
        return f"{clean}.SZ"
    return f"{clean}.SH" if clean.startswith("0") else f"{clean}.SZ"


def fetch_index_kline(
    code: str,
    days: int = 60,
    market: int | None = None,
    token: str | None = None,
    mode: str | None = None,
) -> pd.DataFrame:
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=max(int(days or 60) * 2, 120))).strftime("%Y%m%d")
    rows = query_replay_api(
        "index_daily",
        {"ts_code": _index_ts_code(code, market), "start_date": start, "end_date": end, "fields": "ts_code,trade_date,open,high,low,close,vol,amount"},
        token=token,
        mode=mode,
        timeout=12,
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"trade_date": "date", "vol": "volume"})
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["date", "close"]).sort_values("date").tail(int(days or 60)).reset_index(drop=True)


def fetch_index_quotes(
    pairs: list[tuple[int, str]],
    token: str | None = None,
    mode: str | None = None,
) -> list[dict]:
    rows: list[dict] = []
    for market, code in pairs or []:
        df = fetch_index_kline(code, days=2, market=market, token=token, mode=mode)
        if df is None or df.empty:
            continue
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else last
        price = _num(last.get("close"))
        prev_close = _num(prev.get("close"))
        pct = (price / prev_close - 1) * 100 if prev_close else 0.0
        rows.append({"code": _plain_code(str(code)), "price": price, "change_pct": round(pct, 2), "source": "tushare_index_daily"})
    return rows


def fetch_finance_info(
    code: str,
    token: str | None = None,
    mode: str | None = None,
) -> dict:
    ts_code = _ts_code(code)
    info: dict[str, Any] = {"code": _plain_code(code), "ts_code": ts_code, "source": "tushare"}
    try:
        basic_rows = query_replay_api(
            "daily_basic",
            {"ts_code": ts_code, "fields": "ts_code,trade_date,total_mv,circ_mv,pe,pe_ttm,pb,turnover_rate,volume_ratio"},
            token=token,
            mode=mode,
            timeout=12,
        )
        if basic_rows:
            row = sorted(basic_rows, key=lambda r: str(r.get("trade_date") or ""))[-1]
            info.update({
                "mktcap": round(_num(row.get("total_mv")) / 10000, 2) if row.get("total_mv") is not None else None,
                "float_cap": round(_num(row.get("circ_mv")) / 10000, 2) if row.get("circ_mv") is not None else None,
                "pe_dyn": _num(row.get("pe")),
                "pe_ttm": _num(row.get("pe_ttm")),
                "pb": _num(row.get("pb")),
                "turnover": _num(row.get("turnover_rate")),
                "volume_ratio": _num(row.get("volume_ratio")),
                "trade_date": str(row.get("trade_date") or ""),
            })
    except Exception as exc:
        logger.debug(f"[tushare] daily_basic finance failed for {code}: {exc}")
    try:
        fina_rows = query_replay_api(
            "fina_indicator",
            {"ts_code": ts_code, "fields": "ts_code,end_date,roe,roe_dt,grossprofit_margin,netprofit_margin,bps,dt_eps"},
            token=token,
            mode=mode,
            timeout=12,
        )
        if fina_rows:
            row = sorted(fina_rows, key=lambda r: str(r.get("end_date") or ""))[-1]
            info.update({
                "roe": _num(row.get("roe_dt", row.get("roe"))),
                "gross_margin": _num(row.get("grossprofit_margin")),
                "net_margin": _num(row.get("netprofit_margin")),
                "bps": _num(row.get("bps")),
                "eps": _num(row.get("dt_eps")),
                "report_date": str(row.get("end_date") or ""),
            })
    except Exception as exc:
        logger.debug(f"[tushare] fina_indicator failed for {code}: {exc}")
    return {k: v for k, v in info.items() if v not in (None, "")}


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
