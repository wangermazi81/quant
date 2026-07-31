from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import sqlite3
from unittest.mock import patch

import pytest

from analytics import relay_factor_mining
from analytics.relay_factor_experiment import (
    RelayFactorExperimentStore,
    canonical_json,
    experiment_id_for_manifest,
    model_instance_id_for_manifest,
)

_JULY_2026_FIXED_SESSIONS = [
    "2026-07-02",
    "2026-07-03",
    "2026-07-06",
    "2026-07-07",
    "2026-07-08",
    "2026-07-09",
    "2026-07-10",
    "2026-07-13",
    "2026-07-14",
    "2026-07-15",
    "2026-07-16",
    "2026-07-17",
    "2026-07-20",
    "2026-07-21",
    "2026-07-22",
    "2026-07-23",
    "2026-07-24",
    "2026-07-27",
    "2026-07-28",
    "2026-07-29",
]


@pytest.fixture(autouse=True)
def _verified_test_calendar(monkeypatch):
    from market import trading_calendar

    monkeypatch.setattr(
        trading_calendar,
        "has_verified_calendar",
        lambda _year: True,
    )
    monkeypatch.setattr(
        trading_calendar,
        "is_trading_day",
        lambda value=None: (
            value is not None
            and value.weekday() < 5
        ),
    )


def _manifest(*, required_sessions: int = 20, maximum_drawdown_pct: float = -15.0) -> dict:
    registration = relay_factor_mining.load_model_preregistration()
    assert registration["ok"] is True
    model = relay_factor_mining._registered_model_from_preregistration(
        registration
    )
    return {
        "schema_version": 3,
        "strategy_key": relay_factor_mining.STRATEGY_KEY,
        "protocol_binding": relay_factor_mining.build_current_frozen_protocol_binding(),
        "model": model,
        "rules": {"rank_method": "frozen_ridge"},
        "market_gate": {"minimum_emotion_score": 0.6},
        "trade_parameters": {"hold_days": 2, "stop_loss": -0.035, "take_profit": 0.07},
        "execution_rules": {
            "round_trip_cost_pct": 0.16,
            "slippage_pct": 0.20,
            "high_open_block_pct": 7.5,
            "weak_open_block_pct": -4.0,
            "near_limit_gap_pct": 9.2,
            "low_amount_ratio": 0.55,
            "path_policy": "t_plus_1_stop_first_on_same_bar_v1",
            "limit_threshold_policy": "board_and_point_in_time_st_v1",
        },
        "config": {"top_k": 1, "holding_sessions": 3},
        "stock_pool": {"watchlist_id": "relay-v2", "n_stocks": 300},
        "model_specification": {
            "protocol_version": relay_factor_mining.MODEL_SPEC_PROTOCOL_VERSION,
            "registered_at": registration["record"]["registered_at"],
            "calibrated_through": (
                relay_factor_mining.MODEL_SPEC_CALIBRATED_THROUGH
            ),
            "oos_not_before": registration["record"]["oos_not_before"],
            "registration_sha256": registration["sha256"],
            "model_sha256": registration["model_sha256"],
            "calibration_sha256": registration["record"][
                "calibration_evidence"
            ]["sha256"],
            "parameters": dict(relay_factor_mining.FIXED_MODEL_PARAMETERS),
        },
        "preregistration": {
            **registration["record"],
            "record_sha256": registration["sha256"],
        },
        "acceptance_rules": {
            "protocol_version": "prospective_60_session_bootstrap_v2",
            "required_sessions": required_sessions,
            "minimum_total_return_pct": 0.0,
            "maximum_drawdown_pct": maximum_drawdown_pct,
            "minimum_executed_trades": 1,
            "minimum_bootstrap_lower_return_pct": None,
            "maximum_protocol_violations": 0,
            "preregistered_at": (
                relay_factor_mining.MODEL_SPEC_CALIBRATED_THROUGH
            ),
            "calibrated_through": (
                relay_factor_mining.MODEL_SPEC_CALIBRATED_THROUGH
            ),
        },
    }


