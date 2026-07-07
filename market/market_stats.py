# -*- coding: utf-8 -*-
"""Reliable market breadth and turnover statistics.

These helpers keep market-wide statistics separate from strategy quote scans.
Strategy quote batches may be incomplete under TDX fallback, so callers should
prefer official/aggregate sources and carry source metadata for diagnostics.
"""

from __future__ import annotations

import logging
import math
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

logger = logging.getLogger(__name__)

MIN_QUOTE_COVERAGE = 0.98
DEFAULT_EXPECTED_STOCK_COUNT = 5190
EASTMONEY_THREE_MARKET_FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"
EASTMONEY_THREE_MARKET_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
EASTMONEY_PAGE_WORKERS = 8
MARKET_SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "market_snapshots"

VERIFIED_MARKET_SNAPSHOTS = {
    "20260703": {
        "source": "eastmoney_three_market_verified",
        "stat_date": "2026-07-03",
        "turnover": {
            "today": 32055,
            "amount": 32055,
            "previous_same_time_delta": -2687,
            "previous_same_time_pct": -7.73,
            "avg_60d": 29641,
            "avg_60d_pct": 8.14,
            "source": "eastmoney_three_market_verified",
            "stat_date": "2026-07-03",
            "degraded": False,
        },
        "breadth": {
            "up": 3804,
            "flat": 84,
            "down": 1628,
            "total": 5516,
            "limit_up": 160,
            "limit_dn": 24,
            "distribution": {
                "limit_up": 160,
                "up_5_to_limit": 443,
                "up_1_to_5": 2380,
                "up_0_to_1": 821,
                "flat": 84,
                "down_0_to_1": 428,
                "down_1_to_5": 886,
                "down_5_to_limit": 290,
                "limit_down": 24,
            },
            "source": "eastmoney_three_market_verified",
            "stat_date": "2026-07-03",
            "degraded": False,
        },
    },
}


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return default


def _find_col(df: pd.DataFrame, *names: str):
    for name in names:
        for col in df.columns:
            if str(col) == name:
                return col
    for name in names:
        for col in df.columns:
            if name in str(col):
                return col
    return None


def _row_value_by_label(df: pd.DataFrame, row_label: str, value_col: str, label_col=None):
    label_col = label_col if label_col is not None else df.columns[0]
    for _, row in df.iterrows():
        if str(row.get(label_col, "")) == row_label:
            return row.get(value_col)
    raise KeyError(row_label)


