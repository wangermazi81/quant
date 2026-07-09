# -*- coding: utf-8 -*-
"""Tushare Replay helpers for market-wide statistics.

The API key is intentionally read only from environment variables. Never put it
in source files, frontend bundles, tests, or logs.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Iterable

import requests


DEFAULT_BASE_URLS = (
    "https://ai-tool.indevs.in/tushare/pro",
    "https://tushare.indevs.in/tushare/pro",
    "https://ai-tool.indevs.in/quant/tushare/pro",
)
API_KEY_ENV = "TUSHARE_REPLAY_API_KEY"
BASE_URL_ENV = "TUSHARE_REPLAY_BASE_URL"


def is_configured() -> bool:
    return bool(os.getenv(API_KEY_ENV, "").strip())


def _compact_date(date_text: str | None) -> str:
    digits = "".join(ch for ch in str(date_text or "") if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return datetime.now().strftime("%Y%m%d")


def _iso_date(date_text: str | None) -> str:
    compact = _compact_date(date_text)
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return default


def _base_urls() -> tuple[str, ...]:
    configured = os.getenv(BASE_URL_ENV, "").strip().rstrip("/")
    if configured:
        return (configured,)
    return DEFAULT_BASE_URLS


def _extract_rows(payload) -> list[dict]:
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


def replay(api_name: str, params: dict | None = None, timeout: int = 30) -> list[dict]:
    api_key = os.getenv(API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"{API_KEY_ENV} is not configured")
    headers = {"X-API-Key": api_key, "Accept": "application/json"}
    errors = []
    for base_url in _base_urls():
        url = f"{base_url.rstrip('/')}/{api_name}"
        try:
            response = requests.get(url, params=params or {}, headers=headers, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and _safe_int(payload.get("code"), 0) not in (0, 200):
                raise RuntimeError(str(payload.get("msg") or payload.get("message") or payload.get("error") or payload))
            rows = _extract_rows(payload)
            if rows:
                return rows
            raise RuntimeError("empty tushare replay payload")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("; ".join(errors))


def _pick(row: dict, *keys: str):
    for key in keys:
        if key in row:
            return row.get(key)
    return None


def _pct_change(row: dict) -> float | None:
    value = _pick(row, "pct_chg", "change_pct", "pct", "涨跌幅", "pctChange")
    if value not in (None, "", "-"):
        return _safe_float(value)
    close = _safe_float(_pick(row, "close", "price", "最新价", "last_price"), 0.0)
    pre_close = _safe_float(_pick(row, "pre_close", "preclose", "昨收价"), 0.0)
    if close > 0 and pre_close > 0:
        return (close / pre_close - 1.0) * 100.0
    return None


def _amount_yuan(row: dict) -> float:
    return _safe_float(_pick(row, "amount", "amt", "成交额", "trade_amount"), 0.0)


def _amount_to_yi(amount_sum: float) -> float:
    # Tushare daily amount is often thousand-yuan, while realtime providers may
    # use yuan. Market totals make the unit easy to infer without hard-coding.
    if amount_sum > 100_000_000_000:
        return amount_sum / 1e8
    if amount_sum > 100_000_000:
        return amount_sum / 1e5
    return amount_sum / 1e8


def snapshot_from_rt_k_rows(rows: Iterable[dict], date_text: str | None = None, source: str = "tushare_replay_rt_k") -> dict:
    up = flat = down = limit_up = limit_down = 0
    distribution = {
        "limit_up": 0,
        "up_5_to_limit": 0,
        "up_1_to_5": 0,
        "up_0_to_1": 0,
        "flat": 0,
        "down_0_to_1": 0,
        "down_1_to_5": 0,
        "down_5_to_limit": 0,
        "limit_down": 0,
    }
    active_total = 0
    listed_total = 0
    turnover_amount = 0.0
    stat_date = _iso_date(date_text)

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        listed_total += 1
        code = str(_pick(row, "ts_code", "code", "symbol", "证券代码") or "").upper()
        pct = _pct_change(row)
        if pct is None:
            continue
        active_total += 1
        trade_date = str(_pick(row, "trade_date", "date", "交易日期") or "")
        digits = "".join(ch for ch in trade_date if ch.isdigit())
        if len(digits) >= 8:
            stat_date = _iso_date(digits[:8])

        if code.endswith(".SH") or code.endswith(".SZ") or (not code.endswith(".BJ") and len(code) == 6):
            turnover_amount += _amount_yuan(row)

        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1

        if pct >= 9.8:
            limit_up += 1
            distribution["limit_up"] += 1
        elif pct >= 5:
            distribution["up_5_to_limit"] += 1
        elif pct >= 1:
            distribution["up_1_to_5"] += 1
        elif pct > 0:
            distribution["up_0_to_1"] += 1
        elif pct == 0:
            distribution["flat"] += 1
        elif pct > -1:
            distribution["down_0_to_1"] += 1
        elif pct > -5:
            distribution["down_1_to_5"] += 1
        elif pct > -9.8:
            distribution["down_5_to_limit"] += 1
        else:
            limit_down += 1
            distribution["limit_down"] += 1

    if active_total <= 0:
        raise ValueError("empty tushare rt_k rows")
    amount = round(_amount_to_yi(turnover_amount), 2)
    coverage = round(active_total / listed_total, 3) if listed_total else 1.0
    degraded = listed_total > 0 and coverage < 0.98
    return {
        "source": source,
        "stat_date": stat_date,
        "turnover": {
            "today": round(amount, 0),
            "amount": amount,
            "source": source,
            "stat_date": stat_date,
            "degraded": amount <= 0,
        },
        "breadth": {
            "up": up,
            "flat": flat,
            "down": down,
            "total": active_total,
            "listed_total": listed_total,
            "coverage": coverage,
            "limit_up": limit_up,
            "limit_dn": limit_down,
            "distribution": distribution,
            "source": source,
            "stat_date": stat_date,
            "degraded": degraded,
            **({"reason": f"tushare active count coverage {coverage:.1%} below 98%"} if degraded else {}),
        },
    }


def fetch_rt_k_market_snapshot(date_text: str | None = None) -> dict:
    compact = _compact_date(date_text)
    rows = replay("rt_k", params={"trade_date": compact})
    snapshot = snapshot_from_rt_k_rows(rows, date_text=compact)
    requested = _iso_date(compact)
    if snapshot.get("stat_date") != requested:
        raise ValueError(f"tushare rt_k date {snapshot.get('stat_date')} does not match requested {requested}")
    return snapshot


def _daily_info_two_market_amount(rows: Iterable[dict]) -> float | None:
    rows = [row for row in rows or [] if isinstance(row, dict)]
    if not rows:
        return None

    by_code = {str(_pick(row, "ts_code", "code") or "").upper(): row for row in rows}
    preferred = ["SH_MARKET", "SZ_MARKET"]
    if all(code in by_code for code in preferred):
        amount = sum(_safe_float(_pick(by_code[code], "amount", "成交额"), 0.0) for code in preferred)
        return round(amount, 2) if amount > 0 else None

    components = ["SH_A", "SH_STAR", "SZ_MAIN", "SZ_GEM"]
    amount = sum(_safe_float(_pick(by_code[code], "amount", "成交额"), 0.0) for code in components if code in by_code)
    return round(amount, 2) if amount > 0 else None


def snapshot_from_daily_rows(rows: Iterable[dict], date_text: str | None = None) -> dict:
    return snapshot_from_rt_k_rows(rows, date_text=date_text, source="tushare_replay_daily")


def fetch_daily_market_snapshot(date_text: str | None = None) -> dict:
    compact = _compact_date(date_text)
    rows = replay("daily", params={"trade_date": compact})
    snapshot = snapshot_from_daily_rows(rows, date_text=compact)
    requested = _iso_date(compact)
    if snapshot.get("stat_date") != requested:
        raise ValueError(f"tushare daily date {snapshot.get('stat_date')} does not match requested {requested}")

    try:
        info_rows = replay("daily_info", params={"trade_date": compact})
        amount = _daily_info_two_market_amount(info_rows)
    except Exception:
        amount = None
    if amount:
        snapshot["turnover"].update(
            {
                "today": round(amount, 0),
                "amount": amount,
                "source": "tushare_replay_daily_info",
                "stat_date": requested,
                "degraded": False,
            }
        )
    return snapshot
