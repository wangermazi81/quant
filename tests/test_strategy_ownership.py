import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

from strategy_engine import strategy_manager as sm


VALID_CODE = '''# META: {"name": "alpha", "builtin": false}
def scan(codes, get_kline, get_quotes):
    return []
'''


def test_custom_strategy_is_saved_only_under_its_owner_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "STRATEGIES_DIR", tmp_path / "strategies")

    assert sm.save("alpha", VALID_CODE, user_id=101) == {"ok": True, "file": "alpha"}
    assert (tmp_path / "strategies" / "users" / "101" / "alpha.py").exists()
    assert not (tmp_path / "strategies" / "alpha.py").exists()


def test_other_user_cannot_resolve_or_mutate_custom_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "STRATEGIES_DIR", tmp_path / "strategies")
    sm.save("alpha", VALID_CODE, user_id=101)

    assert sm.get_code("alpha", user_id=202) is None
    assert sm.get_meta("alpha", user_id=202) is None
    assert sm.delete("alpha", user_id=202) == {"ok": False, "error": "not found"}
    assert sm.rename("alpha", "beta", user_id=202) == {"ok": False, "error": "not found"}


def test_global_lookup_only_exposes_builtin_strategies(tmp_path, monkeypatch):
    strategies_dir = tmp_path / "strategies"
    monkeypatch.setattr(sm, "STRATEGIES_DIR", strategies_dir)
    strategies_dir.mkdir(parents=True)
    (strategies_dir / "builtin.py").write_text(
        '# META: {"name": "Builtin", "builtin": true}\ndef scan(codes, get_kline, get_quotes): return []\n',
        encoding="utf-8",
    )
    (strategies_dir / "legacy.py").write_text(VALID_CODE, encoding="utf-8")
    sm.save("private", VALID_CODE, user_id=101)

    assert [item["file"] for item in sm.list_strategies()] == ["builtin"]
    assert sm.get_code("private") is None
    assert sm.get_code("legacy") is None
    assert {item["file"] for item in sm.list_strategies(user_id=101)} == {"builtin", "private"}


def test_custom_strategy_metadata_cannot_claim_builtin_status(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "STRATEGIES_DIR", tmp_path / "strategies")
    code = VALID_CODE.replace('"builtin": false', '"builtin": true')

    assert sm.save("alpha", code, user_id=101)["ok"] is True