class _ShortWindowExperimentStore(RelayFactorExperimentStore):
    @staticmethod
    def _require_verified_protocol(experiment) -> None:
        return None

    def _latest_completed_session_date(self):
        return self._test_completed_session_date

    def record_verified_shadow_session(
        self,
        experiment_id,
        signal_date,
        *,
        panel,
    ):
        self._test_completed_session_date = str(signal_date)[:10]
        return super().record_verified_shadow_session(
            experiment_id,
            signal_date,
            panel=panel,
        )

    def create_locked_experiment(self, *args, **kwargs):
        locked_at = kwargs.pop("locked_at", None)
        if locked_at is not None:
            parsed = datetime.fromisoformat(
                str(locked_at).replace("Z", "+00:00")
            )
            self._clock = lambda: parsed
        experiment = super().create_locked_experiment(*args, **kwargs)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE experiments
                SET protocol_verified = 1
                WHERE experiment_id = ?
                """,
                (experiment["experiment_id"],),
            )
        return self.get_experiment(experiment["experiment_id"])


def _store(tmp_path) -> RelayFactorExperimentStore:
    return _ShortWindowExperimentStore(tmp_path / "prospective.sqlite3")


def _clock(value: str):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return lambda: parsed


def _locked(store: RelayFactorExperimentStore, **kwargs) -> dict:
    return store.create_locked_experiment(
        "run-20260730",
        _manifest(**kwargs),
        locked_at="2026-07-01T12:00:00+00:00",
    )


def _eligible_protocol_registration() -> dict:
    source = relay_factor_mining.load_model_preregistration()
    assert source["ok"] is True
    record = deepcopy(source["record"])
    model = deepcopy(record["executable_model"])
    model.update(
        {
            "validation_accepted": True,
            "validation_rejection_reason": None,
            "validation_stable": True,
            "validation_stability_reason": "ok",
            "frozen_model_candidate_eligible": True,
            "frozen_model_candidate_reason": "ok",
        }
    )
    model_sha256 = relay_factor_mining.factor_model_digest(model)
    record["executable_model"] = model
    record["executable_model_sha256"] = model_sha256
    record["prospective_acceptance_rules"] = {
        "protocol_version": (
            "prospective_60_session_paired_control_v3"
        ),
        "required_sessions": 60,
        "minimum_total_return_pct": 2.0,
        "minimum_excess_return_pct": 0.0,
        "maximum_drawdown_pct": -10.0,
        "minimum_executed_trades": 60,
        "minimum_bootstrap_lower_return_pct": 0.0,
        "minimum_excess_bootstrap_lower_return_pct": 0.0,
        "maximum_protocol_violations": 0,
        "preregistered_at": (
            relay_factor_mining.MODEL_SPEC_REGISTERED_AT
        ),
        "calibrated_through": (
            relay_factor_mining.MODEL_SPEC_CALIBRATED_THROUGH
        ),
    }
    return {
        "ok": True,
        "reason": "ok",
        "record": record,
        "sha256": "2" * 64,
        "model_sha256": model_sha256,
        "current_protocol": True,
    }


def test_experiment_sqlite_uses_wal_and_busy_timeout(tmp_path):
    store = _store(tmp_path)

    with store._connect() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) >= 30_000


def test_locked_experiment_uses_injected_trusted_clock(
    tmp_path,
):
    locked_at = datetime.fromisoformat(
        "2026-07-31T18:20:00+08:00"
    )
    store = RelayFactorExperimentStore(
        tmp_path / "trusted-clock.sqlite3",
        clock=lambda: locked_at,
    )

    experiment = store.create_locked_experiment(
        "run-trusted-clock",
        _manifest(required_sessions=60),
    )

    assert experiment["locked_at"] == locked_at.isoformat()
    with pytest.raises(TypeError):
        store.create_locked_experiment(
            "run-backdated",
            _manifest(required_sessions=60),
            locked_at="2026-07-01T12:00:00+00:00",
        )


def test_production_protocol_requires_fixed_rules_and_post_cutoff_lock(
    tmp_path,
    monkeypatch,
):
    registration = _eligible_protocol_registration()
    monkeypatch.setattr(
        relay_factor_mining,
        "load_model_preregistration",
        lambda: registration,
    )
    manifest = _manifest(required_sessions=60)
    manifest["schema_version"] = 4
    manifest["acceptance_rules"] = dict(
        relay_factor_mining.PROSPECTIVE_DUAL_TRACK_ACCEPTANCE_RULES
    )

    valid_store = RelayFactorExperimentStore(
        tmp_path / "valid.sqlite3",
        clock=_clock("2026-08-01T00:10:00+08:00"),
    )
    valid = valid_store.create_locked_experiment(
        "run-valid-protocol",
        manifest,
    )
    assert valid["protocol_verified"] is True

    legacy = deepcopy(manifest)
    legacy["schema_version"] = 3
    legacy_store = RelayFactorExperimentStore(
        tmp_path / "legacy-schema.sqlite3",
        clock=_clock("2026-07-31T18:20:00+08:00"),
    )
    legacy_experiment = legacy_store.create_locked_experiment(
        "run-legacy-schema",
        legacy,
    )
    assert legacy_experiment["protocol_verified"] is False

    retuned = deepcopy(manifest)
    retuned["acceptance_rules"]["minimum_total_return_pct"] = 0.0
    retuned_store = RelayFactorExperimentStore(
        tmp_path / "retuned.sqlite3",
        clock=_clock("2026-07-31T18:20:00+08:00"),
    )
    retuned_experiment = retuned_store.create_locked_experiment(
        "run-retuned-protocol",
        retuned,
    )
    assert retuned_experiment["protocol_verified"] is False

    pre_registration_store = RelayFactorExperimentStore(
        tmp_path / "pre-registration.sqlite3",
        clock=_clock("2026-07-30T17:10:00+08:00"),
    )
    pre_registration = pre_registration_store.create_locked_experiment(
        "run-pre-registration",
        manifest,
    )
    assert pre_registration["protocol_verified"] is False

    pre_cutoff_store = RelayFactorExperimentStore(
        tmp_path / "pre-cutoff.sqlite3",
        clock=_clock("2026-07-29T17:10:00+08:00"),
    )
    pre_cutoff = pre_cutoff_store.create_locked_experiment(
        "run-pre-cutoff",
        manifest,
    )
    assert pre_cutoff["protocol_verified"] is False


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("model", "preregistration_record_sha256"),
        ("model_specification", "registration_sha256"),
        ("preregistration", "record_sha256"),
    ],
)
def test_production_protocol_rejects_tampered_registration_sha(
    tmp_path,
    section,
    key,
):
    manifest = _manifest(required_sessions=60)
    manifest["schema_version"] = 4
    manifest["acceptance_rules"] = dict(
        relay_factor_mining.PROSPECTIVE_DUAL_TRACK_ACCEPTANCE_RULES
    )
    manifest[section][key] = "0" * 64
    store = RelayFactorExperimentStore(
        tmp_path / f"tampered-{section}.sqlite3",
        clock=_clock("2026-07-31T18:20:00+08:00"),
    )

    experiment = store.create_locked_experiment(
        f"run-tampered-{section}",
        manifest,
    )

    assert experiment["protocol_verified"] is False


def _record_settled_session(
    store: RelayFactorExperimentStore,
    experiment_id: str,
    signal_date: str,
    return_pct: float,
) -> None:
    code = f"000{int(signal_date[-2:]):03d}.SZ"
    _record_verified_session(
        store,
        experiment_id,
        signal_date,
        candidates=[
            {
                "code": code,
                "candidate_rank": 1,
                "shadow_target_position_pct": 1.0,
            }
        ],
        returns={code: return_pct},
    )


def _next_weekday(value: str) -> str:
    cursor = date.fromisoformat(value) + timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor += timedelta(days=1)
    return cursor.isoformat()


def _record_verified_session(
    store: RelayFactorExperimentStore,
    experiment_id: str,
    signal_date: str,
    *,
    candidates: list[dict],
    returns: dict[str, float],
) -> None:
    experiment = store.get_experiment(experiment_id)
    entry_date = _next_weekday(signal_date)
    exit_date = _next_weekday(entry_date)

    def frozen_snapshot(_panel, manifest):
        rows = []
        for raw in candidates:
            code = str(raw["code"])
            rows.append(
                {
                    **raw,
                    "code": code,
                    "ts_code": code,
                    "trade_date": signal_date,
                    "experiment_id": manifest["experiment_id"],
                    "model_instance_id": manifest["model_instance_id"],
                    "source_run_id": manifest["source_run_id"],
                    "research_status": "locked_shadow",
                    "deployment_eligible": False,
                    "research_only": True,
                    "gate_pass": True,
                    "position_pct": 0.0,
                }
            )
        return {
            "ok": True,
            "data_status": "ok",
            "reason": "ok",
            "signal_date": signal_date,
            "market_gate": {"enabled": False, "pass": True},
            "data_quality": {
                "latest_real_amount_coverage": 1.0,
                "latest_st_status_coverage": 1.0,
                "latest_market_data_trusted": True,
            },
            "candidates": rows,
        }

    def frozen_settlement(_panel, candidate, _manifest):
        code = str(candidate["code"])
        realized = float(returns[code])
        entry_price = 10.0
        exit_price = entry_price * (1.0 + (realized + 0.36) / 100.0)
        return {
            "status": "settled",
            "execution_blocked": False,
            "block_reasons": [],
            "entry_date": entry_date,
            "exit_date": exit_date,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_reason": "max_holding_period",
            "realized_return_pct": realized,
            "valuation_path": [
                {"date": entry_date, "return_pct": -0.36},
                {"date": exit_date, "return_pct": realized},
            ],
        }

    with (
        patch.object(
            relay_factor_mining,
            "build_frozen_shadow_snapshot",
            frozen_snapshot,
        ),
        patch.object(
            relay_factor_mining,
            "settle_frozen_shadow_candidate",
            frozen_settlement,
        ),
    ):
        recorded = store.record_verified_shadow_session(
            experiment_id,
            signal_date,
            panel={},
        )
        assert recorded["recorded"] is True
        settled = store.settle_verified_candidates(experiment_id, panel={})
        assert settled["settled"] == len(returns)
    assert experiment["protocol_verified"] is True


def _dual_track_manifest(*, required_sessions: int = 20) -> dict:
    manifest = _manifest(required_sessions=required_sessions)
    manifest["schema_version"] = 4
    manifest["acceptance_rules"].update(
        {
            "minimum_excess_return_pct": 0.0,
            "minimum_excess_bootstrap_lower_return_pct": None,
        }
    )
    return manifest


def _dual_track_snapshot(manifest: dict, signal_date: str) -> dict:
    bindings = {
        "experiment_id": manifest["experiment_id"],
        "model_instance_id": manifest["model_instance_id"],
        "source_run_id": manifest["source_run_id"],
        "research_status": "locked_shadow",
        "deployment_eligible": False,
        "research_only": True,
        "gate_pass": True,
        "position_pct": 0.0,
    }
    return {
        "ok": True,
        "data_status": "ok",
        "reason": "ok",
        "signal_date": signal_date,
        "market_gate": {"enabled": False, "pass": True},
        "data_quality": {
            "latest_real_amount_coverage": 1.0,
            "latest_st_status_coverage": 1.0,
            "latest_market_data_trusted": True,
        },
        "control_policy_version": (
            relay_factor_mining.BENCHMARK_POLICY_VERSION
        ),
        "control_universe_codes_sha256": "a" * 64,
        "candidates": [
            {
                **bindings,
                "code": "000001.SZ",
                "ts_code": "000001.SZ",
                "trade_date": signal_date,
                "candidate_rank": 1,
                "shadow_target_position_pct": 0.1,
            }
        ],
        "control_candidates": [
            {
                **bindings,
                "code": "000001.SZ",
                "ts_code": "000001.SZ",
                "trade_date": signal_date,
                "candidate_rank": 1,
                "shadow_target_position_pct": 1.0,
                "prospective_track": "control",
            }
        ],
    }


def test_verified_dual_track_snapshot_is_recorded_atomically(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path)
    experiment = store.create_locked_experiment(
        "run-dual-track",
        _dual_track_manifest(required_sessions=1),
        locked_at="2026-07-01T12:00:00+00:00",
    )
    snapshot = _dual_track_snapshot(
        experiment["manifest"],
        "2026-07-02",
    )
    monkeypatch.setattr(
        relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda _panel, _manifest: snapshot,
    )

    recorded = store.record_verified_shadow_session(
        experiment["experiment_id"],
        "2026-07-02",
        panel={},
    )

    assert recorded["candidate_snapshot"]["inserted"] == 1
    assert recorded["control_snapshot"]["inserted"] == 1
    assert len(store.list_candidates(experiment["experiment_id"])) == 1
    controls = store.list_control_candidates(
        experiment["experiment_id"]
    )
    assert len(controls) == 1
    assert controls[0]["code"] == "000001.SZ"
    session = store.list_sessions(experiment["experiment_id"])[0]
    assert session["controls_recorded"] is True
    assert session["control_candidate_count"] == 1


def test_dual_track_snapshot_rolls_back_both_tracks_on_control_insert_failure(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path)
    experiment = store.create_locked_experiment(
        "run-dual-track-rollback",
        _dual_track_manifest(required_sessions=1),
        locked_at="2026-07-01T12:00:00+00:00",
    )
    snapshot = _dual_track_snapshot(
        experiment["manifest"],
        "2026-07-02",
    )
    monkeypatch.setattr(
        relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda _panel, _manifest: snapshot,
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_prospective_control
            BEFORE INSERT ON prospective_controls
            BEGIN
                SELECT RAISE(ABORT, 'forced control failure');
            END
            """
        )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="forced control failure",
    ):
        store.record_verified_shadow_session(
            experiment["experiment_id"],
            "2026-07-02",
            panel={},
        )

    assert store.list_sessions(experiment["experiment_id"]) == []
    assert store.list_candidates(experiment["experiment_id"]) == []
    assert store.list_control_candidates(
        experiment["experiment_id"]
    ) == []


