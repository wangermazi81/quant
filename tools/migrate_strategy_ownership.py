from __future__ import annotations

import argparse
import hashlib
import json
import uuid
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.user_manager import get_user

STRATEGIES_DIR = PROJECT_ROOT / "strategies"
MANIFEST_FILENAME = "legacy_strategy_ownership_manifest.json"


def quarantine_dir() -> Path:
    return STRATEGIES_DIR / "legacy_unassigned"


def manifest_path() -> Path:
    return STRATEGIES_DIR / MANIFEST_FILENAME


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_filename(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.endswith(".py"):
        value = value[:-3]
    if not value or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in value):
        return None
    return value

def _relative(path: Path) -> str:
    return path.relative_to(STRATEGIES_DIR).as_posix()


def _managed_path(relative: str) -> Path:
    root = STRATEGIES_DIR.resolve()
    candidate = (STRATEGIES_DIR / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("manifest path escapes strategies directory")
    return candidate


def _read_manifest() -> dict[str, Any]:
    path = manifest_path()
    if not path.exists():
        return {"version": 1, "operations": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("migration manifest is unreadable") from error
    if isinstance(data, dict) and isinstance(data.get("operations"), list):
        return data
    raise RuntimeError("migration manifest has an invalid format")


def _write_manifest(manifest: dict[str, Any]) -> None:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def _read_meta(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with path.open(encoding="utf-8") as handle:
            first_line = handle.readline().strip()
    except OSError:
        return None, "unreadable"
    if not first_line.startswith("# META:"):
        return {}, None
    try:
        metadata = json.loads(first_line[7:].strip())
    except json.JSONDecodeError:
        return None, "invalid_meta"
    if not isinstance(metadata, dict):
        return None, "invalid_meta"
    return metadata, None

def inventory() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    candidates: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    issues: list[dict[str, str]] = []
    for source in sorted(STRATEGIES_DIR.glob("*.py")):
        if source.name.startswith("_"):
            skipped.append({"filename": source.name, "reason": "private_module"})
            continue
        metadata, error = _read_meta(source)
        if error:
            issues.append({"filename": source.name, "reason": error})
            continue
        if metadata and metadata.get("builtin") is True:
            skipped.append({"filename": source.name, "reason": "builtin"})
            continue
        target = quarantine_dir() / source.name
        candidate = {"filename": source.name, "source": _relative(source), "target": _relative(target), "sha256": sha256_file(source)}
        if target.exists():
            issues.append({"filename": source.name, "reason": "quarantine_target_exists"})
        else:
            candidates.append(candidate)
    return candidates, skipped, issues

def dry_run() -> dict[str, Any]:
    candidates, skipped, issues = inventory()
    return {"ok": not issues, "candidates": candidates, "skipped": skipped, "issues": issues}

def _move_and_record(operation: str, source: Path, target: Path, expected_hash: str, target_user_id: int | None = None, extras: dict[str, Any] | None = None) -> dict[str, Any]:
    if not source.exists() or not source.is_file():
        return {"ok": False, "error": "source_not_found"}
    actual_hash = sha256_file(source)
    if actual_hash != expected_hash:
        return {"ok": False, "error": "source_hash_changed"}
    if target.exists():
        return {"ok": False, "error": "target_exists"}
    target.parent.mkdir(parents=True, exist_ok=True)
    source.replace(target)
    record: dict[str, Any] = {
        "operation": operation,
        "operation_id": uuid.uuid4().hex,
        "timestamp": _timestamp(),
        "status": "completed",
        "source": _relative(source),
        "target": _relative(target),
        "sha256": actual_hash,
    }
    if target_user_id is not None:
        record["target_user_id"] = target_user_id
    if extras:
        record.update(extras)
    manifest = _read_manifest()
    manifest["operations"].append(record)
    try:
        _write_manifest(manifest)
    except Exception:
        target.replace(source)
        raise
    return {"ok": True, "operation_id": record["operation_id"], "filename": target.name}

def apply() -> dict[str, Any]:
    report = dry_run()
    if not report["ok"]:
        report["moved"] = []
        return report
    moved = []
    for candidate in report["candidates"]:
        source = _managed_path(candidate["source"])
        target = _managed_path(candidate["target"])
        result = _move_and_record("quarantine", source, target, candidate["sha256"])
        if result["ok"]:
            moved.append(result)
        else:
            report["issues"].append({"filename": candidate["filename"], "reason": result["error"]})
    report["moved"] = moved
    report["ok"] = not report["issues"]
    return report


def list_unassigned() -> dict[str, Any]:
    if not quarantine_dir().exists():
        return {"ok": True, "unassigned": []}
    unassigned = []
    for path in sorted(quarantine_dir().glob("*.py")):
        if _safe_filename(path.name) is not None:
            unassigned.append({"filename": path.name, "sha256": sha256_file(path)})
    return {"ok": True, "unassigned": unassigned}


def assign(filename: str, user_id: int) -> dict[str, Any]:
    stem = _safe_filename(filename)
    if stem is None:
        return {"ok": False, "error": "invalid_filename"}
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return {"ok": False, "error": "invalid_user_id"}
    if not get_user(user_id):
        return {"ok": False, "error": "user_not_found"}

    source = quarantine_dir() / f"{stem}.py"
    target = STRATEGIES_DIR / "users" / str(user_id) / f"{stem}.py"
    if not source.exists():
        return {"ok": False, "error": "source_not_found"}
    return _move_and_record("assign", source, target, sha256_file(source), target_user_id=user_id)


def rollback(operation_id: str) -> dict[str, Any]:
    if not isinstance(operation_id, str) or not operation_id.strip():
        return {"ok": False, "error": "invalid_operation_id"}
    manifest = _read_manifest()
    operation = next(
        (
            item for item in manifest["operations"]
            if item.get("operation_id") == operation_id
            and item.get("status") == "completed"
            and item.get("operation") in {"quarantine", "assign"}
        ),
        None,
    )
    if operation is None:
        return {"ok": False, "error": "operation_not_found"}
    if any(item.get("operation") == "rollback" and item.get("reverses") == operation_id for item in manifest["operations"]):
        return {"ok": False, "error": "already_rolled_back"}

    try:
        source = _managed_path(operation["source"])
        target = _managed_path(operation["target"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "invalid_manifest_operation"}
    if source.exists():
        return {"ok": False, "error": "rollback_source_exists"}
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "rollback_target_not_found"}
    if sha256_file(target) != operation.get("sha256"):
        return {"ok": False, "error": "rollback_hash_mismatch"}
    return _move_and_record("rollback", target, source, operation["sha256"], extras={"reverses": operation_id})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quarantine and explicitly assign legacy strategies.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("dry-run")
    commands.add_parser("apply")
    commands.add_parser("list")
    assign_parser = commands.add_parser("assign")
    assign_parser.add_argument("filename")
    assign_parser.add_argument("user_id", type=int)
    rollback_parser = commands.add_parser("rollback")
    rollback_parser.add_argument("operation_id")
    args = parser.parse_args(argv)

    if args.command == "dry-run":
        result = dry_run()
    elif args.command == "apply":
        result = apply()
    elif args.command == "list":
        result = list_unassigned()
    elif args.command == "assign":
        result = assign(args.filename, args.user_id)
    else:
        result = rollback(args.operation_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