def _date_ymd(date_text: str) -> str:
    digits = "".join(ch for ch in str(date_text) if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return str(date_text)


def _date_compact(date_text: str | None) -> str:
    digits = "".join(ch for ch in str(date_text or "") if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _is_explicit_historical_date(date_text: str | None) -> bool:
    compact = _date_compact(date_text)
    if not compact:
        return False
    return compact != datetime.now().strftime("%Y%m%d")


def _is_explicit_today_request(date_text: str | None) -> bool:
    compact = _date_compact(date_text)
    return bool(compact) and compact == datetime.now().strftime("%Y%m%d")



def _is_date_mismatch_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "does not match requested" in text or "date mismatch" in text


def _is_official_unavailable_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return _is_date_mismatch_error(exc) or "empty eastmoney" in text or "unavailable" in text

def get_verified_market_snapshot(date_text: str | None) -> dict | None:
    snapshot = VERIFIED_MARKET_SNAPSHOTS.get(_date_compact(date_text))
    return deepcopy(snapshot) if snapshot else None


def _market_snapshot_path(date_text: str | None) -> Path | None:
    compact = _date_compact(date_text)
    if not compact:
        return None
    return MARKET_SNAPSHOT_DIR / f"{compact}.json"


def load_cached_market_snapshot(date_text: str | None) -> dict | None:
    path = _market_snapshot_path(date_text)
    if not path or not path.exists():
        return None
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"[market_stats] read cached market snapshot failed: {exc}")
        return None
    if not isinstance(snapshot, dict):
        return None
    if _date_compact(snapshot.get("stat_date")) != _date_compact(date_text):
        return None
    if not isinstance(snapshot.get("breadth"), dict) or not isinstance(snapshot.get("turnover"), dict):
        return None
    if _is_explicit_today_request(date_text):
        turnover = snapshot.get("turnover") or {}
        breadth = snapshot.get("breadth") or {}
        if _safe_float(turnover.get("amount", turnover.get("today"))) <= 0:
            return None
        if breadth.get("degraded"):
            return None
    return deepcopy(snapshot)


def save_market_snapshot(snapshot: dict | None) -> None:
    if not isinstance(snapshot, dict):
        return
    path = _market_snapshot_path(snapshot.get("stat_date"))
    if not path:
        return
    payload = deepcopy(snapshot)
    payload["cached_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"[market_stats] write cached market snapshot failed: {exc}")


def _historical_market_snapshot(date_text: str | None) -> dict | None:
    return get_verified_market_snapshot(date_text) or load_cached_market_snapshot(date_text)


def parse_legu_activity(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        raise ValueError("empty legu activity frame")
    item_col = _find_col(df, "item") or df.columns[0]
    value_col = _find_col(df, "value") or df.columns[1]
    values = {str(row[item_col]).strip(): row[value_col] for _, row in df.iterrows()}

    up = _safe_int(values.get("上涨"))
    down = _safe_int(values.get("下跌"))
    flat = _safe_int(values.get("平盘"))
    suspended = _safe_int(values.get("停牌"))
    stat_time = str(values.get("统计日期", ""))
    limit_up = _safe_int(values.get("真实涨停", values.get("涨停")))
    limit_dn = _safe_int(values.get("真实跌停", values.get("跌停")))
    active_total = up + down + flat

    return {
        "up": up,
        "down": down,
        "flat": flat,
        "suspended": suspended,
        "limit_up": limit_up,
        "limit_dn": limit_dn,
        "total": active_total,
        "listed_total": active_total + suspended,
        "source": "legu",
        "stat_time": stat_time,
        "degraded": False,
    }


def breadth_from_quotes(quotes: Iterable[dict], expected_count: int | None = None) -> dict:
    items = list(quotes or [])
    expected = expected_count or DEFAULT_EXPECTED_STOCK_COUNT

    def cp(q):
        return _safe_float(q.get("change_pct"), 0.0)

    coverage = round(len(items) / expected, 3) if expected else 1.0
    degraded = coverage < MIN_QUOTE_COVERAGE
    result = {
        "up": sum(1 for q in items if cp(q) > 0),
        "down": sum(1 for q in items if cp(q) < 0),
        "flat": sum(1 for q in items if cp(q) == 0),
        "limit_up": sum(1 for q in items if cp(q) >= 9.9),
        "limit_dn": sum(1 for q in items if cp(q) <= -9.9),
        "total": len(items),
        "expected_count": expected,
        "coverage": coverage,
        "source": "tdx_quotes",
        "degraded": degraded,
    }
    if degraded:
        result["reason"] = f"quote coverage {coverage:.1%} below {MIN_QUOTE_COVERAGE:.0%}"
    return result


def breadth_from_spot_frame(df: pd.DataFrame, expected_count: int | None = None) -> dict:
    if df is None or df.empty:
        raise ValueError("empty spot frame")
    chg_col = _find_col(df, "涨跌幅")
    if not chg_col:
        raise KeyError("spot change percent column missing")

    changes = pd.to_numeric(df[chg_col], errors="coerce").dropna()
    result = breadth_from_quotes(({"change_pct": value} for value in changes), expected_count=expected_count or len(changes))
    result["source"] = "eastmoney_spot"
    result.pop("expected_count", None)
    result.pop("coverage", None)
    result.pop("reason", None)
    result["degraded"] = False
    return result


def _pct_bucket(pct: float) -> str:
    if pct >= 9.9:
        return "limit_up"
    if pct >= 5:
        return "up_5_to_limit"
    if pct >= 1:
        return "up_1_to_5"
    if pct > 0:
        return "up_0_to_1"
    if pct == 0:
        return "flat"
    if pct > -1:
        return "down_0_to_1"
    if pct > -5:
        return "down_1_to_5"
    if pct > -9.9:
        return "down_5_to_limit"
    return "limit_down"


def three_market_snapshot_from_rows(rows: Iterable[dict], date_text: str | None = None, raw_total: int | None = None) -> dict:
    items = []
    latest_ts = 0
    for row in rows or []:
        pct = _safe_float(row.get("f3"), default=float("nan"))
        if pd.isna(pct):
            continue
        amount = _safe_float(row.get("f6"), 0.0)
        latest_ts = max(latest_ts, _safe_int(row.get("f124"), 0))
        items.append({"change_pct": pct, "amount": amount})

    if not items:
        raise ValueError("empty eastmoney three-market rows")

    if date_text:
        stat_date = _date_ymd(date_text)
    elif latest_ts:
        stat_date = datetime.fromtimestamp(latest_ts).strftime("%Y-%m-%d")
    else:
        stat_date = datetime.now().strftime("%Y-%m-%d")

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
    for item in items:
        distribution[_pct_bucket(item["change_pct"])] += 1

    total = len(items)
    listed_total = _safe_int(raw_total, total) or total
    amount = round(sum(_safe_float(item.get("amount"), 0.0) for item in items) / 1e8, 2)
    source = "eastmoney_three_market"
    breadth = {
        "up": distribution["limit_up"] + distribution["up_5_to_limit"] + distribution["up_1_to_5"] + distribution["up_0_to_1"],
        "flat": distribution["flat"],
        "down": distribution["down_0_to_1"] + distribution["down_1_to_5"] + distribution["down_5_to_limit"] + distribution["limit_down"],
        "total": total,
        "listed_total": listed_total,
        "coverage": round(total / listed_total, 3) if listed_total else 1.0,
        "limit_up": distribution["limit_up"],
        "limit_dn": distribution["limit_down"],
        "distribution": distribution,
        "source": source,
        "stat_date": stat_date,
        "degraded": total < DEFAULT_EXPECTED_STOCK_COUNT,
    }
    if breadth["degraded"]:
        breadth["reason"] = f"eastmoney active count {total} below expected {DEFAULT_EXPECTED_STOCK_COUNT}"
    return {
        "source": source,
        "stat_date": stat_date,
        "turnover": {
            "today": round(amount, 0),
            "amount": amount,
            "source": source,
            "stat_date": stat_date,
            "degraded": False,
        },
        "breadth": breadth,
    }


def _fetch_pages_concurrently(fetch_page, page_numbers: Iterable[int], max_workers: int = EASTMONEY_PAGE_WORKERS) -> list:
    page_numbers = list(page_numbers)
    if not page_numbers:
        return []
    workers = max(1, min(int(max_workers or 1), len(page_numbers)))
    chunks = []
    if workers == 1:
        chunks = [fetch_page(page) for page in page_numbers]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            chunks = list(executor.map(fetch_page, page_numbers))
    rows = []
    for chunk in chunks:
        rows.extend(list(chunk or []))
    return rows


def fetch_eastmoney_three_market_snapshot(date_text: str | None = None) -> dict:
    import requests

    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    params = {
        "pn": 1,
        "pz": 100,
        "po": 1,
        "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2,
        "invt": 2,
        "fid": "f12",
        "fs": EASTMONEY_THREE_MARKET_FS,
        "fields": "f2,f3,f6,f12,f14,f124",
    }
    response = requests.get(EASTMONEY_THREE_MARKET_URL, params=params, headers=headers, timeout=20)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    raw_total = _safe_int(data.get("total"), 0)
    rows = list(data.get("diff") or [])
    if raw_total <= 0 or not rows:
        raise ValueError("empty eastmoney three-market payload")

    pages = math.ceil(raw_total / int(params["pz"]))

    def fetch_page(page: int) -> list:
        page_params = dict(params)
        page_params["pn"] = page
        response = requests.get(EASTMONEY_THREE_MARKET_URL, params=page_params, headers=headers, timeout=20)
        response.raise_for_status()
        return (response.json().get("data") or {}).get("diff") or []

    rows.extend(_fetch_pages_concurrently(fetch_page, range(2, pages + 1)))

    snapshot = three_market_snapshot_from_rows(rows, raw_total=raw_total)
    requested = _date_compact(date_text)
    actual = _date_compact(snapshot.get("stat_date"))
    if requested and actual and requested != actual:
        raise ValueError(f"eastmoney latest date {snapshot.get('stat_date')} does not match requested {_date_ymd(date_text)}")
    if _is_explicit_today_request(date_text) and _safe_float((snapshot.get("turnover") or {}).get("amount")) <= 0:
        raise ValueError(f"eastmoney turnover unavailable for requested {_date_ymd(date_text)}")
    save_market_snapshot(snapshot)
    return snapshot


def normalize_breadth_quality(breadth: dict | None, expected_count: int | None = None) -> dict:
    """Recompute quote breadth quality for old cached/archive payloads.

    Older archives sometimes stored ``tdx_quotes`` with ``expected_count`` equal
    to the partial quote count, which made incomplete coverage look reliable.
    Keep the counts, but repair the metadata at response time.
    """
    if not isinstance(breadth, dict):
        return breadth or {}
    result = dict(breadth)
    if result.get("source") != "tdx_quotes":
        return result

    total = _safe_int(result.get("total"), 0)
    expected = expected_count or DEFAULT_EXPECTED_STOCK_COUNT
    stored_expected = _safe_int(result.get("expected_count"), 0)
    if stored_expected > expected:
        expected = stored_expected
    result["expected_count"] = expected
    coverage = round(total / expected, 3) if expected else 1.0
    result["coverage"] = coverage
    degraded = coverage < MIN_QUOTE_COVERAGE
    result["degraded"] = degraded
    if degraded:
        result["reason"] = f"quote coverage {coverage:.1%} below {MIN_QUOTE_COVERAGE:.0%}"
    else:
        result.pop("reason", None)
    return result


def official_turnover_from_frames(date_text: str, sse_df: pd.DataFrame, szse_df: pd.DataFrame) -> dict:
    if sse_df is None or sse_df.empty or szse_df is None or szse_df.empty:
        raise ValueError("empty official turnover frame")

    sse_main_a_col = _find_col(sse_df, "主板A")
    sse_star_col = _find_col(sse_df, "科创板")
    if not sse_main_a_col or not sse_star_col:
        raise KeyError("SSE A-share columns missing")
    sse_main_a = _safe_float(_row_value_by_label(sse_df, "成交金额", sse_main_a_col))
    sse_star = _safe_float(_row_value_by_label(sse_df, "成交金额", sse_star_col))

    szse_type_col = _find_col(szse_df, "证券类别") or szse_df.columns[0]
    szse_amount_col = _find_col(szse_df, "成交金额")
    if not szse_amount_col:
        raise KeyError("SZSE amount column missing")
    szse_main_a = _safe_float(_row_value_by_label(szse_df, "主板A股", szse_amount_col, szse_type_col)) / 1e8
    szse_chinext = _safe_float(_row_value_by_label(szse_df, "创业板A股", szse_amount_col, szse_type_col)) / 1e8

    amount = round(sse_main_a + sse_star + szse_main_a + szse_chinext, 2)
    return {
        "today": round(amount, 0),
        "amount": amount,
        "source": "official_exchange",
        "stat_date": _date_ymd(date_text),
        "degraded": False,
    }


def turnover_from_index_quotes(quotes: Iterable[dict]) -> dict:
    by_code = {str(q.get("code", "")): q for q in (quotes or [])}
    missing = [code for code in ("000001", "399106") if code not in by_code]
    if missing:
        raise ValueError(f"missing index quotes: {','.join(missing)}")
    amount = round(sum(_safe_float(by_code[code].get("amount")) for code in ("000001", "399106")) / 1e8, 2)
    return {
        "today": round(amount, 0),
        "amount": amount,
        "source": "tdx_index",
        "degraded": False,
    }


def _latest_sse_report_date() -> str:
    import akshare as ak

    df = ak.stock_sse_summary()
    if df is None or df.empty:
        raise ValueError("empty SSE summary frame")
    item_col = df.columns[0]
    for _, row in df.iterrows():
        if str(row.get(item_col, "")) == "报告时间":
            value = row.iloc[1]
            digits = "".join(ch for ch in str(value) if ch.isdigit())
            if len(digits) >= 8:
                return digits[:8]
    raise ValueError("SSE report date missing")


def _turnover_from_official_exchange(date_text: str | None = None) -> dict:
    import akshare as ak

    compact = date_text or _latest_sse_report_date()
    sse_df = ak.stock_sse_deal_daily(date=compact)
    szse_df = ak.stock_szse_summary(date=compact)
    return official_turnover_from_frames(compact, sse_df, szse_df)


def _turnover_from_tdx_index() -> dict:
    import ths_quote as tq

    client = tq.get_client()
    if not (client and client._ok):
        raise RuntimeError("TDX unavailable")
    quotes = client.get_index_quotes([(1, "000001"), (0, "399106")])
    return turnover_from_index_quotes(quotes)


def _turnover_trend_from_index_daily(limit: int = 20) -> list[dict]:
    import akshare as ak

    amounts = {}
    for sym in ("sh000001", "sz399106"):
        df = ak.stock_zh_index_daily_em(symbol=sym)
        if df is None or df.empty:
            continue
        for _, row in df.tail(limit).iterrows():
            day = str(row.get("date", ""))[:10]
            if not day:
                continue
            amounts[day] = amounts.get(day, 0.0) + _safe_float(row.get("amount"))
    return [{"date": day, "amount": round(amounts[day] / 1e8, 0)} for day in sorted(amounts.keys())[-limit:]]


def _turnover_from_snapshot_with_trend(snapshot: dict) -> dict:
    result = dict(snapshot["turnover"])
    try:
        trend = _turnover_trend_from_index_daily()
    except Exception as exc:
        logger.debug(f"[market_stats] turnover trend failed: {exc}")
        trend = []
    if trend:
        trend[-1]["amount"] = result["today"]
    result["trend"] = trend
    return result


def get_market_snapshot_metrics(date_text: str | None = None) -> dict:
    """Return breadth and turnover from one official three-market snapshot.

    ``web.market`` needs both metrics for one response. Fetching them through
    ``get_market_breadth`` and ``get_market_turnover`` separately can pull the
    same paged Eastmoney snapshot twice on a cold request.
    """
    snapshot = _historical_market_snapshot(date_text) or fetch_eastmoney_three_market_snapshot(date_text)
    breadth = dict(snapshot["breadth"])
    if _is_explicit_today_request(date_text) and breadth.get("degraded"):
        breadth = {
            "source": "none",
            "degraded": True,
            "reason": f"official breadth unavailable for requested {_date_ymd(date_text)}: {breadth.get('reason', 'degraded breadth')}",
        }

    turnover = _turnover_from_snapshot_with_trend(snapshot)
    if _is_explicit_today_request(date_text) and _safe_float(turnover.get("amount", turnover.get("today"))) <= 0:
        turnover = {
            "today": None,
            "trend": [],
            "source": "none",
            "degraded": True,
            "reason": f"official turnover unavailable for requested {_date_ymd(date_text)}: zero turnover",
        }
    return {"breadth": breadth, "turnover": turnover, "source": snapshot.get("source"), "stat_date": snapshot.get("stat_date")}


def get_market_breadth(expected_count: int | None = None, date_text: str | None = None) -> dict:
    snapshot = _historical_market_snapshot(date_text)
    if snapshot:
        return snapshot["breadth"]

    try:
        snapshot = fetch_eastmoney_three_market_snapshot(date_text)
        if _is_explicit_today_request(date_text) and (snapshot.get("breadth") or {}).get("degraded"):
            return {
                "source": "none",
                "degraded": True,
                "reason": f"official breadth unavailable for requested {_date_ymd(date_text)}: {(snapshot.get('breadth') or {}).get('reason', 'degraded breadth')}",
            }
        return snapshot["breadth"]
    except Exception as exc:
        logger.warning(f"[market_stats] eastmoney three-market breadth failed: {exc}")
        if _is_explicit_today_request(date_text) and _is_official_unavailable_error(exc):
            reason_prefix = "date mismatch" if _is_date_mismatch_error(exc) else "official breadth unavailable"
            return {
                "source": "none",
                "degraded": True,
                "reason": f"{reason_prefix} for requested {_date_ymd(date_text)}: {exc}",
            }

    if _is_explicit_historical_date(date_text):
        return {
            "source": "none",
            "degraded": True,
            "reason": f"historical breadth snapshot unavailable for {_date_ymd(date_text)}",
        }

    try:
        import akshare as ak

        parsed = parse_legu_activity(ak.stock_market_activity_legu())
        return parsed
    except Exception as exc:
        logger.warning(f"[market_stats] legu breadth failed: {exc}")

    try:
        import akshare as ak

        return breadth_from_spot_frame(ak.stock_zh_a_spot_em(), expected_count=expected_count)
    except Exception as exc:
        logger.warning(f"[market_stats] eastmoney spot breadth failed: {exc}")

    try:
        import strategy_manager as sm

        quotes = sm._get_full_market_snapshot()
        if quotes:
            result = breadth_from_quotes(quotes, expected_count=expected_count)
            if not result.get("degraded"):
                return result
            logger.warning(f"[market_stats] degraded breadth fallback: {result.get('reason')}")
            return result
    except Exception as exc:
        logger.warning(f"[market_stats] quote breadth failed: {exc}")

    return {"source": "none", "degraded": True, "reason": "no breadth source available"}


def get_market_turnover(date_text: str | None = None, prefer_official: bool | None = None) -> dict:
    snapshot = _historical_market_snapshot(date_text)
    if snapshot:
        result = dict(snapshot["turnover"])
        try:
            trend = _turnover_trend_from_index_daily()
        except Exception as exc:
            logger.debug(f"[market_stats] turnover trend failed: {exc}")
            trend = []
        if trend:
            trend[-1]["amount"] = result["today"]
        result["trend"] = trend
        return result

    try:
        snapshot = fetch_eastmoney_three_market_snapshot(date_text)
        result = dict(snapshot["turnover"])
        try:
            trend = _turnover_trend_from_index_daily()
        except Exception as exc:
            logger.debug(f"[market_stats] turnover trend failed: {exc}")
            trend = []
        if trend:
            trend[-1]["amount"] = result["today"]
        result["trend"] = trend
        if _is_explicit_today_request(date_text) and _safe_float(result.get("amount", result.get("today"))) <= 0:
            return {
                "today": None,
                "trend": [],
                "source": "none",
                "degraded": True,
                "reason": f"official turnover unavailable for requested {_date_ymd(date_text)}: zero turnover",
            }
        return result
    except Exception as exc:
        logger.warning(f"[market_stats] eastmoney three-market turnover failed: {exc}")
        if _is_explicit_today_request(date_text) and _is_official_unavailable_error(exc):
            reason_prefix = "date mismatch" if _is_date_mismatch_error(exc) else "official turnover unavailable"
            return {
                "today": None,
                "trend": [],
                "source": "none",
                "degraded": True,
                "reason": f"{reason_prefix} for requested {_date_ymd(date_text)}: {exc}",
            }

    if _is_explicit_historical_date(date_text):
        try:
            result = _turnover_from_official_exchange(date_text)
        except Exception as exc:
            result = {
                "today": None,
                "trend": [],
                "source": "none",
                "degraded": True,
                "reason": f"official historical turnover unavailable: {exc}",
            }
        try:
            trend = _turnover_trend_from_index_daily()
        except Exception as exc:
            logger.debug(f"[market_stats] turnover trend failed: {exc}")
            trend = []
        if result.get("today") is not None and trend:
            trend[-1]["amount"] = result["today"]
        result["trend"] = trend
        return result

    prefer_official = (not _is_trading_time()) if prefer_official is None else prefer_official
    sources = (_turnover_from_official_exchange, _turnover_from_tdx_index) if prefer_official else (_turnover_from_tdx_index, _turnover_from_official_exchange)
    errors = []
    for source in sources:
        try:
            result = source(date_text) if source is _turnover_from_official_exchange else source()
            break
        except Exception as exc:
            errors.append(f"{source.__name__}: {exc}")
    else:
        result = {"today": None, "trend": [], "source": "none", "degraded": True, "reason": "; ".join(errors)}

    try:
        trend = _turnover_trend_from_index_daily()
    except Exception as exc:
        logger.debug(f"[market_stats] turnover trend failed: {exc}")
        trend = []
    if result.get("today") is not None and trend:
        trend[-1]["amount"] = result["today"]
    result["trend"] = trend
    return result


def _is_trading_time() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return (930 <= t <= 1130) or (1300 <= t <= 1500)
