"""Frozen prospective-experiment storage for relay factor research."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from analytics.relay_factor_model import factor_model_payload
from analytics.relay_portfolio import simulate_portfolio

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = _ROOT / "data" / "research" / "relay_factor" / "prospective.sqlite3"
_ACTIVE_STATUS = "locked_shadow"
_TERMINAL_STATUSES = {"validated", "failed", "cancelled"}
_STATUSES = {"exploratory", _ACTIVE_STATUS, *_TERMINAL_STATUSES}
_VERIFIED_SCHEMA_VERSION = 3
_VERIFIED_STRATEGY_KEY = "relay_factor_mining_v2"
_REGISTERED_MODEL_BINDING_KEYS = (
    "preregistration_record_sha256",
    "preregistration_oos_not_before",
    "registered_model_sha256",
)
_EXECUTION_CONFIG_KEYS = (
    "n_stocks",
    "hold_days",
    "holding_sessions",
    "stop_loss",
    "take_profit",
    "top_k",
    "candidate_pool_size",
    "include_market_signals",
    "universe_selection",
)


def normalize_json(value: Any) -> Any:
    """Recursively reduce supported values to a stable JSON-compatible form."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("manifest contains a non-finite float")
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): normalize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_json(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [normalize_json(item) for item in value]
        return sorted(normalized, key=canonical_json)
    item_method = getattr(value, "item", None)
    if callable(item_method):
        return normalize_json(item_method())
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        normalize_json(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _bootstrap_total_return_ci(
    portfolio: Mapping[str, Any],
    *,
    samples: int = 2000,
    block_size: int = 5,
) -> dict[str, float | int | None]:
    curve = portfolio.get("equity_curve")
    values = np.asarray(
        [
            float(item.get("equity"))
            for item in (curve or [])
            if isinstance(item, Mapping) and item.get("equity") is not None
        ],
        dtype=float,
    )
    if len(values) < 21 or not np.isfinite(values).all() or np.any(values <= 0):
        return {
            "lower": None,
            "median": None,
            "upper": None,
            "samples": 0,
            "block_size": int(block_size),
        }
    returns = values[1:] / values[:-1] - 1.0
    n_returns = len(returns)
    block = max(1, min(int(block_size), n_returns))
    sample_count = max(100, int(samples))
    rng = np.random.default_rng(20260730)
    totals = np.empty(sample_count, dtype=float)
    max_start = n_returns - block
    for sample_index in range(sample_count):
        sampled: list[float] = []
        while len(sampled) < n_returns:
            start = int(rng.integers(0, max_start + 1)) if max_start > 0 else 0
            sampled.extend(returns[start : start + block].tolist())
        totals[sample_index] = (
            float(np.prod(1.0 + np.asarray(sampled[:n_returns], dtype=float))) - 1.0
        ) * 100.0
    lower, median, upper = np.percentile(totals, [2.5, 50.0, 97.5])
    return {
        "lower": round(float(lower), 4),
        "median": round(float(median), 4),
        "upper": round(float(upper), 4),
        "samples": sample_count,
        "block_size": block,
    }


def _without_derived_fields(manifest: Mapping[str, Any]) -> dict[str, Any]:
    frozen = normalize_json(manifest)
    if not isinstance(frozen, dict):
        raise TypeError("manifest must be an object")
    for key in ("model_instance_id", "experiment_id", "status"):
        frozen.pop(key, None)
    return frozen


def _executable_model_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _without_derived_fields(manifest)
    model = normalized.get("model", normalized.get("factor_model"))
    if not isinstance(model, Mapping):
        raise ValueError("manifest must contain frozen model content")
    executable = normalize_json(factor_model_payload(model))
    executable["registration_binding"] = {
        key: normalize_json(model[key])
        for key in _REGISTERED_MODEL_BINDING_KEYS
        if key in model
    }
    return executable


def _execution_protocol_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _without_derived_fields(manifest)
    config = normalized.get("config")
    trade_parameters = normalized.get("trade_parameters")
    config = config if isinstance(config, Mapping) else {}
    trade_parameters = (
        trade_parameters if isinstance(trade_parameters, Mapping) else {}
    )
    execution_config = {}
    for key in _EXECUTION_CONFIG_KEYS:
        if key in config:
            execution_config[key] = config[key]
        elif key in trade_parameters:
            execution_config[key] = trade_parameters[key]
    mining_rules = normalized.get("mining_rules")
    if not isinstance(mining_rules, Mapping):
        mining_rules = normalized.get("rules")
    return {
        "schema_version": normalized.get("schema_version", 1),
        "strategy_key": normalized.get("strategy_key"),
        "model": _executable_model_payload(normalized),
        "model_specification": (
            normalized.get("model_specification")
            if isinstance(normalized.get("model_specification"), Mapping)
            else {}
        ),
        "execution_config": execution_config,
        "execution_rules": (
            normalized.get("execution_rules")
            if isinstance(normalized.get("execution_rules"), Mapping)
            else {}
        ),
        "mining_rules": mining_rules if isinstance(mining_rules, Mapping) else {},
        "market_gate": (
            normalized.get("market_gate")
            if isinstance(normalized.get("market_gate"), Mapping)
            else {}
        ),
        "stock_pool": (
            normalized.get("stock_pool")
            if isinstance(normalized.get("stock_pool"), Mapping)
            else {}
        ),
        "data_quality_policy": (
            normalized.get("data_quality_policy")
            if isinstance(normalized.get("data_quality_policy"), Mapping)
            else {}
        ),
        "acceptance_rules": (
            normalized.get("acceptance_rules")
            if isinstance(normalized.get("acceptance_rules"), Mapping)
            else {}
        ),
    }


def model_instance_id_for_manifest(manifest: Mapping[str, Any]) -> str:
    return _sha256(_executable_model_payload(manifest))


def experiment_id_for_manifest(manifest: Mapping[str, Any]) -> str:
    return _sha256(_execution_protocol_payload(manifest))


def _verified_experiment_manifest(row: sqlite3.Row) -> dict[str, Any]:
    try:
        manifest = _json_loads(row["manifest_json"], {})
        if not isinstance(manifest, dict):
            raise TypeError("manifest is not an object")
        model_instance_id = model_instance_id_for_manifest(manifest)
        protocol_id = experiment_id_for_manifest(manifest)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("experiment integrity verification failed") from exc
    expected = {
        "model_instance_id": str(row["model_instance_id"] or ""),
        "protocol_id": str(row["protocol_id"] or ""),
        "experiment_id": str(row["experiment_id"] or ""),
        "source_run_id": str(row["source_run_id"] or ""),
    }
    actual = {
        "model_instance_id": model_instance_id,
        "protocol_id": protocol_id,
        "experiment_id": protocol_id,
        "source_run_id": str(manifest.get("source_run_id") or ""),
    }
    manifest_bindings = {
        "model_instance_id": str(manifest.get("model_instance_id") or ""),
        "protocol_id": str(manifest.get("protocol_id") or ""),
        "experiment_id": str(manifest.get("experiment_id") or ""),
        "source_run_id": str(manifest.get("source_run_id") or ""),
    }
    if actual != expected or manifest_bindings != expected:
        raise ValueError("experiment integrity verification failed")
    return manifest


def _json_loads(value: str | None, default: Any) -> Any:
    return default if value is None else json.loads(value)


def _as_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def _as_datetime(value: str | datetime) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _timestamp(value: str | datetime | None) -> str:
    if value is None:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _fixed_session_dates(
    locked_at: str | date | datetime,
    required_sessions: int,
) -> list[str]:
    from market import trading_calendar

    required = max(1, int(required_sessions))
    cursor = _as_date(locked_at) + timedelta(days=1)
    sessions: list[str] = []
    max_scanned_days = max(366, required * 10)
    for _ in range(max_scanned_days):
        if trading_calendar.is_trading_day(cursor):
            sessions.append(cursor.isoformat())
            if len(sessions) == required:
                return sessions
        cursor += timedelta(days=1)
    raise RuntimeError("fixed prospective session calendar unavailable")


def _manifest_protocol_is_verified(manifest: Mapping[str, Any]) -> bool:
    if (
        int(manifest.get("schema_version") or 0) != _VERIFIED_SCHEMA_VERSION
        or str(manifest.get("strategy_key") or "") != _VERIFIED_STRATEGY_KEY
    ):
        return False
    model = manifest.get("model")
    if not isinstance(model, Mapping) or (
        model.get("fallback") is not False
        or model.get("validation_accepted") is not True
        or model.get("validation_stable") is not True
    ):
        return False
    from analytics import relay_factor_mining

    registration = relay_factor_mining.load_model_preregistration()
    valid, _reason = relay_factor_mining.validate_frozen_protocol_binding(
        manifest
    )
    expected_preregistration = (
        {
            **registration["record"],
            "record_sha256": registration["sha256"],
        }
        if registration.get("ok")
        else None
    )
    return bool(
        registration.get("ok")
        and valid
        and relay_factor_mining._registered_frozen_model_is_eligible(
            model,
            registration,
        )
        and manifest.get("model_specification")
        == {
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
        }
        and manifest.get("preregistration") == expected_preregistration
        and manifest.get("acceptance_rules")
        == relay_factor_mining.PROSPECTIVE_ACCEPTANCE_RULES
    )


def _validate_settlement_evidence(
    *,
    signal_date: str,
    manifest: Mapping[str, Any],
    settlement: Mapping[str, Any],
) -> None:
    blocked = bool(settlement.get("blocked"))
    entry_date = settlement.get("entry_date")
    exit_date = settlement.get("exit_date")
    entry_price = settlement.get("entry_price")
    exit_price = settlement.get("exit_price")
    realized_return = settlement.get("realized_return_pct")
    path = settlement.get("valuation_path")

    def positive_price(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0.0

    if entry_date is not None and str(entry_date) <= signal_date:
        raise ValueError("settlement entry_date must be after signal_date")
    if entry_price is not None and not positive_price(entry_price):
        raise ValueError("settlement entry_price must be finite and positive")

    if blocked:
        if realized_return not in (None, 0, 0.0):
            raise ValueError("settlement blocked return must be zero")
        if exit_date is not None or exit_price is not None:
            raise ValueError("settlement blocked candidate cannot contain an exit")
        if path:
            raise ValueError("settlement blocked candidate cannot contain a valuation path")
        return

    if entry_date is None or exit_date is None:
        raise ValueError("settlement requires entry_date and exit_date")
    if str(exit_date) < str(entry_date):
        raise ValueError("settlement exit_date must not precede entry_date")
    if not positive_price(entry_price) or not positive_price(exit_price):
        raise ValueError("settlement prices must be finite and positive")
    if not str(settlement.get("exit_reason") or "").strip():
        raise ValueError("settlement exit_reason is required")
    try:
        realized = float(realized_return)
    except (TypeError, ValueError) as exc:
        raise ValueError("settlement realized return is required") from exc
    if not math.isfinite(realized):
        raise ValueError("settlement realized return must be finite")

    rules = manifest.get("execution_rules")
    rules = rules if isinstance(rules, Mapping) else {}
    total_cost_pct = float(rules.get("round_trip_cost_pct", 0.16)) + float(
        rules.get("slippage_pct", 0.20)
    )
    expected_return = (
        float(exit_price) / float(entry_price) - 1.0
    ) * 100.0 - total_cost_pct
    if not math.isclose(realized, expected_return, abs_tol=0.011):
        raise ValueError("settlement return is inconsistent with prices and frozen costs")

    if not isinstance(path, list) or not path:
        raise ValueError("settlement valuation path is required")
    path_dates: list[str] = []
    path_returns: list[float] = []
    for item in path:
        if not isinstance(item, Mapping):
            raise ValueError("settlement valuation path item must be an object")
        path_date = str(item.get("date") or "")[:10]
        try:
            path_return = float(item.get("return_pct"))
        except (TypeError, ValueError) as exc:
            raise ValueError("settlement valuation path return is invalid") from exc
        if not path_date or not math.isfinite(path_return):
            raise ValueError("settlement valuation path item is invalid")
        path_dates.append(path_date)
        path_returns.append(path_return)
    if (
        path_dates[0] != str(entry_date)
        or path_dates[-1] != str(exit_date)
        or path_dates != sorted(set(path_dates))
    ):
        raise ValueError("settlement valuation path dates are inconsistent")
    if not math.isclose(path_returns[-1], realized, abs_tol=0.011):
        raise ValueError("settlement valuation path final return is inconsistent")


class RelayFactorExperimentStore:
    """SQLite-backed, append-only prospective experiment state machine."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    protocol_id TEXT NOT NULL,
                    source_run_id TEXT NOT NULL,
                    model_instance_id TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('exploratory', 'locked_shadow', 'validated', 'failed', 'cancelled')),
                    locked_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expected_session_dates_json TEXT NOT NULL,
                    protocol_verified INTEGER NOT NULL DEFAULT 0 CHECK (protocol_verified IN (0, 1)),
                    evidence_verified INTEGER NOT NULL DEFAULT 0 CHECK (evidence_verified IN (0, 1)),
                    cancelled_at TEXT,
                    evaluated_at TEXT,
                    evaluation_json TEXT,
                    UNIQUE (source_run_id, experiment_id)
                );

                CREATE TABLE IF NOT EXISTS prospective_sessions (
                    experiment_id TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    data_complete INTEGER NOT NULL CHECK (data_complete IN (0, 1)),
                    data_trusted INTEGER NOT NULL CHECK (data_trusted IN (0, 1)),
                    source_snapshot_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    candidates_recorded INTEGER NOT NULL DEFAULT 0 CHECK (candidates_recorded IN (0, 1)),
                    evidence_verified INTEGER NOT NULL DEFAULT 0 CHECK (evidence_verified IN (0, 1)),
                    PRIMARY KEY (experiment_id, signal_date),
                    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id)
                );

                CREATE TABLE IF NOT EXISTS prospective_candidates (
                    experiment_id TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    candidate_rank INTEGER NOT NULL,
                    target_position_pct REAL NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 1 CHECK (selected IN (0, 1)),
                    settlement_status TEXT NOT NULL CHECK (settlement_status IN ('pending', 'settled')) DEFAULT 'pending',
                    entry_price REAL,
                    exit_price REAL,
                    entry_date TEXT,
                    exit_date TEXT,
                    exit_reason TEXT,
                    realized_return_pct REAL,
                    blocked INTEGER NOT NULL DEFAULT 0 CHECK (blocked IN (0, 1)),
                    block_reason TEXT,
                    valuation_path_json TEXT,
                    settled_at TEXT,
                    snapshot_verified INTEGER NOT NULL DEFAULT 0 CHECK (snapshot_verified IN (0, 1)),
                    settlement_verified INTEGER NOT NULL DEFAULT 0 CHECK (settlement_verified IN (0, 1)),
                    PRIMARY KEY (experiment_id, signal_date, code),
                    FOREIGN KEY (experiment_id, signal_date)
                        REFERENCES prospective_sessions(experiment_id, signal_date)
                );
                """
            )
            self._ensure_column(connection, "prospective_sessions", "candidates_recorded", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "prospective_candidates", "selected", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(connection, "prospective_candidates", "entry_date", "TEXT")
            self._ensure_column(connection, "prospective_candidates", "exit_date", "TEXT")
            self._ensure_column(connection, "prospective_candidates", "exit_reason", "TEXT")
            self._ensure_column(
                connection,
                "prospective_candidates",
                "snapshot_verified",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "prospective_candidates",
                "settlement_verified",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "prospective_sessions",
                "evidence_verified",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "experiments", "protocol_id", "TEXT")
            self._ensure_column(
                connection,
                "experiments",
                "protocol_verified",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "experiments",
                "evidence_verified",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "experiments",
                "expected_session_dates_json",
                "TEXT",
            )
            rows = connection.execute(
                """
                SELECT experiment_id, manifest_json
                FROM experiments
                WHERE protocol_id IS NULL OR protocol_id = ''
                """
            ).fetchall()
            for row in rows:
                manifest = _json_loads(row["manifest_json"], {})
                connection.execute(
                    "UPDATE experiments SET protocol_id = ? WHERE experiment_id = ?",
                    (
                        experiment_id_for_manifest(manifest),
                        row["experiment_id"],
                    ),
                )
            window_rows = connection.execute(
                """
                SELECT experiment_id, manifest_json, locked_at
                FROM experiments
                WHERE expected_session_dates_json IS NULL
                   OR expected_session_dates_json = ''
                """
            ).fetchall()
            for row in window_rows:
                manifest = _json_loads(row["manifest_json"], {})
                rules = manifest.get("acceptance_rules") or {}
                required = max(
                    1,
                    int(
                        rules.get(
                            "required_sessions",
                            rules.get("prospective_sessions_required", 20),
                        )
                    ),
                )
                expected_dates = _fixed_session_dates(row["locked_at"], required)
                connection.execute(
                    """
                    UPDATE experiments
                    SET expected_session_dates_json = ?
                    WHERE experiment_id = ?
                    """,
                    (canonical_json(expected_dates), row["experiment_id"]),
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_experiments_protocol_id ON experiments(protocol_id)"
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _require_experiment(self, connection: sqlite3.Connection, experiment_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown experiment_id: {experiment_id}")
        _verified_experiment_manifest(row)
        return row

    @staticmethod
    def _require_locked(row: sqlite3.Row) -> None:
        if row["status"] in _TERMINAL_STATUSES:
            raise ValueError("terminal experiment cannot be changed")
        if row["status"] != _ACTIVE_STATUS:
            raise ValueError("only locked_shadow experiments can be changed")

    @staticmethod
    def _experiment_output(row: sqlite3.Row) -> dict[str, Any]:
        manifest = _verified_experiment_manifest(row)
        return {
            "experiment_id": row["experiment_id"],
            "protocol_id": row["protocol_id"],
            "source_run_id": row["source_run_id"],
            "model_instance_id": row["model_instance_id"],
            "status": row["status"],
            "manifest": manifest,
            "locked_at": row["locked_at"],
            "created_at": row["created_at"],
            "expected_session_dates": _json_loads(
                row["expected_session_dates_json"],
                [],
            ),
            "protocol_verified": bool(row["protocol_verified"]),
            "evidence_verified": bool(row["evidence_verified"]),
            "cancelled_at": row["cancelled_at"],
            "evaluated_at": row["evaluated_at"],
            "evaluation": _json_loads(row["evaluation_json"], None),
        }

    def create_locked_experiment(
        self,
        source_run_id: str,
        manifest: Mapping[str, Any],
        locked_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        locked_timestamp = _timestamp(locked_at)
        frozen = _without_derived_fields(manifest)
        model_instance_id = model_instance_id_for_manifest(frozen)
        protocol_id = experiment_id_for_manifest(frozen)
        frozen["model_instance_id"] = model_instance_id
        frozen["protocol_id"] = protocol_id
        frozen["source_run_id"] = str(source_run_id)
        frozen["status"] = _ACTIVE_STATUS
        experiment_id = protocol_id
        frozen["experiment_id"] = experiment_id
        manifest_json = canonical_json(frozen)
        rules = frozen.get("acceptance_rules") or {}
        required_sessions = max(
            1,
            int(
                rules.get(
                    "required_sessions",
                    rules.get("prospective_sessions_required", 20),
                )
            ),
        )
        expected_session_dates = _fixed_session_dates(
            locked_timestamp,
            required_sessions,
        )
        expected_session_dates_json = canonical_json(expected_session_dates)
        from analytics import relay_factor_mining

        registration = relay_factor_mining.load_model_preregistration()
        protocol_verified = (
            _manifest_protocol_is_verified(frozen)
            and registration.get("ok")
            and _as_datetime(locked_timestamp)
            >= _as_datetime(registration["record"]["registered_at"])
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM experiments
                WHERE protocol_id = ?
                ORDER BY created_at, experiment_id
                LIMIT 1
                """,
                (protocol_id,),
            ).fetchone()
            if existing is not None:
                return self._experiment_output(existing)
            active = connection.execute(
                "SELECT experiment_id FROM experiments WHERE status = ? LIMIT 1",
                (_ACTIVE_STATUS,),
            ).fetchone()
            if active is not None:
                raise ValueError("another active locked_shadow experiment already exists")
            connection.execute(
                """
                INSERT INTO experiments (
                    experiment_id, protocol_id, source_run_id, model_instance_id,
                    manifest_json, status, locked_at, created_at,
                    expected_session_dates_json, protocol_verified,
                    evidence_verified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    experiment_id,
                    protocol_id,
                    str(source_run_id),
                    model_instance_id,
                    manifest_json,
                    _ACTIVE_STATUS,
                    locked_timestamp,
                    locked_timestamp,
                    expected_session_dates_json,
                    int(protocol_verified),
                ),
            )
            row = self._require_experiment(connection, experiment_id)
            return self._experiment_output(row)

    def get_experiment(self, experiment_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._experiment_output(self._require_experiment(connection, experiment_id))

    def get_active_experiment(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM experiments
                WHERE status = ?
                ORDER BY locked_at DESC, experiment_id DESC
                LIMIT 2
                """,
                (_ACTIVE_STATUS,),
            ).fetchall()
            if len(rows) > 1:
                raise RuntimeError("multiple active locked_shadow experiments found")
            return None if not rows else self._experiment_output(rows[0])

    def get_latest_validated_experiment(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM experiments
                WHERE status = 'validated'
                  AND protocol_verified = 1
                  AND evidence_verified = 1
                ORDER BY evaluated_at DESC, locked_at DESC, experiment_id DESC
                LIMIT 1
                """
            ).fetchone()
            return None if row is None else self._experiment_output(row)

    def list_experiments(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(100, int(limit or 20)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM experiments
                ORDER BY created_at DESC, experiment_id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
            return [self._experiment_output(row) for row in rows]

    def get_overview(self, limit: int = 10) -> dict[str, Any]:
        """Read active, incumbent, history, and progress from one DB snapshot."""
        safe_limit = max(1, min(100, int(limit or 10)))
        with self._connect() as connection:
            connection.execute("BEGIN")
            active_rows = connection.execute(
                """
                SELECT * FROM experiments
                WHERE status = ?
                ORDER BY locked_at DESC, experiment_id DESC
                LIMIT 2
                """,
                (_ACTIVE_STATUS,),
            ).fetchall()
            if len(active_rows) > 1:
                raise RuntimeError("multiple active locked_shadow experiments found")
            active_row = active_rows[0] if active_rows else None
            active = (
                None
                if active_row is None
                else self._experiment_output(active_row)
            )

            latest_validated_row = connection.execute(
                """
                SELECT * FROM experiments
                WHERE status = 'validated'
                  AND protocol_verified = 1
                  AND evidence_verified = 1
                ORDER BY evaluated_at DESC, locked_at DESC, experiment_id DESC
                LIMIT 1
                """
            ).fetchone()
            latest_validated = (
                None
                if latest_validated_row is None
                else self._experiment_output(latest_validated_row)
            )
            history_rows = connection.execute(
                """
                SELECT * FROM experiments
                ORDER BY created_at DESC, experiment_id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()

            progress_by_id: dict[str, dict[str, Any]] = {}

            def progress_for(row: sqlite3.Row) -> dict[str, Any]:
                experiment_id = str(row["experiment_id"])
                if experiment_id not in progress_by_id:
                    progress_by_id[experiment_id] = (
                        self._progress_from_connection(connection, row)
                    )
                return progress_by_id[experiment_id]

            active_progress = (
                None if active_row is None else progress_for(active_row)
            )
            experiments = [
                {
                    **self._experiment_output(row),
                    "progress": progress_for(row),
                }
                for row in history_rows
            ]
            return {
                "found": active is not None,
                "experiment": active,
                "progress": active_progress,
                "latest_validated": latest_validated,
                "experiments": experiments,
            }

    def cancel_experiment(
        self, experiment_id: str, cancelled_at: str | datetime | None = None
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_experiment(connection, experiment_id)
            self._require_locked(row)
            timestamp = _timestamp(cancelled_at)
            connection.execute(
                "UPDATE experiments SET status = 'cancelled', cancelled_at = ? WHERE experiment_id = ?",
                (timestamp, experiment_id),
            )
            return self._experiment_output(self._require_experiment(connection, experiment_id))

    @staticmethod
    def _candidate_snapshots(
        candidates: Sequence[Mapping[str, Any]],
    ) -> dict[str, tuple[int, float, bool, str]]:
        snapshots: dict[str, tuple[int, float, bool, str]] = {}
        for candidate in candidates:
            snapshot = normalize_json(candidate)
            if not isinstance(snapshot, dict):
                raise TypeError("candidate must be an object")
            code = str(snapshot.get("code") or snapshot.get("ts_code") or "").strip()
            if not code:
                raise ValueError("candidate code is required")
            rank = int(snapshot.get("candidate_rank", snapshot.get("rank", 0)) or 0)
            if rank <= 0:
                raise ValueError("candidate rank must be positive")
            position = float(
                snapshot.get(
                    "target_position_pct",
                    snapshot.get("position_pct", 0.0),
                )
                or 0.0
            )
            if not math.isfinite(position) or position < 0.0:
                raise ValueError(
                    "target_position_pct must be a finite non-negative number"
                )
            selected = bool(snapshot.get("selected", True))
            snapshot["selected"] = selected
            if code in snapshots:
                raise ValueError(
                    "candidate codes must be unique within a frozen snapshot"
                )
            snapshots[code] = (
                rank,
                position,
                selected,
                canonical_json(snapshot),
            )
        return snapshots

    @staticmethod
    def _finalize_candidate_snapshot(
        connection: sqlite3.Connection,
        experiment_id: str,
        signal_text: str,
        snapshots: Mapping[str, tuple[int, float, bool, str]],
        *,
        evidence_verified: bool = False,
    ) -> dict[str, Any]:
        session = connection.execute(
            """
            SELECT candidates_recorded FROM prospective_sessions
            WHERE experiment_id = ? AND signal_date = ?
            """,
            (experiment_id, signal_text),
        ).fetchone()
        if session is None:
            raise ValueError("session must be recorded before candidates")
        existing = connection.execute(
            """
            SELECT code, candidate_rank, target_position_pct, selected,
                   snapshot_json, snapshot_verified
            FROM prospective_candidates
            WHERE experiment_id = ? AND signal_date = ?
            """,
            (experiment_id, signal_text),
        ).fetchall()
        existing_snapshot = {
            row["code"]: (
                int(row["candidate_rank"]),
                float(row["target_position_pct"]),
                bool(row["selected"]),
                row["snapshot_json"],
                bool(row["snapshot_verified"]),
            )
            for row in existing
        }
        expected_snapshot = {
            code: (*values, bool(evidence_verified))
            for code, values in snapshots.items()
        }
        if bool(session["candidates_recorded"]):
            if existing_snapshot == expected_snapshot:
                return {"recorded": True, "inserted": 0, "idempotent": True}
            raise ValueError("candidate snapshot is immutable after finalization")
        if existing_snapshot and existing_snapshot != expected_snapshot:
            raise ValueError("candidate snapshot is immutable after partial persistence")
        inserted = 0
        if not existing_snapshot:
            for code, (
                rank,
                position,
                selected,
                snapshot_json,
            ) in snapshots.items():
                connection.execute(
                    """
                    INSERT INTO prospective_candidates (
                        experiment_id, signal_date, code, candidate_rank,
                        target_position_pct, snapshot_json, selected,
                        snapshot_verified
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        experiment_id,
                        signal_text,
                        code,
                        rank,
                        position,
                        snapshot_json,
                        int(selected),
                        int(evidence_verified),
                    ),
                )
                inserted += 1
        connection.execute(
            """
            UPDATE prospective_sessions SET candidates_recorded = 1
            WHERE experiment_id = ? AND signal_date = ?
            """,
            (experiment_id, signal_text),
        )
        return {"recorded": True, "inserted": inserted, "idempotent": False}

    def record_session(
        self,
        experiment_id: str,
        signal_date: str | date | datetime,
        *,
        data_complete: bool = True,
        data_trusted: bool | None = None,
        source_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        signal_day = _as_date(signal_date)
        snapshot = normalize_json(source_snapshot or {})
        if not isinstance(snapshot, dict):
            raise TypeError("source_snapshot must be an object")
        complete = bool(data_complete) and bool(snapshot.get("data_complete", True))
        trusted = bool(snapshot.get("market_data_trusted", False)) if data_trusted is None else bool(data_trusted)
        signal_text = signal_day.isoformat()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            experiment = self._require_experiment(connection, experiment_id)
            self._require_locked(experiment)
            if signal_day <= _as_date(experiment["locked_at"]):
                raise ValueError("signal_date must be after locked_at trading day")
            existing = connection.execute(
                """
                SELECT * FROM prospective_sessions
                WHERE experiment_id = ? AND signal_date = ?
                """,
                (experiment_id, signal_text),
            ).fetchone()
            if existing is not None:
                return {"recorded": True, "idempotent": True, "signal_date": signal_text}
            if not complete or not trusted:
                return {"recorded": False, "reason": "untrusted_or_incomplete_data"}
            connection.execute(
                """
                INSERT INTO prospective_sessions (
                    experiment_id, signal_date, data_complete, data_trusted,
                    source_snapshot_json, recorded_at, evidence_verified
                ) VALUES (?, ?, 1, 1, ?, ?, 0)
                """,
                (experiment_id, signal_text, canonical_json(snapshot), _timestamp(None)),
            )
            return {"recorded": True, "idempotent": False, "signal_date": signal_text}

    def record_frozen_snapshot(
        self,
        experiment_id: str,
        signal_date: str | date | datetime,
        *,
        candidates: Sequence[Mapping[str, Any]],
        data_complete: bool = True,
        data_trusted: bool | None = None,
        source_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._record_frozen_snapshot(
            experiment_id,
            signal_date,
            candidates=candidates,
            data_complete=data_complete,
            data_trusted=data_trusted,
            source_snapshot=source_snapshot,
            evidence_verified=False,
        )

    def _record_frozen_snapshot(
        self,
        experiment_id: str,
        signal_date: str | date | datetime,
        *,
        candidates: Sequence[Mapping[str, Any]],
        data_complete: bool = True,
        data_trusted: bool | None = None,
        source_snapshot: Mapping[str, Any] | None = None,
        evidence_verified: bool,
    ) -> dict[str, Any]:
        signal_day = _as_date(signal_date)
        signal_text = signal_day.isoformat()
        snapshot = normalize_json(source_snapshot or {})
        if not isinstance(snapshot, dict):
            raise TypeError("source_snapshot must be an object")
        complete = bool(data_complete) and bool(snapshot.get("data_complete", True))
        trusted = (
            bool(snapshot.get("market_data_trusted", False))
            if data_trusted is None
            else bool(data_trusted)
        )
        if (not complete or not trusted) and candidates:
            raise ValueError("untrusted or incomplete sessions cannot contain candidates")
        snapshots = self._candidate_snapshots(candidates)
        snapshot.setdefault("data_complete", complete)
        snapshot.setdefault("market_data_trusted", trusted)
        snapshot_json = canonical_json(snapshot)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            experiment = self._require_experiment(connection, experiment_id)
            self._require_locked(experiment)
            if signal_day <= _as_date(experiment["locked_at"]):
                raise ValueError("signal_date must be after locked_at trading day")
            existing = connection.execute(
                """
                SELECT * FROM prospective_sessions
                WHERE experiment_id = ? AND signal_date = ?
                """,
                (experiment_id, signal_text),
            ).fetchone()
            if existing is not None and bool(existing["candidates_recorded"]):
                candidate_result = self._finalize_candidate_snapshot(
                    connection,
                    experiment_id,
                    signal_text,
                    snapshots,
                    evidence_verified=evidence_verified,
                )
                if (
                    bool(existing["data_complete"]) == complete
                    and bool(existing["data_trusted"]) == trusted
                    and existing["source_snapshot_json"] == snapshot_json
                ):
                    return {
                        "recorded": True,
                        "idempotent": True,
                        "signal_date": signal_text,
                        "data_complete": complete,
                        "data_trusted": trusted,
                        "candidate_snapshot": candidate_result,
                    }
                raise ValueError("frozen session snapshot is immutable")
            if existing is not None:
                connection.execute(
                    """
                    DELETE FROM prospective_candidates
                    WHERE experiment_id = ? AND signal_date = ?
                    """,
                    (experiment_id, signal_text),
                )
                connection.execute(
                    """
                    DELETE FROM prospective_sessions
                    WHERE experiment_id = ? AND signal_date = ?
                    """,
                    (experiment_id, signal_text),
                )
            connection.execute(
                """
                INSERT INTO prospective_sessions (
                    experiment_id, signal_date, data_complete, data_trusted,
                    source_snapshot_json, recorded_at, candidates_recorded,
                    evidence_verified
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    experiment_id,
                    signal_text,
                    int(complete),
                    int(trusted),
                    snapshot_json,
                    _timestamp(None),
                    int(evidence_verified),
                ),
            )
            candidate_result = self._finalize_candidate_snapshot(
                connection,
                experiment_id,
                signal_text,
                snapshots,
                evidence_verified=evidence_verified,
            )
            return {
                "recorded": True,
                "idempotent": False,
                "signal_date": signal_text,
                "data_complete": complete,
                "data_trusted": trusted,
                "candidate_snapshot": candidate_result,
            }

    @staticmethod
    def _require_verified_protocol(experiment: Mapping[str, Any]) -> None:
        if not bool(experiment.get("protocol_verified")):
            raise ValueError("experiment protocol is not verified")
        manifest = experiment.get("manifest")
        if not isinstance(manifest, Mapping) or not _manifest_protocol_is_verified(
            manifest
        ):
            raise ValueError("experiment frozen protocol no longer matches runtime")

    @staticmethod
    def _bound_verified_candidates(
        experiment: Mapping[str, Any],
        signal_text: str,
        candidates: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        manifest = experiment["manifest"]
        config = manifest.get("config")
        config = config if isinstance(config, Mapping) else {}
        top_k = max(1, int(config.get("top_k") or 1))
        pool_size = max(
            top_k,
            int(config.get("candidate_pool_size") or top_k),
        )
        if len(candidates) > pool_size:
            raise ValueError("verified candidate count exceeds frozen candidate pool")
        expected_bindings = {
            "experiment_id": str(experiment["experiment_id"]),
            "model_instance_id": str(experiment["model_instance_id"]),
            "source_run_id": str(experiment["source_run_id"]),
        }
        protocol_binding = manifest.get("protocol_binding")
        bundle_sha256 = (
            str(protocol_binding.get("bundle_sha256") or "")
            if isinstance(protocol_binding, Mapping)
            else ""
        )
        rows: list[dict[str, Any]] = []
        ranks: list[int] = []
        for candidate in candidates:
            row = normalize_json(candidate)
            if not isinstance(row, dict):
                raise TypeError("verified candidate must be an object")
            for key, expected in expected_bindings.items():
                if str(row.get(key) or "") != expected:
                    raise ValueError(f"candidate {key} does not match experiment")
            candidate_date = str(
                row.get("signal_date") or row.get("trade_date") or ""
            )[:10]
            if candidate_date != signal_text:
                raise ValueError("candidate signal_date does not match session")
            if (
                row.get("research_status") != "locked_shadow"
                or row.get("deployment_eligible") is not False
                or row.get("research_only") is not True
                or float(row.get("position_pct") or 0.0) != 0.0
            ):
                raise ValueError("candidate shadow execution state is invalid")
            rank = int(row.get("candidate_rank") or 0)
            if rank <= 0 or rank > pool_size:
                raise ValueError("candidate rank is outside frozen candidate pool")
            ranks.append(rank)
            target = float(row.get("shadow_target_position_pct") or 0.0)
            if not math.isfinite(target) or target < 0.0 or target > 1.0:
                raise ValueError("candidate target position is invalid")
            if not bool(row.get("gate_pass", True)) and target != 0.0:
                raise ValueError("candidate blocked market gate must have zero target")
            row.update(
                {
                    "code": str(row.get("ts_code") or row.get("code") or ""),
                    "signal_date": signal_text,
                    "protocol_id": str(experiment["protocol_id"]),
                    "protocol_bundle_sha256": bundle_sha256,
                    "selected": rank <= top_k,
                    "target_position_pct": target,
                }
            )
            rows.append(row)
        if sorted(ranks) != list(range(1, len(rows) + 1)):
            raise ValueError("candidate ranks must be unique and contiguous")
        return rows

    def record_verified_shadow_session(
        self,
        experiment_id: str,
        signal_date: str | date | datetime,
        *,
        panel: Mapping[str, Any],
    ) -> dict[str, Any]:
        from analytics import relay_factor_mining

        signal_text = _as_date(signal_date).isoformat()
        experiment = self.get_experiment(experiment_id)
        self._require_verified_protocol(experiment)
        if signal_text not in self._persisted_session_dates(experiment):
            raise ValueError("verified signal_date is outside frozen session window")
        snapshot = relay_factor_mining.build_frozen_shadow_snapshot(
            dict(panel),
            experiment["manifest"],
        )
        if not isinstance(snapshot, Mapping):
            raise ValueError("verified shadow snapshot is invalid")
        snapshot_date = str(snapshot.get("signal_date") or "")[:10]
        if snapshot.get("ok") and snapshot.get("data_status") == "ok":
            if snapshot_date != signal_text:
                raise ValueError("verified shadow snapshot signal_date mismatch")
            candidates = self._bound_verified_candidates(
                experiment,
                signal_text,
                list(snapshot.get("candidates") or []),
            )
            source_snapshot = {
                "data_complete": True,
                "market_data_trusted": True,
                "data_quality": snapshot.get("data_quality") or {},
                "market_gate": snapshot.get("market_gate") or {},
                "candidate_count": len(candidates),
                "signal_date": signal_text,
                "experiment_id": experiment_id,
                "protocol_id": experiment["protocol_id"],
                "model_instance_id": experiment["model_instance_id"],
                "source_run_id": experiment["source_run_id"],
                "protocol_bundle_sha256": experiment["manifest"][
                    "protocol_binding"
                ]["bundle_sha256"],
                "protocol_violation_count": 0,
            }
            return self._record_frozen_snapshot(
                experiment_id,
                signal_text,
                data_complete=True,
                data_trusted=True,
                source_snapshot=source_snapshot,
                candidates=candidates,
                evidence_verified=True,
            )
        source_snapshot = {
            "data_complete": False,
            "market_data_trusted": False,
            "data_quality": snapshot.get("data_quality") or {},
            "market_gate": snapshot.get("market_gate") or {},
            "failure_reason": snapshot.get("reason")
            or "verified_shadow_snapshot_not_trusted",
            "snapshot_signal_date": snapshot_date or None,
            "data_status": snapshot.get("data_status"),
            "signal_date": signal_text,
            "experiment_id": experiment_id,
            "protocol_id": experiment["protocol_id"],
            "model_instance_id": experiment["model_instance_id"],
            "source_run_id": experiment["source_run_id"],
            "protocol_bundle_sha256": experiment["manifest"][
                "protocol_binding"
            ]["bundle_sha256"],
            "protocol_violation_count": 0,
        }
        recorded = self._record_frozen_snapshot(
            experiment_id,
            signal_text,
            data_complete=False,
            data_trusted=False,
            source_snapshot=source_snapshot,
            candidates=[],
            evidence_verified=True,
        )
        return {
            **recorded,
            "reason": source_snapshot["failure_reason"],
            "data_status": source_snapshot["data_status"],
        }

    def record_verified_failure_session(
        self,
        experiment_id: str,
        signal_date: str | date | datetime,
        *,
        reason: str,
        details: Any = None,
    ) -> dict[str, Any]:
        signal_text = _as_date(signal_date).isoformat()
        experiment = self.get_experiment(experiment_id)
        self._require_verified_protocol(experiment)
        if signal_text not in self._persisted_session_dates(experiment):
            raise ValueError("verified signal_date is outside frozen session window")
        return self._record_frozen_snapshot(
            experiment_id,
            signal_text,
            data_complete=False,
            data_trusted=False,
            source_snapshot={
                "data_complete": False,
                "market_data_trusted": False,
                "failure_reason": str(reason),
                "failure_details": normalize_json(details),
                "signal_date": signal_text,
                "experiment_id": experiment_id,
                "protocol_id": experiment["protocol_id"],
                "model_instance_id": experiment["model_instance_id"],
                "source_run_id": experiment["source_run_id"],
                "protocol_bundle_sha256": experiment["manifest"][
                    "protocol_binding"
                ]["bundle_sha256"],
                "protocol_violation_count": 0,
            },
            candidates=[],
            evidence_verified=True,
        )

    def settle_verified_candidates(
        self,
        experiment_id: str,
        *,
        panel: Mapping[str, Any],
    ) -> dict[str, int]:
        from analytics import relay_factor_mining

        experiment = self.get_experiment(experiment_id)
        self._require_verified_protocol(experiment)
        settled = 0
        pending = 0
        for stored in self.list_candidates(experiment_id):
            if (
                not stored.get("selected")
                or stored.get("settlement_status") == "settled"
            ):
                continue
            if not stored.get("snapshot_verified"):
                raise ValueError("candidate snapshot is not verified")
            candidate = dict(stored.get("snapshot") or {})
            candidate.setdefault("ts_code", stored.get("code"))
            candidate.setdefault("trade_date", stored.get("signal_date"))
            candidate.setdefault("signal_date", stored.get("signal_date"))
            result = relay_factor_mining.settle_frozen_shadow_candidate(
                dict(panel),
                candidate,
                experiment["manifest"],
            )
            if result.get("status") == "rejected":
                raise ValueError(
                    f"verified settlement rejected: {result.get('reason')}"
                )
            if result.get("status") != "settled":
                pending += 1
                continue
            block_reasons = result.get("block_reasons") or []
            self._settle_candidate(
                experiment_id,
                stored["signal_date"],
                stored["code"],
                entry_date=result.get("entry_date"),
                exit_date=result.get("exit_date"),
                entry_price=result.get("entry_price"),
                exit_price=result.get("exit_price"),
                exit_reason=result.get("exit_reason"),
                realized_return_pct=result.get("realized_return_pct"),
                blocked=bool(result.get("execution_blocked")),
                block_reason=",".join(str(item) for item in block_reasons),
                valuation_path=result.get("valuation_path") or [],
                evidence_verified=True,
            )
            settled += 1
        return {"settled": settled, "pending": pending}

    def record_candidates(
        self,
        experiment_id: str,
        signal_date: str | date | datetime,
        candidates: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        signal_text = _as_date(signal_date).isoformat()
        snapshots = self._candidate_snapshots(candidates)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_locked(self._require_experiment(connection, experiment_id))
            return self._finalize_candidate_snapshot(
                connection,
                experiment_id,
                signal_text,
                snapshots,
            )

    def list_candidates(
        self, experiment_id: str, signal_date: str | date | datetime | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM prospective_candidates WHERE experiment_id = ?"
        params: list[Any] = [experiment_id]
        if signal_date is not None:
            query += " AND signal_date = ?"
            params.append(_as_date(signal_date).isoformat())
        query += " ORDER BY signal_date, candidate_rank, code"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {
                "experiment_id": row["experiment_id"],
                "signal_date": row["signal_date"],
                "code": row["code"],
                "candidate_rank": row["candidate_rank"],
                "target_position_pct": row["target_position_pct"],
                "snapshot": _json_loads(row["snapshot_json"], {}),
                "snapshot_verified": bool(row["snapshot_verified"]),
                "selected": bool(row["selected"]),
                "settlement_status": row["settlement_status"],
                "entry_price": row["entry_price"],
                "exit_price": row["exit_price"],
                "entry_date": row["entry_date"],
                "exit_date": row["exit_date"],
                "exit_reason": row["exit_reason"],
                "realized_return_pct": row["realized_return_pct"],
                "blocked": bool(row["blocked"]),
                "block_reason": row["block_reason"],
                "valuation_path": _json_loads(row["valuation_path_json"], None),
                "settlement_verified": bool(row["settlement_verified"]),
                "settled_at": row["settled_at"],
            }
            for row in rows
        ]

    def settle_candidate(
        self,
        experiment_id: str,
        signal_date: str | date | datetime,
        code: str,
        *,
        entry_date: str | date | datetime | None = None,
        exit_date: str | date | datetime | None = None,
        entry_price: float | None = None,
        exit_price: float | None = None,
        exit_reason: str | None = None,
        realized_return_pct: float | None = None,
        blocked: bool = False,
        block_reason: str | None = None,
        valuation_path: Sequence[Mapping[str, Any]] | None = None,
        settled_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        return self._settle_candidate(
            experiment_id,
            signal_date,
            code,
            entry_date=entry_date,
            exit_date=exit_date,
            entry_price=entry_price,
            exit_price=exit_price,
            exit_reason=exit_reason,
            realized_return_pct=realized_return_pct,
            blocked=blocked,
            block_reason=block_reason,
            valuation_path=valuation_path,
            settled_at=settled_at,
            evidence_verified=False,
        )

    def _settle_candidate(
        self,
        experiment_id: str,
        signal_date: str | date | datetime,
        code: str,
        *,
        entry_date: str | date | datetime | None = None,
        exit_date: str | date | datetime | None = None,
        entry_price: float | None = None,
        exit_price: float | None = None,
        exit_reason: str | None = None,
        realized_return_pct: float | None = None,
        blocked: bool = False,
        block_reason: str | None = None,
        valuation_path: Sequence[Mapping[str, Any]] | None = None,
        settled_at: str | datetime | None = None,
        evidence_verified: bool,
    ) -> dict[str, Any]:
        signal_text = _as_date(signal_date).isoformat()
        entry_text = None if entry_date is None else _as_date(entry_date).isoformat()
        exit_text = None if exit_date is None else _as_date(exit_date).isoformat()
        if not blocked and (entry_text is None or exit_text is None):
            raise ValueError("non-blocked settlements require entry_date and exit_date")
        if block_reason is not None and not isinstance(block_reason, str):
            raise TypeError("block_reason must be a stable string")
        path_json = canonical_json(list(valuation_path or []))
        settlement = {
            "entry_date": entry_text,
            "exit_date": exit_text,
            "entry_price": None if entry_price is None else float(entry_price),
            "exit_price": None if exit_price is None else float(exit_price),
            "exit_reason": exit_reason,
            "realized_return_pct": None if realized_return_pct is None else float(realized_return_pct),
            "blocked": bool(blocked),
            "block_reason": block_reason,
            "valuation_path": list(valuation_path or []),
            "valuation_path_json": path_json,
            "settlement_verified": bool(evidence_verified),
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            experiment_row = self._require_experiment(connection, experiment_id)
            self._require_locked(experiment_row)
            _validate_settlement_evidence(
                signal_date=signal_text,
                manifest=_json_loads(experiment_row["manifest_json"], {}),
                settlement=settlement,
            )
            row = connection.execute(
                """
                SELECT * FROM prospective_candidates
                WHERE experiment_id = ? AND signal_date = ? AND code = ?
                """,
                (experiment_id, signal_text, str(code)),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown candidate: {code}")
            if row["settlement_status"] == "settled":
                prior = {
                    "entry_date": row["entry_date"],
                    "exit_date": row["exit_date"],
                    "entry_price": row["entry_price"],
                    "exit_price": row["exit_price"],
                    "exit_reason": row["exit_reason"],
                    "realized_return_pct": row["realized_return_pct"],
                    "blocked": bool(row["blocked"]),
                    "block_reason": row["block_reason"],
                    "valuation_path_json": row["valuation_path_json"],
                    "settlement_verified": bool(row["settlement_verified"]),
                }
                comparable = {
                    key: value
                    for key, value in settlement.items()
                    if key != "valuation_path"
                }
                if prior == comparable:
                    return {"settled": True, "idempotent": True}
                raise ValueError("candidate settlement is immutable once settled")
            connection.execute(
                """
                UPDATE prospective_candidates
                SET settlement_status = 'settled', entry_date = ?, exit_date = ?,
                    entry_price = ?, exit_price = ?, exit_reason = ?, realized_return_pct = ?, blocked = ?, block_reason = ?,
                    valuation_path_json = ?, settled_at = ?,
                    settlement_verified = ?
                WHERE experiment_id = ? AND signal_date = ? AND code = ?
                """,
                (
                    settlement["entry_date"],
                    settlement["exit_date"],
                    settlement["entry_price"],
                    settlement["exit_price"],
                    settlement["exit_reason"],
                    settlement["realized_return_pct"],
                    int(settlement["blocked"]),
                    settlement["block_reason"],
                    settlement["valuation_path_json"],
                    _timestamp(settled_at),
                    int(settlement["settlement_verified"]),
                    experiment_id,
                    signal_text,
                    str(code),
                ),
            )
            return {"settled": True, "idempotent": False}

    update_candidate_settlement = settle_candidate

    def list_sessions(self, experiment_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            self._require_experiment(connection, experiment_id)
            sessions = connection.execute(
                """
                SELECT * FROM prospective_sessions
                WHERE experiment_id = ? ORDER BY signal_date
                """,
                (experiment_id,),
            ).fetchall()
            output = []
            for row in sessions:
                counts = connection.execute(
                    """
                    SELECT COUNT(*) AS candidate_count,
                           SUM(CASE WHEN settlement_status = 'settled' THEN 1 ELSE 0 END) AS settled_count
                           ,SUM(CASE WHEN selected = 1 THEN 1 ELSE 0 END) AS selected_candidate_count
                           ,SUM(CASE WHEN selected = 1 AND settlement_status = 'settled' THEN 1 ELSE 0 END) AS selected_settled_count
                    FROM prospective_candidates
                    WHERE experiment_id = ? AND signal_date = ?
                    """,
                    (experiment_id, row["signal_date"]),
                ).fetchone()
                candidate_count = int(counts["candidate_count"])
                settled_count = int(counts["settled_count"] or 0)
                selected_candidate_count = int(counts["selected_candidate_count"] or 0)
                selected_settled_count = int(counts["selected_settled_count"] or 0)
                output.append(
                    {
                        "experiment_id": experiment_id,
                        "signal_date": row["signal_date"],
                        "data_complete": bool(row["data_complete"]),
                        "data_trusted": bool(row["data_trusted"]),
                        "source_snapshot": _json_loads(row["source_snapshot_json"], {}),
                        "evidence_verified": bool(row["evidence_verified"]),
                        "recorded_at": row["recorded_at"],
                        "candidates_recorded": bool(row["candidates_recorded"]),
                        "candidate_count": candidate_count,
                        "settled_candidate_count": settled_count,
                        "selected_candidate_count": selected_candidate_count,
                        "selected_settled_count": selected_settled_count,
                        "settlement_status": (
                            "settled"
                            if bool(row["candidates_recorded"])
                            and selected_candidate_count == selected_settled_count
                            else "pending"
                        ),
                    }
                )
            return output

    @staticmethod
    def _required_sessions(experiment: dict[str, Any]) -> int:
        rules = experiment["manifest"].get("acceptance_rules") or {}
        return max(1, int(rules.get("required_sessions", rules.get("prospective_sessions_required", 20))))

    @classmethod
    def _persisted_session_dates(cls, experiment: dict[str, Any]) -> list[str]:
        required = cls._required_sessions(experiment)
        dates = experiment.get("expected_session_dates")
        if not isinstance(dates, list):
            raise RuntimeError("persisted prospective session window is unavailable")
        normalized = [str(item) for item in dates]
        if len(normalized) != required or len(set(normalized)) != required:
            raise RuntimeError("persisted prospective session window is invalid")
        return normalized

    def expected_session_dates(self, experiment_id: str) -> list[str]:
        experiment = self.get_experiment(experiment_id)
        return self._persisted_session_dates(experiment)

    def _progress_from_connection(
        self,
        connection: sqlite3.Connection,
        experiment_row: sqlite3.Row,
    ) -> dict[str, Any]:
        experiment = self._experiment_output(experiment_row)
        experiment_id = experiment["experiment_id"]
        required = self._required_sessions(experiment)
        expected_dates = self._persisted_session_dates(experiment)
        session_rows = connection.execute(
            """
            SELECT s.signal_date,
                   s.candidates_recorded,
                   SUM(CASE WHEN c.selected = 1 THEN 1 ELSE 0 END)
                       AS selected_candidate_count,
                   SUM(
                       CASE
                           WHEN c.selected = 1
                                AND c.settlement_status = 'settled'
                           THEN 1
                           ELSE 0
                       END
                   ) AS selected_settled_count
            FROM prospective_sessions AS s
            LEFT JOIN prospective_candidates AS c
              ON c.experiment_id = s.experiment_id
             AND c.signal_date = s.signal_date
            WHERE s.experiment_id = ?
            GROUP BY s.signal_date, s.candidates_recorded
            ORDER BY s.signal_date
            """,
            (experiment_id,),
        ).fetchall()
        rows_by_date = {str(item["signal_date"]): item for item in session_rows}
        sessions = [
            rows_by_date[signal_date]
            for signal_date in expected_dates
            if signal_date in rows_by_date
        ]
        missing_dates = [
            signal_date
            for signal_date in expected_dates
            if signal_date not in rows_by_date
        ]
        settled = sum(
            bool(row["candidates_recorded"])
            and int(row["selected_candidate_count"] or 0)
            == int(row["selected_settled_count"] or 0)
            for row in sessions
        )
        return {
            "experiment_id": experiment_id,
            "status": experiment["status"],
            "required_sessions": required,
            "observed_sessions": len(sessions),
            "settled_sessions": settled,
            "ready_to_evaluate": len(sessions) == required and settled == required,
            "expected_session_dates": expected_dates,
            "missing_session_dates": missing_dates,
        }

    def get_progress(self, experiment_id: str) -> dict[str, Any]:
        experiment = self.get_experiment(experiment_id)
        required = self._required_sessions(experiment)
        expected_dates = self._persisted_session_dates(experiment)
        expected_set = set(expected_dates)
        scoped = [
            item
            for item in self.list_sessions(experiment_id)
            if item["signal_date"] in expected_set
        ]
        observed_dates = {item["signal_date"] for item in scoped}
        settled = sum(item["settlement_status"] == "settled" for item in scoped)
        return {
            "experiment_id": experiment_id,
            "status": experiment["status"],
            "required_sessions": required,
            "observed_sessions": len(scoped),
            "settled_sessions": settled,
            "ready_to_evaluate": len(scoped) == required and settled == required,
            "expected_session_dates": expected_dates,
            "missing_session_dates": [
                signal_date
                for signal_date in expected_dates
                if signal_date not in observed_dates
            ],
        }

    def evaluate_once(self, experiment_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_experiment(connection, experiment_id)
            if row["evaluation_json"] is not None:
                stored = _json_loads(row["evaluation_json"], {})
                return {**stored, "already_evaluated": True}
            self._require_locked(row)

            experiment = self._experiment_output(row)
            required = self._required_sessions(experiment)
            expected_dates = self._persisted_session_dates(experiment)
            session_rows = connection.execute(
                """
                SELECT * FROM prospective_sessions
                WHERE experiment_id = ? ORDER BY signal_date
                """,
                (experiment_id,),
            ).fetchall()
            rows_by_date = {
                str(session["signal_date"]): session
                for session in session_rows
            }
            missing_dates = [
                signal_date
                for signal_date in expected_dates
                if signal_date not in rows_by_date
            ]
            if missing_dates:
                reason = (
                    "fixed_window_session_gap"
                    if len(session_rows) >= required
                    else "insufficient_observed_sessions"
                )
                return {
                    "evaluated": False,
                    "status": _ACTIVE_STATUS,
                    "reason": reason,
                    "expected_session_dates": expected_dates,
                    "missing_session_dates": missing_dates,
                }
            sessions = [rows_by_date[signal_date] for signal_date in expected_dates]

            protocol_violation_count = 0
            evidence_verified = bool(row["protocol_verified"])
            attempts: list[dict[str, Any]] = []
            for session in sessions:
                evidence_verified = evidence_verified and bool(
                    session["evidence_verified"]
                )
                if not bool(session["candidates_recorded"]):
                    return {
                        "evaluated": False,
                        "status": _ACTIVE_STATUS,
                        "reason": "insufficient_settled_sessions",
                    }
                candidates = connection.execute(
                    """
                    SELECT * FROM prospective_candidates
                    WHERE experiment_id = ? AND signal_date = ?
                    ORDER BY candidate_rank, code
                    """,
                    (experiment_id, session["signal_date"]),
                ).fetchall()
                if any(
                    bool(candidate["selected"])
                    and candidate["settlement_status"] != "settled"
                    for candidate in candidates
                ):
                    return {
                        "evaluated": False,
                        "status": _ACTIVE_STATUS,
                        "reason": "insufficient_settled_sessions",
                    }
                snapshot = _json_loads(session["source_snapshot_json"], {})
                protocol_violation_count += int(
                    not bool(session["data_complete"])
                    or not bool(session["data_trusted"])
                )
                protocol_violation_count += int(snapshot.get("protocol_violation_count", 0) or 0)
                for candidate in candidates:
                    if not bool(candidate["selected"]):
                        continue
                    evidence_verified = evidence_verified and bool(
                        candidate["snapshot_verified"]
                    ) and bool(candidate["settlement_verified"])
                    attempt = {
                        "trade_date": session["signal_date"],
                        "ts_code": candidate["code"],
                        "entry_date": candidate["entry_date"],
                        "exit_date": candidate["exit_date"],
                        "live_adjusted_return_pct": float(
                            candidate["realized_return_pct"] or 0.0
                        ),
                        "execution_blocked": bool(candidate["blocked"]),
                        "valuation_path": _json_loads(
                            candidate["valuation_path_json"],
                            [],
                        ),
                    }
                    frozen_candidate = _json_loads(candidate["snapshot_json"], {})
                    if isinstance(frozen_candidate, dict) and (
                        "target_position_pct" in frozen_candidate
                        or "position_pct" in frozen_candidate
                    ):
                        attempt["target_position_pct"] = float(
                            candidate["target_position_pct"]
                        )
                    attempts.append(attempt)

            rules = experiment["manifest"].get("acceptance_rules") or {}
            config = experiment["manifest"].get("config") or {}
            trade_parameters = experiment["manifest"].get("trade_parameters") or {}
            top_k = max(1, int(config.get("top_k", trade_parameters.get("top_k", 1)) or 1))
            holding_sessions = config.get("holding_sessions")
            if holding_sessions is None:
                hold_days = config.get("hold_days", trade_parameters.get("hold_days", 2))
                holding_sessions = int(hold_days) + 1
            portfolio = simulate_portfolio(
                attempts,
                top_k=top_k,
                holding_sessions=max(1, int(holding_sessions)),
                session_dates=expected_dates,
            )
            confidence_interval = _bootstrap_total_return_ci(portfolio)
            metrics = {
                "total_return_pct": float(portfolio["total_return_pct"]),
                "max_drawdown_pct": float(portfolio["max_drawdown_pct"]),
                "executed_trade_count": int(portfolio["executed_trade_count"]),
                "protocol_violation_count": protocol_violation_count,
                "bootstrap_total_return_ci_pct": confidence_interval,
                "portfolio": portfolio,
            }
            failures = []
            if metrics["total_return_pct"] <= float(rules.get("minimum_total_return_pct", 0.0)):
                failures.append("minimum_total_return_pct_not_met")
            if metrics["max_drawdown_pct"] < float(rules.get("maximum_drawdown_pct", -15.0)):
                failures.append("maximum_drawdown_pct_exceeded")
            if metrics["executed_trade_count"] < int(rules.get("minimum_executed_trades", 1)):
                failures.append("minimum_executed_trades_not_met")
            minimum_lower_bound = rules.get("minimum_bootstrap_lower_return_pct")
            if minimum_lower_bound is not None:
                lower_bound = confidence_interval.get("lower")
                if lower_bound is None:
                    failures.append("bootstrap_confidence_unavailable")
                elif float(lower_bound) <= float(minimum_lower_bound):
                    failures.append("bootstrap_lower_bound_not_positive")
            if metrics["protocol_violation_count"] != 0:
                failures.append("protocol_violation_count_nonzero")
            if not evidence_verified:
                failures.append("unverified_prospective_evidence")
            status = "validated" if not failures else "failed"
            result = {
                "evaluated": True,
                "already_evaluated": False,
                "experiment_id": experiment_id,
                "status": status,
                "required_sessions": required,
                "metrics": metrics,
                "failure_reasons": failures,
            }
            timestamp = _timestamp(None)
            connection.execute(
                """
                UPDATE experiments
                SET status = ?, evaluated_at = ?, evaluation_json = ?,
                    evidence_verified = ?
                WHERE experiment_id = ?
                """,
                (
                    status,
                    timestamp,
                    canonical_json(result),
                    int(evidence_verified),
                    experiment_id,
                ),
            )
            return result


ProspectiveExperimentStore = RelayFactorExperimentStore


def create_locked_experiment(
    source_run_id: str,
    manifest: Mapping[str, Any],
    locked_at: str | datetime | None = None,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    return RelayFactorExperimentStore(db_path).create_locked_experiment(source_run_id, manifest, locked_at)
