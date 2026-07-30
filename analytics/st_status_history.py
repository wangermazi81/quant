"""Point-in-time ST status derived from Tushare security name intervals."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from analytics import tushare_provider


_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = _ROOT / "data" / "st_namechange_cache.json"
CACHE_VERSION = 1
DEFAULT_MAX_AGE_DAYS = 30
_CACHE_LOCK = threading.RLock()


def _plain_code(value: Any) -> str:
    raw = str(value or "").strip().upper().split(".", 1)[0]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


def is_st_name(value: Any) -> bool:
    text = "".join(str(value or "").upper().split())
    return text.startswith(("*ST", "ST", "S*ST", "SST"))


def _read_cache() -> dict:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    codes = payload.get("codes") if isinstance(payload.get("codes"), dict) else {}
    return {
        "version": CACHE_VERSION,
        "updated_at": payload.get("updated_at"),
        "source": "tushare_namechange",
        "codes": codes,
    }


def load_cache() -> dict:
    with _CACHE_LOCK:
        return _read_cache()


def _write_cache(payload: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload["version"] = CACHE_VERSION
    payload["source"] = "tushare_namechange"
    payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fd, temp_name = tempfile.mkstemp(prefix=f".{CACHE_PATH.name}.", suffix=".tmp", dir=CACHE_PATH.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, CACHE_PATH)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _parse_date(value: Any) -> pd.Timestamp | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 8:
        return None
    parsed = pd.to_datetime(digits[-8:], format="%Y%m%d", errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed).normalize()


def status_series(
    code: str,
    dates: Sequence[Any] | pd.Index,
    cache: dict | None = None,
) -> tuple[pd.Series, pd.Series]:
    index = pd.DatetimeIndex(pd.to_datetime(list(dates), errors="coerce")).normalize()
    is_st = pd.Series(False, index=index, dtype=bool)
    known = pd.Series(False, index=index, dtype=bool)
    payload = cache if isinstance(cache, dict) else load_cache()
    item = (payload.get("codes") or {}).get(_plain_code(code), {})
    records = item.get("records") if isinstance(item, dict) else []
    records = records if isinstance(records, list) else []
    for record in sorted(
        (row for row in records if isinstance(row, dict)),
        key=lambda row: str(row.get("start_date") or ""),
    ):
        start = _parse_date(record.get("start_date"))
        if start is None:
            continue
        announced = _parse_date(record.get("ann_date"))
        effective = max(start, announced) if announced is not None else start
        end = _parse_date(record.get("end_date"))
        mask = index >= effective
        if end is not None:
            mask &= index <= end
        if not bool(mask.any()):
            continue
        known.loc[mask] = True
        is_st.loc[mask] = is_st_name(record.get("name"))
    return is_st, known


def _is_fresh(item: dict, now: datetime, max_age_days: int) -> bool:
    if not isinstance(item.get("records"), list) or not item["records"]:
        return False
    try:
        fetched_at = datetime.fromisoformat(str(item.get("fetched_at") or ""))
    except ValueError:
        return False
    return fetched_at >= now - timedelta(days=max(1, int(max_age_days)))


def refresh_cache(
    codes: Sequence[str],
    token: str | None = None,
    mode: str | None = None,
    max_workers: int = 6,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    force: bool = False,
) -> dict:
    clean_codes = list(dict.fromkeys(code for code in (_plain_code(value) for value in codes) if code))
    if not clean_codes:
        return {
            "ok": False,
            "requested": 0,
            "fresh": 0,
            "fetched": 0,
            "failed": 0,
            "usable": 0,
            "coverage": 0.0,
            "fresh_coverage": 0.0,
            "usable_coverage": 0.0,
        }

    with _CACHE_LOCK:
        cache = _read_cache()
        now = datetime.now()
        stale = []
        fresh = 0
        for code in clean_codes:
            item = (cache.get("codes") or {}).get(code, {})
            if not force and _is_fresh(item, now, max_age_days):
                fresh += 1
            else:
                stale.append(code)

        fetched = 0
        errors: list[str] = []
        if stale:
            workers = max(1, min(16, int(max_workers or 1), len(stale)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="st-history") as pool:
                futures = {
                    pool.submit(tushare_provider.fetch_namechange_history, code, token=token, mode=mode): code
                    for code in stale
                }
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        records = future.result()
                    except Exception as exc:
                        records = []
                        if len(errors) < 20:
                            errors.append(f"{code}: {str(exc)[:160]}")
                    if not records:
                        continue
                    cache["codes"][code] = {
                        "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "records": records,
                    }
                    fetched += 1
            if fetched:
                _write_cache(cache)

        usable = sum(
            1
            for code in clean_codes
            if isinstance((cache.get("codes") or {}).get(code, {}).get("records"), list)
            and (cache.get("codes") or {}).get(code, {}).get("records")
        )
        fresh_usable = sum(
            1
            for code in clean_codes
            if _is_fresh((cache.get("codes") or {}).get(code, {}), now, max_age_days)
        )
        fresh_coverage = fresh_usable / len(clean_codes)
        usable_coverage = usable / len(clean_codes)
        result = {
            "ok": fresh_coverage >= 0.98,
            "requested": len(clean_codes),
            "fresh": fresh,
            "fetched": fetched,
            "failed": len(clean_codes) - fresh_usable,
            "usable": usable,
            "coverage": round(fresh_coverage, 4),
            "fresh_coverage": round(fresh_coverage, 4),
            "usable_coverage": round(usable_coverage, 4),
            "cache_path": str(CACHE_PATH),
        }
        if errors:
            result["errors"] = errors
        return result
