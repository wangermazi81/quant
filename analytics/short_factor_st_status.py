from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from analytics import st_status_history


_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = (
    _ROOT
    / "data"
    / "research"
    / "short_factor_discovery"
    / "auxiliary"
    / "baostock_st_status.json"
)
LEGACY_SHARED_CACHE_PATH = _ROOT / "data" / "st_namechange_cache.json"
CACHE_VERSION = 1
DAILY_STATUS_MIN_COVERAGE = 0.98
_CACHE_LOCK = threading.RLock()


def _plain_code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


def _empty_cache() -> dict[str, Any]:
    return {
        "version": CACHE_VERSION,
        "source": "baostock_history_k_data_isST",
        "updated_at": None,
        "codes": {},
    }


def _read_payload(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    raw_codes = payload.get("codes")
    if not isinstance(raw_codes, dict):
        return None
    codes = {
        code: value
        for raw_code, value in raw_codes.items()
        if (code := _plain_code(raw_code))
        and isinstance(value, dict)
        and (
            isinstance(value.get("daily_status"), dict)
            or isinstance(value.get("records"), list)
        )
    }
    return {
        "version": CACHE_VERSION,
        "source": "baostock_history_k_data_isST",
        "updated_at": payload.get("updated_at"),
        "codes": codes,
    }


def _read_cache() -> dict[str, Any]:
    primary = _read_payload(CACHE_PATH)
    if primary is not None:
        return primary
    migrated = _read_payload(LEGACY_SHARED_CACHE_PATH)
    return migrated if migrated is not None else _empty_cache()


def load_cache() -> dict[str, Any]:
    with _CACHE_LOCK:
        return _read_cache()


def _write_cache(payload: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload["version"] = CACHE_VERSION
    payload["source"] = "baostock_history_k_data_isST"
    payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{CACHE_PATH.name}.",
        suffix=".tmp",
        dir=CACHE_PATH.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, CACHE_PATH)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def status_series(
    code: str,
    dates: Sequence[Any],
    *,
    cache: dict[str, Any] | None = None,
) -> tuple[pd.Series, pd.Series]:
    index = pd.DatetimeIndex(pd.to_datetime(list(dates))).normalize()
    payload = cache if isinstance(cache, dict) else load_cache()
    plain_code = _plain_code(code)
    item = (payload.get("codes") or {}).get(plain_code, {})
    if isinstance(item, dict) and isinstance(item.get("records"), list):
        is_st, known = st_status_history.status_series(
            plain_code,
            index,
            cache={"codes": {plain_code: item}},
        )
    else:
        is_st = pd.Series(False, index=index, dtype=bool)
        known = pd.Series(False, index=index, dtype=bool)
    daily_status = (
        item.get("daily_status")
        if isinstance(item, dict)
        else {}
    )
    if isinstance(daily_status, dict) and daily_status:
        date_keys = pd.Series(
            index.strftime("%Y%m%d"),
            index=index,
            dtype="string",
        )
        exact_known = date_keys.isin(daily_status)
        if bool(exact_known.any()):
            exact_values = date_keys.map(daily_status)
            known.loc[exact_known] = True
            is_st.loc[exact_known] = (
                exact_values.loc[exact_known].astype(bool)
            )
    return is_st, known


def _date_text(value: Any) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"invalid ST status date: {value}")
    return pd.Timestamp(parsed).strftime("%Y%m%d")


def _missing_ranges(
    intervals: Any,
    start: str,
    end: str,
) -> list[tuple[str, str]]:
    requested_start = pd.Timestamp(start)
    requested_end = pd.Timestamp(end)
    covered = []
    if isinstance(intervals, list):
        for interval in intervals:
            if not isinstance(interval, dict):
                continue
            left = pd.to_datetime(
                str(interval.get("start") or ""),
                format="%Y%m%d",
                errors="coerce",
            )
            right = pd.to_datetime(
                str(interval.get("end") or ""),
                format="%Y%m%d",
                errors="coerce",
            )
            if pd.notna(left) and pd.notna(right) and left <= right:
                covered.append((left, right))
    cursor = requested_start
    one_day = pd.Timedelta(days=1)
    gaps = []
    for left, right in sorted(covered):
        if right < cursor:
            continue
        if left > requested_end:
            break
        if left > cursor:
            gaps.append(
                (
                    cursor.strftime("%Y%m%d"),
                    min(requested_end, left - one_day).strftime(
                        "%Y%m%d"
                    ),
                )
            )
        cursor = max(cursor, right + one_day)
        if cursor > requested_end:
            break
    if cursor <= requested_end:
        gaps.append(
            (
                cursor.strftime("%Y%m%d"),
                requested_end.strftime("%Y%m%d"),
            )
        )
    return gaps


