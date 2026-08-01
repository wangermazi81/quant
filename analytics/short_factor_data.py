from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from analytics import agudata_provider, datasource_config


ROOT = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "research"
    / "short_factor_discovery"
    / "auxiliary"
)
DAILY_BASIC_COLUMNS = [
    "code",
    "date",
    "turnover_rate",
    "turnover_rate_f",
    "volume_ratio",
    "float_share",
    "total_share",
    "float_mv",
    "circ_mv",
]
AUCTION_COLUMNS = [
    "code",
    "date",
    "auction_volume",
    "auction_price",
    "auction_amount",
    "pre_close",
    "auction_turnover_rate",
    "auction_volume_ratio",
    "float_share",
]
BAOSTOCK_TURNOVER_COLUMNS = [
    "code",
    "date",
    "turnover_rate",
]


def _date_text(value: Any) -> str:
    text = "".join(
        character
        for character in str(value or "")
        if character.isdigit()
    )[:8]
    parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"invalid history date: {value}")
    return parsed.strftime("%Y%m%d")


def _codes(values) -> list[str]:
    result = []
    seen = set()
    for value in values or []:
        raw = str(value or "").strip().upper()
        digits = "".join(
            character for character in raw if character.isdigit()
        )
        code = digits[-6:] if digits else ""
        if len(code) != 6 or code in seen:
            continue
        seen.add(code)
        result.append(code)
    return sorted(result)