def test_verified_dual_track_settlement_is_atomic(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path)
    experiment = store.create_locked_experiment(
        "run-dual-settlement",
        _dual_track_manifest(required_sessions=1),
        locked_at="2026-07-01T12:00:00+00:00",
    )
    snapshot = _dual_track_snapshot(
        experiment["manifest"],
        "2026-07-02",
    )
    monkeypatch.setattr(
        relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda _panel, _manifest: snapshot,
    )
    store.record_verified_shadow_session(
        experiment["experiment_id"],
        "2026-07-02",
        panel={},
    )

    def settlement(_panel, candidate, _manifest):
        if candidate.get("prospective_track") == "control":
            raise RuntimeError("forced control settlement failure")
        return {
            "status": "settled",
            "execution_blocked": False,
            "block_reasons": [],
            "entry_date": "2026-07-03",
            "exit_date": "2026-07-06",
            "entry_price": 10.0,
            "exit_price": 11.0,
            "exit_reason": "take_profit",
            "realized_return_pct": 9.64,
            "valuation_path": [
                {"date": "2026-07-03", "return_pct": -0.36},
                {"date": "2026-07-06", "return_pct": 9.64},
            ],
        }

    monkeypatch.setattr(
        relay_factor_mining,
        "settle_frozen_shadow_candidate",
        settlement,
    )

    with pytest.raises(
        RuntimeError,
        match="forced control settlement failure",
    ):
        store.settle_verified_candidates(
            experiment["experiment_id"],
            panel={},
        )

    assert store.list_candidates(experiment["experiment_id"])[0][
        "settlement_status"
    ] == "pending"
    assert store.list_control_candidates(
        experiment["experiment_id"]
    )[0]["settlement_status"] == "pending"


def _record_verified_dual_session(
    store: RelayFactorExperimentStore,
    experiment_id: str,
    signal_date: str,
    *,
    strategy_return_pct: float,
    control_return_pct: float,
) -> None:
    experiment = store.get_experiment(experiment_id)
    snapshot = _dual_track_snapshot(
        experiment["manifest"],
        signal_date,
    )
    strategy_code = f"STRATEGY-{signal_date}"
    control_code = f"CONTROL-{signal_date}"
    snapshot["candidates"][0].update(
        {"code": strategy_code, "ts_code": strategy_code}
    )
    snapshot["control_candidates"][0].update(
        {"code": control_code, "ts_code": control_code}
    )
    entry_date = _next_weekday(signal_date)
    exit_date = _next_weekday(entry_date)

    def settlement(_panel, candidate, _manifest):
        realized = (
            control_return_pct
            if candidate.get("prospective_track") == "control"
            else strategy_return_pct
        )
        entry_price = 10.0
        exit_price = entry_price * (
            1.0 + (realized + 0.36) / 100.0
        )
        return {
            "status": "settled",
            "execution_blocked": False,
            "block_reasons": [],
            "entry_date": entry_date,
            "exit_date": exit_date,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_reason": "max_holding_period",
            "realized_return_pct": realized,
            "valuation_path": [
                {"date": entry_date, "return_pct": -0.36},
                {"date": exit_date, "return_pct": realized},
            ],
        }

    with (
        patch.object(
            relay_factor_mining,
            "build_frozen_shadow_snapshot",
            return_value=snapshot,
        ),
        patch.object(
            relay_factor_mining,
            "settle_frozen_shadow_candidate",
            settlement,
        ),
    ):
        store.record_verified_shadow_session(
            experiment_id,
            signal_date,
            panel={},
        )
        assert store.settle_verified_candidates(
            experiment_id,
            panel={},
        ) == {"settled": 2, "pending": 0}


def test_dual_track_evaluation_rejects_positive_strategy_that_loses_to_control(
    tmp_path,
):
    store = _store(tmp_path)
    experiment = store.create_locked_experiment(
        "run-relative-failure",
        _dual_track_manifest(required_sessions=2),
        locked_at="2026-07-01T12:00:00+00:00",
    )
    for signal_date in ("2026-07-02", "2026-07-03"):
        _record_verified_dual_session(
            store,
            experiment["experiment_id"],
            signal_date,
            strategy_return_pct=1.0,
            control_return_pct=2.0,
        )

    evaluation = store.evaluate_once(experiment["experiment_id"])

    assert evaluation["status"] == "failed"
    assert evaluation["metrics"]["total_return_pct"] > 0.0
    assert evaluation["metrics"]["control_total_return_pct"] > (
        evaluation["metrics"]["total_return_pct"]
    )
    assert evaluation["metrics"]["excess_return_pct"] < 0.0
    assert (
        "minimum_excess_return_pct_not_met"
        in evaluation["failure_reasons"]
    )


def test_verified_shadow_session_rejects_historical_success_backfill(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path)
    experiment = _locked(store, required_sessions=3)
    monkeypatch.setattr(
        store,
        "_latest_completed_session_date",
        lambda: "2026-07-03",
        raising=False,
    )

    with patch.object(
        relay_factor_mining,
        "build_frozen_shadow_snapshot",
        return_value={
            "ok": True,
            "data_status": "ok",
            "reason": "ok",
            "signal_date": "2026-07-02",
            "market_gate": {"enabled": False, "pass": True},
            "data_quality": {},
            "candidates": [],
        },
    ):
        with pytest.raises(
            ValueError,
            match="historical success backfill",
        ):
            store.record_verified_shadow_session(
                experiment["experiment_id"],
                "2026-07-02",
                panel={},
            )

    assert store.list_sessions(experiment["experiment_id"]) == []