def _merge_intervals(
    intervals: Any,
    start: str,
    end: str,
) -> list[dict[str, str]]:
    values = []
    if isinstance(intervals, list):
        for interval in intervals:
            if not isinstance(interval, dict):
                continue
            left = str(interval.get("start") or "")
            right = str(interval.get("end") or "")
            if len(left) == 8 and len(right) == 8 and left <= right:
                values.append((left, right))
    values.append((start, end))
    values.sort()
    merged: list[list[str]] = []
    for left, right in values:
        adjacent = (
            pd.Timestamp(merged[-1][1]) + pd.Timedelta(days=1)
            if merged
            else None
        )
        if not merged or pd.Timestamp(left) > adjacent:
            merged.append([left, right])
        else:
            merged[-1][1] = max(merged[-1][1], right)
    return [
        {"start": left, "end": right}
        for left, right in merged
    ]


def _baostock_symbol(code: str) -> str:
    prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
    return f"{prefix}.{code}"


def _fetch_baostock_daily_status(
    codes: Sequence[str],
    start: str,
    end: str,
) -> dict[str, Any]:
    try:
        import baostock as bs
    except Exception as exc:
        raise RuntimeError(
            "Baostock ST status provider is unavailable"
        ) from exc

    start_date = pd.Timestamp(start).strftime("%Y-%m-%d")
    end_date = pd.Timestamp(end).strftime("%Y-%m-%d")
    statuses: dict[str, dict[str, bool]] = {}
    successful_codes: list[str] = []
    errors: list[str] = []
    with redirect_stdout(StringIO()):
        login = bs.login()
        if str(getattr(login, "error_code", "")) != "0":
            raise RuntimeError(
                "Baostock ST status login failed: "
                f"{getattr(login, 'error_msg', '')}"
            )
        try:
            for code in codes:
                result = bs.query_history_k_data_plus(
                    _baostock_symbol(code),
                    "date,code,isST",
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag="2",
                )
                if str(getattr(result, "error_code", "")) != "0":
                    errors.append(
                        f"{code}: {getattr(result, 'error_msg', '')}"
                    )
                    continue
                values: dict[str, bool] = {}
                invalid = False
                while result.next():
                    raw = dict(
                        zip(result.fields, result.get_row_data())
                    )
                    raw_code = _plain_code(raw.get("code"))
                    raw_status = str(raw.get("isST") or "").strip()
                    try:
                        trade_date = _date_text(raw.get("date"))
                    except ValueError:
                        invalid = True
                        break
                    if raw_code != code or raw_status not in {"0", "1"}:
                        invalid = True
                        break
                    values[trade_date] = raw_status == "1"
                if (
                    invalid
                    or str(getattr(result, "error_code", "")) != "0"
                ):
                    errors.append(f"{code}: invalid daily ST status")
                    continue
                statuses[code] = values
                successful_codes.append(code)
        finally:
            bs.logout()
    return {
        "statuses": statuses,
        "successful_codes": successful_codes,
        "errors": errors,
    }