def _empty(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def _read_csv(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.is_file():
        return _empty(columns)
    try:
        data = pd.read_csv(
            path,
            parse_dates=["date"],
            dtype={"code": "string"},
        )
    except Exception:
        return _empty(columns)
    for column in columns:
        if column not in data.columns:
            data[column] = pd.NA
    if "code" in data:
        data["code"] = (
            data["code"]
            .astype("string")
            .str.replace(r"\.0$", "", regex=True)
            .str.zfill(6)
        )
    return data[columns]


def _write_csv_atomic(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8")
    temporary.replace(path)


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "daily_basic": {}, "auction": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "daily_basic": {}, "auction": {}}
    if not isinstance(value, dict) or int(value.get("version") or 0) != 1:
        return {"version": 1, "daily_basic": {}, "auction": {}}
    value.setdefault("daily_basic", {})
    value.setdefault("auction", {})
    value.setdefault("baostock_turnover", {})
    return value


def _write_manifest_atomic(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _range_covered(
    intervals: Any,
    start: str,
    end: str,
) -> bool:
    return not _missing_ranges(intervals, start, end)


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
    gaps = []
    one_day = pd.Timedelta(days=1)
    for left, right in sorted(covered):
        if right < cursor:
            continue
        if left > requested_end:
            break
        if left > cursor:
            gap_end = min(requested_end, left - one_day)
            gaps.append(
                (
                    cursor.strftime("%Y%m%d"),
                    gap_end.strftime("%Y%m%d"),
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
        adjacent_limit = (
            pd.Timestamp(merged[-1][1]) + pd.Timedelta(days=1)
            if merged
            else None
        )
        if (
            not merged
            or pd.Timestamp(left) > adjacent_limit
        ):
            merged.append([left, right])
        else:
            merged[-1][1] = max(merged[-1][1], right)
    return [
        {"start": left, "end": right}
        for left, right in merged
    ]


def _merge_history(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
    columns: list[str],
) -> pd.DataFrame:
    if incoming is None or incoming.empty:
        return existing
    combined = (
        incoming.copy()
        if existing.empty
        else pd.concat(
            [existing, incoming],
            ignore_index=True,
        )
    )
    combined["code"] = (
        combined["code"]
        .astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.zfill(6)
    )
    combined["date"] = pd.to_datetime(
        combined["date"],
        errors="coerce",
    ).dt.normalize()
    return (
        combined.dropna(subset=["date"])
        .drop_duplicates(["code", "date"], keep="last")
        .sort_values(["code", "date"])
        .reset_index(drop=True)[columns]
    )


def _refresh_dataset(
    *,
    name: str,
    codes: list[str],
    start: str,
    end: str,
    fetcher: Callable[..., pd.DataFrame],
    columns: list[str],
    root: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    api_key: str,
    base_url: str | None,
    batch_size: int,
) -> pd.DataFrame:
    path = root / f"{name}.csv"
    frame = _read_csv(path, columns)
    coverage = manifest.setdefault(name, {})
    pending_by_range: dict[
        tuple[str, str],
        list[str],
    ] = {}
    for code in codes:
        for missing in _missing_ranges(
            coverage.get(code),
            start,
            end,
        ):
            pending_by_range.setdefault(missing, []).append(code)
    for (gap_start, gap_end), pending in sorted(
        pending_by_range.items()
    ):
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset:offset + batch_size]
            incoming = fetcher(
                batch,
                gap_start,
                gap_end,
                api_key=api_key,
                base_url=base_url,
            )
            frame = _merge_history(frame, incoming, columns)
            _write_csv_atomic(path, frame)
            for code in batch:
                coverage[code] = _merge_intervals(
                    coverage.get(code),
                    gap_start,
                    gap_end,
                )
            _write_manifest_atomic(manifest_path, manifest)
    return frame


def refresh_agudata_auxiliary_history(
    codes,
    start: str,
    end: str,
    *,
    root: str | Path = ROOT,
    api_key: str | None = None,
    base_url: str | None = None,
    batch_size: int = 50,
) -> dict[str, Any]:
    if isinstance(batch_size, bool) or not 1 <= int(batch_size) <= 50:
        raise ValueError("batch_size must be between 1 and 50")
    normalized_codes = _codes(codes)
    start_date = _date_text(start)
    end_date = _date_text(end)
    if start_date > end_date:
        raise ValueError("history start must not be after end")
    destination = Path(root)
    manifest_path = destination / "coverage.json"
    manifest = _read_manifest(manifest_path)
    config = datasource_config.get_internal_config()
    resolved_key = str(
        api_key
        or config.get("agudata_api_key")
        or datasource_config.resolve_agudata_api_key(config)
    ).strip()
    if not resolved_key:
        raise ValueError("Agudata API key is not configured")
    resolved_url = str(
        base_url
        or config.get("agudata_base_url")
        or ""
    ).strip() or None
    daily_basic = _refresh_dataset(
        name="daily_basic",
        codes=normalized_codes,
        start=start_date,
        end=end_date,
        fetcher=agudata_provider.fetch_daily_basic_history,
        columns=DAILY_BASIC_COLUMNS,
        root=destination,
        manifest=manifest,
        manifest_path=manifest_path,
        api_key=resolved_key,
        base_url=resolved_url,
        batch_size=int(batch_size),
    )
    auction = _refresh_dataset(
        name="auction",
        codes=normalized_codes,
        start=start_date,
        end=end_date,
        fetcher=agudata_provider.fetch_auction_history,
        columns=AUCTION_COLUMNS,
        root=destination,
        manifest=manifest,
        manifest_path=manifest_path,
        api_key=resolved_key,
        base_url=resolved_url,
        batch_size=int(batch_size),
    )
    return {
        "ok": True,
        "provider": "agudata",
        "code_count": len(normalized_codes),
        "start": start_date,
        "end": end_date,
        "daily_basic_rows": int(len(daily_basic)),
        "auction_rows": int(len(auction)),
        "root": str(destination),
    }


def _baostock_symbol(code: str) -> str:
    prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
    return f"{prefix}.{code}"


def _baostock_turnover_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    parsed = pd.to_numeric(
        pd.Series([value]),
        errors="coerce",
    ).iloc[0]
    if pd.isna(parsed) or float(parsed) < 0.0:
        raise ValueError("invalid Baostock turnover value")
    return float(parsed)


def _fetch_baostock_turnover_batch(
    codes: list[str],
    start: str,
    end: str,
) -> dict[str, Any]:
    try:
        import baostock as bs
    except Exception as exc:
        raise RuntimeError(
            "Baostock turnover provider is unavailable"
        ) from exc
    start_date = pd.Timestamp(start).strftime("%Y-%m-%d")
    end_date = pd.Timestamp(end).strftime("%Y-%m-%d")
    rows = []
    successful_codes = []
    errors = []
    with redirect_stdout(StringIO()):
        login = bs.login()
        if str(getattr(login, "error_code", "")) != "0":
            raise RuntimeError(
                "Baostock turnover login failed: "
                f"{getattr(login, 'error_msg', '')}"
            )
        try:
            for code in codes:
                result = bs.query_history_k_data_plus(
                    _baostock_symbol(code),
                    "date,code,turn",
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
                code_rows = []
                invalid = False
                while result.next():
                    raw = dict(
                        zip(result.fields, result.get_row_data())
                    )
                    raw_code = _codes([raw.get("code")])
                    trade_date = pd.to_datetime(
                        raw.get("date"),
                        errors="coerce",
                    )
                    try:
                        turnover = _baostock_turnover_value(
                            raw.get("turn")
                        )
                    except ValueError:
                        invalid = True
                        break
                    if (
                        raw_code != [code]
                        or pd.isna(trade_date)
                    ):
                        invalid = True
                        break
                    if turnover is None:
                        continue
                    code_rows.append(
                        {
                            "code": code,
                            "date": pd.Timestamp(
                                trade_date
                            ).normalize(),
                            "turnover_rate": turnover,
                        }
                    )
                if (
                    invalid
                    or str(getattr(result, "error_code", "")) != "0"
                ):
                    errors.append(
                        f"{code}: invalid turnover history"
                    )
                    continue
                rows.extend(code_rows)
                successful_codes.append(code)
        finally:
            bs.logout()
    return {
        "frame": pd.DataFrame(
            rows,
            columns=BAOSTOCK_TURNOVER_COLUMNS,
        ),
        "successful_codes": successful_codes,
        "errors": errors,
    }


def refresh_baostock_turnover_history(
    codes,
    start: str,
    end: str,
    *,
    root: str | Path = ROOT,
    batch_size: int = 50,
) -> dict[str, Any]:
    normalized_codes = _codes(codes)
    start_date = _date_text(start)
    end_date = _date_text(end)
    if start_date > end_date:
        raise ValueError("turnover start must not be after end")
    if isinstance(batch_size, bool) or not 1 <= int(batch_size) <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    destination = Path(root)
    manifest_path = destination / "coverage.json"
    manifest = _read_manifest(manifest_path)
    coverage = manifest.setdefault("baostock_turnover", {})
    path = destination / "baostock_turnover.csv"
    frame = _read_csv(path, BAOSTOCK_TURNOVER_COLUMNS)
    pending_by_range: dict[tuple[str, str], list[str]] = {}
    for code in normalized_codes:
        for missing in _missing_ranges(
            coverage.get(code),
            start_date,
            end_date,
        ):
            pending_by_range.setdefault(missing, []).append(code)
    errors = []
    fetched = 0
    for (gap_start, gap_end), pending in sorted(
        pending_by_range.items()
    ):
        for offset in range(0, len(pending), int(batch_size)):
            batch = pending[offset:offset + int(batch_size)]
            try:
                response = _fetch_baostock_turnover_batch(
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
            errors.extend(
                str(value)[:200]
                for value in response.get("errors") or []
            )
            incoming = response.get("frame")
            if successful:
                frame = _merge_history(
                    frame,
                    incoming,
                    BAOSTOCK_TURNOVER_COLUMNS,
                )
                _write_csv_atomic(path, frame)
                for code in batch:
                    if code not in successful:
                        continue
                    coverage[code] = _merge_intervals(
                        coverage.get(code),
                        gap_start,
                        gap_end,
                    )
                    fetched += 1
                _write_manifest_atomic(manifest_path, manifest)
    covered = sum(
        _range_covered(
            coverage.get(code),
            start_date,
            end_date,
        )
        for code in normalized_codes
    )
    ratio = (
        covered / len(normalized_codes)
        if normalized_codes
        else 0.0
    )
    result = {
        "ok": ratio >= 0.98,
        "provider": "baostock",
        "requested": len(normalized_codes),
        "covered": covered,
        "fetched": fetched,
        "coverage": round(ratio, 6),
        "rows": int(len(frame)),
        "root": str(destination),
    }
    if errors:
        result["errors"] = errors[:20]
    return result


def _merge_turnover_fallback(
    daily_basic: pd.DataFrame,
    fallback: pd.DataFrame,
) -> pd.DataFrame:
    if fallback.empty:
        return daily_basic
    base = daily_basic.copy()
    extra = fallback.copy()
    extra["code"] = extra["code"].astype("string").str.zfill(6)
    extra["date"] = pd.to_datetime(
        extra["date"],
        errors="coerce",
    ).dt.normalize()
    extra = extra.dropna(subset=["date"]).drop_duplicates(
        ["code", "date"],
        keep="last",
    )
    if base.empty:
        base = _empty(DAILY_BASIC_COLUMNS)
    base["code"] = base["code"].astype("string").str.zfill(6)
    base["date"] = pd.to_datetime(
        base["date"],
        errors="coerce",
    ).dt.normalize()
    merged = base.merge(
        extra.rename(
            columns={"turnover_rate": "_fallback_turnover"}
        ),
        on=["code", "date"],
        how="outer",
        validate="one_to_one",
    )
    merged["turnover_rate"] = pd.to_numeric(
        merged["turnover_rate"],
        errors="coerce",
    ).combine_first(
        pd.to_numeric(
            merged.pop("_fallback_turnover"),
            errors="coerce",
        )
    )
    return (
        merged[DAILY_BASIC_COLUMNS]
        .sort_values(["code", "date"])
        .reset_index(drop=True)
    )


def load_auxiliary_history(
    *,
    root: str | Path = ROOT,
) -> dict[str, pd.DataFrame]:
    source = Path(root)
    daily_basic = _read_csv(
        source / "daily_basic.csv",
        DAILY_BASIC_COLUMNS,
    )
    fallback = _read_csv(
        source / "baostock_turnover.csv",
        BAOSTOCK_TURNOVER_COLUMNS,
    )
    return {
        "daily_basic": _merge_turnover_fallback(
            daily_basic,
            fallback,
        ),
        "auction": _read_csv(
            source / "auction.csv",
            AUCTION_COLUMNS,
        ),
    }
