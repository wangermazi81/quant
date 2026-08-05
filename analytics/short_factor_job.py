from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from analytics import (
    agudata_provider,
    data_manager,
    factor_history_quarantine,
    relay_factor_mining,
    short_factor_data,
    short_factor_discovery,
    short_factor_st_status,
)
from analytics.factors_zoo import scorer
from core import factor_watchlist


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


def _ensure_factor_cache_freshness(
    *,
    now: datetime,
    days: int,
    min_required: int,
    allow_newer_security_master: bool = False,
) -> dict[str, Any]:
    initial = dict(
        data_manager.factor_cache_freshness(
            now=now,
            min_required=min_required,
            allow_newer_security_master=allow_newer_security_master,
        )
    )
    if initial.get("status") == "fresh":
        return {
            **initial,
            "refresh_attempted": False,
            "cache_refresh": None,
        }

    refresh = data_manager.refresh_factor_daily_cache(
        days=days,
        min_required=min_required,
        now=now,
        refresh_security_master=not bool(
            initial.get("security_master_fresh")
        ),
    )
    final = dict(
        data_manager.factor_cache_freshness(
            now=now,
            min_required=min_required,
            allow_newer_security_master=allow_newer_security_master,
        )
    )
    result = {
        **final,
        "refresh_attempted": True,
        "cache_refresh": refresh,
    }
    if final.get("status") != "fresh":
        raise ValueError(
            "short factor cache remains stale: "
            f"expected={final.get('expected_trade_date')}, "
            f"master={final.get('security_master_trade_date')}, "
            f"daily={final.get('daily_cache_trade_date')}, "
            f"latest_count={int(final.get('daily_latest_count') or 0)}/"
            f"{int(final.get('min_required') or min_required)}"
        )
    return result


