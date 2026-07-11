import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools import migrate_strategy_ownership as migration


CUSTOM = '# META: {"name": "legacy", "builtin": false}\ndef scan(codes, get_kline, get_quotes): return []\n'
BUILTIN = '# META: {"name": "builtin", "builtin": true}\ndef scan(codes, get_kline, get_quotes): return []\n'


def _write_strategy(directory, name, code):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    return path


def test_dry_run_reports_only_root_custom_strategies_without_writing(tmp_path, monkeypatch):
    strategies = tmp_path / "strategies"
    monkeypatch.setattr(migration, "STRATEGIES_DIR", strategies)
    custom = _write_strategy(strategies, "legacy", CUSTOM)
    _write_strategy(strategies, "builtin", BUILTIN)

    report = migration.dry_run()

    assert report["ok"] is True
    assert [item["filename"] for item in report["candidates"]] == ["legacy.py"]
    assert report["skipped"] == [{"filename": "builtin.py", "reason": "builtin"}]
    assert custom.exists()
    assert not (strategies / "legacy_unassigned" / "legacy.py").exists()
    assert not migration.manifest_path().exists()

def test_apply_moves_custom_strategy_and_records_hash_manifest(tmp_path, monkeypatch):
    strategies = tmp_path / "strategies"
    monkeypatch.setattr(migration, "STRATEGIES_DIR", strategies)
    _write_strategy(strategies, "legacy", CUSTOM)

    report = migration.apply()

    quarantined = strategies / "legacy_unassigned" / "legacy.py"
    assert report["ok"] is True
    assert not (strategies / "legacy.py").exists()
    assert quarantined.exists()
    manifest = json.loads(migration.manifest_path().read_text(encoding="utf-8"))
    operation = manifest["operations"][0]
    assert operation["operation"] == "quarantine"
    assert operation["status"] == "completed"
    assert operation["source"] == "legacy.py"
    assert operation["target"] == "legacy_unassigned/legacy.py"
    assert operation["sha256"] == migration.sha256_file(quarantined)

def test_assign_requires_verified_user_and_rollback_checks_manifest(tmp_path, monkeypatch):
    strategies = tmp_path / "strategies"
    monkeypatch.setattr(migration, "STRATEGIES_DIR", strategies)
    monkeypatch.setattr(migration, "get_user", lambda uid: {"id": uid} if uid == 42 else None)
    _write_strategy(strategies, "legacy", CUSTOM)
    migration.apply()

    rejected = migration.assign("legacy.py", 99)
    assert rejected["ok"] is False
    assert (strategies / "legacy_unassigned" / "legacy.py").exists()

    assigned = migration.assign("legacy.py", 42)
    destination = strategies / "users" / "42" / "legacy.py"
    assert assigned["ok"] is True
    assert destination.exists()

    rolled_back = migration.rollback(assigned["operation_id"])

    assert rolled_back["ok"] is True
    assert not destination.exists()
    assert (strategies / "legacy_unassigned" / "legacy.py").exists()
    manifest = json.loads(migration.manifest_path().read_text(encoding="utf-8"))
    assert manifest["operations"][-1]["reverses"] == assigned["operation_id"]


def test_cli_can_run_directly_from_the_tools_directory():
    result = subprocess.run(
        [sys.executable, str(Path(migration.__file__)), "--help"],
        cwd=migration.PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
