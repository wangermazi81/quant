from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Mapping

QUARANTINE_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "research"
    / "factor_history_quarantine.json"
)
_LOCK = threading.RLock()
_SCHEMA_VERSION = "factor_history_quarantine_v1"


def _empty() -> dict:
    return {
        "schema_version": _SCHEMA_VERSION,
        "updated_at": None,
        "codes": {},
    }


def load() -> dict:
    with _LOCK:
        if not QUARANTINE_PATH.is_file():
            return _empty()
        try:
            payload = json.loads(QUARANTINE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return _empty()
        if not isinstance(payload, dict):
            return _empty()
        codes = payload.get("codes")
        if not isinstance(codes, dict):
            codes = {}
        return {
            "schema_version": _SCHEMA_VERSION,
            "updated_at": payload.get("updated_at"),
            "codes": {
                str(code).zfill(6): dict(details)
                for code, details in codes.items()
                if str(code).strip()
                and isinstance(details, Mapping)
            },
        }


def _write(payload: dict) -> None:
    QUARANTINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = QUARANTINE_PATH.with_suffix(QUARANTINE_PATH.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(QUARANTINE_PATH)


def active_codes() -> set[str]:
    return set(load()["codes"])


def record_failures(failures: Mapping[str, str]) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    with _LOCK:
        payload = load()
        codes = payload["codes"]
        for raw_code, raw_reason in failures.items():
            code = str(raw_code).strip().split(".", 1)[0].zfill(6)
            if len(code) != 6 or not code.isdigit():
                continue
            previous = codes.get(code) or {}
            codes[code] = {
                "reason": str(raw_reason or "history unavailable")[:240],
                "quarantined_at": previous.get("quarantined_at") or now,
                "updated_at": now,
            }
        payload["updated_at"] = now
        _write(payload)
        return payload


def clear_codes(values) -> dict:
    normalized = {
        str(value).strip().split(".", 1)[0].zfill(6)
        for value in values or []
    }
    with _LOCK:
        payload = load()
        changed = False
        for code in normalized:
            if code in payload["codes"]:
                payload["codes"].pop(code, None)
                changed = True
        if changed:
            payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
            _write(payload)
        return payload