def refresh_daily_status_cache(
    codes: Sequence[str],
    start: Any,
    end: Any,
    *,
    batch_size: int = 50,
    force: bool = False,
) -> dict[str, Any]:
    clean_codes = list(
        dict.fromkeys(
            code
            for code in (_plain_code(value) for value in codes)
            if code
        )
    )
    start_date = _date_text(start)
    end_date = _date_text(end)
    if start_date > end_date:
        raise ValueError("ST status start must not be after end")
    if isinstance(batch_size, bool) or not 1 <= int(batch_size) <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    if not clean_codes:
        return {
            "ok": False,
            "requested": 0,
            "covered": 0,
            "coverage": 0.0,
            "errors": [],
        }

    with _CACHE_LOCK:
        cache = _read_cache()
        pending_by_range: dict[tuple[str, str], list[str]] = {}
        for code in clean_codes:
            item = cache["codes"].get(code, {})
            intervals = [] if force else item.get("daily_coverage")
            for missing in _missing_ranges(
                intervals,
                start_date,
                end_date,
            ):
                pending_by_range.setdefault(missing, []).append(code)

        errors: list[str] = []
        fetched = 0
        for (gap_start, gap_end), pending in sorted(
            pending_by_range.items()
        ):
            for offset in range(0, len(pending), int(batch_size)):
                batch = pending[offset:offset + int(batch_size)]
                try:
                    response = _fetch_baostock_daily_status(
                        batch,
                        gap_start,
                        gap_end,
                    )
                except Exception as exc:
                    errors.extend(
                        f"{code}: {str(exc)[:160]}"
                        for code in batch
                    )
                    continue
                successful = set(
                    response.get("successful_codes") or []
                )
                statuses = response.get("statuses") or {}
                errors.extend(
                    str(value)[:200]
                    for value in response.get("errors") or []
                )
                now_text = datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                for code in batch:
                    if code not in successful:
                        continue
                    item = cache["codes"].setdefault(code, {})
                    daily = item.get("daily_status")
                    if not isinstance(daily, dict):
                        daily = {}
                    daily.update(
                        {
                            str(date): bool(value)
                            for date, value in (
                                statuses.get(code) or {}
                            ).items()
                        }
                    )
                    item["daily_status"] = dict(sorted(daily.items()))
                    item["daily_coverage"] = _merge_intervals(
                        item.get("daily_coverage"),
                        gap_start,
                        gap_end,
                    )
                    item["daily_fetched_at"] = now_text
                    item["daily_source"] = (
                        "baostock_history_k_data_isST"
                    )
                    fetched += 1
                if successful:
                    _write_cache(cache)

        covered = sum(
            not _missing_ranges(
                cache["codes"].get(code, {}).get(
                    "daily_coverage"
                ),
                start_date,
                end_date,
            )
            for code in clean_codes
        )
        coverage = covered / len(clean_codes)
        if not CACHE_PATH.is_file() and cache["codes"]:
            _write_cache(cache)
        result = {
            "ok": coverage >= DAILY_STATUS_MIN_COVERAGE,
            "requested": len(clean_codes),
            "covered": covered,
            "fetched": fetched,
            "coverage": round(coverage, 6),
            "source": "baostock_history_k_data_isST",
            "cache_path": str(CACHE_PATH),
        }
        if errors:
            result["errors"] = errors[:20]
        return result


def enrich_panel_daily_status(
    panel: Mapping[str, pd.DataFrame],
    *,
    cache: dict[str, Any] | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    close = panel.get("close")
    if not isinstance(close, pd.DataFrame) or close.empty:
        raise ValueError("ST status enrichment requires a close panel")
    payload = cache if isinstance(cache, dict) else load_cache()
    is_st = pd.DataFrame(
        0.0,
        index=close.index,
        columns=close.columns,
    )
    known = pd.DataFrame(
        0.0,
        index=close.index,
        columns=close.columns,
    )
    for code in close.columns:
        code_is_st, code_known = status_series(
            str(code),
            close.index,
            cache=payload,
        )
        is_st.loc[:, code] = code_is_st.to_numpy(dtype=float)
        known.loc[:, code] = code_known.to_numpy(dtype=float)

    eligible = close.notna()
    for field in (
        "universe_eligible",
        "security_master_eligible",
        "continuity_eligible",
    ):
        value = panel.get(field)
        if not isinstance(value, pd.DataFrame):
            continue
        aligned = value.reindex(
            index=close.index,
            columns=close.columns,
        )
        eligible &= aligned.apply(
            pd.to_numeric,
            errors="coerce",
        ).fillna(0.0).gt(0.0)
    eligible_cells = int(eligible.sum().sum())
    known_cells = int(
        (known.gt(0.0) & eligible).sum().sum()
    )
    coverage = (
        known_cells / eligible_cells
        if eligible_cells
        else 0.0
    )
    enriched = dict(panel)
    enriched["is_st"] = is_st
    enriched["st_status_known"] = known
    return enriched, {
        "source": "baostock_history_k_data_isST",
        "eligible_cells": eligible_cells,
        "known_cells": known_cells,
        "coverage": round(coverage, 6),
        "minimum_coverage": DAILY_STATUS_MIN_COVERAGE,
    }