def test_canonical_manifest_hashes_are_key_order_stable_and_sensitive_to_key_fields():
    manifest = _manifest()
    reordered = {
        "schema_version": manifest["schema_version"],
        "strategy_key": manifest["strategy_key"],
        "protocol_binding": manifest["protocol_binding"],
        "stock_pool": manifest["stock_pool"],
        "trade_parameters": manifest["trade_parameters"],
        "execution_rules": manifest["execution_rules"],
        "market_gate": manifest["market_gate"],
        "config": manifest["config"],
        "model_specification": manifest["model_specification"],
        "preregistration": manifest["preregistration"],
        "acceptance_rules": manifest["acceptance_rules"],
        "rules": manifest["rules"],
        "model": manifest["model"],
    }

    assert canonical_json(manifest) == canonical_json(reordered)
    assert model_instance_id_for_manifest(manifest) == model_instance_id_for_manifest(reordered)
    assert experiment_id_for_manifest(manifest) == experiment_id_for_manifest(reordered)

    changed = deepcopy(manifest)
    changed["trade_parameters"]["hold_days"] = 3
    assert experiment_id_for_manifest(changed) != experiment_id_for_manifest(manifest)

    changed_model = deepcopy(manifest)
    changed_model["model"]["coefficients"]["ret_5"] = 0.3
    assert model_instance_id_for_manifest(changed_model) != model_instance_id_for_manifest(manifest)

    changed_execution = deepcopy(manifest)
    changed_execution["execution_rules"]["slippage_pct"] = 0.30
    assert experiment_id_for_manifest(changed_execution) != experiment_id_for_manifest(manifest)


def test_same_executable_protocol_cannot_be_reenrolled_by_new_run_or_diagnostics(
    tmp_path,
):
    store = _store(tmp_path)
    manifest = _manifest()
    first = store.create_locked_experiment(
        "run-original",
        manifest,
        locked_at="2026-07-01T12:00:00+00:00",
    )
    store.cancel_experiment(
        first["experiment_id"],
        cancelled_at="2026-07-02T12:00:00+00:00",
    )

    rerun = deepcopy(manifest)
    rerun["model"]["training_diagnostics"] = {
        "sample_count": 999,
        "rank_ic": 0.123,
    }
    rerun["model"]["research_status"] = "exploratory"
    rerun["historical_evidence"] = {
        "generated_at": "2026-07-30 17:10:00",
        "oos_total_return_pct": 12.3,
    }

    assert model_instance_id_for_manifest(rerun) == first["model_instance_id"]
    assert experiment_id_for_manifest(rerun) == experiment_id_for_manifest(manifest)

    repeated = store.create_locked_experiment(
        "run-renamed",
        rerun,
        locked_at="2026-07-30T12:00:00+00:00",
    )

    assert repeated["experiment_id"] == first["experiment_id"]
    assert repeated["status"] == "cancelled"
    assert repeated["source_run_id"] == "run-original"
    assert len(store.list_experiments()) == 1


def test_locked_experiment_is_idempotent_and_cancel_keeps_audit_record(tmp_path):
    store = _store(tmp_path)
    first = _locked(store)
    second = _locked(store)

    assert first["experiment_id"] == second["experiment_id"]
    assert first["status"] == "locked_shadow"
    assert first["manifest"]["model_instance_id"] == first["model_instance_id"]
    assert first["manifest"]["experiment_id"] == first["experiment_id"]
    assert first["manifest"]["acceptance_rules"]["required_sessions"] == 20

    cancelled = store.cancel_experiment(first["experiment_id"], cancelled_at="2026-07-02T01:00:00+00:00")
    assert cancelled["status"] == "cancelled"
    assert store.get_experiment(first["experiment_id"])["cancelled_at"] == "2026-07-02T01:00:00+00:00"
    assert store.get_active_experiment() is None
    with pytest.raises(ValueError, match="terminal"):
        store.cancel_experiment(first["experiment_id"])


def test_expected_session_window_is_persisted_when_calendar_changes(
    tmp_path, monkeypatch
):
    from analytics import relay_factor_experiment as experiment_module

    calls = []

    def changing_calendar(_locked_at, _required_sessions):
        calls.append(True)
        if len(calls) == 1:
            return ["2026-07-02", "2026-07-03"]
        return ["2026-07-06", "2026-07-07"]

    monkeypatch.setattr(
        experiment_module,
        "_fixed_session_dates",
        changing_calendar,
    )
    db_path = tmp_path / "persisted-window.sqlite3"
    store = RelayFactorExperimentStore(
        db_path,
        clock=_clock("2026-07-01T12:00:00+00:00"),
    )
    experiment = store.create_locked_experiment(
        "run-persisted-window",
        _manifest(required_sessions=2),
    )

    reopened = RelayFactorExperimentStore(db_path)

    assert experiment["expected_session_dates"] == [
        "2026-07-02",
        "2026-07-03",
    ]
    assert reopened.expected_session_dates(experiment["experiment_id"]) == [
        "2026-07-02",
        "2026-07-03",
    ]
    assert calls == [True]


def test_fixed_session_window_rejects_unverified_cross_year_calendar(
    monkeypatch,
):
    from analytics import relay_factor_experiment as experiment_module
    from market import trading_calendar

    monkeypatch.setattr(
        trading_calendar,
        "has_verified_calendar",
        lambda year: int(year) == 2026,
    )
    monkeypatch.setattr(
        trading_calendar,
        "is_trading_day",
        lambda _value=None: True,
    )

    with pytest.raises(
        RuntimeError,
        match="authoritative trade calendar unavailable",
    ):
        experiment_module._fixed_session_dates(
            "2026-12-31T17:10:00+08:00",
            2,
        )


def test_sessions_reject_prelock_and_untrusted_data_without_changing_counts(tmp_path):
    store = _store(tmp_path)
    experiment = _locked(store)
    experiment_id = experiment["experiment_id"]

    with pytest.raises(ValueError, match="after locked"):
        store.record_session(experiment_id, "2026-07-01", source_snapshot={"market_data_trusted": True})

    rejected = store.record_session(
        experiment_id,
        "2026-07-02",
        data_complete=False,
        source_snapshot={"market_data_trusted": False},
    )
    assert rejected == {"recorded": False, "reason": "untrusted_or_incomplete_data"}
    assert store.get_progress(experiment_id)["observed_sessions"] == 0

    missing_trust_marker = store.record_session(
        experiment_id,
        "2026-07-02",
        source_snapshot={"source": "daily-cache"},
    )
    assert missing_trust_marker == {"recorded": False, "reason": "untrusted_or_incomplete_data"}

    saved = store.record_session(
        experiment_id,
        "2026-07-02",
        source_snapshot={"market_data_trusted": True, "revision": 1},
    )
    repeated = store.record_session(
        experiment_id,
        "2026-07-02",
        source_snapshot={"market_data_trusted": True, "revision": 2},
    )
    assert saved["recorded"] is True
    assert repeated["idempotent"] is True
    assert store.get_progress(experiment_id)["observed_sessions"] == 1
    assert store.list_sessions(experiment_id)[0]["source_snapshot"]["revision"] == 1


def test_no_candidate_day_is_pending_until_the_empty_candidate_snapshot_is_finalized(tmp_path):
    store = _store(tmp_path)
    experiment = _locked(store)
    store.record_session(
        experiment["experiment_id"],
        "2026-07-02",
        source_snapshot={"market_data_trusted": True},
    )

    progress = store.get_progress(experiment["experiment_id"])
    assert progress["observed_sessions"] == 1
    assert progress["settled_sessions"] == 0
    assert store.list_sessions(experiment["experiment_id"])[0]["candidates_recorded"] is False
    assert store.record_candidates(experiment["experiment_id"], "2026-07-02", [])["inserted"] == 0
    assert store.get_progress(experiment["experiment_id"])["settled_sessions"] == 1
    assert store.list_sessions(experiment["experiment_id"])[0]["settlement_status"] == "settled"


