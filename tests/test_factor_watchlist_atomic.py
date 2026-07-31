import json
import threading
import time

import pytest

from core import factor_watchlist as fw


def _candidate(code):
    return {
        "ts_code": code,
        "name": f"Stock-{code}",
        "trade_date": "2026-07-27",
        "candidate_rank": 1,
    }


def _validated_candidate(code, **overrides):
    return {
        **_candidate(code),
        "strategy_key": "relay_factor_mining_v2",
        "model_version": "relay_factor_mining_v3_nested_ridge",
        "experiment_id": "experiment-validated-1",
        "model_instance_id": "model-instance-validated-1",
        "validation_accepted": True,
        "research_status": "validated",
        "deployment_eligible": True,
        "research_only": False,
        "is_st": False,
        "st_status_known": True,
        "live_readiness_status": "ready",
        "position_pct": 0.1,
        **overrides,
    }


def test_concurrent_imports_keep_both_candidates(monkeypatch, tmp_path):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    original_read = fw._read_json_file
    start = threading.Barrier(2)

    def slow_read(path, default):
        data = original_read(path, default)
        time.sleep(0.08)
        return data

    monkeypatch.setattr(fw, "_read_json_file", slow_read)
    results = []

    def run(code):
        start.wait()
        results.append(fw.import_factor_candidates_to_watchlist([_candidate(code)]))

    threads = [threading.Thread(target=run, args=(code,)) for code in ("000001.SZ", "600000.SH")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    items = fw.load_factor_watchlist()["items"]
    assert {item["ts_code"] for item in items} == {"000001.SZ", "600000.SH"}


def test_save_uses_atomic_replace(monkeypatch, tmp_path):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    calls = []
    original_replace = fw.os.replace

    def tracked_replace(source, target):
        calls.append((source, target))
        return original_replace(source, target)

    monkeypatch.setattr(fw.os, "replace", tracked_replace)
    fw.save_factor_watchlist({"items": [_candidate("000001.SZ")]})

    assert len(calls) == 1
    source, target = map(fw.Path, calls[0])
    assert source.parent == target.parent
    assert target == fw.factor_watchlist_path()
    assert json.loads(target.read_text(encoding="utf-8"))["items"][0]["ts_code"] == "000001.SZ"


def test_ai_decision_atomic_replace_failure_preserves_previous_file(
    monkeypatch, tmp_path
):
    import app_state
    from analytics import live_factor_feedback

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    monkeypatch.setattr(
        live_factor_feedback,
        "record_ai_decision",
        lambda payload: {"ok": True, "recorded": 1},
    )
    path = fw.ai_trade_decision_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = {"generated_at": "2026-07-29 09:25:00", "results": []}
    path.write_text(json.dumps(previous), encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(fw.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        fw.save_ai_trade_decision(
            {"generated_at": "2026-07-30 09:25:00", "results": []}
        )

    assert fw.load_ai_trade_decision() == previous
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_legacy_candidate_without_trade_date_imports_without_replacing_scope(
    monkeypatch,
    tmp_path,
):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    existing_scope = fw.record_factor_watchlist_scope(
        trade_date="2026-07-29",
        source_run_id="run-empty",
        strategy_key="relay_factor_mining_v2",
        model_version="relay_factor_mining_v3_nested_ridge",
        experiment_id="experiment-validated",
        model_instance_id="model-validated",
        candidate_count=0,
    )["latest_scope"]

    imported = fw.import_factor_candidates_to_watchlist(
        [{"ts_code": "000001.SZ", "name": "平安银行"}],
        run_id="legacy-manual",
    )

    assert imported["ok"] is True
    assert imported["imported"] == 1
    assert imported["latest_scope"] == existing_scope


def test_ai_candidate_loader_excludes_market_gate_blocked_items(monkeypatch, tmp_path):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    monkeypatch.setattr(
        fw,
        "_expected_scope_trade_date",
        lambda: "2026-07-27",
    )
    blocked = {
        **_validated_candidate("000001.SZ"),
        "live_warning_keys": ["market_gate_blocked"],
    }
    allowed = {
        **_validated_candidate("600000.SH"),
        "candidate_rank": 2,
    }
    fw.import_factor_candidates_to_watchlist([blocked, allowed])

    candidates = fw.factor_watchlist_candidates(max_candidates=6)

    assert [item["ts_code"] for item in candidates] == ["600000.SH"]


def test_ai_candidate_loader_rejects_stale_latest_scope(monkeypatch, tmp_path):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    monkeypatch.setattr(
        fw,
        "_expected_scope_trade_date",
        lambda: "2026-07-28",
        raising=False,
    )
    fw.import_factor_candidates_to_watchlist(
        [_validated_candidate("000001.SZ", trade_date="2026-07-27")],
        run_id="run-stale",
    )

    assert fw.factor_watchlist_candidates(max_candidates=6) == []


def test_watchlist_preserves_research_deployment_fields(monkeypatch, tmp_path):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    imported = fw.import_factor_candidates_to_watchlist(
        [_validated_candidate("000001.SZ")],
        run_id="run-validated",
    )

    item = imported["items"][0]
    assert item["validation_accepted"] is True
    assert item["research_status"] == "validated"
    assert item["deployment_eligible"] is True
    assert item["research_only"] is False
    assert item["experiment_id"] == "experiment-validated-1"
    assert item["model_instance_id"] == "model-instance-validated-1"


def test_ai_candidate_loader_fails_closed_for_legacy_and_rejected_items(monkeypatch, tmp_path):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    fw.import_factor_candidates_to_watchlist(
        [
            _candidate("000001.SZ"),
            _validated_candidate(
                "600000.SH",
                validation_accepted=False,
                research_status="failed",
                deployment_eligible=False,
                research_only=True,
            ),
        ],
        run_id="run-rejected",
    )

    assert fw.factor_watchlist_candidates(max_candidates=6) == []


@pytest.mark.parametrize("st_fields", [{"is_st": False, "st_status_known": False}, {}])
def test_ai_candidate_loader_rejects_unknown_st_status(
    monkeypatch,
    tmp_path,
    st_fields,
):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    monkeypatch.setattr(
        fw,
        "_expected_scope_trade_date",
        lambda: "2026-07-27",
    )
    candidate = _validated_candidate("000001.SZ")
    candidate.pop("is_st", None)
    candidate.pop("st_status_known", None)
    candidate.update(st_fields)
    stored = fw.import_factor_candidates_to_watchlist(
        [candidate],
        run_id="run-unknown-st",
    )["items"][0]

    assert "is_st" in stored
    assert "st_status_known" in stored
    assert stored["is_st"] is st_fields.get("is_st")
    assert stored["st_status_known"] is st_fields.get("st_status_known")
    assert fw.factor_watchlist_candidates(max_candidates=6) == []
    assert (
        fw.authoritative_watchlist_candidates(
            [{"id": stored["id"]}],
            max_candidates=6,
        )
        == []
    )


def test_ai_candidate_loader_rejects_validated_claim_without_frozen_experiment_identity(
    monkeypatch,
    tmp_path,
):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    fw.import_factor_candidates_to_watchlist(
        [
            _validated_candidate(
                "000001.SZ",
                experiment_id="",
                model_instance_id="",
            )
        ],
        run_id="run-missing-experiment-identity",
    )

    assert fw.factor_watchlist_candidates(max_candidates=6) == []


def test_rejected_latest_trade_date_does_not_fallback_to_older_validated_run(monkeypatch, tmp_path):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    fw.import_factor_candidates_to_watchlist(
        [_validated_candidate("000001.SZ", trade_date="2026-07-27")],
        run_id="run-old",
    )
    fw.import_factor_candidates_to_watchlist(
        [
            _validated_candidate(
                "600000.SH",
                trade_date="2026-07-28",
                validation_accepted=False,
                research_status="failed",
                deployment_eligible=False,
                research_only=True,
                live_readiness_status="avoid",
                position_pct=0.0,
            )
        ],
        run_id="run-latest-rejected",
    )

    assert fw.factor_watchlist_candidates(max_candidates=6, latest_only=True) == []


def test_rejected_latest_run_does_not_fallback_within_same_trade_date(monkeypatch, tmp_path):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    fw.import_factor_candidates_to_watchlist(
        [_validated_candidate("000001.SZ")],
        run_id="run-older-validated",
    )
    fw.import_factor_candidates_to_watchlist(
        [
            _validated_candidate(
                "600000.SH",
                validation_accepted=False,
                research_status="failed",
                deployment_eligible=False,
                research_only=True,
                live_readiness_status="avoid",
                position_pct=0.0,
            )
        ],
        run_id="run-latest-rejected",
    )

    assert fw.factor_watchlist_candidates(max_candidates=6, latest_only=True) == []


def test_empty_latest_scope_does_not_fallback_to_older_validated_candidates(
    monkeypatch,
    tmp_path,
):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    fw.import_factor_candidates_to_watchlist(
        [_validated_candidate("000001.SZ", trade_date="2026-07-28")],
        run_id="run-validated",
    )

    recorded = fw.record_factor_watchlist_scope(
        trade_date="2026-07-29",
        source_run_id="run-validated",
        strategy_key="relay_factor_mining_v2",
        model_version="relay_factor_mining_v3_nested_ridge",
        experiment_id="experiment-validated-1",
        model_instance_id="model-instance-validated-1",
        candidate_count=0,
    )

    assert recorded["ok"] is True
    assert recorded["latest_scope"]["candidate_count"] == 0
    assert fw.load_factor_watchlist()["latest_scope"]["trade_date"] == "2026-07-29"
    assert fw.factor_watchlist_candidates(max_candidates=6, latest_only=True) == []


def test_same_code_from_different_runs_keeps_immutable_watchlist_history(monkeypatch, tmp_path):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    blocked = {
        **_candidate("000001.SZ"),
        "strategy_key": "relay_factor_mining_v2",
        "model_version": "relay_factor_mining_v2_ridge",
        "live_warning_keys": ["market_gate_blocked"],
    }
    allowed = {
        **_candidate("000001.SZ"),
        "strategy_key": "relay_factor_mining_v2",
        "model_version": "relay_factor_mining_v2_ridge",
    }

    first = fw.import_factor_candidates_to_watchlist([blocked], run_id="run-1")
    second = fw.import_factor_candidates_to_watchlist([allowed], run_id="run-2")

    first_item = next(item for item in first["items"] if item["source_run_id"] == "run-1")
    second_item = next(item for item in second["items"] if item["source_run_id"] == "run-2")
    assert first_item["id"] != second_item["id"]
    stored = fw.load_factor_watchlist()["items"]
    assert len(stored) == 2
    assert "market_gate_blocked" in next(
        item for item in stored if item["source_run_id"] == "run-1"
    )["live_warning_keys"]


def test_legacy_business_key_is_migrated_before_candidate_merge(
    monkeypatch, tmp_path
):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    legacy = {
        **_validated_candidate("000001.SZ"),
        "id": "legacy-watchlist-id",
        "source_run_id": "run-legacy",
        "business_key": "000001.SZ|2026-07-27",
        "business_key_version": 1,
        "status": "watch",
        "note": "preserve this decision",
    }
    path = fw.factor_watchlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"items": [legacy]}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = fw.import_factor_candidates_to_watchlist(
        [_validated_candidate("000001.SZ")],
        run_id="run-legacy",
    )

    assert result["total"] == 1
    item = result["items"][0]
    assert item["id"] == "legacy-watchlist-id"
    assert item["status"] == "watch"
    assert item["note"] == "preserve this decision"
    assert item["business_key_version"] == 2
    assert item["business_key"] == fw.watchlist_business_key(item)
