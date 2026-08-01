from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from analytics import (
    agudata_provider,
    data_manager,
    relay_factor_mining,
    short_factor_data,
    short_factor_discovery,
    short_factor_st_status,
)
from analytics.factors_zoo import scorer


ROOT = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "research"
    / "short_factor_discovery"
)
DEFAULT_CONFIG = {
    "n_stocks": 800,
    "days": 320,
    "train_days": 120,
    "test_days": 20,
    "step_days": 20,
    "hold_days": 2,
    "stop_loss": -0.035,
    "take_profit": 0.07,
    "top_k": 10,
    "max_factors": 6,
    "max_interactions": 30,
    "fdr_alpha": 0.05,
    "max_abs_correlation": 0.85,
    "min_cross_section": 20,
}


def _config(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result = dict(DEFAULT_CONFIG)
    if isinstance(value, Mapping):
        result.update(
            {
                key: value[key]
                for key in DEFAULT_CONFIG
                if key in value
            }
        )
    integer_fields = (
        "n_stocks",
        "days",
        "train_days",
        "test_days",
        "step_days",
        "hold_days",
        "top_k",
        "max_factors",
        "max_interactions",
        "min_cross_section",
    )
    for field in integer_fields:
        raw = result[field]
        if isinstance(raw, bool) or int(raw) != raw or int(raw) < 1:
            raise ValueError(f"{field} must be a positive integer")
        result[field] = int(raw)
    for field in (
        "stop_loss",
        "take_profit",
        "fdr_alpha",
        "max_abs_correlation",
    ):
        result[field] = float(result[field])
    if result["step_days"] < result["test_days"]:
        raise ValueError("step_days must be at least test_days")
    if not -1.0 < result["stop_loss"] < 0.0:
        raise ValueError("stop_loss is out of range")
    if not 0.0 < result["take_profit"] <= 1.0:
        raise ValueError("take_profit is out of range")
    if not 0.0 < result["fdr_alpha"] < 1.0:
        raise ValueError("fdr_alpha is out of range")
    if not 0.0 <= result["max_abs_correlation"] < 1.0:
        raise ValueError("max_abs_correlation is out of range")
    return result


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def run_discovery_job(
    *,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    resolved = _config(config)
    current = now or datetime.now()
    candidate_stocks = relay_factor_mining.research_candidate_pool_size(
        resolved["n_stocks"]
    )
    panel, codes = scorer.build_panel_from_cache(
        n_stocks=candidate_stocks,
        days=resolved["days"],
        min_history=min(resolved["days"], 60),
        require_latest=True,
        point_in_time=True,
        require_security_master=True,
    )
    close = panel.get("close")
    if not isinstance(close, pd.DataFrame) or close.empty:
        raise ValueError("short factor panel is empty")
    latest_trade_date = pd.Timestamp(close.index.max()).strftime("%Y-%m-%d")
    expected_trade_date = data_manager.expected_completed_trade_date(
        current
    )
    if latest_trade_date != expected_trade_date:
        raise ValueError(
            "short factor cache is stale: "
            f"expected {expected_trade_date}, got {latest_trade_date}"
        )
    start_date = pd.Timestamp(close.index.min()).strftime("%Y%m%d")
    end_date = pd.Timestamp(close.index.max()).strftime("%Y%m%d")
    st_refresh = short_factor_st_status.refresh_daily_status_cache(
        codes,
        start_date,
        end_date,
    )
    panel, st_quality = (
        short_factor_st_status.enrich_panel_daily_status(panel)
    )
    if (
        float(st_quality.get("coverage") or 0.0)
        < short_factor_st_status.DAILY_STATUS_MIN_COVERAGE
    ):
        raise ValueError(
            "short factor ST status coverage too low: "
            f"{float(st_quality.get('coverage') or 0.0):.2%}"
        )
    auxiliary_root = ROOT / "auxiliary"
    try:
        refresh = (
            short_factor_data.refresh_agudata_auxiliary_history(
                codes,
                start_date,
                end_date,
                root=auxiliary_root,
            )
        )
    except agudata_provider.AgudataError as exc:
        if isinstance(
            exc,
            agudata_provider.AgudataRateLimitError,
        ):
            error_code = "agudata_rate_limited"
        elif isinstance(
            exc,
            agudata_provider.AgudataPermissionError,
        ):
            error_code = "agudata_permission_denied"
        elif isinstance(
            exc,
            agudata_provider.AgudataTransientError,
        ):
            error_code = "agudata_temporarily_unavailable"
        else:
            error_code = "agudata_request_failed"
        refresh = {
            "ok": False,
            "provider": "agudata",
            "error_code": error_code,
            "error": str(exc)[:180],
            "resume_from_cache": True,
        }
    try:
        turnover_refresh = (
            short_factor_data.refresh_baostock_turnover_history(
                codes,
                start_date,
                end_date,
                root=auxiliary_root,
            )
        )
    except Exception as exc:
        turnover_refresh = {
            "ok": False,
            "provider": "baostock",
            "error": str(exc)[:180],
            "resume_from_cache": True,
        }
    auxiliary = short_factor_data.load_auxiliary_history(
        root=auxiliary_root,
    )
    run_id = current.strftime("sfd-%Y%m%d-%H%M%S")
    out_dir = ROOT / "runs" / run_id
    report = short_factor_discovery.run_short_factor_research(
        panel,
        daily_basic=auxiliary["daily_basic"],
        auction=auxiliary["auction"],
        expected_codes=codes,
        out_dir=out_dir,
        run_id=run_id,
        hold_days=resolved["hold_days"],
        stop_loss=resolved["stop_loss"],
        take_profit=resolved["take_profit"],
        train_days=resolved["train_days"],
        test_days=resolved["test_days"],
        step_days=resolved["step_days"],
        top_k=resolved["top_k"],
        max_factors=resolved["max_factors"],
        max_interactions=resolved["max_interactions"],
        fdr_alpha=resolved["fdr_alpha"],
        max_abs_correlation=resolved["max_abs_correlation"],
        min_cross_section=resolved["min_cross_section"],
    )
    _write_json_atomic(ROOT / "latest.json", report)
    summary = report.get("summary") or {}
    return {
        "ok": True,
        "run_id": run_id,
        "status": report.get("status"),
        "research_only": True,
        "deployment_eligible": False,
        "latest_trade_date": latest_trade_date,
        "promotion_candidate_count": int(
            summary.get("promotion_candidate_count") or 0
        ),
        "oos_rank_ic": float(summary.get("oos_rank_ic") or 0.0),
        "net_return_pct": float(summary.get("net_return_pct") or 0.0),
        "st_status": {
            "refresh": st_refresh,
            **st_quality,
        },
        "turnover_refresh": turnover_refresh,
        "auxiliary_refresh": refresh,
        "report_path": str(out_dir / "discovery_report.json"),
    }


def get_latest_report() -> dict[str, Any] | None:
    path = ROOT / "latest.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema_version")
        != "short_factor_discovery_report_v1"
        or value.get("channel") != "short_factor_discovery"
    ):
        return None
    return value