def test_frozen_snapshot_rolls_back_session_when_candidate_insert_fails(tmp_path):
    store = _store(tmp_path)
    experiment = _locked(store)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_prospective_candidate
            BEFORE INSERT ON prospective_candidates
            BEGIN
                SELECT RAISE(ABORT, 'forced candidate failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced candidate failure"):
        store.record_frozen_snapshot(
            experiment["experiment_id"],
            "2026-07-02",
            source_snapshot={"market_data_trusted": True},
            candidates=[
                {
                    "code": "000001.SZ",
                    "candidate_rank": 1,
                    "selected": True,
                    "target_position_pct": 0.1,
                }
            ],
        )

    assert store.list_sessions(experiment["experiment_id"]) == []
    assert store.list_candidates(experiment["experiment_id"]) == []


def test_untrusted_trading_session_consumes_fixed_window_and_fails_validation(tmp_path):
    store = _store(tmp_path)
    experiment = store.create_locked_experiment(
        "run-untrusted",
        _manifest(required_sessions=1),
        locked_at="2026-07-01T12:00:00+00:00",
    )
    experiment_id = experiment["experiment_id"]

    frozen = store.record_frozen_snapshot(
        experiment_id,
        "2026-07-02",
        data_complete=False,
        data_trusted=False,
        source_snapshot={
            "market_data_trusted": False,
            "failure_reason": "daily_cache_incomplete",
        },
        candidates=[],
    )

    assert frozen["recorded"] is True
    session = store.list_sessions(experiment_id)[0]
    assert session["data_complete"] is False
    assert session["data_trusted"] is False
    assert session["candidates_recorded"] is True
    assert store.get_progress(experiment_id)["ready_to_evaluate"] is True

    evaluation = store.evaluate_once(experiment_id)
    assert evaluation["status"] == "failed"
    assert evaluation["metrics"]["protocol_violation_count"] == 1
    assert "protocol_violation_count_nonzero" in evaluation["failure_reasons"]
    with pytest.raises(ValueError, match="terminal"):
        store.record_frozen_snapshot(
            experiment_id,
            "2026-07-03",
            source_snapshot={"market_data_trusted": True},
            candidates=[],
        )


def test_evaluation_rejects_recorded_sessions_outside_fixed_post_lock_window(tmp_path):
    store = _store(tmp_path)
    experiment = store.create_locked_experiment(
        "run-window-gap",
        _manifest(required_sessions=2),
        locked_at="2026-07-01T12:00:00+00:00",
    )
    experiment_id = experiment["experiment_id"]
    for signal_date in ("2026-07-03", "2026-07-06"):
        store.record_frozen_snapshot(
            experiment_id,
            signal_date,
            source_snapshot={"market_data_trusted": True},
            candidates=[],
        )

    evaluation = store.evaluate_once(experiment_id)

    assert evaluation["evaluated"] is False
    assert evaluation["status"] == "locked_shadow"
    assert evaluation["reason"] == "fixed_window_session_gap"
    assert evaluation["expected_session_dates"] == [
        "2026-07-02",
        "2026-07-03",
    ]
    assert evaluation["missing_session_dates"] == ["2026-07-02"]
    progress = store.get_progress(experiment_id)
    assert progress["observed_sessions"] == 1
    assert progress["ready_to_evaluate"] is False


def test_candidate_snapshots_are_immutable_on_idempotent_replay(tmp_path):
    store = _store(tmp_path)
    experiment = _locked(store)
    experiment_id = experiment["experiment_id"]
    store.record_session(experiment_id, "2026-07-02", source_snapshot={"market_data_trusted": True})
    original = {
        "code": "000001.SZ",
        "candidate_rank": 1,
        "target_position_pct": 0.1,
        "factor_model_score": 99.5,
        "factor_explain": {"selected_factors": ["ret_5"]},
    }
    assert store.record_candidates(experiment_id, "2026-07-02", [original])["inserted"] == 1

    assert store.record_candidates(experiment_id, "2026-07-02", [original])["idempotent"] is True
    altered = deepcopy(original)
    altered["target_position_pct"] = 0.9
    altered["factor_explain"]["selected_factors"] = ["ret_20"]
    with pytest.raises(ValueError, match="immutable"):
        store.record_candidates(experiment_id, "2026-07-02", [altered])
    with pytest.raises(ValueError, match="immutable"):
        store.record_candidates(
            experiment_id,
            "2026-07-02",
            [original, {**original, "code": "000002.SZ", "candidate_rank": 2}],
        )
    candidate = store.list_candidates(experiment_id, "2026-07-02")[0]
    assert candidate["snapshot"]["target_position_pct"] == 0.1
    assert candidate["snapshot"]["factor_explain"]["selected_factors"] == ["ret_5"]
    assert candidate["selected"] is True


def test_empty_candidate_snapshot_is_immutable_after_finalization(tmp_path):
    store = _store(tmp_path)
    experiment = _locked(store)
    experiment_id = experiment["experiment_id"]
    store.record_session(experiment_id, "2026-07-02", source_snapshot={"market_data_trusted": True})
    assert store.record_candidates(experiment_id, "2026-07-02", [])["recorded"] is True
    assert store.record_candidates(experiment_id, "2026-07-02", [])["idempotent"] is True
    with pytest.raises(ValueError, match="immutable"):
        store.record_candidates(experiment_id, "2026-07-02", [{"code": "000001.SZ", "candidate_rank": 1}])


def test_state_transitions_take_an_immediate_sqlite_write_lock(tmp_path, monkeypatch):
    store = _store(tmp_path)
    experiment = _locked(store)
    connection = sqlite3.connect(store.db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    monkeypatch.setattr(store, "_connect", lambda: connection)

    store.record_session(
        experiment["experiment_id"],
        "2026-07-02",
        source_snapshot={"market_data_trusted": True},
    )
    assert any(statement.strip().upper() == "BEGIN IMMEDIATE" for statement in statements)
    statements.clear()

    store.record_candidates(
        experiment["experiment_id"],
        "2026-07-02",
        [{"code": "000001.SZ", "candidate_rank": 1}],
    )
    assert any(statement.strip().upper() == "BEGIN IMMEDIATE" for statement in statements)
    statements.clear()

    store.settle_candidate(
        experiment["experiment_id"],
        "2026-07-02",
        "000001.SZ",
        entry_date="2026-07-03",
        exit_date="2026-07-04",
        entry_price=10.0,
        exit_price=11.0,
        exit_reason="take_profit",
        realized_return_pct=9.64,
        valuation_path=[{"date": "2026-07-03", "return_pct": -0.36}, {"date": "2026-07-04", "return_pct": 9.64}],
    )
    assert any(statement.strip().upper() == "BEGIN IMMEDIATE" for statement in statements)
    connection.close()


def test_unselected_pending_audit_candidate_does_not_block_settlement_or_evaluation(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(required_sessions=1)
    manifest["config"] = {
        **manifest["config"],
        "top_k": 1,
        "candidate_pool_size": 2,
        "holding_sessions": 1,
    }
    experiment = store.create_locked_experiment("run-selected", manifest, locked_at="2026-07-01T12:00:00+00:00")
    experiment_id = experiment["experiment_id"]
    _record_verified_session(
        store,
        experiment_id,
        "2026-07-02",
        candidates=[
            {
                "code": "000001.SZ",
                "candidate_rank": 1,
                "shadow_target_position_pct": 1.0,
            },
            {
                "code": "000002.SZ",
                "candidate_rank": 2,
                "shadow_target_position_pct": 1.0,
            },
        ],
        returns={"000001.SZ": 10.0},
    )

    session = store.list_sessions(experiment_id)[0]
    assert session["candidate_count"] == 2
    assert session["selected_candidate_count"] == 1
    assert session["selected_settled_count"] == 1
    assert session["settlement_status"] == "settled"
    assert store.evaluate_once(experiment_id)["status"] == "validated"


def test_candidates_reject_negative_target_position_pct(tmp_path):
    store = _store(tmp_path)
    experiment = _locked(store)
    store.record_session(experiment["experiment_id"], "2026-07-02", source_snapshot={"market_data_trusted": True})
    with pytest.raises(ValueError, match="target_position_pct"):
        store.record_candidates(
            experiment["experiment_id"],
            "2026-07-02",
            [{"code": "000001.SZ", "candidate_rank": 1, "target_position_pct": -0.01}],
        )


def test_only_one_locked_shadow_experiment_can_exist_until_the_previous_one_is_terminal(tmp_path):
    store = _store(tmp_path)
    first = _locked(store)
    conflicting = _manifest()
    conflicting["trade_parameters"]["hold_days"] = 5
    with pytest.raises(ValueError, match="active"):
        store.create_locked_experiment("run-20260731", conflicting, locked_at="2026-07-02T12:00:00+00:00")

    store.cancel_experiment(first["experiment_id"])
    second = store.create_locked_experiment("run-20260731", conflicting, locked_at="2026-07-02T12:00:00+00:00")
    assert second["status"] == "locked_shadow"
    assert second["experiment_id"] != first["experiment_id"]


def test_settlement_persists_trade_path_dates_and_reason(tmp_path):
    store = _store(tmp_path)
    experiment = _locked(store)
    experiment_id = experiment["experiment_id"]
    store.record_session(experiment_id, "2026-07-02", source_snapshot={"market_data_trusted": True})
    store.record_candidates(experiment_id, "2026-07-02", [{"code": "000001.SZ", "candidate_rank": 1}])
    store.settle_candidate(
        experiment_id,
        "2026-07-02",
        "000001.SZ",
        entry_date="2026-07-03",
        exit_date="2026-07-05",
        entry_price=10.0,
        exit_price=10.5,
        exit_reason="take_profit",
        realized_return_pct=4.64,
        block_reason="limit_up",
        valuation_path=[{"date": "2026-07-03", "return_pct": -0.36}, {"date": "2026-07-05", "return_pct": 4.64}],
    )
    candidate = store.list_candidates(experiment_id)[0]
    assert candidate["entry_date"] == "2026-07-03"
    assert candidate["exit_date"] == "2026-07-05"
    assert candidate["exit_reason"] == "take_profit"
    assert candidate["block_reason"] == "limit_up"


def test_evaluation_uses_selected_candidates_and_frozen_portfolio_configuration(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest(required_sessions=1)
    manifest["config"] = {
        **manifest["config"],
        "top_k": 1,
        "candidate_pool_size": 2,
        "holding_sessions": 1,
    }
    experiment = store.create_locked_experiment("run-portfolio", manifest, locked_at="2026-07-01T12:00:00+00:00")
    experiment_id = experiment["experiment_id"]
    _record_verified_session(
        store,
        experiment_id,
        "2026-07-02",
        candidates=[
            {
                "code": "000001.SZ",
                "candidate_rank": 1,
                "shadow_target_position_pct": 1.0,
            },
            {
                "code": "000002.SZ",
                "candidate_rank": 2,
                "shadow_target_position_pct": 1.0,
            },
        ],
        returns={"000001.SZ": 10.0},
    )

    evaluation = store.evaluate_once(experiment_id)
    assert evaluation["status"] == "validated"
    assert evaluation["metrics"]["total_return_pct"] == 10.0
    assert evaluation["metrics"]["executed_trade_count"] == 1
    assert evaluation["metrics"]["portfolio"]["attempted_slots"] == 1


def test_evaluation_uses_frozen_target_position_pct_for_portfolio_allocation(
    tmp_path,
):
    store = _store(tmp_path)
    manifest = _manifest(required_sessions=1)
    manifest["config"] = {
        **manifest["config"],
        "top_k": 1,
        "candidate_pool_size": 1,
        "holding_sessions": 1,
    }
    experiment = store.create_locked_experiment(
        "run-position-sized",
        manifest,
        locked_at="2026-07-01T12:00:00+00:00",
    )
    experiment_id = experiment["experiment_id"]
    _record_verified_session(
        store,
        experiment_id,
        "2026-07-02",
        candidates=[
            {
                "code": "000001.SZ",
                "candidate_rank": 1,
                "shadow_target_position_pct": 0.1,
            }
        ],
        returns={"000001.SZ": 10.0},
    )

    evaluation = store.evaluate_once(experiment_id)

    assert evaluation["status"] == "validated"
    assert evaluation["metrics"]["total_return_pct"] == 1.0
    trade = evaluation["metrics"]["portfolio"]["executed_trades"][0]
    assert trade["allocation"] == 0.1
    assert trade["target_position_pct"] == 0.1


def test_schema_migration_adds_session_finalization_and_settlement_path_columns(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE experiments (experiment_id TEXT PRIMARY KEY, source_run_id TEXT NOT NULL, model_instance_id TEXT NOT NULL, manifest_json TEXT NOT NULL, status TEXT NOT NULL, locked_at TEXT NOT NULL, created_at TEXT NOT NULL, cancelled_at TEXT, evaluated_at TEXT, evaluation_json TEXT);
            CREATE TABLE prospective_sessions (experiment_id TEXT NOT NULL, signal_date TEXT NOT NULL, data_complete INTEGER NOT NULL, data_trusted INTEGER NOT NULL, source_snapshot_json TEXT NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY (experiment_id, signal_date));
            CREATE TABLE prospective_candidates (experiment_id TEXT NOT NULL, signal_date TEXT NOT NULL, code TEXT NOT NULL, candidate_rank INTEGER NOT NULL, target_position_pct REAL NOT NULL, snapshot_json TEXT NOT NULL, settlement_status TEXT NOT NULL DEFAULT 'pending', entry_price REAL, exit_price REAL, realized_return_pct REAL, blocked INTEGER NOT NULL DEFAULT 0, block_reason TEXT, valuation_path_json TEXT, settled_at TEXT, PRIMARY KEY (experiment_id, signal_date, code));
            """
        )
    RelayFactorExperimentStore(db_path)
    RelayFactorExperimentStore(db_path)
    with sqlite3.connect(db_path) as connection:
        session_columns = {row[1] for row in connection.execute("PRAGMA table_info(prospective_sessions)")}
        candidate_columns = {row[1] for row in connection.execute("PRAGMA table_info(prospective_candidates)")}
    assert "candidates_recorded" in session_columns
    assert {"selected", "entry_date", "exit_date", "exit_reason"} <= candidate_columns


@pytest.mark.parametrize(
    "tamper",
    ["manifest_model", "model_instance_id", "protocol_id"],
)
def test_persisted_experiment_integrity_is_rechecked_on_every_read(
    tmp_path,
    tamper,
):
    store = _store(tmp_path)
    experiment = _locked(store)
    with store._connect() as connection:
        if tamper == "manifest_model":
            manifest = deepcopy(experiment["manifest"])
            manifest["model"]["coefficients"]["ret_5"] = 999.0
            connection.execute(
                "UPDATE experiments SET manifest_json=? WHERE experiment_id=?",
                (
                    canonical_json(manifest),
                    experiment["experiment_id"],
                ),
            )
        elif tamper == "model_instance_id":
            connection.execute(
                "UPDATE experiments SET model_instance_id=? WHERE experiment_id=?",
                ("0" * 64, experiment["experiment_id"]),
            )
        else:
            connection.execute(
                "UPDATE experiments SET protocol_id=? WHERE experiment_id=?",
                ("f" * 64, experiment["experiment_id"]),
            )

    with pytest.raises(ValueError, match="experiment integrity"):
        store.get_experiment(experiment["experiment_id"])


def test_first_twenty_settled_sessions_are_evaluated_once(tmp_path):
    store = _store(tmp_path)
    experiment = _locked(store)
    experiment_id = experiment["experiment_id"]
    for signal_date in _JULY_2026_FIXED_SESSIONS:
        _record_settled_session(store, experiment_id, signal_date, 1.0)

    first = store.evaluate_once(experiment_id)
    second = store.evaluate_once(experiment_id)
    assert first["evaluated"] is True
    assert first["status"] == "validated"
    assert first["metrics"]["executed_trade_count"] == 20
    assert second["already_evaluated"] is True
    assert store.get_experiment(experiment_id)["status"] == "validated"


def test_evaluation_preserves_verified_zero_candidate_sessions_for_bootstrap(
    tmp_path,
):
    store = _store(tmp_path)
    experiment = _locked(store)
    experiment_id = experiment["experiment_id"]

    def empty_snapshot(_panel, _manifest):
        signal_date = empty_snapshot.signal_date
        return {
            "ok": True,
            "data_status": "ok",
            "reason": "ok",
            "signal_date": signal_date,
            "market_gate": {"enabled": False, "pass": True},
            "data_quality": {
                "latest_real_amount_coverage": 1.0,
                "latest_st_status_coverage": 1.0,
                "latest_market_data_trusted": True,
            },
            "candidates": [],
        }

    empty_snapshot.signal_date = ""
    with patch.object(
        relay_factor_mining,
        "build_frozen_shadow_snapshot",
        empty_snapshot,
    ):
        for signal_date in _JULY_2026_FIXED_SESSIONS:
            empty_snapshot.signal_date = signal_date
            store.record_verified_shadow_session(
                experiment_id,
                signal_date,
                panel={},
            )

    evaluation = store.evaluate_once(experiment_id)

    curve = evaluation["metrics"]["portfolio"]["equity_curve"]
    assert [item["date"] for item in curve] == [
        "initial",
        *_JULY_2026_FIXED_SESSIONS,
    ]
    assert all(item["equity"] == 1.0 for item in curve)
    assert (
        evaluation["metrics"]["bootstrap_total_return_ci_pct"]["samples"]
        == 2000
    )


def test_latest_validated_experiment_remains_deployable_while_challenger_is_active(
    tmp_path,
):
    store = _store(tmp_path)
    incumbent = store.create_locked_experiment(
        "run-incumbent",
        _dual_track_manifest(required_sessions=1),
        locked_at="2026-07-01T12:00:00+00:00",
    )
    _record_verified_dual_session(
        store,
        incumbent["experiment_id"],
        "2026-07-02",
        strategy_return_pct=1.0,
        control_return_pct=0.0,
    )
    assert store.evaluate_once(incumbent["experiment_id"])["status"] == "validated"

    challenger_manifest = _dual_track_manifest(
        required_sessions=1
    )
    challenger_manifest["model"]["coefficients"]["ret_5"] = 0.3
    challenger = store.create_locked_experiment(
        "run-challenger",
        challenger_manifest,
        locked_at="2026-07-03T12:00:00+00:00",
    )

    assert (
        store.get_latest_validated_experiment()["experiment_id"]
        == incumbent["experiment_id"]
    )
    assert store.get_active_experiment()["experiment_id"] == challenger["experiment_id"]
    assert [item["status"] for item in store.list_experiments(limit=2)] == [
        "locked_shadow",
        "validated",
    ]


def test_overview_remains_consistent_when_database_changes_mid_read(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path)
    experiment = _locked(store)
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"

    original_output = store._experiment_output
    writer_ran = False

    def mutate_after_first_experiment_read(row):
        nonlocal writer_ran
        output = original_output(row)
        if not writer_ran:
            writer_ran = True
            with sqlite3.connect(store.db_path) as writer:
                writer.execute(
                    """
                    UPDATE experiments
                    SET status = 'cancelled', cancelled_at = ?
                    WHERE experiment_id = ?
                    """,
                    ("2026-07-30T10:00:00+00:00", experiment["experiment_id"]),
                )
        return output

    monkeypatch.setattr(store, "_experiment_output", mutate_after_first_experiment_read)

    overview = store.get_overview(limit=10)

    assert writer_ran is True
    assert overview["found"] is True
    assert overview["experiment"]["status"] == "locked_shadow"
    assert overview["progress"]["status"] == "locked_shadow"
    assert overview["experiments"][0]["status"] == "locked_shadow"
    assert overview["experiments"][0]["progress"]["status"] == "locked_shadow"
    assert store.get_experiment(experiment["experiment_id"])["status"] == "cancelled"


def test_failed_first_twenty_sessions_cannot_be_reversed_by_a_good_twenty_first_day(tmp_path):
    store = _store(tmp_path)
    experiment = _locked(store, maximum_drawdown_pct=-5.0)
    experiment_id = experiment["experiment_id"]
    for signal_date in _JULY_2026_FIXED_SESSIONS:
        _record_settled_session(store, experiment_id, signal_date, -1.0)
    store.record_frozen_snapshot(
        experiment_id,
        "2026-07-30",
        source_snapshot={"market_data_trusted": True},
        candidates=[{"code": "999999.SZ", "candidate_rank": 1}],
    )
    store.settle_candidate(
        experiment_id,
        "2026-07-30",
        "999999.SZ",
        entry_date="2026-07-31",
        exit_date="2026-08-03",
        entry_price=10.0,
        exit_price=20.036,
        exit_reason="max_holding_period",
        realized_return_pct=100.0,
        valuation_path=[
            {"date": "2026-07-31", "return_pct": -0.36},
            {"date": "2026-08-03", "return_pct": 100.0},
        ],
    )

    failed = store.evaluate_once(experiment_id)
    assert failed["status"] == "failed"
    assert store.evaluate_once(experiment_id)["status"] == "failed"
    with pytest.raises(ValueError, match="terminal"):
        store.record_session(experiment_id, "2026-07-23", source_snapshot={"market_data_trusted": True})


def test_volatile_positive_forward_return_does_not_pass_confidence_gate(tmp_path):
    store = _store(tmp_path)
    manifest = _manifest()
    manifest["acceptance_rules"]["minimum_bootstrap_lower_return_pct"] = 0.0
    experiment = store.create_locked_experiment(
        "run-tiny-positive",
        manifest,
        locked_at="2026-07-01T12:00:00+00:00",
    )
    experiment_id = experiment["experiment_id"]
    returns = [10.0, -9.0] * 10
    for signal_date, return_pct in zip(_JULY_2026_FIXED_SESSIONS, returns):
        _record_settled_session(store, experiment_id, signal_date, return_pct)

    evaluation = store.evaluate_once(experiment_id)

    assert evaluation["status"] == "failed"
    assert "bootstrap_lower_bound_not_positive" in evaluation["failure_reasons"]
    assert evaluation["metrics"]["bootstrap_total_return_ci_pct"]["lower"] <= 0.0


def test_low_level_injected_positive_evidence_cannot_become_validated(tmp_path):
    store = _store(tmp_path)
    experiment = store.create_locked_experiment(
        "run-unverified-injection",
        _manifest(required_sessions=1),
        locked_at="2026-07-01T12:00:00+00:00",
    )
    experiment_id = experiment["experiment_id"]
    store.record_session(
        experiment_id,
        "2026-07-02",
        source_snapshot={"market_data_trusted": True},
    )
    store.record_candidates(
        experiment_id,
        "2026-07-02",
        [
            {
                "code": "000001.SZ",
                "candidate_rank": 1,
                "selected": True,
                "target_position_pct": 1.0,
            }
        ],
    )
    store.settle_candidate(
        experiment_id,
        "2026-07-02",
        "000001.SZ",
        entry_date="2026-07-03",
        exit_date="2026-07-06",
        entry_price=10.0,
        exit_price=11.0,
        exit_reason="take_profit",
        realized_return_pct=9.64,
        valuation_path=[
            {"date": "2026-07-03", "return_pct": -0.36},
            {"date": "2026-07-06", "return_pct": 9.64},
        ],
    )

    evaluation = store.evaluate_once(experiment_id)

    assert evaluation["status"] == "failed"
    assert "unverified_prospective_evidence" in evaluation["failure_reasons"]
    assert store.get_latest_validated_experiment() is None


def test_legacy_validated_experiment_is_not_returned_as_deployable(tmp_path):
    store = _store(tmp_path)
    experiment = store.create_locked_experiment(
        "run-legacy-validated",
        _manifest(required_sessions=1),
        locked_at="2026-07-01T12:00:00+00:00",
    )
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE experiments
            SET status = 'validated',
                evaluated_at = '2026-07-30T18:00:00+08:00',
                evaluation_json = ?
            WHERE experiment_id = ?
            """,
            (
                canonical_json(
                    {
                        "evaluated": True,
                        "status": "validated",
                        "failure_reasons": [],
                    }
                ),
                experiment["experiment_id"],
            ),
        )

    assert store.get_latest_validated_experiment() is None
    assert store.get_overview()["latest_validated"] is None


@pytest.mark.parametrize(
    "settlement",
    [
        {
            "entry_date": "2026-07-02",
            "exit_date": "2026-07-06",
            "entry_price": 10.0,
            "exit_price": 11.0,
            "exit_reason": "take_profit",
            "realized_return_pct": 9.64,
            "valuation_path": [
                {"date": "2026-07-02", "return_pct": -0.36},
                {"date": "2026-07-06", "return_pct": 9.64},
            ],
        },
        {
            "entry_date": "2026-07-03",
            "exit_date": "2026-07-06",
            "entry_price": 10.0,
            "exit_price": 11.0,
            "exit_reason": "take_profit",
            "realized_return_pct": 10.0,
            "valuation_path": [
                {"date": "2026-07-03", "return_pct": -0.36},
                {"date": "2026-07-06", "return_pct": 10.0},
            ],
        },
        {
            "entry_date": "2026-07-03",
            "exit_date": "2026-07-06",
            "entry_price": 10.0,
            "exit_price": 11.0,
            "exit_reason": "take_profit",
            "realized_return_pct": 9.64,
            "valuation_path": [
                {"date": "2026-07-03", "return_pct": -0.36},
                {"date": "2026-07-06", "return_pct": 8.0},
            ],
        },
    ],
    ids=[
        "entry_not_after_signal",
        "return_ignores_frozen_costs",
        "valuation_path_disagrees_with_return",
    ],
)
def test_candidate_settlement_rejects_inconsistent_evidence(
    tmp_path,
    settlement,
):
    store = _store(tmp_path)
    experiment = store.create_locked_experiment(
        "run-invalid-settlement",
        _manifest(required_sessions=1),
        locked_at="2026-07-01T12:00:00+00:00",
    )
    experiment_id = experiment["experiment_id"]
    store.record_session(
        experiment_id,
        "2026-07-02",
        source_snapshot={"market_data_trusted": True},
    )
    store.record_candidates(
        experiment_id,
        "2026-07-02",
        [{"code": "000001.SZ", "candidate_rank": 1}],
    )

    with pytest.raises(ValueError, match="settlement"):
        store.settle_candidate(
            experiment_id,
            "2026-07-02",
            "000001.SZ",
            **settlement,
        )


def test_verified_panel_flow_is_the_only_path_to_validated_evidence(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path)
    experiment = store.create_locked_experiment(
        "run-verified-panel",
        _manifest(required_sessions=1),
        locked_at="2026-07-01T12:00:00+00:00",
    )
    experiment_id = experiment["experiment_id"]

    def frozen_snapshot(_panel, manifest):
        return {
            "ok": True,
            "data_status": "ok",
            "reason": "ok",
            "signal_date": "2026-07-02",
            "market_gate": {"enabled": False, "pass": True},
            "data_quality": {
                "latest_real_amount_coverage": 1.0,
                "latest_st_status_coverage": 1.0,
                "latest_market_data_trusted": True,
            },
            "candidates": [
                {
                    "code": "000001.SZ",
                    "ts_code": "000001.SZ",
                    "trade_date": "2026-07-02",
                    "candidate_rank": 1,
                    "experiment_id": manifest["experiment_id"],
                    "model_instance_id": manifest["model_instance_id"],
                    "source_run_id": manifest["source_run_id"],
                    "research_status": "locked_shadow",
                    "deployment_eligible": False,
                    "research_only": True,
                    "gate_pass": True,
                    "shadow_target_position_pct": 0.1,
                    "position_pct": 0.0,
                }
            ],
        }

    def frozen_settlement(_panel, candidate, _manifest):
        assert candidate["signal_date"] == "2026-07-02"
        return {
            "status": "settled",
            "execution_blocked": False,
            "block_reasons": [],
            "entry_date": "2026-07-03",
            "exit_date": "2026-07-06",
            "entry_price": 10.0,
            "exit_price": 11.0,
            "exit_reason": "take_profit",
            "realized_return_pct": 9.64,
            "valuation_path": [
                {"date": "2026-07-03", "return_pct": -0.36},
                {"date": "2026-07-06", "return_pct": 9.64},
            ],
        }

    monkeypatch.setattr(
        relay_factor_mining,
        "build_frozen_shadow_snapshot",
        frozen_snapshot,
    )
    monkeypatch.setattr(
        relay_factor_mining,
        "settle_frozen_shadow_candidate",
        frozen_settlement,
    )

    recorded = store.record_verified_shadow_session(
        experiment_id,
        "2026-07-02",
        panel={},
    )
    settled = store.settle_verified_candidates(experiment_id, panel={})
    evaluation = store.evaluate_once(experiment_id)

    assert recorded["candidate_snapshot"]["inserted"] == 1
    assert settled == {"settled": 1, "pending": 0}
    assert evaluation["status"] == "validated"
    assert store.get_experiment(experiment_id)["evidence_verified"] is True
    candidate = store.list_candidates(experiment_id)[0]
    assert candidate["snapshot_verified"] is True
    assert candidate["settlement_verified"] is True
    assert candidate["snapshot"]["protocol_id"] == experiment["protocol_id"]
    assert (
        candidate["snapshot"]["protocol_bundle_sha256"]
        == experiment["manifest"]["protocol_binding"]["bundle_sha256"]
    )


def test_verified_snapshot_rejects_candidate_with_wrong_experiment_binding(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path)
    experiment = store.create_locked_experiment(
        "run-wrong-binding",
        _manifest(required_sessions=1),
        locked_at="2026-07-01T12:00:00+00:00",
    )

    monkeypatch.setattr(
        relay_factor_mining,
        "build_frozen_shadow_snapshot",
        lambda _panel, manifest: {
            "ok": True,
            "data_status": "ok",
            "reason": "ok",
            "signal_date": "2026-07-02",
            "market_gate": {"enabled": False, "pass": True},
            "data_quality": {},
            "candidates": [
                {
                    "code": "000001.SZ",
                    "ts_code": "000001.SZ",
                    "trade_date": "2026-07-02",
                    "candidate_rank": 1,
                    "experiment_id": "forged-experiment",
                    "model_instance_id": manifest["model_instance_id"],
                    "source_run_id": manifest["source_run_id"],
                    "research_status": "locked_shadow",
                    "deployment_eligible": False,
                    "research_only": True,
                    "gate_pass": True,
                    "shadow_target_position_pct": 0.1,
                    "position_pct": 0.0,
                }
            ],
        },
    )

    with pytest.raises(ValueError, match="candidate experiment_id"):
        store.record_verified_shadow_session(
            experiment["experiment_id"],
            "2026-07-02",
            panel={},
        )
    assert store.list_sessions(experiment["experiment_id"]) == []