def _normalize_stock_code(value: Any) -> str:
    text = str(value or "").strip().upper().split(".", 1)[0]
    if text.endswith(".0"):
        text = text[:-2]
    digits = "".join(character for character in text if character.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def _stock_ts_code(code: str) -> str:
    market = "SH" if code.startswith(("5", "6", "9")) else "SZ"
    return f"{code}.{market}"


def _next_trade_date(signal_date: str) -> str:
    from market import trading_calendar

    current = date.fromisoformat(str(signal_date))
    for offset in range(1, 16):
        candidate = current + timedelta(days=offset)
        if not trading_calendar.has_verified_calendar(candidate.year):
            raise ValueError(
                "verified trading calendar unavailable for "
                f"{candidate.year}"
            )
        if trading_calendar.is_trading_day(candidate):
            return candidate.isoformat()
    raise ValueError("next verified trading date unavailable")


def _accepted_close_window(
    report: Mapping[str, Any],
    promoted_factors: set[str],
) -> tuple[list[str], dict[str, float], dict[str, float]] | None:
    close_report = report.get("close_discovery")
    windows = (
        close_report.get("windows")
        if isinstance(close_report, Mapping)
        else None
    )
    if not isinstance(windows, list):
        return None
    for raw_window in reversed(windows):
        if not isinstance(raw_window, Mapping):
            continue
        if (
            raw_window.get("status") != "evaluated"
            or raw_window.get("calibration_passed") is not True
        ):
            continue
        selected = [
            str(name)
            for name in (raw_window.get("selected_factors") or [])
            if str(name) in promoted_factors
        ]
        if not selected:
            continue
        diagnostics = raw_window.get("factor_diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
        directions: dict[str, float] = {}
        for name in selected:
            details = diagnostics.get(name)
            details = details if isinstance(details, Mapping) else {}
            try:
                direction = float(details.get("direction"))
            except (TypeError, ValueError):
                return None
            if direction == 0.0:
                return None
            directions[name] = direction
        raw_weights = raw_window.get("score_weights")
        raw_weights = raw_weights if isinstance(raw_weights, Mapping) else {}
        weights = {}
        for name in selected:
            try:
                weights[name] = float(raw_weights.get(name, 1.0))
            except (TypeError, ValueError):
                weights[name] = 1.0
        return selected, directions, weights
    return None


def _panel_row(
    panel: Mapping[str, pd.DataFrame],
    field: str,
    signal_date: pd.Timestamp,
) -> pd.Series | None:
    frame = panel.get(field)
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    normalized = pd.DatetimeIndex(
        pd.to_datetime(frame.index, errors="coerce")
    ).normalize()
    matches = frame.loc[normalized == signal_date.normalize()]
    if matches.empty:
        return None
    row = matches.iloc[-1].copy()
    row.index = [_normalize_stock_code(value) for value in row.index]
    return row


def _build_paper_candidates(
    *,
    report: Mapping[str, Any],
    panel: Mapping[str, pd.DataFrame],
    daily_basic: pd.DataFrame,
    codes: list[str],
    config: Mapping[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    channels = report.get("channel_promotion_candidates")
    close_promotions = (
        channels.get("close")
        if isinstance(channels, Mapping)
        else None
    )
    if not isinstance(close_promotions, list) or not close_promotions:
        return []
    promoted_factors = {
        str(item.get("factor"))
        for item in close_promotions
        if isinstance(item, Mapping) and item.get("factor")
    }
    accepted = _accepted_close_window(report, promoted_factors)
    if accepted is None:
        return []
    selected, directions, weights = accepted

    close_base = short_factor_discovery.build_daily_candidate_factors(panel)
    if isinstance(daily_basic, pd.DataFrame) and not daily_basic.empty:
        close_base.update(
            short_factor_discovery.build_daily_basic_candidate_factors(
                daily_basic
            )
        )
    close_base.update(
        short_factor_discovery.build_structural_candidate_factors(close_base)
    )
    max_interactions = int(
        config.get(
            "max_interactions",
            (report.get("config") or {}).get("max_interactions", 0),
        )
        or 0
    )
    base_names, interactions = (
        short_factor_discovery._expression_candidate_plan(
            close_base,
            max_interactions=max_interactions,
        )
    )
    base_name_set = set(base_names)
    interaction_by_name = {
        name: (name, left, right)
        for name, left, right in interactions
    }
    if any(
        name not in base_name_set and name not in interaction_by_name
        for name in selected
    ):
        close_base.clear()
        return []
    required_base = {
        name for name in selected if name in base_name_set
    }
    selected_interactions = []
    for name in selected:
        interaction = interaction_by_name.get(name)
        if interaction is None:
            continue
        selected_interactions.append(interaction)
        required_base.update(interaction[1:])
    _candidate_names, feature_frame = (
        short_factor_discovery._build_expression_feature_frame(
            close_base,
            max_interactions=max_interactions,
            consume_base=True,
            candidate_plan=(
                sorted(required_base),
                selected_interactions,
            ),
        )
    )
    if feature_frame.empty or any(
        name not in feature_frame.columns for name in selected
    ):
        return []

    signal_text = str(report.get("data_through") or "").strip()
    if not signal_text:
        close = panel.get("close")
        if not isinstance(close, pd.DataFrame) or close.empty:
            return []
        signal_text = pd.Timestamp(close.index.max()).strftime("%Y-%m-%d")
    signal_date = pd.Timestamp(signal_text).normalize()
    feature_dates = pd.to_datetime(
        feature_frame["trade_date"],
        errors="coerce",
    ).dt.normalize()
    latest = feature_frame.loc[feature_dates == signal_date].copy()
    if latest.empty:
        return []
    latest = short_factor_discovery._composite_scores(
        latest,
        selected,
        directions,
        weights,
    )
    latest["_code"] = latest["ts_code"].map(_normalize_stock_code)
    latest = latest[latest["_code"].str.fullmatch(r"\d{6}", na=False)]
    allowed_codes = {
        _normalize_stock_code(code)
        for code in codes
        if _normalize_stock_code(code)
    }
    latest = latest[latest["_code"].isin(allowed_codes)]

    required_rows = {
        field: _panel_row(panel, field, signal_date)
        for field in (
            "close",
            "volume",
            "is_st",
            "st_status_known",
            "universe_eligible",
            "security_master_eligible",
        )
    }
    if any(row is None for row in required_rows.values()):
        return []
    close_row = pd.to_numeric(required_rows["close"], errors="coerce")
    volume_row = pd.to_numeric(required_rows["volume"], errors="coerce")
    is_st_row = pd.to_numeric(required_rows["is_st"], errors="coerce")
    st_known_row = pd.to_numeric(
        required_rows["st_status_known"],
        errors="coerce",
    )
    universe_row = pd.to_numeric(
        required_rows["universe_eligible"],
        errors="coerce",
    )
    master_row = pd.to_numeric(
        required_rows["security_master_eligible"],
        errors="coerce",
    )
    quarantined = factor_history_quarantine.active_codes()

    def eligible_code(code: str) -> bool:
        values = (
            close_row.get(code),
            volume_row.get(code),
            is_st_row.get(code),
            st_known_row.get(code),
            universe_row.get(code),
            master_row.get(code),
        )
        if code in quarantined or any(pd.isna(value) for value in values):
            return False
        close_value, volume_value, is_st, st_known, universe, master = values
        return bool(
            float(close_value) > 0.0
            and float(volume_value) > 0.0
            and float(is_st) <= 0.0
            and float(st_known) > 0.0
            and float(universe) > 0.0
            and float(master) > 0.0
        )

    latest = latest[latest["_code"].map(eligible_code)]
    latest["_composite_score"] = pd.to_numeric(
        latest["_composite_score"],
        errors="coerce",
    )
    latest = latest.dropna(subset=["_composite_score"])
    latest = latest.sort_values(
        ["_composite_score", "_code"],
        ascending=[False, True],
        kind="stable",
    ).head(max(1, int(config.get("top_k") or 1)))
    if latest.empty:
        return []

    planned_entry_date = _next_trade_date(signal_date.date().isoformat())
    run_id = str(report.get("run_id") or now.strftime("sfd-%Y%m%d-%H%M%S"))
    factor_details = {
        name: {
            "direction": float(directions[name]),
            "weight": float(weights.get(name, 1.0)),
        }
        for name in selected
    }
    candidates = []
    for rank, (_, row) in enumerate(latest.iterrows(), start=1):
        code = str(row["_code"])
        ts_code = _stock_ts_code(code)
        score = float(row["_composite_score"])
        candidates.append({
            "ts_code": ts_code,
            "code": code,
            "name": factor_watchlist.resolve_watchlist_name(ts_code),
            "state": "paper_candidate",
            "status": "paper_candidate",
            "allow_real_order": False,
            "execution_mode": "paper",
            "research_only": True,
            "deployment_eligible": False,
            "validation_accepted": True,
            "research_status": "paper_candidate",
            "position_pct": 0.0,
            "strategy_key": "short_factor_discovery_paper_v1",
            "strategy_name": "短因子挖掘模拟候选",
            "model_version": "short_factor_discovery_v1",
            "factor_model_version": "short_factor_discovery_v1",
            "experiment_id": run_id,
            "model_instance_id": f"{run_id}:close",
            "source_run_id": run_id,
            "signal_trade_date": signal_date.date().isoformat(),
            "trade_date": signal_date.date().isoformat(),
            "planned_entry_date": planned_entry_date,
            "entry_window": "09:25",
            "hold_days": int(config.get("hold_days") or 1),
            "stop_loss": float(config.get("stop_loss") or -0.035),
            "take_profit": float(config.get("take_profit") or 0.07),
            "slippage": 0.002,
            "limit_up_down_handling": "skip_unfillable",
            "suspension_filter": True,
            "st_filter": True,
            "st_status_known": True,
            "is_st": False,
            "candidate_rank": rank,
            "candidate_tier": "paper",
            "factor_score": score,
            "confidence": score,
            "accepted_factors": list(selected),
            "factor_explain": factor_details,
            "live_readiness_status": "avoid",
            "live_warning_keys": ["paper_only"],
            "live_warnings": ["仅限模拟盘，禁止实盘下单"],
        })
    return candidates


def run_discovery_job(
    *,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    resolved = _config(config)
    explicit_as_of = now is not None
    current = now or datetime.now()
    expected_trade_date = data_manager.expected_completed_trade_date(current)
    candidate_stocks = relay_factor_mining.research_candidate_pool_size(
        resolved["n_stocks"]
    )
    freshness = _ensure_factor_cache_freshness(
        now=current,
        days=resolved["days"],
        min_required=candidate_stocks,
        allow_newer_security_master=explicit_as_of,
    )
    panel, codes = scorer.build_panel_from_cache(
        n_stocks=candidate_stocks,
        days=resolved["days"],
        min_history=min(resolved["days"], 60),
        require_latest=True,
        point_in_time=True,
        require_security_master=True,
        as_of=expected_trade_date,
    )
    close = panel.get("close")
    if not isinstance(close, pd.DataFrame) or close.empty:
        del close
        del panel
        short_factor_discovery._release_research_memory()
        raise ValueError("short factor panel is empty")
    latest_trade_date = pd.Timestamp(close.index.max()).strftime("%Y-%m-%d")
    if latest_trade_date != expected_trade_date:
        del close
        del panel
        short_factor_discovery._release_research_memory()
        raise ValueError(
            "short factor cache is stale: "
            f"expected {expected_trade_date}, got {latest_trade_date}"
        )
    start_date = pd.Timestamp(close.index.min()).strftime("%Y%m%d")
    end_date = pd.Timestamp(close.index.max()).strftime("%Y%m%d")
    del close
    try:
        st_refresh = short_factor_st_status.refresh_daily_status_cache(
            codes,
            start_date,
            end_date,
        )
        panel, st_quality = (
            short_factor_st_status.enrich_panel_daily_status(panel)
        )
    except Exception:
        del panel
        short_factor_discovery._release_research_memory()
        raise
    if (
        float(st_quality.get("coverage") or 0.0)
        < short_factor_st_status.DAILY_STATUS_MIN_COVERAGE
    ):
        del panel
        short_factor_discovery._release_research_memory()
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
    except Exception:
        del panel
        short_factor_discovery._release_research_memory()
        raise
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
    try:
        auxiliary = short_factor_data.load_auxiliary_history(
            root=auxiliary_root,
        )
    except Exception:
        del panel
        short_factor_discovery._release_research_memory()
        raise
    run_id = current.strftime("sfd-%Y%m%d-%H%M%S")
    out_dir = ROOT / "runs" / run_id
    try:
        daily_basic = auxiliary.pop("daily_basic", pd.DataFrame())
        auction = auxiliary.pop("auction", pd.DataFrame())
        report = short_factor_discovery.run_short_factor_research(
            panel,
            daily_basic=daily_basic,
            auction=auction,
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
        paper_candidates = _build_paper_candidates(
            report=report,
            panel=panel,
            daily_basic=daily_basic,
            codes=codes,
            config=resolved,
            now=current,
        )
        if paper_candidates:
            watchlist_import = (
                factor_watchlist.import_factor_candidates_to_watchlist(
                    paper_candidates,
                    run_id=run_id,
                    strategy_name="短因子挖掘模拟候选",
                )
            )
        else:
            watchlist_import = {
                "ok": True,
                "imported": 0,
                "total": None,
            }
        report["paper_candidates"] = paper_candidates
        report["paper_candidate_count"] = len(paper_candidates)
        report["paper_watchlist_import"] = {
            "ok": bool(watchlist_import.get("ok")),
            "imported": int(watchlist_import.get("imported") or 0),
        }
        _write_json_atomic(out_dir / "discovery_report.json", report)
    finally:
        auxiliary.clear()
        del panel
        short_factor_discovery._release_research_memory()
    _write_json_atomic(ROOT / "latest.json", report)
    summary = report.get("summary") or {}
    return {
        "ok": True,
        "run_id": run_id,
        "status": report.get("status"),
        "research_only": True,
        "deployment_eligible": False,
        "latest_trade_date": latest_trade_date,
        "freshness_status": freshness.get("status"),
        "refresh_attempted": bool(freshness.get("refresh_attempted")),
        "expected_trade_date": freshness.get("expected_trade_date"),
        "security_master_trade_date": freshness.get(
            "security_master_trade_date"
        ),
        "daily_cache_trade_date": freshness.get("daily_cache_trade_date"),
        "daily_latest_count": int(freshness.get("daily_latest_count") or 0),
        "cache_refresh": freshness.get("cache_refresh"),
        "paper_candidate_count": len(paper_candidates),
        "paper_watchlist_import": report.get("paper_watchlist_import"),
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
