# -*- coding: utf-8 -*-
"""Relay factor mining pipeline for AI trading research.

The module is research-only. Feature rows are computed from current and past
bars, while labels are generated separately from T+1/T+n prices. Candidate
output is suitable for AI-trading review and paper-trading shadow runs, not for
bypassing live risk controls.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from analytics.factors_zoo.scorer import MAX_ABS_DAILY_RETURN, build_panel_from_cache
from analytics.relay_factor_model import (
    CANDIDATE_FACTORS,
    MIN_REAL_AMOUNT_COVERAGE,
    MODEL_SPEC_CALIBRATED_THROUGH,
    MODEL_SPEC_PROTOCOL_VERSION,
    MODEL_VERSION,
    FIXED_MODEL_PARAMETERS,
    factor_model_digest,
    factor_model_payload,
    factor_diagnostics,
    fit_factor_model,
    score_factor_model,
    select_factor_model,
    selection_turnover,
)
from analytics.market_signal_quality import apply_market_signals, build_market_signal_frame
from analytics.relay_portfolio import (
    MAX_DEFERRED_EXIT_SESSIONS,
    build_trade_path,
    limit_up_threshold,
    simulate_portfolio,
)

_ROOT = Path(__file__).resolve().parent.parent
_OUT_BASE = _ROOT / "data" / "research" / "relay_factor"

_REQUIRED = ("close", "high", "volume")
STRATEGY_KEY = "relay_factor_mining_v2"
RESEARCH_UNIVERSE_RESERVE_PERCENT = 10
_LABEL_FRAME_COLUMNS = (
    "trade_date",
    "ts_code",
    "entry_date",
    "exit_date",
    "hold_days",
    "holding_sessions",
    "forward_return_pct",
    "plan_return_pct",
    "live_adjusted_return_pct",
    "model_target_pct",
    "entry_price",
    "exit_price",
    "exit_reason",
    "valuation_path",
    "entry_gap_pct",
    "is_st",
    "st_status_known",
    "signal_amount_ratio_20",
    "entry_amount_ratio_20",
    "amount_data_real",
    "execution_blocked",
    "execution_flags",
    "label_status",
    "research_evaluable",
    "label_source",
    "path_policy",
)
_RESEARCH_FEATURE_COLUMNS = (
    "trade_date",
    "ts_code",
    "relay_score",
    "amount_data_real",
    "market_data_trusted",
    "market_emotion_score",
    *CANDIDATE_FACTORS,
)
_RESEARCH_LABEL_COLUMNS = (
    "trade_date",
    "ts_code",
    "entry_date",
    "exit_date",
    "hold_days",
    "holding_sessions",
    "forward_return_pct",
    "live_adjusted_return_pct",
    "model_target_pct",
    "exit_reason",
    "execution_blocked",
    "execution_flags",
    "valuation_path",
    "label_status",
    "research_evaluable",
    "path_policy",
)

FROZEN_PROTOCOL_BINDING_VERSION = "relay_frozen_protocol_binding_v1"
PREREGISTERED_OOS_EVIDENCE_VERSION = (
    "relay_preregistered_oos_evidence_v3"
)
LOCKING_PROTOCOL_VERSION = "relay_locking_protocol_v2"
EVIDENCE_CALENDAR_SNAPSHOT_VERSION = (
    "tushare_trade_calendar_snapshot_v2"
)
DATA_QUALITY_ASSESSMENT_VERSION = (
    "relay_data_quality_assessment_v1"
)
FROZEN_BACKTEST_POLICY: dict[str, Any] = {
    "purge": "exit_date_before_test_start",
    "validation": "chronological_tail_20pct",
    "entry": "next_session_open",
    "same_bar_stop_target": "stop_first_conservative",
    "blocked_slots": "remain_cash_without_redistribution",
    "capital": "cash_aware_overlapping_positions",
    "maximum_deferred_exit_sessions": MAX_DEFERRED_EXIT_SESSIONS,
    "compounding": True,
}
FROZEN_DATA_QUALITY_POLICY: dict[str, Any] = {
    "discontinuous_price_history": (
        "quarantine_from_first_detectable_break"
    ),
    "max_abs_daily_return": MAX_ABS_DAILY_RETURN,
    "amount_without_real_cache": (
        "proxy_for_legacy_rules_excluded_from_v2_model"
    ),
    "minimum_real_amount_coverage": MIN_REAL_AMOUNT_COVERAGE,
    "st_status_source": "point_in_time_cache_is_st_then_name",
    "st_status_unknown": (
        "display_only_zero_position_and_reject_execution"
    ),
    "universe_eligibility_missing": (
        "research_data_status_degraded"
    ),
}
FROZEN_PROTOCOL_VERSIONS: dict[str, str] = {
    "candidate_selection": "relay_candidate_selection_v2",
    "readiness": "relay_prospective_readiness_v3",
    "position_sizing": "relay_shadow_position_sizing_v1",
    "trade_path_settlement": "relay_real_bar_settlement_v3",
}
FROZEN_PROTOCOL_SOURCE_PATHS: tuple[str, ...] = (
    "analytics/data_manager.py",
    "analytics/factors_zoo/scorer.py",
    "analytics/market_signal_quality.py",
    "analytics/preregistrations/relay_fixed_ridge_preregistered_v3.json",
    "analytics/relay_factor_experiment.py",
    "analytics/relay_factor_mining.py",
    "analytics/relay_factor_model.py",
    "analytics/relay_portfolio.py",
    "analytics/st_status_history.py",
    "analytics/tushare_provider.py",
    "market/calendar_commitments/tushare_sse_2026_v1.json",
    "market/trading_calendar.py",
)

DEFAULT_MINING_RULES: dict[str, Any] = {
    "weights": {
        "ret_5": 0.18,
        "ret_10": 0.12,
        "ret_20": 0.12,
        "volume_ratio_20": 0.16,
        "amount_ratio_20": 0.10,
        "near_high_20": 0.16,
        "limit_count_20": 0.10,
        "volatility_10": -0.08,
        "overheat_ret_3": -0.08,
    },
    "risk_thresholds": {
        "overextended_3d": 0.24,
        "high_volatility": 0.08,
        "weak_volume": 0.75,
        "deep_drawdown": -0.18,
    },
    "tier_thresholds": {
        "strong_percentile": 0.97,
        "watch_percentile": 0.90,
        "strong_requires_no_risk": True,
    },
    "filters": {
        "min_relay_score": None,
        "min_volume_ratio_20": None,
        "min_near_high_20": None,
        "exclude_high_risk": False,
    },
    # 市场状态门（2026-07-27 引入）：接力策略不该天天进场。
    # 基于 auto-20260724 一轮 90 个交易日的归因：
    #   - 情绪升温日买入（尾盘追高）raw -21.2%，降温日买入 raw +23.0%；
    #   - 情绪带呈倒U：[20,70) 内最好，过热(>=70)接盘、过冷(<20)无承接均亏；
    #   - 门后 live 口径 -19.8% → +26.1%，回撤 -39.5% → -15.9%。
    # ⚠️ 参数是在同段历史上校准的（in-sample），报告会同时输出 门前/门后 双轨指标，
    #    未来窗口若门后跑输门前，前端能直接看见——不许静默失效。
    "market_gate": {
        "enabled": True,
        "emo_min": 20.0,          # 当日市场情绪下限（含）
        "emo_max": 70.0,          # 上限（不含），>=70 视为过热不进场
        "require_cooling": True,  # 要求当日情绪 <= 前一交易日（降温/持平才进）
    },
}

DEFAULT_MARKET_GATE_PROVENANCE = {
    "calibrated_through": "2026-07-24",
    "protocol_version": "market_emotion_gate_v1",
    "calibration_source": "auto-20260724_90_session_research",
}

_RULE_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "weights": {
        "ret_5": "5 day momentum rank weight",
        "ret_10": "10 day momentum rank weight",
        "ret_20": "20 day momentum rank weight",
        "volume_ratio_20": "volume expansion rank weight versus 20 day average",
        "amount_ratio_20": "turnover amount expansion rank weight versus 20 day average",
        "near_high_20": "distance to 20 day high rank weight",
        "limit_count_20": "recent limit-up count rank weight",
        "volatility_10": "10 day volatility penalty weight",
        "overheat_ret_3": "3 day overheated momentum penalty weight",
    },
    "risk_thresholds": {
        "overextended_3d": "flag when 3 day return is above this value",
        "high_volatility": "flag when 10 day volatility is above this value",
        "weak_volume": "flag when volume ratio is below this value",
        "deep_drawdown": "flag when 20 day drawdown is below this value",
    },
    "tier_thresholds": {
        "strong_percentile": "minimum score percentile for strong tier",
        "watch_percentile": "minimum score percentile for watch tier",
        "strong_requires_no_risk": "strong tier requires an empty risk flag list",
    },
    "filters": {
        "min_relay_score": "drop candidates below this relay score when set",
        "min_volume_ratio_20": "drop candidates below this volume ratio when set",
        "min_near_high_20": "drop candidates below this 20 day high proximity when set",
        "exclude_high_risk": "drop candidates that have any risk flag",
    },
    "market_gate": {
        "enabled": "skip entries on days that fail the market regime gate",
        "emo_min": "minimum market emotion score (inclusive) to allow entries",
        "emo_max": "maximum market emotion score (exclusive); above = overheated",
        "require_cooling": "only enter when today's emotion <= previous trading day",
    },
}

_SIGNAL_LABELS: dict[str, str] = {
    "ret_5": "5日动量",
    "ret_10": "10日动量",
    "ret_20": "20日动量",
    "volume_ratio_20": "20日量比",
    "amount_ratio_20": "20日额比",
    "near_high_20": "接近20日高点",
    "limit_count_20": "20日涨停次数",
    "volatility_10": "10日波动",
    "overheat_ret_3": "3日过热",
    "market_emotion_score": "市场情绪分",
}

_POSITIVE_FACTOR_TEXT: dict[str, tuple[str, str]] = {
    "positive_5d_momentum": ("短线动量为正", "近5日收益为正，说明短线资金仍在推动。"),
    "volume_confirmation": ("量能确认", "成交量相对20日均量放大，接力承接更容易被验证。"),
    "near_20d_high": ("强势区间", "收盘价接近20日高点，趋势位置仍处于强势区。"),
    "recent_limit_strength": ("涨停基因", "近20日出现过涨停，说明标的具备短线活跃度。"),
    "supportive_market_emotion": ("市场情绪支持", "市场情绪分较高，接力环境相对友好。"),
}

_RISK_FACTOR_TEXT: dict[str, str] = {
    "overextended_3d": "近3日涨幅过热，次日追高容易承接不足。",
    "high_volatility": "近10日波动偏高，止损触发概率更高。",
    "weak_volume": "量能低于阈值，接力确认不足。",
    "deep_drawdown": "距离20日高点回撤偏深，趋势强度不足。",
}

LIVE_EXECUTION_RULES: dict[str, float] = {
    "round_trip_cost_pct": 0.16,
    "slippage_pct": 0.20,
    "high_open_block_pct": 7.5,
    "weak_open_block_pct": -4.0,
    "near_limit_gap_pct": 9.2,
    "low_amount_ratio": 0.55,
}

EXECUTION_POLICY_VERSIONS: dict[str, str] = {
    "path_policy": "t_plus_1_stop_first_on_same_bar_v1",
    "limit_threshold_policy": "board_and_point_in_time_st_v1",
}

MODEL_SPEC_REGISTERED_AT = "2026-07-31T03:30:00+08:00"
MODEL_SPEC_OOS_NOT_BEFORE = "2026-07-31"

RESEARCH_VALIDATION_RULES: dict[str, Any] = {
    "protocol_version": "relay_research_acceptance_preregistered_v1",
    "calibrated_through": MODEL_SPEC_CALIBRATED_THROUGH,
    "minimum_oos_windows": 12,
    "minimum_positive_window_rate": 0.50,
    "minimum_validation_acceptance_rate": 0.50,
    "minimum_executed_trades": 100,
    "maximum_drawdown_pct": -15.0,
    "minimum_bootstrap_lower_return_pct": 0.0,
    "prospective_sessions_required": 60,
}

PROSPECTIVE_ACCEPTANCE_RULES: dict[str, Any] = {
    "protocol_version": "prospective_60_session_bootstrap_v2",
    "required_sessions": 60,
    "minimum_total_return_pct": 2.0,
    "maximum_drawdown_pct": -10.0,
    "minimum_executed_trades": 60,
    "minimum_bootstrap_lower_return_pct": 0.0,
    "maximum_protocol_violations": 0,
    "preregistered_at": MODEL_SPEC_REGISTERED_AT,
    "calibrated_through": MODEL_SPEC_CALIBRATED_THROUGH,
}

MODEL_PREREGISTRATION_PATH = (
    _ROOT
    / "analytics"
    / "preregistrations"
    / "relay_fixed_ridge_preregistered_v3.json"
)
MODEL_SPEC_EXECUTABLE_SHA256 = (
    "10577621fb63d6db37e3b4274a72ee5e4af17a33e78ff18a99f8e7c83e285730"
)
MODEL_SPEC_CALIBRATION_SHA256 = (
    "f2709fb43fe9f67e5c091107a6632873db6990f2b9498d4c5cbd11828cea8f9b"
)
MODEL_SPEC_REGISTRATION_SHA256 = (
    "b849d8c7dc1cad7b25adfedf2ab48db1c3ccb9536a225149a01a6f34f2c3730c"
)


def _research_evidence_key() -> bytes | None:
    try:
        import config  # noqa: F401
    except Exception:
        pass
    for env_key in ("RESEARCH_EVIDENCE_KEY", "SECRET_KEY"):
        value = str(os.environ.get(env_key) or "").strip()
        if value:
            return value.encode("utf-8")
    for path in (
        _ROOT / "data" / ".research_evidence_key",
        _ROOT / "data" / ".secret_key",
    ):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if value:
            return value.encode("utf-8")
    return None


def _preregistered_oos_evidence_material(
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        key: value
        for key, value in evidence.items()
        if key not in {"evidence_sha256", "evidence_hmac_sha256"}
    }


def _canonical_evidence_bytes(evidence: Mapping[str, Any]) -> bytes:
    return json.dumps(
        _preregistered_oos_evidence_material(evidence),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=lambda value: value.item(),
    ).encode("utf-8")


def _seal_preregistered_oos_evidence(
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    sealed = json.loads(
        json.dumps(
            dict(evidence),
            ensure_ascii=False,
            allow_nan=False,
            default=lambda value: value.item(),
        )
    )
    sealed["evidence_version"] = PREREGISTERED_OOS_EVIDENCE_VERSION
    material = _canonical_evidence_bytes(sealed)
    sealed["evidence_sha256"] = hashlib.sha256(material).hexdigest()
    key = _research_evidence_key()
    sealed["evidence_hmac_sha256"] = (
        hmac.new(key, material, hashlib.sha256).hexdigest()
        if key
        else None
    )
    return sealed


def _verify_preregistered_oos_evidence_seal(
    evidence: Mapping[str, Any],
) -> bool:
    if (
        not isinstance(evidence, Mapping)
        or evidence.get("evidence_version")
        != PREREGISTERED_OOS_EVIDENCE_VERSION
    ):
        return False
    expected_sha256 = str(evidence.get("evidence_sha256") or "")
    expected_hmac = str(evidence.get("evidence_hmac_sha256") or "")
    key = _research_evidence_key()
    if len(expected_sha256) != 64 or len(expected_hmac) != 64 or not key:
        return False
    try:
        material = _canonical_evidence_bytes(evidence)
    except (TypeError, ValueError, AttributeError):
        return False
    actual_sha256 = hashlib.sha256(material).hexdigest()
    actual_hmac = hmac.new(key, material, hashlib.sha256).hexdigest()
    return bool(
        hmac.compare_digest(expected_sha256, actual_sha256)
        and hmac.compare_digest(expected_hmac, actual_hmac)
    )


def load_model_preregistration(
    path: str | Path | None = None,
) -> dict[str, Any]:
    record_path = Path(path) if path is not None else MODEL_PREREGISTRATION_PATH
    try:
        raw = record_path.read_text(encoding="utf-8")
        record = json.loads(raw)
    except FileNotFoundError:
        return {
            "ok": False,
            "reason": "preregistration_record_unavailable",
            "record": {},
            "sha256": None,
        }
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "ok": False,
            "reason": "preregistration_record_invalid",
            "record": {},
            "sha256": None,
        }
    if not isinstance(record, dict):
        return {
            "ok": False,
            "reason": "preregistration_record_invalid",
            "record": {},
            "sha256": None,
        }
    expected = {
        "schema_version": 2,
        "strategy_key": STRATEGY_KEY,
        "protocol_version": MODEL_SPEC_PROTOCOL_VERSION,
        "registered_at": MODEL_SPEC_REGISTERED_AT,
        "calibrated_through": MODEL_SPEC_CALIBRATED_THROUGH,
        "oos_not_before": MODEL_SPEC_OOS_NOT_BEFORE,
        "parameters": dict(FIXED_MODEL_PARAMETERS),
        "candidate_factors": list(CANDIDATE_FACTORS),
    }
    if any(record.get(key) != value for key, value in expected.items()):
        return {
            "ok": False,
            "reason": "preregistration_record_mismatch",
            "record": record,
            "sha256": None,
        }
    executable_model = record.get("executable_model")
    calibration_evidence = record.get("calibration_evidence")
    try:
        executable_sha256 = factor_model_digest(executable_model)
    except (TypeError, ValueError):
        executable_sha256 = None
    calibration_valid = False
    if isinstance(calibration_evidence, Mapping):
        row_count = calibration_evidence.get("row_count")
        trade_date_start = calibration_evidence.get("trade_date_start")
        trade_date_end = calibration_evidence.get("trade_date_end")
        label_max_exit = calibration_evidence.get("label_max_exit")
        try:
            parsed_dates = [
                datetime.strptime(value, "%Y-%m-%d").date()
                for value in (
                    trade_date_start,
                    trade_date_end,
                    label_max_exit,
                )
                if isinstance(value, str)
            ]
        except ValueError:
            parsed_dates = []
        calibration_valid = bool(
            isinstance(row_count, int)
            and not isinstance(row_count, bool)
            and row_count > 0
            and calibration_evidence.get("algorithm")
            == "canonical_csv_sha256_v1"
            and calibration_evidence.get("columns")
            == list(_CALIBRATION_EVIDENCE_COLUMNS)
            and len(parsed_dates) == 3
            and parsed_dates[0] <= parsed_dates[1] <= parsed_dates[2]
            and str(trade_date_end) <= MODEL_SPEC_CALIBRATED_THROUGH
            and str(label_max_exit) <= MODEL_SPEC_CALIBRATED_THROUGH
        )
    if (
        not isinstance(executable_model, Mapping)
        or not isinstance(calibration_evidence, Mapping)
        or executable_sha256 != record.get("executable_model_sha256")
        or executable_sha256 != MODEL_SPEC_EXECUTABLE_SHA256
        or calibration_evidence.get("sha256")
        != MODEL_SPEC_CALIBRATION_SHA256
        or not calibration_valid
    ):
        return {
            "ok": False,
            "reason": "preregistration_record_mismatch",
            "record": record,
            "sha256": None,
        }
    try:
        registered_at = pd.Timestamp(record.get("registered_at"))
        calibrated_through = pd.Timestamp(record["calibrated_through"])
        oos_not_before = pd.Timestamp(record.get("oos_not_before"))
    except (TypeError, ValueError):
        return {
            "ok": False,
            "reason": "preregistration_record_invalid",
            "record": record,
            "sha256": None,
        }
    if (
        pd.isna(registered_at)
        or registered_at.tzinfo is None
        or pd.isna(calibrated_through)
        or pd.isna(oos_not_before)
        or registered_at.date() < calibrated_through.date()
        or oos_not_before <= calibrated_through
    ):
        return {
            "ok": False,
            "reason": "preregistration_record_invalid",
            "record": record,
            "sha256": None,
        }
    canonical = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    canonical_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if canonical_sha256 != MODEL_SPEC_REGISTRATION_SHA256:
        return {
            "ok": False,
            "reason": "preregistration_record_mismatch",
            "record": record,
            "sha256": None,
        }
    return {
        "ok": True,
        "reason": "ok",
        "record": record,
        "sha256": canonical_sha256,
        "model_sha256": executable_sha256,
        "path": str(record_path),
    }


def _detect_release_revision() -> str | None:
    for env_key in (
        "JWALPHA_RELEASE_SHA",
        "GIT_COMMIT",
        "SOURCE_VERSION",
        "RENDER_GIT_COMMIT",
    ):
        value = str(os.environ.get(env_key) or "").strip()
        if value:
            return value
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else None


def build_current_frozen_protocol_binding() -> dict[str, Any]:
    source_sha256 = {
        relative_path: hashlib.sha256(
            (_ROOT / relative_path).read_bytes()
        ).hexdigest()
        for relative_path in FROZEN_PROTOCOL_SOURCE_PATHS
    }
    binding_material = {
        "binding_version": FROZEN_PROTOCOL_BINDING_VERSION,
        "protocol_versions": dict(FROZEN_PROTOCOL_VERSIONS),
        "source_sha256": source_sha256,
    }
    bundle_sha256 = hashlib.sha256(
        json.dumps(
            binding_material,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    binding = {
        **binding_material,
        "bundle_sha256": bundle_sha256,
    }
    release_revision = _detect_release_revision()
    if release_revision:
        binding["release_revision"] = release_revision
    return binding


def validate_frozen_protocol_binding(
    manifest: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    binding = manifest.get("protocol_binding") if isinstance(manifest, Mapping) else None
    if not isinstance(binding, Mapping):
        return False, "frozen_protocol_binding_missing"
    try:
        current = build_current_frozen_protocol_binding()
    except OSError:
        return False, "frozen_protocol_fingerprint_unavailable"
    for key in (
        "binding_version",
        "protocol_versions",
        "source_sha256",
        "bundle_sha256",
    ):
        if binding.get(key) != current.get(key):
            return False, "frozen_protocol_fingerprint_mismatch"
    return True, "ok"


_LIVE_WARNING_TEXT: dict[str, str] = {
    "real_amount_unavailable": "该股票当日缺少真实成交额，代理值不得参与因子执行决策。",
    "known_st": "已确认属于ST股票，仅保留研究记录，不允许分配交易仓位。",
    "st_status_unknown": "ST状态未知，竞价和开盘按5%涨跌停阈值保守判断。",
    "entry_near_limit_up": "次日开盘接近涨停，实盘可能排队买不到。",
    "entry_high_open": "次日高开过多，训练收益容易高估追入后的盈亏。",
    "entry_weak_open": "次日弱开，接力资金承接不足。",
    "entry_low_liquidity": "次日成交额相对均值偏低，滑点和冲击成本更高。",
    "proxy_overextended": "短线涨幅偏热，早盘追高失败概率上升。",
    "proxy_high_volatility": "波动偏高，实盘止损触发概率更高。",
    "proxy_weak_volume": "量能确认不足，需要竞价成交额二次验证。",
    "proxy_limit_queue": "接近强势高位，若竞价直接顶板容易不可成交。",
    "proxy_market_weak": "市场情绪不强，候选应降级观察。",
    "feedback_frequent_blocked": "实盘反馈显示多次被风控或执行条件阻断，先降级观察。",
    "feedback_low_execution": "实盘买入建议转成交比例偏低，可能存在排队或流动性问题。",
    "feedback_near_limit_queue": "盘口反馈多次接近涨停队列，容易出现信号好但买不到。",
    "feedback_paper_loss": "模拟盘闭环收益为负，历史因子需要等待新样本修复。",
    "feedback_low_win_rate": "模拟盘胜率偏低，暂不按强候选处理。",
    "market_gate_blocked": "今日未过市场状态门（情绪带/降温条件），接力胜率历史上明显偏低，建议观望不进场。",
    "model_validation_rejected": "时间顺序验证窗口未通过，当前模型只保留研究观察，不允许分配交易仓位。",
    "research_not_validated": "样本外证据尚未通过研究门槛或未来观察期，候选仅供研究，不允许自动交易。",
}

_GATE_REASON_TEXT: dict[str, str] = {
    "pass": "通过：市场情绪处于可接力区间且未在升温追高",
    "gate_disabled": "状态门未启用",
    "gate_data_missing": "市场情绪数据不足，状态门阻断",
    "emotion_too_cold": "市场情绪过冷（低于下限），接力缺乏承接",
    "emotion_overheated": "市场情绪过热（达到上限），追高接盘风险大",
    "emotion_heating_up": "市场情绪快速升温中，按门规则等待降温再接",
}


def _to_float_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.index = pd.to_datetime(out.index).normalize()
    out.columns = [str(c) for c in out.columns]
    return out.astype(float)


def _aligned_panel(panel: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    missing = [key for key in _REQUIRED if key not in panel]
    if missing:
        raise KeyError(f"panel missing required keys: {missing}")
    frames = {key: _to_float_frame(value) for key, value in panel.items() if isinstance(value, pd.DataFrame)}
    idx = frames["close"].index
    cols = frames["close"].columns
    for frame in frames.values():
        idx = idx.intersection(frame.index)
        cols = cols.intersection(frame.columns)
    if len(idx) == 0 or len(cols) == 0:
        raise ValueError("panel has no common dates or codes")
    return {key: value.loc[idx, cols].sort_index() for key, value in frames.items()}


def _safe_div(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    out = a.div(b.where(b != 0))
    return out.replace([np.inf, -np.inf], np.nan)


def _rank(df: pd.DataFrame, ascending: bool = True) -> pd.DataFrame:
    return df.rank(axis=1, pct=True, ascending=ascending, na_option="keep")


def research_candidate_pool_size(n_stocks: int) -> int:
    target = max(0, int(n_stocks))
    if target == 0:
        return 0
    reserve = max(
        1,
        (
            target * RESEARCH_UNIVERSE_RESERVE_PERCENT
            + 99
        )
        // 100,
    )
    return target + reserve


def _stable_daily_feature_sample(
    features: pd.DataFrame,
    required_count: int,
) -> pd.DataFrame:
    target = max(0, int(required_count))
    if features.empty or target == 0:
        return features.iloc[0:0].copy()
    sampled = features.copy()
    sampled["_universe_order"] = sampled["ts_code"].astype(str).map(
        lambda code: hashlib.sha256(
            code.encode("ascii", errors="ignore")
        ).hexdigest()
    )
    sampled = (
        sampled.sort_values(
            ["trade_date", "_universe_order", "ts_code"],
            kind="stable",
        )
        .groupby("trade_date", sort=False, group_keys=False)
        .head(target)
        .drop(columns=["_universe_order"])
        .sort_values(["trade_date", "ts_code"], kind="stable")
        .reset_index(drop=True)
    )
    return sampled


def _panel_for_feature_universe(
    panel: dict[str, pd.DataFrame],
    features: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    close = panel.get("close")
    if not isinstance(close, pd.DataFrame):
        return panel
    mask = pd.DataFrame(False, index=close.index, columns=close.columns)
    if not features.empty:
        pairs = features[["trade_date", "ts_code"]].drop_duplicates()
        date_positions = {
            pd.Timestamp(value).normalize(): index
            for index, value in enumerate(mask.index)
        }
        code_positions = {
            str(value): index
            for index, value in enumerate(mask.columns)
        }
        row_positions = (
            pd.to_datetime(pairs["trade_date"], errors="coerce")
            .dt.normalize()
            .map(date_positions)
        )
        column_positions = pairs["ts_code"].astype(str).map(code_positions)
        valid = row_positions.notna() & column_positions.notna()
        mask_values = mask.to_numpy(copy=False)
        mask_values[
            row_positions.loc[valid].astype(int).to_numpy(),
            column_positions.loc[valid].astype(int).to_numpy(),
        ] = True
    selected_panel = dict(panel)
    selected_panel["universe_eligible"] = mask
    return selected_panel


def _copy_default_rules() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_MINING_RULES))


def _clean_float(value: Any, default: float | None, minimum: float | None = None, maximum: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        out = float(value)
    except Exception:
        return default
    if not np.isfinite(out):
        return default
    if minimum is not None:
        out = max(minimum, out)
    if maximum is not None:
        out = min(maximum, out)
    return round(out, 6)


def normalize_execution_rules(value: Mapping[str, Any] | None = None) -> dict[str, float | str]:
    source = value if isinstance(value, Mapping) else {}
    rules: dict[str, float | str] = {}
    for key, default in LIVE_EXECUTION_RULES.items():
        number = _clean_float(source.get(key), default)
        if number is None:
            raise ValueError(f"execution rule {key} is required")
        rules[key] = float(number)
    for key, default in EXECUTION_POLICY_VERSIONS.items():
        rules[key] = str(source.get(key) or default)
    return rules


def normalize_mining_rules(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a safe, explicit factor-mining rule set.

    The API accepts only known keys. Unknown or invalid frontend fields are
    ignored so a saved report always shows the exact rules that were applied.
    """
    rules = _copy_default_rules()
    raw = raw if isinstance(raw, dict) else {}

    weight_raw = raw.get("weights") if isinstance(raw.get("weights"), dict) else {}
    for key, default in DEFAULT_MINING_RULES["weights"].items():
        rules["weights"][key] = _clean_float(weight_raw.get(key), float(default), -2.0, 2.0)

    risk_raw = raw.get("risk_thresholds") if isinstance(raw.get("risk_thresholds"), dict) else {}
    risk_bounds = {
        "overextended_3d": (-0.5, 1.5),
        "high_volatility": (0.0, 1.0),
        "weak_volume": (0.0, 10.0),
        "deep_drawdown": (-1.0, 0.5),
    }
    for key, default in DEFAULT_MINING_RULES["risk_thresholds"].items():
        lo, hi = risk_bounds[key]
        rules["risk_thresholds"][key] = _clean_float(risk_raw.get(key), float(default), lo, hi)

    tier_raw = raw.get("tier_thresholds") if isinstance(raw.get("tier_thresholds"), dict) else {}
    strong = _clean_float(tier_raw.get("strong_percentile"), DEFAULT_MINING_RULES["tier_thresholds"]["strong_percentile"], 0.0, 1.0)
    watch = _clean_float(tier_raw.get("watch_percentile"), DEFAULT_MINING_RULES["tier_thresholds"]["watch_percentile"], 0.0, 1.0)
    if watch is not None and strong is not None and watch > strong:
        watch = strong
    rules["tier_thresholds"]["strong_percentile"] = strong
    rules["tier_thresholds"]["watch_percentile"] = watch
    rules["tier_thresholds"]["strong_requires_no_risk"] = bool(
        tier_raw.get("strong_requires_no_risk", DEFAULT_MINING_RULES["tier_thresholds"]["strong_requires_no_risk"])
    )

    filter_raw = raw.get("filters") if isinstance(raw.get("filters"), dict) else {}
    rules["filters"]["min_relay_score"] = _clean_float(filter_raw.get("min_relay_score"), None, -200.0, 200.0)
    rules["filters"]["min_volume_ratio_20"] = _clean_float(filter_raw.get("min_volume_ratio_20"), None, 0.0, 20.0)
    rules["filters"]["min_near_high_20"] = _clean_float(filter_raw.get("min_near_high_20"), None, 0.0, 2.0)
    rules["filters"]["exclude_high_risk"] = bool(filter_raw.get("exclude_high_risk", False))

    gate_raw = raw.get("market_gate") if isinstance(raw.get("market_gate"), dict) else {}
    gate_defaults = DEFAULT_MINING_RULES["market_gate"]
    emo_min = _clean_float(gate_raw.get("emo_min"), float(gate_defaults["emo_min"]), 0.0, 100.0)
    emo_max = _clean_float(gate_raw.get("emo_max"), float(gate_defaults["emo_max"]), 0.0, 100.0)
    if emo_min is not None and emo_max is not None and emo_min >= emo_max:
        emo_min, emo_max = float(gate_defaults["emo_min"]), float(gate_defaults["emo_max"])
    default_provenance = DEFAULT_MARKET_GATE_PROVENANCE if not gate_raw else {}
    rules["market_gate"] = {
        "enabled": bool(gate_raw.get("enabled", gate_defaults["enabled"])),
        "emo_min": emo_min,
        "emo_max": emo_max,
        "require_cooling": bool(gate_raw.get("require_cooling", gate_defaults["require_cooling"])),
        "calibrated_through": gate_raw.get(
            "calibrated_through",
            default_provenance.get("calibrated_through"),
        ),
        "protocol_version": gate_raw.get(
            "protocol_version",
            default_provenance.get("protocol_version"),
        ),
        "calibration_source": gate_raw.get(
            "calibration_source",
            default_provenance.get("calibration_source"),
        ),
    }
    rules["descriptions"] = _RULE_DESCRIPTIONS
    return rules


_LOCKING_CONFIG_KEYS = (
    "n_stocks",
    "days",
    "train_days",
    "test_days",
    "step_days",
    "hold_days",
    "stop_loss",
    "take_profit",
    "top_k",
    "candidate_pool_size",
    "include_market_signals",
    "model_version",
    "universe_selection",
    "data_quality_policy",
)


def _locking_protocol_error(reason: str) -> ValueError:
    return ValueError(f"locking protocol {reason}")


def _strict_protocol_int(
    value: Any,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _locking_protocol_error(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise _locking_protocol_error(
            f"{field} must be between {minimum} and {maximum}"
        )
    return int(value)


def _strict_protocol_float(
    value: Any,
    field: str,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _locking_protocol_error(f"{field} must be numeric")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        raise _locking_protocol_error(f"{field} must be finite") from None
    if not np.isfinite(number):
        raise _locking_protocol_error(f"{field} must be finite")
    return number


def _build_locking_protocol(
    *,
    config: Mapping[str, Any],
    mining_rules: Mapping[str, Any],
    execution_rules: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(config, Mapping) or set(config) != set(
        _LOCKING_CONFIG_KEYS
    ):
        raise _locking_protocol_error("config is incomplete or unsupported")

    normalized_config: dict[str, Any] = {
        "n_stocks": _strict_protocol_int(
            config["n_stocks"],
            "config.n_stocks",
            minimum=1,
            maximum=20_000,
        ),
        "days": _strict_protocol_int(
            config["days"],
            "config.days",
            minimum=1,
            maximum=10_000,
        ),
        "train_days": _strict_protocol_int(
            config["train_days"],
            "config.train_days",
            minimum=1,
            maximum=10_000,
        ),
        "test_days": _strict_protocol_int(
            config["test_days"],
            "config.test_days",
            minimum=1,
            maximum=10_000,
        ),
        "step_days": _strict_protocol_int(
            config["step_days"],
            "config.step_days",
            minimum=1,
            maximum=10_000,
        ),
        "hold_days": _strict_protocol_int(
            config["hold_days"],
            "config.hold_days",
            minimum=1,
            maximum=1_000,
        ),
        "stop_loss": _strict_protocol_float(
            config["stop_loss"],
            "config.stop_loss",
        ),
        "take_profit": _strict_protocol_float(
            config["take_profit"],
            "config.take_profit",
        ),
        "top_k": _strict_protocol_int(
            config["top_k"],
            "config.top_k",
            minimum=1,
            maximum=10_000,
        ),
        "candidate_pool_size": _strict_protocol_int(
            config["candidate_pool_size"],
            "config.candidate_pool_size",
            minimum=1,
            maximum=20_000,
        ),
    }
    if (
        normalized_config["step_days"] < normalized_config["test_days"]
        or normalized_config["candidate_pool_size"]
        < normalized_config["top_k"]
    ):
        raise _locking_protocol_error("config dimensions are inconsistent")
    if not -1.0 < normalized_config["stop_loss"] < 0.0:
        raise _locking_protocol_error("config.stop_loss is out of range")
    if not 0.0 < normalized_config["take_profit"] <= 1.0:
        raise _locking_protocol_error("config.take_profit is out of range")

    include_market_signals = config["include_market_signals"]
    if not isinstance(include_market_signals, bool):
        raise _locking_protocol_error(
            "config.include_market_signals must be boolean"
        )
    normalized_config["include_market_signals"] = include_market_signals
    if config["model_version"] != MODEL_VERSION:
        raise _locking_protocol_error("config.model_version is unsupported")
    normalized_config["model_version"] = MODEL_VERSION
    universe_selection = "point_in_time_eligible_stable_sha256_sample"
    if config["universe_selection"] != universe_selection:
        raise _locking_protocol_error(
            "config.universe_selection is unsupported"
        )
    normalized_config["universe_selection"] = universe_selection

    data_quality_policy = config["data_quality_policy"]
    if (
        not isinstance(data_quality_policy, Mapping)
        or set(data_quality_policy) != set(FROZEN_DATA_QUALITY_POLICY)
    ):
        raise _locking_protocol_error(
            "config.data_quality_policy is incomplete or unsupported"
        )
    try:
        normalized_policy = json.loads(
            json.dumps(
                dict(data_quality_policy),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError):
        raise _locking_protocol_error(
            "config.data_quality_policy is invalid"
        ) from None
    max_abs_return = _strict_protocol_float(
        normalized_policy.get("max_abs_daily_return"),
        "config.data_quality_policy.max_abs_daily_return",
    )
    if not np.isclose(
        max_abs_return,
        MAX_ABS_DAILY_RETURN,
        rtol=0.0,
        atol=1e-12,
    ):
        raise _locking_protocol_error(
            "config.data_quality_policy changes the price quarantine"
        )
    coverage = _strict_protocol_float(
        normalized_policy.get("minimum_real_amount_coverage"),
        "config.data_quality_policy.minimum_real_amount_coverage",
    )
    if not np.isclose(
        coverage,
        MIN_REAL_AMOUNT_COVERAGE,
        rtol=0.0,
        atol=1e-12,
    ):
        raise _locking_protocol_error(
            "config.data_quality_policy weakens amount coverage"
        )
    normalized_policy["max_abs_daily_return"] = max_abs_return
    normalized_policy["minimum_real_amount_coverage"] = coverage
    if not _canonical_json_equal(
        normalized_policy,
        FROZEN_DATA_QUALITY_POLICY,
    ):
        raise _locking_protocol_error(
            "config.data_quality_policy is not canonical"
        )
    normalized_config["data_quality_policy"] = normalized_policy

    if not isinstance(mining_rules, Mapping):
        raise _locking_protocol_error("mining_rules must be an object")
    normalized_mining_rules = normalize_mining_rules(dict(mining_rules))
    if not _canonical_json_equal(
        dict(mining_rules),
        normalized_mining_rules,
    ):
        raise _locking_protocol_error(
            "mining_rules must be complete and canonical"
        )

    if not isinstance(execution_rules, Mapping):
        raise _locking_protocol_error("execution_rules must be an object")
    normalized_execution_rules = normalize_execution_rules(execution_rules)
    if not _canonical_json_equal(
        dict(execution_rules),
        normalized_execution_rules,
    ):
        raise _locking_protocol_error(
            "execution_rules must be complete and canonical"
        )
    execution_bounds = {
        "round_trip_cost_pct": (0.0, 10.0),
        "slippage_pct": (0.0, 10.0),
        "high_open_block_pct": (0.0, 20.0),
        "weak_open_block_pct": (-20.0, 0.0),
        "near_limit_gap_pct": (0.0, 20.0),
        "low_amount_ratio": (0.0, 20.0),
    }
    for key, (minimum, maximum) in execution_bounds.items():
        value = normalized_execution_rules[key]
        if not minimum <= float(value) <= maximum:
            raise _locking_protocol_error(
                f"execution_rules.{key} is out of range"
            )
    for key, expected in EXECUTION_POLICY_VERSIONS.items():
        if normalized_execution_rules.get(key) != expected:
            raise _locking_protocol_error(
                f"execution_rules.{key} is unsupported"
            )

    return {
        "protocol_version": LOCKING_PROTOCOL_VERSION,
        "config": normalized_config,
        "mining_rules": normalized_mining_rules,
        "execution_rules": normalized_execution_rules,
        "backtest_policy": dict(FROZEN_BACKTEST_POLICY),
    }


def condition_explain(
    rules: dict[str, Any],
    *,
    selected_market_gate: dict[str, Any] | None = None,
) -> list[str]:
    weights = rules["weights"]
    risk = rules["risk_thresholds"]
    tiers = rules["tier_thresholds"]
    filters = rules["filters"]
    active_filters = [f"{key}>={value}" for key, value in filters.items() if value not in (None, False)]
    gate = (
        dict(selected_market_gate)
        if selected_market_gate is not None
        else rules.get("market_gate") or {}
    )
    if gate.get("enabled"):
        gate_text = (
            f"market gate: emotion in [{gate.get('emo_min')}, {gate.get('emo_max')})"
            + (", cooling required (today <= prev day)" if gate.get("require_cooling") else "")
            + " — entries skipped on failing days; report keeps both gated and ungated tracks"
        )
    else:
        gate_text = "market gate: disabled (trades every test day)"
    return [
        "relay_score = weighted cross-sectional ranks of momentum, volume, amount, high proximity and limit strength, minus volatility and 3 day overheat penalties",
        "weights: " + ", ".join(f"{key}={value}" for key, value in weights.items()),
        "risk flags: " + ", ".join(f"{key}={value}" for key, value in risk.items()),
        "tiers: strong>={strong_percentile}, watch>={watch_percentile}, strong_requires_no_risk={strong_requires_no_risk}".format(**tiers),
        "filters: " + (", ".join(active_filters) if active_filters else "none"),
        gate_text,
    ]


def _risk_flags(row: pd.Series, rules: dict[str, Any] | None = None) -> str:
    thresholds = normalize_mining_rules(rules)["risk_thresholds"] if rules is None or "risk_thresholds" not in rules else rules["risk_thresholds"]
    flags: list[str] = []
    if float(row.get("ret_3", 0.0) or 0.0) > thresholds["overextended_3d"]:
        flags.append("overextended_3d")
    if float(row.get("volatility_10", 0.0) or 0.0) > thresholds["high_volatility"]:
        flags.append("high_volatility")
    if float(row.get("volume_ratio_20", 1.0) or 1.0) < thresholds["weak_volume"]:
        flags.append("weak_volume")
    if float(row.get("drawdown_20", 0.0) or 0.0) < thresholds["deep_drawdown"]:
        flags.append("deep_drawdown")
    return ",".join(flags)


def build_feature_frame(panel: dict[str, pd.DataFrame], mining_rules: dict[str, Any] | None = None) -> pd.DataFrame:
    """Build point-in-time relay features as a long DataFrame.

    Every feature uses only the current row and historical rows. The row is
    marked available at the signal date close (15:00), so consumers must use it
    for next-session decisions unless they have intraday equivalents.
    """
    rules = normalize_mining_rules(mining_rules)
    weights = rules["weights"]
    p = _aligned_panel(panel)
    close = p["close"]
    high = p["high"]
    volume = p["volume"]
    amount = p["amount"] if "amount" in p else close * volume
    if "amount_real" in p:
        amount_real = p["amount_real"].fillna(0.0).astype(bool)
    else:
        amount_real = pd.DataFrame(
            False,
            index=close.index,
            columns=close.columns,
        )
    if "universe_eligible" in p:
        universe_eligible = p["universe_eligible"].fillna(0.0).astype(bool)
        universe_eligible_known = pd.DataFrame(
            True,
            index=close.index,
            columns=close.columns,
        )
    else:
        universe_eligible = pd.DataFrame(True, index=close.index, columns=close.columns)
        universe_eligible_known = pd.DataFrame(
            False,
            index=close.index,
            columns=close.columns,
        )
    if "is_st" in p:
        raw_is_st = p["is_st"]
        if "st_status_known" in p:
            st_status_known = p["st_status_known"].fillna(0.0).astype(bool)
        else:
            st_status_known = raw_is_st.notna()
        is_st = raw_is_st.fillna(0.0).astype(bool) & st_status_known
    else:
        is_st = pd.DataFrame(False, index=close.index, columns=close.columns)
        st_status_known = pd.DataFrame(False, index=close.index, columns=close.columns)

    ret_1 = _safe_div(close, close.shift(1)) - 1.0
    ret_3 = _safe_div(close, close.shift(3)) - 1.0
    ret_5 = _safe_div(close, close.shift(5)) - 1.0
    ret_10 = _safe_div(close, close.shift(10)) - 1.0
    ret_20 = _safe_div(close, close.shift(20)) - 1.0
    volume_ratio_20 = _safe_div(volume, volume.rolling(20, min_periods=10).mean())
    amount_window_coverage = amount_real.rolling(20, min_periods=10).mean()
    amount_ratio_20 = _safe_div(
        amount,
        amount.rolling(20, min_periods=10).mean(),
    ).where(amount_window_coverage >= MIN_REAL_AMOUNT_COVERAGE)
    near_high_20 = _safe_div(close, high.rolling(20, min_periods=10).max())
    drawdown_20 = near_high_20 - 1.0
    volatility_10 = close.pct_change(fill_method=None).rolling(10, min_periods=5).std(ddof=1)
    limit_flags = pd.DataFrame(np.nan, index=ret_1.index, columns=ret_1.columns, dtype=float)
    for code in ret_1.columns:
        thresholds = pd.Series(
            [
                limit_up_threshold(str(code), trade_date, is_st=bool(is_st.at[trade_date, code]))
                for trade_date in ret_1.index
            ],
            index=ret_1.index,
            dtype=float,
        )
        limit_flags[code] = (ret_1[code] >= thresholds).astype(float).where(st_status_known[code])
    limit_count_20 = limit_flags.rolling(20, min_periods=5).sum()
    cross_section_mask = universe_eligible & close.notna()
    limit_count_rank = _rank(limit_count_20.where(cross_section_mask)).fillna(0.5)

    relay_score = (
        _rank(ret_5.where(cross_section_mask)) * weights["ret_5"]
        + _rank(ret_10.where(cross_section_mask)) * weights["ret_10"]
        + _rank(ret_20.where(cross_section_mask)) * weights["ret_20"]
        + _rank(volume_ratio_20.where(cross_section_mask)) * weights["volume_ratio_20"]
        + _rank(amount_ratio_20.where(cross_section_mask)).fillna(0.5)
        * weights["amount_ratio_20"]
        + _rank(near_high_20.where(cross_section_mask)) * weights["near_high_20"]
        + limit_count_rank * weights["limit_count_20"]
        + _rank(volatility_10.where(cross_section_mask)) * weights["volatility_10"]
        + _rank(ret_3.clip(lower=0.0).where(cross_section_mask))
        * weights["overheat_ret_3"]
    ) * 100.0

    fields = {
        "ret_1": ret_1,
        "ret_3": ret_3,
        "ret_5": ret_5,
        "ret_10": ret_10,
        "ret_20": ret_20,
        "volume_ratio_20": volume_ratio_20,
        "amount_ratio_20": amount_ratio_20,
        "near_high_20": near_high_20,
        "drawdown_20": drawdown_20,
        "volatility_10": volatility_10,
        "limit_count_20": limit_count_20,
        "relay_score": relay_score,
    }
    stacked_fields = [
        frame.stack(future_stack=True).rename(name)
        for name, frame in fields.items()
    ]
    stacked_fields.extend(
        [
            amount_real.stack(future_stack=True).rename(
                "amount_data_real"
            ),
            universe_eligible.stack(future_stack=True).rename(
                "universe_eligible"
            ),
            universe_eligible_known.stack(future_stack=True).rename(
                "universe_eligible_known"
            ),
            is_st.stack(future_stack=True).rename("is_st"),
            st_status_known.stack(future_stack=True).rename(
                "st_status_known"
            ),
        ]
    )
    long = pd.concat(stacked_fields, axis=1, join="outer", copy=False)
    long.index.names = ["trade_date", "ts_code"]
    out = long.reset_index()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y-%m-%d")
    out = out.dropna(subset=["relay_score"]).reset_index(drop=True)
    out = out[out["universe_eligible"].fillna(False).astype(bool)].reset_index(drop=True)
    out["available_at"] = out["trade_date"] + " 15:00:00"
    out["feature_as_of"] = out["trade_date"]
    out["decision_time"] = "next_session"
    out["amount_data_real"] = out["amount_data_real"].fillna(False).astype(bool)
    out["amount_data_source"] = np.where(
        out["amount_data_real"],
        "cache_amount",
        "close_times_volume_proxy",
    )
    out["risk_flags"] = out.apply(lambda row: _risk_flags(row, rules), axis=1)
    return out


def build_label_frame(
    panel: dict[str, pd.DataFrame],
    hold_days: int = 2,
    stop_loss: float = -0.035,
    take_profit: float = 0.07,
    compact_diagnostics: bool = False,
) -> pd.DataFrame:
    """Build forward labels from next-session entry to hold-period exit."""
    if hold_days < 1:
        raise ValueError("hold_days must be positive")
    p = _aligned_panel(panel)
    close = p["close"]
    if "open" not in p:
        raise KeyError("panel missing required label key: open")
    open_price = p["open"]
    high = p.get("high", close)
    low = p.get("low", close)
    amount = p.get("amount", close * p["volume"])
    amount_real = p.get("amount_real")
    if amount_real is None:
        amount_real = pd.DataFrame(
            False,
            index=close.index,
            columns=close.columns,
        )
    else:
        amount_real = amount_real.fillna(0.0).astype(bool)
    raw_is_st = p.get("is_st")
    if raw_is_st is not None:
        if "st_status_known" in p:
            st_status_known = p["st_status_known"].apply(
                lambda column: column.map(_is_trusted_marker)
            )
        else:
            st_status_known = raw_is_st.notna()
        is_st = raw_is_st.apply(
            lambda column: column.map(_is_trusted_marker)
        ) & st_status_known
    else:
        is_st = pd.DataFrame(False, index=close.index, columns=close.columns)
        st_status_known = pd.DataFrame(False, index=close.index, columns=close.columns)
    universe_eligible = p.get("universe_eligible")
    if universe_eligible is None:
        universe_eligible = pd.DataFrame(
            True,
            index=close.index,
            columns=close.columns,
        )
    else:
        universe_eligible = universe_eligible.fillna(0.0).astype(bool)
    conservative_is_st = is_st.where(st_status_known, True)
    entry = open_price.shift(-1)
    entry_gap = (_safe_div(entry, close) - 1.0) * 100.0
    signal_amount_ratio = _safe_div(amount, amount.rolling(20, min_periods=5).mean())
    bars_by_code = {}
    for code in close.columns:
        bar_data = {
            "open": open_price[code],
            "high": high[code],
            "low": low[code],
            "close": close[code],
            "volume": p["volume"][code],
        }
        bar_data["is_st"] = conservative_is_st[code]
        bars_by_code[str(code)] = pd.DataFrame(bar_data, index=close.index)

    output_columns = (
        _RESEARCH_LABEL_COLUMNS
        if compact_diagnostics
        else _LABEL_FRAME_COLUMNS
    )
    label_columns: dict[str, list[Any]] = {
        column: []
        for column in output_columns
    }

    def append_label(row: dict[str, Any]) -> None:
        for column, values in label_columns.items():
            value = row.get(column)
            if (
                compact_diagnostics
                and column in {"valuation_path", "execution_flags"}
            ):
                value = json.dumps(
                    value or [],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            values.append(value)

    dates = list(close.index)
    for i, trade_date in enumerate(dates):
        max_exit_i = i + hold_days + 1
        if max_exit_i >= len(dates):
            continue
        for code in close.columns:
            if not bool(universe_eligible.iloc[i].get(code)):
                continue
            entry_date_text = dates[i + 1].strftime("%Y-%m-%d")
            entry_price_value = _maybe_float(open_price.iloc[i + 1].get(code))
            entry_volume_value = _maybe_float(p["volume"].iloc[i + 1].get(code))
            gap = _maybe_float(entry_gap.iloc[i].get(code))
            amount_ratio = _maybe_float(signal_amount_ratio.iloc[i].get(code))
            signal_amount_real = bool(amount_real.iloc[i].get(code))
            st_known_value = _is_trusted_marker(
                st_status_known.iloc[i + 1].get(code)
            )
            is_st_value = (
                _is_trusted_marker(is_st.iloc[i + 1].get(code))
                if st_known_value
                else False
            )
            execution_flags = _entry_st_execution_flags(
                st_status_known=st_known_value,
                is_st=is_st_value,
            )
            execution_flags.extend(
                _entry_execution_flags(
                    gap,
                    amount_ratio,
                    code=str(code),
                    entry_date=entry_date_text,
                    is_st=is_st_value if st_known_value else True,
                )
            )
            if not signal_amount_real:
                execution_flags.append("real_amount_unavailable")
            path = build_trade_path(
                code=str(code),
                bars=bars_by_code[str(code)],
                signal_index=i,
                hold_days=hold_days,
                stop_loss=stop_loss,
                take_profit=take_profit,
                round_trip_cost_pct=LIVE_EXECUTION_RULES["round_trip_cost_pct"],
                slippage_pct=LIVE_EXECUTION_RULES["slippage_pct"],
                max_deferred_exit_sessions=MAX_DEFERRED_EXIT_SESSIONS,
            )
            if not path:
                entry_unavailable = (
                    entry_price_value is None
                    or entry_price_value <= 0
                    or entry_volume_value is None
                    or entry_volume_value <= 0
                )
                if entry_unavailable or execution_flags:
                    block_flags = list(execution_flags)
                    if entry_unavailable:
                        block_flags.append("entry_bar_unavailable")
                    append_label(
                        {
                            "trade_date": trade_date.strftime("%Y-%m-%d"),
                            "ts_code": str(code),
                            "entry_date": entry_date_text,
                            "exit_date": dates[max_exit_i].strftime("%Y-%m-%d"),
                            "hold_days": int(hold_days),
                            "holding_sessions": int(hold_days) + 1,
                            "forward_return_pct": 0.0,
                            "plan_return_pct": 0.0,
                            "live_adjusted_return_pct": 0.0,
                            "model_target_pct": 0.0,
                            "entry_price": entry_price_value,
                            "exit_price": None,
                            "exit_reason": block_flags[0],
                            "valuation_path": [],
                            "entry_gap_pct": gap,
                            "is_st": is_st_value,
                            "st_status_known": st_known_value,
                            "signal_amount_ratio_20": amount_ratio,
                            "entry_amount_ratio_20": amount_ratio,
                            "amount_data_real": signal_amount_real,
                            "execution_blocked": True,
                            "execution_flags": block_flags,
                            "label_status": "entry_blocked",
                            "research_evaluable": True,
                            "label_source": "entry_blocked_before_execution",
                            "path_policy": "t_plus_1_stop_first_on_same_bar",
                        }
                    )
                    continue
                append_label(
                    {
                        "trade_date": trade_date.strftime("%Y-%m-%d"),
                        "ts_code": str(code),
                        "entry_date": entry_date_text,
                        "exit_date": None,
                        "hold_days": int(hold_days),
                        "holding_sessions": int(hold_days) + 1,
                        "forward_return_pct": np.nan,
                        "plan_return_pct": np.nan,
                        "live_adjusted_return_pct": np.nan,
                        "model_target_pct": np.nan,
                        "entry_price": entry_price_value,
                        "exit_price": None,
                        "exit_reason": "awaiting_exit_data",
                        "valuation_path": [],
                        "entry_gap_pct": gap,
                        "is_st": is_st_value,
                        "st_status_known": st_known_value,
                        "signal_amount_ratio_20": amount_ratio,
                        "entry_amount_ratio_20": amount_ratio,
                        "amount_data_real": signal_amount_real,
                        "execution_blocked": False,
                        "execution_flags": [],
                        "label_status": "entered_pending_exit",
                        "research_evaluable": False,
                        "label_source": "entered_trade_awaiting_exit_data",
                        "path_policy": "t_plus_1_stop_first_on_same_bar",
                    }
                )
                continue
            fixed_exit_price = _maybe_float(close.iloc[max_exit_i].get(code))
            if fixed_exit_price is None:
                fixed_exit_price = _maybe_float(path.get("exit_price"))
            if entry_price_value is None or fixed_exit_price is None or entry_price_value <= 0:
                continue
            forward_ret = (fixed_exit_price / entry_price_value - 1.0) * 100.0
            blocked = bool(execution_flags)
            live_ret = float(path["live_adjusted_return_pct"])
            append_label(
                {
                    "trade_date": trade_date.strftime("%Y-%m-%d"),
                    "ts_code": str(code),
                    "entry_date": path["entry_date"],
                    "exit_date": path["exit_date"],
                    "hold_days": int(hold_days),
                    "holding_sessions": int(hold_days) + 1,
                    "forward_return_pct": round(float(forward_ret), 4),
                    "plan_return_pct": path["plan_return_pct"],
                    "live_adjusted_return_pct": round(float(live_ret), 4),
                    "model_target_pct": 0.0 if blocked else round(float(live_ret), 4),
                    "entry_price": path["entry_price"],
                    "exit_price": path["exit_price"],
                    "exit_reason": path["exit_reason"],
                    "valuation_path": path["valuation_path"],
                    "entry_gap_pct": gap,
                    "is_st": is_st_value,
                    "st_status_known": st_known_value,
                    "signal_amount_ratio_20": amount_ratio,
                    "entry_amount_ratio_20": amount_ratio,
                    "amount_data_real": signal_amount_real,
                    "execution_blocked": blocked,
                    "execution_flags": execution_flags,
                    "label_status": "entry_blocked" if blocked else "settled",
                    "research_evaluable": True,
                    "label_source": "t_plus_1_entry_to_trade_plan_exit",
                    "path_policy": path["path_policy"],
                }
            )
    return pd.DataFrame(label_columns)


def _entry_st_execution_flags(
    *,
    st_status_known: bool,
    is_st: bool,
) -> list[str]:
    if not st_status_known:
        return ["st_status_unknown"]
    if is_st:
        return ["known_st_excluded"]
    return []


def _entry_execution_flags(
    entry_gap_pct: float | None,
    entry_amount_ratio_20: float | None,
    code: str | None = None,
    entry_date: Any = None,
    is_st: bool = False,
    execution_rules: Mapping[str, Any] | None = None,
) -> list[str]:
    rules = normalize_execution_rules(execution_rules)
    flags: list[str] = []
    if entry_gap_pct is not None:
        near_limit_gap = float(rules["near_limit_gap_pct"])
        if code:
            near_limit_gap = limit_up_threshold(code, entry_date, is_st=is_st) * 100.0 - 0.3
        if entry_gap_pct >= near_limit_gap:
            flags.append("entry_near_limit_up")
        elif entry_gap_pct > float(rules["high_open_block_pct"]):
            flags.append("entry_high_open")
        elif entry_gap_pct < float(rules["weak_open_block_pct"]):
            flags.append("entry_weak_open")
    if (
        entry_amount_ratio_20 is not None
        and entry_amount_ratio_20 < float(rules["low_amount_ratio"])
    ):
        flags.append("entry_low_liquidity")
    return flags


def _trade_summary(returns: Sequence[float]) -> dict[str, Any]:
    arr = np.array(list(returns), dtype=float)
    if arr.size == 0:
        return {"trade_count": 0, "hit_rate": 0.0, "avg_return_pct": 0.0, "total_return_pct": 0.0, "max_drawdown_pct": 0.0}
    equity = np.concatenate(
        ([1.0], np.cumprod(1.0 + arr / 100.0))
    )
    peak = np.maximum.accumulate(equity)
    drawdown = equity / peak - 1.0
    return {
        "trade_count": int(arr.size),
        "hit_rate": round(float((arr > 0).mean()), 4),
        "avg_return_pct": round(float(arr.mean()), 4),
        "total_return_pct": round(float((equity[-1] - 1.0) * 100.0), 4),
        "max_drawdown_pct": round(float(drawdown.min() * 100.0), 4),
    }


def _live_summary_fields(
    raw_returns: Sequence[float],
    live_returns: Sequence[float],
    blocked_counts: Sequence[int],
    top_k: int = 1,
) -> dict[str, Any]:
    raw = _trade_summary(raw_returns)
    live = _trade_summary(live_returns)
    attempted_slots = max(0, int(len(raw_returns) * max(1, top_k)))
    blocked_slots = int(sum(int(x) for x in blocked_counts))
    return {
        "raw_hit_rate": raw["hit_rate"],
        "raw_avg_return_pct": raw["avg_return_pct"],
        "raw_total_return_pct": raw["total_return_pct"],
        "raw_max_drawdown_pct": raw["max_drawdown_pct"],
        "live_trade_count": live["trade_count"],
        "live_hit_rate": live["hit_rate"],
        "live_avg_return_pct": live["avg_return_pct"],
        "live_total_return_pct": live["total_return_pct"],
        "live_max_drawdown_pct": live["max_drawdown_pct"],
        "live_execution_block_rate": round(blocked_slots / attempted_slots, 4) if attempted_slots else 0.0,
        "live_cost_model": {
            "round_trip_cost_pct": LIVE_EXECUTION_RULES["round_trip_cost_pct"],
            "slippage_pct": LIVE_EXECUTION_RULES["slippage_pct"],
            "high_open_block_pct": LIVE_EXECUTION_RULES["high_open_block_pct"],
            "weak_open_block_pct": LIVE_EXECUTION_RULES["weak_open_block_pct"],
            "near_limit_gap_pct": LIVE_EXECUTION_RULES["near_limit_gap_pct"],
            "low_amount_ratio": LIVE_EXECUTION_RULES["low_amount_ratio"],
        },
    }


def _market_emotion_context(
    frame: pd.DataFrame,
) -> tuple[dict[str, float], dict[str, float], dict[str, bool]]:
    if frame.empty or "market_emotion_score" not in frame.columns:
        return {}, {}, {}
    emotion_series = (
        frame.groupby("trade_date")["market_emotion_score"]
        .first()
        .sort_index()
    )
    if "market_data_trusted" in frame.columns:
        trusted_series = (
            frame.groupby("trade_date")["market_data_trusted"]
            .apply(
                lambda values: bool(
                    values.map(_is_trusted_marker).all()
                )
            )
            .reindex(emotion_series.index, fill_value=False)
        )
    else:
        trusted_series = pd.Series(
            True,
            index=emotion_series.index,
            dtype=bool,
        )

    emotion_by_date: dict[str, float] = {}
    delta_by_date: dict[str, float] = {}
    trusted_by_date: dict[str, bool] = {}
    dates = list(emotion_series.index)
    for index, trade_date in enumerate(dates):
        date_text = str(trade_date)
        trusted = bool(trusted_series.loc[trade_date])
        trusted_by_date[date_text] = trusted
        emotion = emotion_series.loc[trade_date]
        if not trusted or emotion is None or pd.isna(emotion):
            continue
        emotion_by_date[date_text] = float(emotion)
        if index == 0:
            continue
        previous_date = dates[index - 1]
        previous = emotion_series.loc[previous_date]
        if (
            bool(trusted_series.loc[previous_date])
            and previous is not None
            and not pd.isna(previous)
        ):
            delta_by_date[date_text] = float(emotion) - float(previous)
    return emotion_by_date, delta_by_date, trusted_by_date


def _gate_check(
    gate: dict[str, Any] | None,
    emotion: float | None,
    delta: float | None,
) -> tuple[bool, str]:
    """市场状态门判定。返回 (是否放行, 原因键)。

    数据缺失时阻断（gate_data_missing），避免把未知市场状态当成可交易状态。
    """
    if not gate or not gate.get("enabled"):
        return True, "gate_disabled"
    if emotion is None:
        return False, "gate_data_missing"
    if emotion < float(gate.get("emo_min", 0.0)):
        return False, "emotion_too_cold"
    if emotion >= float(gate.get("emo_max", 100.0)):
        return False, "emotion_overheated"
    if gate.get("require_cooling"):
        if delta is None:
            return False, "gate_data_missing"
        if delta > 0:
            return False, "emotion_heating_up"
    return True, "pass"


def _market_gate_evidence_status(
    gate: dict[str, Any] | None,
    test_start: str,
) -> tuple[bool, str]:
    if not gate or not gate.get("enabled"):
        return True, "gate_disabled"
    calibrated_through = str(gate.get("calibrated_through") or "")[:10]
    try:
        cutoff = pd.Timestamp(calibrated_through)
        test_start_ts = pd.Timestamp(str(test_start)[:10])
    except (TypeError, ValueError):
        return False, "calibration_provenance_missing"
    if pd.isna(cutoff) or pd.isna(test_start_ts):
        return False, "calibration_provenance_missing"
    if test_start_ts <= cutoff:
        return False, "calibration_overlaps_test_history"
    return True, "post_calibration_holdout"


def _is_preregistered_oos_window(window: Mapping[str, Any]) -> bool:
    registration = load_model_preregistration()
    if not registration.get("ok"):
        return False
    test_start = str(window.get("test_start") or "")[:10]
    if not test_start:
        return False
    try:
        return pd.Timestamp(test_start) >= pd.Timestamp(
            registration["record"]["oos_not_before"]
        )
    except (TypeError, ValueError):
        return False


def _portfolio_attempt(row: pd.Series, gate_pass: bool) -> dict[str, Any]:
    trade_date = str(row.get("trade_date") or "")
    entry_date = str(row.get("entry_date") or trade_date)
    exit_date = str(row.get("exit_date") or entry_date)
    forward_return = _maybe_float(row.get("forward_return_pct"))
    if forward_return is None:
        forward_return = 0.0
    live_return = _maybe_float(row.get("live_adjusted_return_pct"))
    if live_return is None:
        live_return = float(row.get("forward_return_pct") or 0.0)
    blocked_value = row.get("execution_blocked", False)
    blocked = False if blocked_value is None or pd.isna(blocked_value) else bool(blocked_value)
    exit_reason = str(row.get("exit_reason") or "").strip()
    execution_flags = row.get("execution_flags")
    if isinstance(execution_flags, str):
        try:
            decoded_flags = json.loads(execution_flags)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded_flags = None
        if isinstance(decoded_flags, list):
            execution_flags = decoded_flags
    if not isinstance(execution_flags, list):
        execution_flags = []
    path = row.get("valuation_path")
    if isinstance(path, str):
        try:
            decoded_path = json.loads(path)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded_path = None
        if isinstance(decoded_path, list):
            path = decoded_path
    if not isinstance(path, list):
        path = [{"date": exit_date, "return_pct": live_return}]
    if blocked:
        exit_date = entry_date
        live_return = 0.0
        path = []
        exit_reason = (
            str(execution_flags[0]).strip()
            if execution_flags
            else exit_reason or "execution_blocked"
        )
    else:
        exit_reason = exit_reason or "unspecified_exit"
    return {
        "trade_date": trade_date,
        "ts_code": str(row.get("ts_code") or ""),
        "entry_date": entry_date,
        "exit_date": exit_date,
        "execution_blocked": blocked,
        "forward_return_pct": float(forward_return),
        "live_adjusted_return_pct": float(live_return),
        "valuation_path": path,
        "exit_reason": exit_reason,
        "gate_pass": bool(gate_pass),
    }


def _portfolio_summary_fields(portfolio: dict[str, Any]) -> dict[str, Any]:
    return {
        "portfolio_initial_equity": portfolio.get("initial_equity", 1.0),
        "portfolio_final_equity": portfolio.get("final_equity", 1.0),
        "portfolio_total_return_pct": portfolio.get("total_return_pct", 0.0),
        "portfolio_max_drawdown_pct": portfolio.get("max_drawdown_pct", 0.0),
        "portfolio_attempted_slots": portfolio.get("attempted_slots", 0),
        "portfolio_blocked_slots": portfolio.get("blocked_slots", 0),
        "portfolio_gate_skipped_slots": portfolio.get("gate_skipped_slots", 0),
        "portfolio_executed_trade_count": portfolio.get("executed_trade_count", 0),
        "portfolio_average_capital_utilization": portfolio.get("average_capital_utilization", 0.0),
        "portfolio_average_cash_pct": portfolio.get("average_cash_pct", 1.0),
        "portfolio_max_gross_exposure": portfolio.get("max_gross_exposure", 0.0),
        "portfolio_min_cash": portfolio.get("min_cash", 1.0),
    }


def _select_market_gate(
    validation_frame: pd.DataFrame,
    model: dict[str, Any],
    *,
    configured_gate: dict[str, Any] | None,
    top_k: int,
    holding_sessions: int,
) -> dict[str, Any]:
    disabled_gate = {"enabled": False}
    if (
        validation_frame.empty
        or not configured_gate
        or not configured_gate.get("enabled")
        or "market_emotion_score" not in validation_frame.columns
    ):
        return {
            "selection_source": "chronological_validation",
            "selected_gate": disabled_gate,
            "selection_reason": "configured_gate_unavailable",
            "minimum_improvement_pct": 0.25,
            "candidates": [],
        }

    scored = validation_frame.copy()
    scored["_factor_model_score"] = score_factor_model(scored, model)
    emotion_by_date, delta_by_date, _trusted_by_date = (
        _market_emotion_context(scored)
    )
    picked_rows = []
    for trade_date in sorted(scored["trade_date"].astype(str).unique().tolist()):
        day = scored[scored["trade_date"].astype(str) == trade_date]
        picks = day.sort_values("_factor_model_score", ascending=False).head(top_k)
        picked_rows.extend(row for _, row in picks.iterrows())

    candidate_reports = []
    candidate_portfolios = []
    for name, gate in (("disabled", disabled_gate), ("configured", dict(configured_gate))):
        attempts = []
        for row in picked_rows:
            trade_date = str(row.get("trade_date") or "")
            gate_pass, _reason = _gate_check(
                gate,
                emotion_by_date.get(trade_date),
                delta_by_date.get(trade_date),
            )
            attempts.append(_portfolio_attempt(row, gate_pass=gate_pass))
        portfolio = simulate_portfolio(
            attempts,
            top_k=top_k,
            holding_sessions=holding_sessions,
        )
        candidate_portfolios.append(portfolio)
        candidate_reports.append(
            {
                "name": name,
                "gate": gate,
                "total_return_pct": portfolio["total_return_pct"],
                "max_drawdown_pct": portfolio["max_drawdown_pct"],
                "executed_trade_count": portfolio["executed_trade_count"],
                "gate_skipped_slots": portfolio["gate_skipped_slots"],
            }
        )

    disabled_portfolio, configured_portfolio = candidate_portfolios
    minimum_trades = max(
        3,
        int(np.ceil(float(disabled_portfolio["executed_trade_count"]) * 0.25)),
    )
    improvement = (
        float(configured_portfolio["total_return_pct"])
        - float(disabled_portfolio["total_return_pct"])
    )
    if configured_portfolio["executed_trade_count"] < minimum_trades:
        selected_gate = disabled_gate
        selection_reason = "configured_gate_insufficient_trades"
    elif improvement >= 0.25:
        selected_gate = dict(configured_gate)
        selection_reason = "validation_improvement"
    else:
        selected_gate = disabled_gate
        selection_reason = "configured_gate_not_better"
    return {
        "selection_source": "chronological_validation",
        "selected_gate": selected_gate,
        "selection_reason": selection_reason,
        "minimum_improvement_pct": 0.25,
        "minimum_configured_trades": minimum_trades,
        "validation_improvement_pct": round(improvement, 4),
        "candidates": candidate_reports,
    }


def _ensure_model_target(frame: pd.DataFrame) -> pd.DataFrame:
    if "model_target_pct" in frame.columns:
        return frame
    out = frame.copy()
    if "live_adjusted_return_pct" in out.columns:
        target = pd.to_numeric(out["live_adjusted_return_pct"], errors="coerce")
    else:
        target = pd.to_numeric(out.get("forward_return_pct"), errors="coerce")
    if "execution_blocked" in out.columns:
        blocked = out["execution_blocked"].fillna(False).astype(bool)
        target = target.where(~blocked, 0.0)
    out["model_target_pct"] = target
    return out


def _fit_validation_dates(train_dates: Sequence[str]) -> tuple[list[str], list[str]]:
    ordered = list(train_dates)
    if len(ordered) < 8:
        return ordered, []
    validation_count = max(3, int(round(len(ordered) * 0.20)))
    validation_count = min(validation_count, len(ordered) - 5)
    return ordered[:-validation_count], ordered[-validation_count:]


def _label_horizon_sessions(
    feature_data: pd.DataFrame,
    label_data: pd.DataFrame,
) -> int:
    if "holding_sessions" in label_data.columns:
        values = pd.to_numeric(
            label_data["holding_sessions"],
            errors="coerce",
        ).dropna()
        if not values.empty:
            return max(0, int(values.median()))
    if "hold_days" in label_data.columns:
        values = pd.to_numeric(label_data["hold_days"], errors="coerce").dropna()
        if not values.empty:
            return max(0, int(values.median()) + 1)
    if "exit_date" not in label_data.columns:
        return 0

    feature_dates = sorted(feature_data["trade_date"].dropna().astype(str).unique())
    date_positions = {trade_date: index for index, trade_date in enumerate(feature_dates)}
    offsets = []
    for trade_date, exit_date in label_data[["trade_date", "exit_date"]].itertuples(
        index=False,
        name=None,
    ):
        start = date_positions.get(str(trade_date))
        end = date_positions.get(str(exit_date))
        if start is not None and end is not None and end >= start:
            offsets.append(end - start)
    return max(offsets, default=0)


def _merge_research_universe(features: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    if features.empty or labels.empty:
        return pd.DataFrame()

    feature_data = features.loc[
        :,
        [
            column
            for column in _RESEARCH_FEATURE_COLUMNS
            if column in features.columns
        ],
    ].copy()
    label_data = labels.loc[
        :,
        [
            column
            for column in _RESEARCH_LABEL_COLUMNS
            if column in labels.columns
        ],
    ].copy()
    for frame in (feature_data, label_data):
        frame["trade_date"] = frame["trade_date"].astype(str)
        frame["ts_code"] = frame["ts_code"].astype(str)

    feature_dates = sorted(feature_data["trade_date"].dropna().unique())
    horizon_sessions = _label_horizon_sessions(feature_data, label_data)
    if horizon_sessions:
        mature_dates = feature_dates[:-horizon_sessions]
    else:
        mature_dates = feature_dates
    feature_data = feature_data[
        feature_data["trade_date"].isin(mature_dates)
    ].copy()
    data = feature_data.merge(
        label_data,
        on=["trade_date", "ts_code"],
        how="left",
        indicator="_label_join",
    )
    missing_path = data["_label_join"].eq("left_only")
    data = data.drop(columns=["_label_join"])
    if not missing_path.any():
        return _ensure_model_target(data)

    for column in (
        "forward_return_pct",
        "live_adjusted_return_pct",
        "model_target_pct",
    ):
        if column not in data.columns:
            data[column] = np.nan
        data.loc[missing_path, column] = np.nan

    if "execution_blocked" not in data.columns:
        data["execution_blocked"] = False
    data["execution_blocked"] = data["execution_blocked"].map(
        lambda value: bool(value) if value is not None and not pd.isna(value) else False
    )
    data.loc[missing_path, "execution_blocked"] = False

    for column in ("entry_date", "exit_date"):
        if column not in data.columns:
            data[column] = None
        data.loc[missing_path, column] = None

    if "exit_reason" not in data.columns:
        data["exit_reason"] = None
    if "label_source" not in data.columns:
        data["label_source"] = None
    data.loc[missing_path, "label_source"] = "missing_forward_execution_path"
    if "label_status" not in data.columns:
        data["label_status"] = None
    data.loc[missing_path, "label_status"] = "missing_forward_execution_path"
    if "research_evaluable" not in data.columns:
        data["research_evaluable"] = True
    data["research_evaluable"] = data["research_evaluable"].map(
        lambda value: bool(value)
        if value is not None and not pd.isna(value)
        else True
    )
    data.loc[missing_path, "research_evaluable"] = False

    if "execution_flags" not in data.columns:
        data["execution_flags"] = None
    if "valuation_path" not in data.columns:
        data["valuation_path"] = None
    for index in data.index[missing_path]:
        data.at[index, "execution_flags"] = ["missing_forward_execution_path"]
        data.at[index, "valuation_path"] = []
    return _ensure_model_target(data)


_CALIBRATION_EVIDENCE_COLUMNS = (
    "trade_date",
    "ts_code",
    "exit_date",
    "model_target_pct",
    "amount_data_real",
    "market_data_trusted",
    "research_evaluable",
    *CANDIDATE_FACTORS,
)


def _preregistered_calibration_frame(
    data: pd.DataFrame,
    cutoff: str = MODEL_SPEC_CALIBRATED_THROUGH,
) -> pd.DataFrame:
    if data.empty or "trade_date" not in data.columns:
        return pd.DataFrame(columns=_CALIBRATION_EVIDENCE_COLUMNS)
    calibration = data[
        data["trade_date"].astype(str).str[:10] <= str(cutoff)[:10]
    ].copy()
    if "exit_date" in calibration.columns:
        exits = calibration["exit_date"].astype("string").str[:10]
        calibration = calibration[
            exits.notna() & (exits <= str(cutoff)[:10])
        ].copy()
    if "market_data_trusted" in calibration.columns:
        calibration = calibration[
            calibration["market_data_trusted"].map(_is_trusted_marker)
        ].copy()
    if "research_evaluable" in calibration.columns:
        calibration = calibration[
            calibration["research_evaluable"].map(_is_trusted_marker)
        ].copy()
    return calibration


def _calibration_data_evidence(
    data: pd.DataFrame,
    cutoff: str = MODEL_SPEC_CALIBRATED_THROUGH,
) -> dict[str, Any]:
    calibration = _preregistered_calibration_frame(data, cutoff)
    normalized = calibration.reindex(
        columns=_CALIBRATION_EVIDENCE_COLUMNS
    ).copy()
    for column in ("trade_date", "ts_code", "exit_date"):
        normalized[column] = normalized[column].astype("string").str[:10]
    for column in (
        "model_target_pct",
        *CANDIDATE_FACTORS,
    ):
        normalized[column] = pd.to_numeric(
            normalized[column],
            errors="coerce",
        )
    for column in (
        "amount_data_real",
        "market_data_trusted",
        "research_evaluable",
    ):
        normalized[column] = normalized[column].map(
            lambda value: (
                1
                if _is_trusted_marker(value)
                else 0
                if value is not None and not pd.isna(value)
                else pd.NA
            )
        )
    normalized = normalized.sort_values(
        ["trade_date", "ts_code"],
        kind="mergesort",
    ).reset_index(drop=True)

    digest = hashlib.sha256()

    class _DigestWriter:
        def write(self, value: str) -> int:
            digest.update(value.encode("utf-8"))
            return len(value)

    normalized.to_csv(
        _DigestWriter(),
        index=False,
        lineterminator="\n",
        na_rep="<NA>",
        float_format="%.17g",
    )
    trade_dates = normalized["trade_date"].dropna().astype(str)
    exits = normalized["exit_date"].dropna().astype(str)
    return {
        "algorithm": "canonical_csv_sha256_v1",
        "columns": list(_CALIBRATION_EVIDENCE_COLUMNS),
        "row_count": int(len(normalized)),
        "trade_date_start": (
            trade_dates.min() if not trade_dates.empty else None
        ),
        "trade_date_end": (
            trade_dates.max() if not trade_dates.empty else None
        ),
        "label_max_exit": exits.max() if not exits.empty else None,
        "sha256": digest.hexdigest(),
    }


def _registered_model_from_preregistration(
    registration: Mapping[str, Any],
) -> dict[str, Any]:
    if not registration.get("ok"):
        raise ValueError("preregistration record is unavailable")
    record = registration.get("record")
    if not isinstance(record, Mapping):
        raise ValueError("preregistration record is invalid")
    model = json.loads(
        json.dumps(
            record.get("executable_model"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    if (
        not isinstance(model, dict)
        or factor_model_digest(model) != registration.get("model_sha256")
    ):
        raise ValueError("preregistered executable model is invalid")
    model.update(
        {
            "preregistration_record_sha256": registration["sha256"],
            "preregistration_registered_at": record["registered_at"],
            "preregistration_oos_not_before": record["oos_not_before"],
            "registered_model_sha256": registration["model_sha256"],
        }
    )
    return model


def _unavailable_preregistered_model(
    reason: str,
    registration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    model = fit_factor_model(
        pd.DataFrame(),
        target_col="model_target_pct",
    )
    record = (
        registration.get("record")
        if isinstance(registration, Mapping)
        and isinstance(registration.get("record"), Mapping)
        else {}
    )
    model.update(
        {
            "strategy_key": STRATEGY_KEY,
            "training_scope": "preregistered_calibration_only",
            "selection_source": "repository_preregistered_fixed_parameters",
            "model_spec_protocol_version": MODEL_SPEC_PROTOCOL_VERSION,
            "model_spec_calibrated_through": MODEL_SPEC_CALIBRATED_THROUGH,
            "preregistration_record_sha256": (
                registration.get("sha256")
                if isinstance(registration, Mapping)
                else None
            ),
            "preregistration_registered_at": record.get("registered_at"),
            "preregistration_oos_not_before": record.get("oos_not_before"),
            "validation_accepted": False,
            "validation_stable": False,
            "validation_rejection_reason": reason,
            "validation_stability_reason": reason,
            "frozen_model_candidate_eligible": False,
            "frozen_model_candidate_reason": reason,
            "selected_market_gate": {"enabled": False},
        }
    )
    return model


def _fit_preregistered_frozen_model(
    data: pd.DataFrame,
    *,
    market_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    registration = load_model_preregistration()
    if not registration.get("ok"):
        return _unavailable_preregistered_model(
            str(
                registration.get("reason")
                or "preregistration_record_unavailable"
            ),
            registration,
        )
    try:
        model = _registered_model_from_preregistration(registration)
    except ValueError as exc:
        return _unavailable_preregistered_model(str(exc), registration)

    expected_evidence = registration["record"]["calibration_evidence"]
    actual_evidence = _calibration_data_evidence(data)
    expected_start = str(expected_evidence.get("trade_date_start") or "")
    expected_end = str(expected_evidence.get("trade_date_end") or "")
    actual_start = str(actual_evidence.get("trade_date_start") or "")
    actual_end = str(actual_evidence.get("trade_date_end") or "")
    has_full_calibration_range = bool(
        expected_start
        and expected_end
        and actual_start
        and actual_end
        and actual_start <= expected_start
        and actual_end >= expected_end
    )
    if has_full_calibration_range:
        calibration_match = actual_evidence == expected_evidence
    else:
        calibration_match = None
    model["calibration_data_match"] = calibration_match
    model["calibration_evidence"] = dict(expected_evidence)
    if calibration_match is False:
        model["frozen_model_candidate_eligible"] = False
        model[
            "frozen_model_candidate_reason"
        ] = "preregistered_calibration_data_mismatch"
    return model


def _build_preregistered_oos_results(
    *,
    session_dates: Sequence[str],
    trades: Sequence[Mapping[str, Any]],
    attempts: Sequence[Mapping[str, Any]],
    model: Mapping[str, Any],
    test_days: int,
    top_k: int,
    holding_sessions: int,
) -> dict[str, Any]:
    normalized_dates = [str(value) for value in session_dates]
    normalized_trades = [dict(value) for value in trades]
    normalized_attempts = [dict(value) for value in attempts]
    attempts_by_date: dict[str, list[dict[str, Any]]] = {}
    for attempt in normalized_attempts:
        attempts_by_date.setdefault(
            str(attempt.get("trade_date") or ""),
            [],
        ).append(attempt)

    windows: list[dict[str, Any]] = []
    model_sha256 = factor_model_digest(model)
    selected_gate = dict(
        model.get("selected_market_gate") or {"enabled": False}
    )
    for offset in range(0, len(normalized_dates), int(test_days)):
        window_dates = normalized_dates[offset : offset + int(test_days)]
        if len(window_dates) != int(test_days):
            continue
        window_trades = [
            trade
            for trade in normalized_trades
            if str(trade.get("trade_date") or "") in window_dates
        ]
        window_attempts = [
            attempt
            for trade_date in window_dates
            for attempt in attempts_by_date.get(trade_date, [])
        ]
        ungated = [
            {**attempt, "gate_pass": True}
            for attempt in window_attempts
        ]
        window_portfolio = simulate_portfolio(
            ungated,
            top_k=top_k,
            holding_sessions=holding_sessions,
            session_dates=window_dates,
        )
        gated_window_portfolio = simulate_portfolio(
            window_attempts,
            top_k=top_k,
            holding_sessions=holding_sessions,
            session_dates=window_dates,
        )
        windows.append(
            {
                "test_start": window_dates[0],
                "test_end": window_dates[-1],
                "model_sha256": model_sha256,
                "model_validation_stable": bool(
                    model.get("validation_stable")
                ),
                "live_total_return_pct": window_portfolio[
                    "total_return_pct"
                ],
                "gated_live_total_return_pct": gated_window_portfolio[
                    "total_return_pct"
                ],
                "unevaluable_oos_days": sum(
                    trade.get("research_evaluable") is False
                    for trade in window_trades
                ),
                "unevaluable_oos_slots": sum(
                    top_k
                    for trade in window_trades
                    if trade.get("research_evaluable") is False
                ),
                "market_gate_evidence_eligible": (
                    _market_gate_evidence_status(
                        selected_gate,
                        window_dates[0],
                    )[0]
                ),
            }
        )

    ungated_attempts = [
        {**attempt, "gate_pass": True}
        for attempt in normalized_attempts
    ]
    return {
        "windows": windows,
        "portfolio": simulate_portfolio(
            ungated_attempts,
            top_k=top_k,
            holding_sessions=holding_sessions,
            session_dates=normalized_dates,
        ),
        "gated_portfolio": simulate_portfolio(
            normalized_attempts,
            top_k=top_k,
            holding_sessions=holding_sessions,
            session_dates=normalized_dates,
        ),
    }


def _evaluate_preregistered_oos(
    data: pd.DataFrame,
    model: Mapping[str, Any],
    *,
    registration: Mapping[str, Any],
    test_days: int,
    top_k: int,
    holding_sessions: int,
    emotion_by_date: Mapping[str, float | None],
    emotion_delta_by_date: Mapping[str, float | None],
    locking_protocol: Mapping[str, Any] | None,
    data_quality_assessment: Mapping[str, Any] | None,
) -> dict[str, Any]:
    record = (
        registration.get("record")
        if isinstance(registration.get("record"), Mapping)
        else {}
    )
    base = {
        "evidence_version": PREREGISTERED_OOS_EVIDENCE_VERSION,
        "protocol_version": record.get("protocol_version"),
        "calibrated_through": record.get("calibrated_through"),
        "oos_not_before": record.get("oos_not_before"),
        "registration_sha256": registration.get("sha256"),
        "model_sha256": (
            factor_model_digest(model)
            if isinstance(model, Mapping)
            else None
        ),
        "window_count": 0,
        "window_sessions": int(test_days),
        "top_k": int(top_k),
        "holding_sessions": int(holding_sessions),
        "calendar_snapshot": None,
        "data_quality_assessment": (
            dict(data_quality_assessment)
            if isinstance(data_quality_assessment, Mapping)
            else None
        ),
        "locking_protocol": (
            dict(locking_protocol)
            if isinstance(locking_protocol, Mapping)
            else None
        ),
        "observation_cutoff": None,
        "pending_session_dates": [],
        "session_dates": [],
        "windows": [],
        "trades": [],
        "attempts": [],
    }
    empty_portfolio = simulate_portfolio(
        [],
        top_k=top_k,
        holding_sessions=holding_sessions,
    )
    if not registration.get("ok"):
        return _seal_preregistered_oos_evidence(
            {
                **base,
                "reason": "preregistration_record_unavailable",
                "portfolio": empty_portfolio,
                "gated_portfolio": empty_portfolio,
            }
        )
    oos_not_before = _strict_iso_date(record.get("oos_not_before"))
    if oos_not_before is None:
        return _seal_preregistered_oos_evidence(
            {
                **base,
                "reason": "preregistration_oos_start_invalid",
                "portfolio": empty_portfolio,
                "gated_portfolio": empty_portfolio,
            }
        )
    eligible_dates = sorted(
        str(value)
        for value in data.get(
            "trade_date",
            pd.Series(dtype="string"),
        ).dropna().astype(str).unique()
        if str(value) >= oos_not_before
    )
    if not eligible_dates:
        return _seal_preregistered_oos_evidence(
            {
                **base,
                "reason": "no_complete_preregistered_oos_window",
                "portfolio": empty_portfolio,
                "gated_portfolio": empty_portfolio,
            }
        )
    observation_cutoff = eligible_dates[-1]
    initial_calendar_snapshot = _build_evidence_calendar_snapshot(
        oos_not_before,
        observation_cutoff,
    )
    if initial_calendar_snapshot is None:
        return _seal_preregistered_oos_evidence(
            {
                **base,
                "observation_cutoff": observation_cutoff,
                "reason": "authoritative_trade_calendar_unavailable",
                "portfolio": empty_portfolio,
                "gated_portfolio": empty_portfolio,
            }
        )
    authoritative_sessions = _calendar_snapshot_sessions(
        initial_calendar_snapshot
    )
    if any(value not in authoritative_sessions for value in eligible_dates):
        return _seal_preregistered_oos_evidence(
            {
                **base,
                "observation_cutoff": observation_cutoff,
                "reason": "non_trading_oos_observation",
                "portfolio": empty_portfolio,
                "gated_portfolio": empty_portfolio,
            }
        )
    complete_count = (
        len(authoritative_sessions) // int(test_days)
    ) * int(
        test_days
    )
    session_dates = authoritative_sessions[:complete_count]
    pending_session_dates = authoritative_sessions[complete_count:]
    base["observation_cutoff"] = observation_cutoff
    base["pending_session_dates"] = pending_session_dates
    if not session_dates:
        return _seal_preregistered_oos_evidence(
            {
                **base,
                "reason": "no_complete_preregistered_oos_window",
                "portfolio": empty_portfolio,
                "gated_portfolio": empty_portfolio,
            }
        )

    selected_gate = dict(
        model.get("selected_market_gate") or {"enabled": False}
    )
    attempts: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    for trade_date in session_dates:
        day = data[data["trade_date"].astype(str) == trade_date].copy()
        if day.empty:
            trades.append(
                {
                    "trade_date": trade_date,
                    "codes": [],
                    "return_pct": None,
                    "live_adjusted_return_pct": None,
                    "research_evaluable": False,
                    "skip_reason": "missing_oos_session",
                    "gate_pass": False,
                    "model_sha256": base["model_sha256"],
                }
            )
            continue
        if (
            "market_data_trusted" in day.columns
            and not bool(
                day["market_data_trusted"].map(_is_trusted_marker).all()
            )
        ):
            trades.append(
                {
                    "trade_date": trade_date,
                    "codes": [],
                    "return_pct": None,
                    "live_adjusted_return_pct": None,
                    "research_evaluable": False,
                    "skip_reason": "untrusted_market_data",
                    "gate_pass": False,
                    "model_sha256": base["model_sha256"],
                }
            )
            continue
        try:
            day["factor_model_score"] = score_factor_model(
                day,
                dict(model),
                strict=True,
            )
        except ValueError as exc:
            return _seal_preregistered_oos_evidence(
                {
                    **base,
                    "reason": str(exc),
                    "portfolio": empty_portfolio,
                    "gated_portfolio": empty_portfolio,
                }
            )
        picks = day.sort_values(
            "factor_model_score",
            ascending=False,
        ).head(top_k)
        if picks.empty:
            trades.append(
                {
                    "trade_date": trade_date,
                    "codes": [],
                    "return_pct": None,
                    "live_adjusted_return_pct": None,
                    "research_evaluable": False,
                    "skip_reason": "no_candidates",
                    "gate_pass": False,
                    "model_sha256": base["model_sha256"],
                }
            )
            continue
        codes = picks["ts_code"].astype(str).tolist()
        if "research_evaluable" in picks.columns:
            evaluable = picks["research_evaluable"].map(
                _is_trusted_marker
            )
        else:
            evaluable = pd.Series(True, index=picks.index, dtype=bool)
        if not bool(evaluable.all()):
            trades.append(
                {
                    "trade_date": trade_date,
                    "codes": codes,
                    "return_pct": None,
                    "live_adjusted_return_pct": None,
                    "research_evaluable": False,
                    "skip_reason": "incomplete_oos_outcomes",
                    "gate_pass": False,
                    "model_sha256": base["model_sha256"],
                }
            )
            continue
        raw_return = round(
            float(picks["forward_return_pct"].mean()),
            4,
        )
        if "execution_blocked" in picks.columns:
            executable = picks[
                ~picks["execution_blocked"].map(_is_trusted_marker)
            ]
        else:
            executable = picks
        live_field = (
            "live_adjusted_return_pct"
            if "live_adjusted_return_pct" in executable.columns
            else "forward_return_pct"
        )
        live_return = (
            round(
                float(executable[live_field].sum()) / float(top_k),
                4,
            )
            if not executable.empty
            else 0.0
        )
        market_gate_pass, gate_reason = _gate_check(
            selected_gate,
            emotion_by_date.get(trade_date),
            emotion_delta_by_date.get(trade_date),
        )
        day_attempts = [
            _portfolio_attempt(row, gate_pass=market_gate_pass)
            for _, row in picks.iterrows()
        ]
        attempts.extend(day_attempts)
        trades.append(
            {
                "trade_date": trade_date,
                "codes": codes,
                "return_pct": raw_return,
                "live_adjusted_return_pct": live_return,
                "research_evaluable": True,
                "gate_pass": market_gate_pass,
                "gate_reason": gate_reason,
                "model_sha256": base["model_sha256"],
            }
        )

    calendar_end = max(
        [
            session_dates[-1],
            *[
                str(attempt.get("exit_date") or "")
                for attempt in attempts
            ],
        ]
    )
    calendar_snapshot = _build_evidence_calendar_snapshot(
        oos_not_before,
        calendar_end,
    )
    if calendar_snapshot is None:
        return _seal_preregistered_oos_evidence(
            {
                **base,
                "reason": "authoritative_trade_calendar_unavailable",
                "portfolio": empty_portfolio,
                "gated_portfolio": empty_portfolio,
            }
        )

    results = _build_preregistered_oos_results(
        session_dates=session_dates,
        trades=trades,
        attempts=attempts,
        model=model,
        test_days=test_days,
        top_k=top_k,
        holding_sessions=holding_sessions,
    )
    return _seal_preregistered_oos_evidence(
        {
            **base,
            "reason": "ok",
            "window_count": len(results["windows"]),
            "calendar_snapshot": calendar_snapshot,
            "observation_cutoff": observation_cutoff,
            "pending_session_dates": pending_session_dates,
            "session_dates": session_dates,
            "windows": results["windows"],
            "trades": trades,
            "attempts": attempts,
            "portfolio": results["portfolio"],
            "gated_portfolio": results["gated_portfolio"],
        }
    )


def walk_forward_relay(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    train_days: int = 60,
    test_days: int = 10,
    step_days: int | None = None,
    top_k: int = 5,
    market_gate: dict[str, Any] | None = None,
    nested_gate_selection: bool = True,
    locking_protocol: Mapping[str, Any] | None = None,
    data_quality_assessment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """滚动窗口回测。双轨输出：

    - 原轨（ungated）：每个测试日照旧成篮，与历史报告可比；
    - 门轨（gated_*）：仅统计通过市场状态门的日子——这才是建议实际执行的口径。
    每笔 trade 记录当日情绪/情绪变化/门判定，复盘归因不再需要回拼 features.csv。
    """
    if train_days <= 0 or test_days <= 0 or top_k <= 0:
        raise ValueError("train_days, test_days and top_k must be positive")
    if step_days is not None and int(step_days) < test_days:
        raise ValueError("step_days must be greater than or equal to test_days")
    data = _merge_research_universe(features, labels)
    registered_model = _fit_preregistered_frozen_model(
        data,
        market_gate=market_gate,
    )
    holding_sessions = 1
    if "holding_sessions" in data.columns:
        values = pd.to_numeric(data["holding_sessions"], errors="coerce").dropna()
        if not values.empty:
            holding_sessions = max(1, int(values.median()))
    elif "hold_days" in data.columns:
        values = pd.to_numeric(data["hold_days"], errors="coerce").dropna()
        if not values.empty:
            holding_sessions = max(1, int(values.median()) + 1)
    empty_portfolio = simulate_portfolio([], top_k=top_k, holding_sessions=holding_sessions)
    empty_summary = {
        **_trade_summary([]), **_live_summary_fields([], [], []),
        "window_count": 0,
        "positive_window_rate": 0.0,
        "ungated_positive_window_rate": 0.0,
        "gated_positive_window_rate": 0.0,
        **{f"gated_{k}": v for k, v in _trade_summary([]).items()},
        "gated_live_total_return_pct": 0.0, "gated_skipped_days": 0,
        "unevaluable_oos_days": 0,
        "unevaluable_oos_slots": 0,
        "untrusted_oos_sessions": 0,
        "market_gate_evidence_eligible": True,
        "market_gate_ineligible_window_count": 0,
        "market_gate_applied": bool(market_gate and market_gate.get("enabled")),
        **_portfolio_summary_fields(empty_portfolio),
    }
    if data.empty:
        empty_model = fit_factor_model(pd.DataFrame(), target_col="model_target_pct")
        return {
            "windows": [],
            "trades": [],
            "summary": empty_summary,
            "portfolio": empty_portfolio,
            "latest_model": empty_model,
            "registered_model": registered_model,
        }

    # 每个交易日的市场情绪与前日差（列缺失=门自动失效并如实标注）
    has_emotion = "market_emotion_score" in data.columns
    emo_by_date, delta_by_date, _trusted_by_date = (
        _market_emotion_context(data)
        if has_emotion
        else ({}, {}, {})
    )

    dates = sorted(data["trade_date"].unique().tolist())
    step = int(step_days) if step_days is not None else int(test_days)
    windows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    portfolio_attempts: list[dict[str, Any]] = []
    seen_trade_dates: set[str] = set()
    unevaluable_trade_dates: set[str] = set()
    unevaluable_slots: set[tuple[str, str]] = set()
    untrusted_trade_dates: set[str] = set()
    latest_model = fit_factor_model(pd.DataFrame(), target_col="model_target_pct")
    start = 0
    while start + train_days + test_days <= len(dates):
        raw_train_dates = dates[start : start + train_days]
        test_dates = dates[start + train_days : start + train_days + test_days]
        raw_train = data[data["trade_date"].isin(raw_train_dates)]
        trusted_train = raw_train
        if "market_data_trusted" in trusted_train.columns:
            trusted_train = trusted_train[
                trusted_train["market_data_trusted"].map(_is_trusted_marker)
            ]
        if "research_evaluable" in trusted_train.columns:
            trusted_train = trusted_train[
                trusted_train["research_evaluable"].map(
                    lambda value: bool(value)
                    if value is not None and not pd.isna(value)
                    else False
                )
            ]
        train = trusted_train
        if "exit_date" in trusted_train.columns:
            exits = pd.to_datetime(trusted_train["exit_date"], errors="coerce")
            test_start_ts = pd.Timestamp(test_dates[0])
            train = trusted_train[exits < test_start_ts]
        train_dates = sorted(train["trade_date"].astype(str).unique().tolist())
        raw_fit_dates, validation_dates = _fit_validation_dates(train_dates)
        raw_fit_frame = train[train["trade_date"].isin(raw_fit_dates)]
        fit_frame = raw_fit_frame
        if validation_dates and "exit_date" in raw_fit_frame.columns:
            fit_exits = pd.to_datetime(raw_fit_frame["exit_date"], errors="coerce")
            validation_start_ts = pd.Timestamp(validation_dates[0])
            fit_frame = raw_fit_frame[fit_exits < validation_start_ts]
        fit_dates = sorted(fit_frame["trade_date"].astype(str).unique().tolist())
        validation_frame = train[train["trade_date"].isin(validation_dates)]
        fit_label_max_exit = None
        if not fit_frame.empty and "exit_date" in fit_frame.columns:
            fit_exits = fit_frame["exit_date"].dropna().astype(str)
            fit_label_max_exit = fit_exits.max() if not fit_exits.empty else None
        selection = select_factor_model(
            fit_frame,
            validation_frame,
            target_col="model_target_pct",
        )
        if nested_gate_selection:
            gate_selection = _select_market_gate(
                validation_frame,
                selection["model"],
                configured_gate=market_gate,
                top_k=top_k,
                holding_sessions=holding_sessions,
            )
        else:
            selected_gate = dict(market_gate) if market_gate and market_gate.get("enabled") else {"enabled": False}
            gate_selection = {
                "selection_source": "fixed_configuration",
                "selected_gate": selected_gate,
                "selection_reason": "nested_selection_disabled",
                "minimum_improvement_pct": None,
                "candidates": [],
            }
        selected_market_gate = dict(gate_selection["selected_gate"])
        market_gate_evidence_eligible, market_gate_evidence_reason = (
            _market_gate_evidence_status(selected_market_gate, str(test_dates[0]))
        )
        selected_parameters = dict(selection["selected_parameters"])
        model = fit_factor_model(
            train,
            target_col="model_target_pct",
            max_factors=int(selected_parameters["max_factors"]),
            ridge_alpha=float(selected_parameters["ridge_alpha"]),
        )
        if len(train) >= 2:
            corr = train[["relay_score", "model_target_pct"]].corr().iloc[0, 1]
        else:
            corr = np.nan
        direction = 1 if pd.isna(corr) or corr >= 0 else -1
        refit_label_max_exit = None
        if not train.empty and "exit_date" in train.columns:
            refit_exits = train["exit_date"].dropna().astype(str)
            refit_label_max_exit = refit_exits.max() if not refit_exits.empty else None
        model.update(
            {
                "strategy_key": STRATEGY_KEY,
                "selection_source": selection["selection_source"],
                "selected_parameters": selected_parameters,
                "model_spec_protocol_version": selection[
                    "model_spec_protocol_version"
                ],
                "model_spec_calibrated_through": selection[
                    "model_spec_calibrated_through"
                ],
                "validation_accepted": bool(selection["validation_accepted"]),
                "validation_rejection_reason": selection["validation_rejection_reason"],
                "validation_stable": bool(selection["validation_stable"]),
                "validation_stability_reason": selection["validation_stability_reason"],
                "model_selection_candidates": selection["candidates"],
                "gate_selection": gate_selection,
                "selected_market_gate": selected_market_gate,
                "fit_start": fit_dates[0] if fit_dates else None,
                "fit_end": fit_dates[-1] if fit_dates else None,
                "validation_start": validation_dates[0] if validation_dates else None,
                "validation_end": validation_dates[-1] if validation_dates else None,
                "fit_sample_count": int(len(fit_frame)),
                "validation_sample_count": int(len(validation_frame)),
                "fit_label_max_exit": fit_label_max_exit,
                "selection_fit_start": fit_dates[0] if fit_dates else None,
                "selection_fit_end": fit_dates[-1] if fit_dates else None,
                "selection_fit_sample_count": int(len(fit_frame)),
                "selection_fit_label_max_exit": fit_label_max_exit,
                "validation_purged_date_count": int(len(raw_fit_dates) - len(fit_dates)),
                "validation_purged_sample_count": int(len(raw_fit_frame) - len(fit_frame)),
                "validation_purge_policy": "fit_exit_date_strictly_before_validation_start",
                "refit_policy": "fixed_parameters_on_full_purged_train",
                "refit_start": train_dates[0] if train_dates else None,
                "refit_end": train_dates[-1] if train_dates else None,
                "refit_sample_count": int(len(train)),
                "refit_label_max_exit": refit_label_max_exit,
                "fallback_direction": direction,
            }
        )
        latest_model = model
        fit_scores = score_factor_model(train, model)
        training_diagnostics = factor_diagnostics(
            train,
            fit_scores,
            target_col="model_target_pct",
        )
        validation_diagnostics = selection["validation_diagnostics"]
        window_returns: list[float] = []
        window_live_returns: list[float] = []
        window_blocked_counts: list[int] = []
        gated_window_returns: list[float] = []
        gated_window_live_returns: list[float] = []
        gated_skipped = 0
        window_attempts: list[dict[str, Any]] = []
        window_scored_rows: list[pd.DataFrame] = []
        picks_by_date: list[list[str]] = []
        window_unevaluable_days = 0
        window_unevaluable_slots = 0
        window_untrusted_sessions = 0
        for trade_date in test_dates:
            day = data[data["trade_date"] == trade_date].copy()
            if day.empty:
                continue
            if (
                "market_data_trusted" in day.columns
                and not bool(
                    day["market_data_trusted"].map(_is_trusted_marker).all()
                )
            ):
                trade_record = {
                    "trade_date": trade_date,
                    "direction": direction,
                    "codes": [],
                    "return_pct": None,
                    "raw_return_pct": None,
                    "live_adjusted_return_pct": None,
                    "executable_count": 0,
                    "blocked_count": 0,
                    "research_evaluable": False,
                    "skip_reason": "untrusted_market_data",
                    "gate_pass": False,
                    "gate_reason": "untrusted_market_data",
                    "market_gate_pass": False,
                    "model_validation_accepted": bool(
                        model.get("validation_stable")
                    ),
                }
                window_unevaluable_days += 1
                window_unevaluable_slots += int(top_k)
                window_untrusted_sessions += 1
                if str(trade_date) not in seen_trade_dates:
                    trades.append(trade_record)
                    seen_trade_dates.add(str(trade_date))
                    unevaluable_trade_dates.add(str(trade_date))
                    untrusted_trade_dates.add(str(trade_date))
                    unevaluable_slots.update(
                        (str(trade_date), f"__untrusted_slot_{slot}")
                        for slot in range(int(top_k))
                    )
                continue
            day["factor_model_score"] = score_factor_model(day, model)
            day["rank_score"] = day["factor_model_score"]
            if model.get("fallback"):
                day["rank_score"] = day["rank_score"] * direction
            window_scored_rows.append(day)
            picks = day.sort_values("rank_score", ascending=False).head(top_k)
            if picks.empty:
                continue
            codes = picks["ts_code"].astype(str).tolist()
            picks_by_date.append(codes)
            if "research_evaluable" in picks.columns:
                evaluable_mask = picks["research_evaluable"].map(
                    lambda value: bool(value)
                    if value is not None and not pd.isna(value)
                    else True
                )
            else:
                evaluable_mask = pd.Series(True, index=picks.index, dtype=bool)
            if not bool(evaluable_mask.all()):
                unevaluable = picks[~evaluable_mask]
                statuses = sorted(
                    {
                        str(value)
                        for value in unevaluable.get(
                            "label_status",
                            pd.Series("outcome_unavailable", index=unevaluable.index),
                        ).dropna()
                    }
                )
                skip_reason = statuses[0] if len(statuses) == 1 else "mixed_unavailable_outcomes"
                trade_record = {
                    "trade_date": trade_date,
                    "direction": direction,
                    "codes": codes,
                    "return_pct": None,
                    "raw_return_pct": None,
                    "live_adjusted_return_pct": None,
                    "executable_count": 0,
                    "blocked_count": 0,
                    "research_evaluable": False,
                    "skip_reason": skip_reason,
                    "gate_pass": False,
                    "gate_reason": "unevaluable_oos_data",
                    "market_gate_pass": False,
                    "model_validation_accepted": bool(model.get("validation_stable")),
                }
                window_unevaluable_days += 1
                window_unevaluable_slots += int(len(unevaluable))
                if str(trade_date) not in seen_trade_dates:
                    trades.append(trade_record)
                    seen_trade_dates.add(str(trade_date))
                    unevaluable_trade_dates.add(str(trade_date))
                    unevaluable_slots.update(
                        (str(trade_date), str(code))
                        for code in unevaluable["ts_code"].astype(str).tolist()
                    )
                continue
            ret = round(float(picks["forward_return_pct"].mean()), 4)
            if "execution_blocked" in picks.columns:
                executable = picks[~picks["execution_blocked"].map(lambda value: bool(value))]
            else:
                executable = picks
            blocked_count = int(len(picks) - len(executable))
            live_field = "live_adjusted_return_pct" if "live_adjusted_return_pct" in executable.columns else "forward_return_pct"
            live_ret = round(float(executable[live_field].sum()) / float(top_k), 4) if not executable.empty else 0.0
            emotion = emo_by_date.get(str(trade_date))
            delta = delta_by_date.get(str(trade_date))
            market_gate_pass, gate_reason = _gate_check(selected_market_gate, emotion, delta)
            validation_accepted = bool(model.get("validation_stable"))
            gate_pass = market_gate_pass and validation_accepted
            if not validation_accepted:
                gate_reason = str(
                    model.get("validation_stability_reason")
                    or model.get("validation_rejection_reason")
                    or "model_validation_rejected"
                )
            trade_record = {
                "trade_date": trade_date,
                "direction": direction,
                "codes": codes,
                "return_pct": ret,
                "raw_return_pct": ret,
                "live_adjusted_return_pct": live_ret,
                "executable_count": int(len(executable)),
                "blocked_count": blocked_count,
                "market_emotion_score": emotion,
                "emotion_delta": None if delta is None else round(delta, 4),
                "gate_pass": gate_pass,
                "gate_reason": gate_reason,
                "market_gate_pass": market_gate_pass,
                "model_validation_accepted": validation_accepted,
                "research_evaluable": True,
            }
            if str(trade_date) not in seen_trade_dates:
                trades.append(trade_record)
            window_returns.append(ret)
            window_live_returns.append(live_ret)
            window_blocked_counts.append(blocked_count)
            day_attempts = [_portfolio_attempt(row, gate_pass=gate_pass) for _, row in picks.iterrows()]
            window_attempts.extend(day_attempts)
            if str(trade_date) not in seen_trade_dates:
                portfolio_attempts.extend(day_attempts)
                seen_trade_dates.add(str(trade_date))
            if gate_pass:
                gated_window_returns.append(ret)
                gated_window_live_returns.append(live_ret)
            else:
                gated_skipped += 1
        ws = _trade_summary(window_returns)
        live_ws = _live_summary_fields(window_returns, window_live_returns, window_blocked_counts, top_k=top_k)
        gated_ws = {f"gated_{k}": v for k, v in _trade_summary(gated_window_returns).items()}
        window_ungated_attempts = [{**attempt, "gate_pass": True} for attempt in window_attempts]
        window_portfolio = simulate_portfolio(
            window_ungated_attempts,
            top_k=top_k,
            holding_sessions=holding_sessions,
        )
        gated_window_portfolio = simulate_portfolio(
            window_attempts,
            top_k=top_k,
            holding_sessions=holding_sessions,
        )
        live_ws.update(
            {
                "live_trade_count": window_portfolio["executed_trade_count"],
                "live_hit_rate": window_portfolio["hit_rate"],
                "live_avg_return_pct": window_portfolio["average_trade_return_pct"],
                "live_total_return_pct": window_portfolio["total_return_pct"],
                "live_max_drawdown_pct": window_portfolio["max_drawdown_pct"],
                "live_execution_block_rate": round(
                    window_portfolio["blocked_slots"] / window_portfolio["attempted_slots"], 4
                )
                if window_portfolio["attempted_slots"]
                else 0.0,
            }
        )
        retained_max_exit = None
        if not train.empty and "exit_date" in train.columns:
            retained_exits = train["exit_date"].dropna().astype(str)
            retained_max_exit = retained_exits.max() if not retained_exits.empty else None
        test_scored = pd.concat(window_scored_rows, ignore_index=True) if window_scored_rows else pd.DataFrame()
        test_diagnostics = factor_diagnostics(
            test_scored,
            test_scored.get("factor_model_score", pd.Series(dtype=float)),
            target_col="model_target_pct",
        )
        windows.append(
            {
                "train_start": train_dates[0] if train_dates else None,
                "train_end": train_dates[-1] if train_dates else None,
                "raw_train_start": raw_train_dates[0],
                "raw_train_end": raw_train_dates[-1],
                "purged_date_count": int(len(raw_train_dates) - len(train_dates)),
                "purged_sample_count": int(len(raw_train) - len(train)),
                "train_label_max_exit": retained_max_exit,
                "purge_policy": "train_exit_date_strictly_before_test_start",
                "test_start": test_dates[0],
                "test_end": test_dates[-1],
                "direction": direction,
                "train_corr": None if pd.isna(corr) else round(float(corr), 6),
                "model": model,
                "gate_selection": gate_selection,
                "selected_market_gate": selected_market_gate,
                "market_gate_evidence_eligible": market_gate_evidence_eligible,
                "market_gate_evidence_reason": market_gate_evidence_reason,
                "training_diagnostics": training_diagnostics,
                "validation_diagnostics": validation_diagnostics,
                "test_diagnostics": test_diagnostics,
                "selection_turnover": round(selection_turnover(picks_by_date, top_k=top_k), 4),
                **ws,
                **live_ws,
                **gated_ws,
                **_portfolio_summary_fields(window_portfolio),
                "gated_live_total_return_pct": gated_window_portfolio["total_return_pct"],
                "gated_live_max_drawdown_pct": gated_window_portfolio["max_drawdown_pct"],
                "gated_live_hit_rate": gated_window_portfolio["hit_rate"],
                "gated_live_trade_count": gated_window_portfolio["executed_trade_count"],
                "gated_skipped_days": gated_skipped,
                "unevaluable_oos_days": window_unevaluable_days,
                "unevaluable_oos_slots": window_unevaluable_slots,
                "untrusted_oos_sessions": window_untrusted_sessions,
            }
        )
        start += step
    evaluable_trades = [trade for trade in trades if trade.get("research_evaluable") is not False]
    raw_returns = [float(t["return_pct"]) for t in evaluable_trades]
    live_returns = [
        float(t.get("live_adjusted_return_pct", t["return_pct"]))
        for t in evaluable_trades
    ]
    blocked_counts = [int(t.get("blocked_count", 0)) for t in evaluable_trades]
    summary = _trade_summary(raw_returns)
    summary.update(_live_summary_fields(raw_returns, live_returns, blocked_counts, top_k=top_k))
    ungated_attempts = [{**attempt, "gate_pass": True} for attempt in portfolio_attempts]
    portfolio = simulate_portfolio(
        ungated_attempts,
        top_k=top_k,
        holding_sessions=holding_sessions,
    )
    gated_portfolio = simulate_portfolio(
        portfolio_attempts,
        top_k=top_k,
        holding_sessions=holding_sessions,
    )
    summary.update(_portfolio_summary_fields(portfolio))
    summary.update(
        {
            "live_trade_count": portfolio["executed_trade_count"],
            "live_hit_rate": portfolio["hit_rate"],
            "live_avg_return_pct": portfolio["average_trade_return_pct"],
            "live_total_return_pct": portfolio["total_return_pct"],
            "live_max_drawdown_pct": portfolio["max_drawdown_pct"],
            "live_execution_block_rate": round(
                portfolio["blocked_slots"] / portfolio["attempted_slots"], 4
            )
            if portfolio["attempted_slots"]
            else 0.0,
        }
    )
    summary["window_count"] = len(windows)
    summary["unevaluable_oos_days"] = len(unevaluable_trade_dates)
    summary["unevaluable_oos_slots"] = len(unevaluable_slots)
    summary["untrusted_oos_sessions"] = len(untrusted_trade_dates)
    summary["positive_window_rate"] = round(sum(1 for w in windows if w["total_return_pct"] > 0) / len(windows), 4) if windows else 0.0
    summary["ungated_positive_window_rate"] = summary["positive_window_rate"]
    summary["gated_positive_window_rate"] = (
        round(
            sum(1 for window in windows if float(window.get("gated_live_total_return_pct") or 0.0) > 0.0)
            / len(windows),
            4,
        )
        if windows
        else 0.0
    )
    factor_counts: dict[str, int] = {}
    parameter_counts: dict[str, int] = {}
    validation_accepted_count = 0
    selected_gate_count = 0
    for window in windows:
        window_model = window.get("model") or {}
        if window_model.get("validation_stable"):
            validation_accepted_count += 1
        for factor in window_model.get("selected_factors") or []:
            factor_counts[str(factor)] = factor_counts.get(str(factor), 0) + 1
        parameters = window_model.get("selected_parameters") or {}
        parameter_key = (
            f"max_factors={parameters.get('max_factors')},"
            f"ridge_alpha={parameters.get('ridge_alpha')}"
        )
        parameter_counts[parameter_key] = parameter_counts.get(parameter_key, 0) + 1
        if (window.get("selected_market_gate") or {}).get("enabled"):
            selected_gate_count += 1
    summary["model_validation_acceptance_rate"] = (
        round(validation_accepted_count / len(windows), 4)
        if windows
        else 0.0
    )
    summary["selected_factor_frequency"] = {
        factor: {
            "count": count,
            "rate": round(count / len(windows), 4) if windows else 0.0,
        }
        for factor, count in sorted(factor_counts.items())
    }
    summary["selected_parameter_frequency"] = {
        key: {
            "count": count,
            "rate": round(count / len(windows), 4) if windows else 0.0,
        }
        for key, count in sorted(parameter_counts.items())
    }
    summary["selected_market_gate_rate"] = (
        round(selected_gate_count / len(windows), 4)
        if windows
        else 0.0
    )
    ineligible_gate_windows = [
        window
        for window in windows
        if not bool(window.get("market_gate_evidence_eligible", True))
    ]
    summary["market_gate_evidence_eligible"] = not ineligible_gate_windows
    summary["market_gate_ineligible_window_count"] = len(ineligible_gate_windows)
    # 门轨总览
    gated_trades = [
        t
        for t in trades
        if t.get("research_evaluable") is not False and t.get("gate_pass")
    ]
    gated_raw = [float(t["return_pct"]) for t in gated_trades]
    gated_live = [float(t.get("live_adjusted_return_pct", t["return_pct"])) for t in gated_trades]
    summary.update({f"gated_{k}": v for k, v in _trade_summary(gated_raw).items()})
    summary["gated_live_total_return_pct"] = gated_portfolio["total_return_pct"]
    summary["gated_live_max_drawdown_pct"] = gated_portfolio["max_drawdown_pct"]
    summary["gated_live_hit_rate"] = gated_portfolio["hit_rate"]
    summary["gated_live_trade_count"] = gated_portfolio["executed_trade_count"]
    summary["gated_skipped_days"] = int(len(trades) - len(gated_trades))
    summary["market_gate_applied"] = bool(
        has_emotion
        and any(
            bool((window.get("selected_market_gate") or {}).get("enabled"))
            for window in windows
        )
    )
    summary["market_gate_selection_policy"] = (
        "chronological_validation_on_off"
        if nested_gate_selection
        else "fixed_configuration"
    )
    if market_gate and market_gate.get("enabled") and not has_emotion:
        summary["market_gate_note"] = "market_emotion_score column missing; gate inactive"
    registration = load_model_preregistration()
    preregistered_oos = _evaluate_preregistered_oos(
        data,
        registered_model,
        registration=registration,
        test_days=test_days,
        top_k=top_k,
        holding_sessions=holding_sessions,
        emotion_by_date=emo_by_date,
        emotion_delta_by_date=delta_by_date,
        locking_protocol=locking_protocol,
        data_quality_assessment=data_quality_assessment,
    )
    summary["model_spec_calibrated_through"] = MODEL_SPEC_CALIBRATED_THROUGH
    summary["preregistered_oos_window_count"] = int(
        preregistered_oos.get("window_count") or 0
    )
    return {
        "windows": windows,
        "trades": trades,
        "summary": summary,
        "portfolio": portfolio,
        "gated_portfolio": gated_portfolio,
        "preregistered_oos": preregistered_oos,
        "latest_model": latest_model,
        "registered_model": registered_model,
    }


def _bootstrap_total_return_ci(
    portfolio: dict[str, Any],
    *,
    samples: int = 1000,
    block_size: int = 5,
) -> dict[str, float | int | None]:
    curve = portfolio.get("equity_curve") if isinstance(portfolio, dict) else None
    values = np.array(
        [
            float(item.get("equity"))
            for item in (curve or [])
            if isinstance(item, dict) and item.get("equity") is not None
        ],
        dtype=float,
    )
    if len(values) < 21 or not np.isfinite(values).all() or np.any(values <= 0):
        return {"lower": None, "median": None, "upper": None, "samples": 0, "block_size": int(block_size)}

    returns = values[1:] / values[:-1] - 1.0
    n_returns = len(returns)
    block = max(1, min(int(block_size), n_returns))
    sample_count = max(100, int(samples))
    rng = np.random.default_rng(20260730)
    totals = np.empty(sample_count, dtype=float)
    max_start = n_returns - block
    for sample_index in range(sample_count):
        sampled = []
        while len(sampled) < n_returns:
            start = int(rng.integers(0, max_start + 1)) if max_start > 0 else 0
            sampled.extend(returns[start : start + block])
        sampled_returns = np.asarray(sampled[:n_returns], dtype=float)
        totals[sample_index] = (float(np.prod(1.0 + sampled_returns)) - 1.0) * 100.0

    lower, median, upper = np.percentile(totals, [2.5, 50.0, 97.5])
    return {
        "lower": round(float(lower), 4),
        "median": round(float(median), 4),
        "upper": round(float(upper), 4),
        "samples": sample_count,
        "block_size": block,
    }


def _registered_frozen_model_is_eligible(
    model: Any,
    registration: Mapping[str, Any],
) -> bool:
    if not registration.get("ok") or not isinstance(model, Mapping):
        return False
    try:
        registered_model = _registered_model_from_preregistration(
            registration
        )
        model_payload_matches = (
            factor_model_payload(model)
            == factor_model_payload(registered_model)
            and factor_model_digest(model)
            == registration.get("model_sha256")
        )
    except (TypeError, ValueError):
        return False
    if not model_payload_matches:
        return False
    record = registration["record"]
    cutoff = str(record["calibrated_through"])[:10]
    try:
        cutoff_date = pd.Timestamp(cutoff)
        refit_end = pd.Timestamp(model.get("refit_end"))
        refit_label_max_exit = pd.Timestamp(
            model.get("refit_label_max_exit")
        )
    except (TypeError, ValueError):
        return False
    if (
        pd.isna(cutoff_date)
        or pd.isna(refit_end)
        or pd.isna(refit_label_max_exit)
    ):
        return False
    return bool(
        model.get("training_scope")
        == "preregistered_calibration_only"
        and model.get("frozen_model_candidate_eligible") is True
        and model.get("fallback") is False
        and model.get("validation_accepted") is True
        and model.get("validation_stable") is True
        and model.get("model_spec_protocol_version")
        == record["protocol_version"]
        and model.get("model_spec_calibrated_through") == cutoff
        and model.get("preregistration_record_sha256")
        == registration.get("sha256")
        and model.get("preregistration_oos_not_before")
        == record["oos_not_before"]
        and model.get("registered_model_sha256")
        == registration.get("model_sha256")
        and model.get("calibration_data_match") is not False
        and bool(model.get("selected_factors"))
        and refit_end <= cutoff_date
        and refit_label_max_exit <= cutoff_date
    )


def _strict_iso_date(value: Any) -> str | None:
    text = str(value or "")
    if len(text) != 10:
        return None
    try:
        parsed = pd.Timestamp(text)
    except (TypeError, ValueError):
        return None
    if pd.isna(parsed) or parsed.strftime("%Y-%m-%d") != text:
        return None
    return text


def _positive_evidence_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return int(value)


_EVIDENCE_ATTEMPT_KEYS = frozenset(
    {
        "trade_date",
        "ts_code",
        "entry_date",
        "exit_date",
        "execution_blocked",
        "forward_return_pct",
        "live_adjusted_return_pct",
        "valuation_path",
        "exit_reason",
        "gate_pass",
    }
)
_EVIDENCE_PATH_POINT_KEYS = frozenset({"date", "return_pct"})
_EVIDENCE_BLOCKED_EXIT_REASONS = frozenset(
    {
        "entry_bar_unavailable",
        "entry_high_open",
        "entry_low_liquidity",
        "entry_near_limit_up",
        "entry_weak_open",
        "execution_blocked",
        "known_st_excluded",
        "real_amount_unavailable",
        "st_status_unknown",
    }
)
_EVIDENCE_REGULAR_EXIT_REASONS = frozenset(
    {
        "max_holding_period",
        "stop_loss",
        "stop_loss_gap",
        "take_profit",
        "take_profit_gap",
    }
)
_EVIDENCE_DEFERRED_EXIT_REASONS = frozenset(
    {
        "deferred_exit",
        "deferred_limit_down_exit",
        "deferred_missing_bar_exit",
    }
)
_EVIDENCE_TRADE_KEYS = frozenset(
    {
        "trade_date",
        "codes",
        "return_pct",
        "live_adjusted_return_pct",
        "research_evaluable",
        "gate_pass",
        "model_sha256",
    }
)
_MIN_EVIDENCE_RETURN_PCT = -100.0
_MAX_EVIDENCE_RETURN_PCT = 1000.0


def _finite_evidence_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _bounded_evidence_return(value: Any) -> float | None:
    number = _finite_evidence_number(value)
    if number is None or not (
        _MIN_EVIDENCE_RETURN_PCT
        <= number
        <= _MAX_EVIDENCE_RETURN_PCT
    ):
        return None
    return number


_EVIDENCE_CALENDAR_SNAPSHOT_KEYS = frozenset(
    {
        "snapshot_version",
        "source",
        "start_date",
        "end_date",
        "closed_days",
        "provenance",
        "snapshot_sha256",
    }
)
_EVIDENCE_CALENDAR_PROVENANCE_KEYS = frozenset(
    {
        "provenance_version",
        "source",
        "provider_mode",
        "fetched_at",
        "open_days_count",
        "open_days_sha256",
    }
)
_DATA_QUALITY_ASSESSMENT_KEYS = frozenset(
    {
        "assessment_version",
        "amount_data_coverage",
        "latest_amount_data_coverage",
        "st_status_coverage",
        "latest_st_status_coverage",
        "universe_eligibility_coverage",
        "latest_universe_eligibility_coverage",
        "panel_max_abs_close_return",
        "panel_stock_count",
        "universe_required_count",
        "status",
        "failure_reasons",
    }
)


def _calendar_snapshot_material(
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        key: value
        for key, value in snapshot.items()
        if key != "snapshot_sha256"
    }


def _calendar_year_open_days(
    year: int,
    closed_days: set[str],
) -> list[str]:
    current = date(year, 1, 1)
    end_date = date(year, 12, 31)
    open_days: list[str] = []
    while current <= end_date:
        text = current.isoformat()
        if current.weekday() < 5 and text not in closed_days:
            open_days.append(text)
        current += timedelta(days=1)
    return open_days


def _calendar_open_days_sha256(open_days: Sequence[str]) -> str:
    payload = json.dumps(
        sorted(str(value) for value in open_days),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize_calendar_provenance(
    raw: Any,
    *,
    year: int,
    closed_days: set[str],
) -> dict[str, Any] | None:
    if (
        not isinstance(raw, Mapping)
        or set(raw) != _EVIDENCE_CALENDAR_PROVENANCE_KEYS
        or raw.get("provenance_version")
        != "tushare_trade_cal_provenance_v1"
        or raw.get("source") != "tushare_trade_cal"
        or raw.get("provider_mode") not in {"replay", "sdk"}
    ):
        return None
    fetched_at = raw.get("fetched_at")
    if not isinstance(fetched_at, str) or not fetched_at.strip():
        return None
    try:
        parsed_fetched_at = datetime.fromisoformat(
            fetched_at.replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if parsed_fetched_at.tzinfo is None:
        return None
    open_days = _calendar_year_open_days(year, closed_days)
    open_days_count = raw.get("open_days_count")
    if (
        isinstance(open_days_count, bool)
        or not isinstance(open_days_count, int)
        or open_days_count != len(open_days)
        or raw.get("open_days_sha256")
        != _calendar_open_days_sha256(open_days)
    ):
        return None
    return {
        key: raw[key]
        for key in (
            "provenance_version",
            "source",
            "provider_mode",
            "fetched_at",
            "open_days_count",
            "open_days_sha256",
        )
    }


def _build_evidence_calendar_snapshot(
    start: str,
    end: str,
    *,
    closed_days_by_year: Mapping[str, Any] | None = None,
    provenance_by_year: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    start_text = _strict_iso_date(start)
    end_text = _strict_iso_date(end)
    if start_text is None or end_text is None:
        return None
    start_date = date.fromisoformat(start_text)
    end_date = date.fromisoformat(end_text)
    span = (end_date - start_date).days
    if span < 0 or span > 3_660:
        return None

    if closed_days_by_year is None or provenance_by_year is None:
        from market import trading_calendar

    normalized_closed: dict[str, list[str]] = {}
    normalized_provenance: dict[str, dict[str, Any]] = {}
    for year in range(start_date.year, end_date.year + 1):
        year_key = str(year)
        raw_days = (
            closed_days_by_year.get(year_key)
            if isinstance(closed_days_by_year, Mapping)
            else trading_calendar.closed_days(year)
        )
        if not isinstance(raw_days, (list, tuple, set, frozenset)):
            return None
        clean_days: set[str] = set()
        for raw_day in raw_days:
            day_text = _strict_iso_date(raw_day)
            if day_text is None:
                return None
            parsed_day = date.fromisoformat(day_text)
            if parsed_day.year != year or parsed_day.weekday() >= 5:
                return None
            clean_days.add(day_text)
        if not 5 <= len(clean_days) <= 40:
            return None
        normalized_closed[year_key] = sorted(clean_days)
        raw_provenance = (
            provenance_by_year.get(year_key)
            if isinstance(provenance_by_year, Mapping)
            else trading_calendar.calendar_provenance(year)
        )
        clean_provenance = _normalize_calendar_provenance(
            raw_provenance,
            year=year,
            closed_days=clean_days,
        )
        if clean_provenance is None:
            return None
        normalized_provenance[year_key] = clean_provenance

    material = {
        "snapshot_version": EVIDENCE_CALENDAR_SNAPSHOT_VERSION,
        "source": "tushare_trade_cal",
        "start_date": start_text,
        "end_date": end_text,
        "closed_days": normalized_closed,
        "provenance": normalized_provenance,
    }
    payload = json.dumps(
        material,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return {
        **material,
        "snapshot_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _calendar_snapshot_sessions(
    snapshot: Mapping[str, Any],
) -> list[str]:
    closed_days = {
        str(value)
        for values in snapshot["closed_days"].values()
        for value in values
    }
    current = date.fromisoformat(str(snapshot["start_date"]))
    end_date = date.fromisoformat(str(snapshot["end_date"]))
    sessions: list[str] = []
    while current <= end_date:
        text = current.isoformat()
        if current.weekday() < 5 and text not in closed_days:
            sessions.append(text)
        current += timedelta(days=1)
    return sessions


def _validate_evidence_calendar_snapshot(
    raw_snapshot: Any,
) -> tuple[dict[str, Any], list[str]] | None:
    if (
        not isinstance(raw_snapshot, Mapping)
        or set(raw_snapshot) != _EVIDENCE_CALENDAR_SNAPSHOT_KEYS
        or not isinstance(raw_snapshot.get("closed_days"), Mapping)
        or not isinstance(raw_snapshot.get("provenance"), Mapping)
    ):
        return None
    expected = _build_evidence_calendar_snapshot(
        str(raw_snapshot.get("start_date") or ""),
        str(raw_snapshot.get("end_date") or ""),
        closed_days_by_year=raw_snapshot["closed_days"],
        provenance_by_year=raw_snapshot["provenance"],
    )
    if expected is None or not _canonical_json_equal(
        dict(raw_snapshot),
        expected,
    ):
        return None
    from market import trading_calendar

    for year_key, closed_days in expected["closed_days"].items():
        authority = trading_calendar.authoritative_year(year_key)
        provenance = expected["provenance"].get(year_key)
        if (
            authority is None
            or not isinstance(provenance, Mapping)
            or frozenset(closed_days) != authority.closed_days
            or provenance.get("open_days_count")
            != authority.open_days_count
            or not isinstance(
                provenance.get("open_days_sha256"),
                str,
            )
            or not hmac.compare_digest(
                provenance["open_days_sha256"],
                authority.open_days_sha256,
            )
        ):
            return None
    return expected, _calendar_snapshot_sessions(expected)


def _quality_coverage(value: Any, field: str) -> float:
    number = _finite_evidence_number(value)
    if number is None or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field} must be between 0 and 1")
    return round(float(number), 4)


def _quality_count(value: Any, field: str, *, positive: bool) -> int:
    minimum = 1 if positive else 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= 20_000
    ):
        raise ValueError(f"{field} is out of range")
    return int(value)


def _build_data_quality_assessment(
    *,
    amount_data_coverage: Any,
    latest_amount_data_coverage: Any,
    st_status_coverage: Any,
    latest_st_status_coverage: Any,
    universe_eligibility_coverage: Any,
    latest_universe_eligibility_coverage: Any,
    panel_max_abs_close_return: Any,
    panel_stock_count: Any,
    universe_required_count: Any,
) -> dict[str, Any]:
    values = {
        "amount_data_coverage": _quality_coverage(
            amount_data_coverage,
            "amount_data_coverage",
        ),
        "latest_amount_data_coverage": _quality_coverage(
            latest_amount_data_coverage,
            "latest_amount_data_coverage",
        ),
        "st_status_coverage": _quality_coverage(
            st_status_coverage,
            "st_status_coverage",
        ),
        "latest_st_status_coverage": _quality_coverage(
            latest_st_status_coverage,
            "latest_st_status_coverage",
        ),
        "universe_eligibility_coverage": _quality_coverage(
            universe_eligibility_coverage,
            "universe_eligibility_coverage",
        ),
        "latest_universe_eligibility_coverage": _quality_coverage(
            latest_universe_eligibility_coverage,
            "latest_universe_eligibility_coverage",
        ),
        "panel_stock_count": _quality_count(
            panel_stock_count,
            "panel_stock_count",
            positive=False,
        ),
        "universe_required_count": _quality_count(
            universe_required_count,
            "universe_required_count",
            positive=True,
        ),
    }
    max_return = _finite_evidence_number(panel_max_abs_close_return)
    if max_return is None or not 0.0 <= max_return <= 100.0:
        raise ValueError("panel_max_abs_close_return is out of range")
    values["panel_max_abs_close_return"] = round(
        float(max_return),
        6,
    )

    failure_reasons: list[str] = []
    if (
        values["amount_data_coverage"] < MIN_REAL_AMOUNT_COVERAGE
        or values["latest_amount_data_coverage"]
        < MIN_REAL_AMOUNT_COVERAGE
    ):
        failure_reasons.append("real_amount_coverage_below_minimum")
    if (
        values["st_status_coverage"] < 0.98
        or values["latest_st_status_coverage"] < 0.98
    ):
        failure_reasons.append("st_status_coverage_below_minimum")
    if (
        values["universe_eligibility_coverage"] < 0.9999
        or values["latest_universe_eligibility_coverage"] < 0.9999
        or values["panel_stock_count"]
        < values["universe_required_count"]
    ):
        failure_reasons.append("universe_coverage_below_minimum")
    if values["panel_max_abs_close_return"] > MAX_ABS_DAILY_RETURN:
        failure_reasons.append("discontinuous_price_history_detected")
    return {
        "assessment_version": DATA_QUALITY_ASSESSMENT_VERSION,
        **values,
        "status": "ok" if not failure_reasons else "degraded",
        "failure_reasons": failure_reasons,
    }


def _validate_data_quality_assessment(
    raw_assessment: Any,
) -> dict[str, Any] | None:
    if (
        not isinstance(raw_assessment, Mapping)
        or set(raw_assessment) != _DATA_QUALITY_ASSESSMENT_KEYS
    ):
        return None
    try:
        expected = _build_data_quality_assessment(
            **{
                key: raw_assessment[key]
                for key in (
                    "amount_data_coverage",
                    "latest_amount_data_coverage",
                    "st_status_coverage",
                    "latest_st_status_coverage",
                    "universe_eligibility_coverage",
                    "latest_universe_eligibility_coverage",
                    "panel_max_abs_close_return",
                    "panel_stock_count",
                    "universe_required_count",
                )
            }
        )
    except (TypeError, ValueError):
        return None
    return (
        expected
        if _canonical_json_equal(dict(raw_assessment), expected)
        else None
    )


def _canonical_json_equal(left: Any, right: Any) -> bool:
    try:
        return json.dumps(
            left,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=lambda value: value.item(),
        ) == json.dumps(
            right,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=lambda value: value.item(),
        )
    except (TypeError, ValueError, AttributeError):
        return False


def _validate_preregistered_oos_evidence(
    evidence: Mapping[str, Any],
    *,
    model: Any,
    registration: Mapping[str, Any],
) -> dict[str, Any]:
    invalid = {
        "ok": False,
        "session_dates": [],
        "windows": [],
        "portfolio": {},
        "gated_portfolio": {},
        "calendar_snapshot": {},
        "data_quality_assessment": {},
    }
    if (
        not registration.get("ok")
        or not isinstance(model, Mapping)
        or not _verify_preregistered_oos_evidence_seal(evidence)
    ):
        return invalid

    top_k = _positive_evidence_int(evidence.get("top_k"))
    holding_sessions = _positive_evidence_int(
        evidence.get("holding_sessions")
    )
    window_sessions = _positive_evidence_int(
        evidence.get("window_sessions")
    )
    if top_k is None or holding_sessions is None or window_sessions is None:
        return invalid

    raw_locking_protocol = evidence.get("locking_protocol")
    if (
        not isinstance(raw_locking_protocol, Mapping)
        or raw_locking_protocol.get("protocol_version")
        != LOCKING_PROTOCOL_VERSION
    ):
        return invalid
    try:
        locking_protocol = _build_locking_protocol(
            config=raw_locking_protocol.get("config"),
            mining_rules=raw_locking_protocol.get("mining_rules"),
            execution_rules=raw_locking_protocol.get("execution_rules"),
        )
    except (TypeError, ValueError):
        return invalid
    locking_config = locking_protocol["config"]
    if (
        not _canonical_json_equal(
            raw_locking_protocol,
            locking_protocol,
        )
        or locking_config["top_k"] != top_k
        or locking_config["test_days"] != window_sessions
        or locking_config["hold_days"] + 1 != holding_sessions
    ):
        return invalid
    maximum_holding_sessions = (
        holding_sessions + MAX_DEFERRED_EXIT_SESSIONS
    )
    if (
        locking_protocol["backtest_policy"].get(
            "maximum_deferred_exit_sessions"
        )
        != MAX_DEFERRED_EXIT_SESSIONS
    ):
        return invalid

    data_quality_assessment = _validate_data_quality_assessment(
        evidence.get("data_quality_assessment")
    )
    calendar_validation = _validate_evidence_calendar_snapshot(
        evidence.get("calendar_snapshot")
    )
    if data_quality_assessment is None or calendar_validation is None:
        return invalid
    calendar_snapshot, calendar_sessions = calendar_validation
    calendar_index = {
        value: index
        for index, value in enumerate(calendar_sessions)
    }

    raw_dates = evidence.get("session_dates")
    raw_pending_dates = evidence.get("pending_session_dates")
    raw_windows = evidence.get("windows")
    raw_trades = evidence.get("trades")
    raw_attempts = evidence.get("attempts")
    if not all(
        isinstance(value, list)
        for value in (
            raw_dates,
            raw_pending_dates,
            raw_windows,
            raw_trades,
            raw_attempts,
        )
    ):
        return invalid
    session_dates = [_strict_iso_date(value) for value in raw_dates]
    pending_session_dates = [
        _strict_iso_date(value)
        for value in raw_pending_dates
    ]
    if (
        not session_dates
        or
        any(value is None for value in session_dates)
        or any(value is None for value in pending_session_dates)
        or session_dates != sorted(session_dates)
        or pending_session_dates != sorted(pending_session_dates)
        or len(session_dates) != len(set(session_dates))
        or len(pending_session_dates) != len(
            set(pending_session_dates)
        )
        or len(session_dates) % window_sessions != 0
        or len(pending_session_dates) >= window_sessions
    ):
        return invalid
    session_dates = [str(value) for value in session_dates]
    pending_session_dates = [
        str(value) for value in pending_session_dates
    ]
    oos_not_before = _strict_iso_date(
        registration["record"].get("oos_not_before")
    )
    observation_cutoff = _strict_iso_date(
        evidence.get("observation_cutoff")
    )
    if (
        oos_not_before is None
        or evidence.get("oos_not_before") != oos_not_before
        or observation_cutoff is None
        or not isinstance(evidence.get("observation_cutoff"), str)
        or calendar_snapshot.get("start_date") != oos_not_before
        or observation_cutoff not in calendar_index
    ):
        return invalid
    expected_session_dates = [
        value
        for value in calendar_sessions
        if value <= observation_cutoff
    ]
    if (
        expected_session_dates
        != session_dates + pending_session_dates
        or set(session_dates).intersection(pending_session_dates)
    ):
        return invalid

    expected_model_sha256 = registration.get("model_sha256")
    try:
        actual_model_sha256 = factor_model_digest(model)
    except (TypeError, ValueError, AttributeError):
        return invalid
    if (
        evidence.get("model_sha256") != expected_model_sha256
        or actual_model_sha256 != expected_model_sha256
    ):
        return invalid

    attempts: list[dict[str, Any]] = []
    attempts_by_date: dict[str, list[dict[str, Any]]] = {
        value: [] for value in session_dates
    }
    attempt_keys: set[tuple[str, str]] = set()
    for raw_attempt in raw_attempts:
        if (
            not isinstance(raw_attempt, Mapping)
            or set(raw_attempt) != _EVIDENCE_ATTEMPT_KEYS
        ):
            return invalid
        attempt = dict(raw_attempt)
        trade_date = _strict_iso_date(attempt.get("trade_date"))
        entry_date = _strict_iso_date(attempt.get("entry_date"))
        exit_date = _strict_iso_date(attempt.get("exit_date"))
        raw_code = attempt.get("ts_code")
        code = raw_code.strip() if isinstance(raw_code, str) else ""
        if (
            trade_date not in attempts_by_date
            or entry_date is None
            or exit_date is None
            or not code
            or not all(
                isinstance(attempt.get(key), str)
                for key in ("trade_date", "entry_date", "exit_date")
            )
            or entry_date <= trade_date
            or exit_date < entry_date
            or not isinstance(attempt.get("execution_blocked"), bool)
            or not isinstance(attempt.get("gate_pass"), bool)
            or not isinstance(attempt.get("exit_reason"), str)
            or not str(attempt.get("exit_reason") or "").strip()
            or attempt.get("exit_reason")
            != str(attempt.get("exit_reason")).strip()
        ):
            return invalid
        trade_calendar_index = calendar_index.get(trade_date)
        entry_calendar_index = calendar_index.get(entry_date)
        exit_calendar_index = calendar_index.get(exit_date)
        if (
            trade_calendar_index is None
            or entry_calendar_index is None
            or exit_calendar_index is None
            or entry_calendar_index != trade_calendar_index + 1
            or exit_calendar_index < entry_calendar_index
        ):
            return invalid
        holding_dates = calendar_sessions[
            entry_calendar_index : exit_calendar_index + 1
        ]
        if len(holding_dates) > maximum_holding_sessions:
            return invalid
        key = (trade_date, code)
        if key in attempt_keys:
            return invalid
        attempt_keys.add(key)
        live_return = _bounded_evidence_return(
            attempt.get("live_adjusted_return_pct")
        )
        forward_return = _bounded_evidence_return(
            attempt.get("forward_return_pct")
        )
        if live_return is None or forward_return is None:
            return invalid
        path = attempt.get("valuation_path")
        execution_blocked = attempt["execution_blocked"]
        if execution_blocked:
            if (
                exit_date != entry_date
                or path != []
                or attempt["exit_reason"]
                not in _EVIDENCE_BLOCKED_EXIT_REASONS
                or not np.isclose(
                    live_return,
                    0.0,
                    rtol=0.0,
                    atol=1e-12,
                )
            ):
                return invalid
            attempts.append(attempt)
            attempts_by_date[trade_date].append(attempt)
            continue
        if (
            not isinstance(path, list)
            or not path
            or len(path) > maximum_holding_sessions
        ):
            return invalid
        is_deferred = len(holding_dates) > holding_sessions
        if (
            (
                is_deferred
                and attempt["exit_reason"]
                not in _EVIDENCE_DEFERRED_EXIT_REASONS
            )
            or (
                not is_deferred
                and attempt["exit_reason"]
                not in _EVIDENCE_REGULAR_EXIT_REASONS
            )
        ):
            return invalid
        path_dates: list[str] = []
        path_returns: list[float] = []
        for raw_point in path:
            if (
                not isinstance(raw_point, Mapping)
                or set(raw_point) != _EVIDENCE_PATH_POINT_KEYS
            ):
                return invalid
            path_date = _strict_iso_date(raw_point.get("date"))
            path_return = _bounded_evidence_return(
                raw_point.get("return_pct")
            )
            if (
                path_date is None
                or not isinstance(raw_point.get("date"), str)
                or path_return is None
            ):
                return invalid
            path_dates.append(path_date)
            path_returns.append(path_return)
        if (
            path_dates != sorted(path_dates)
            or len(path_dates) != len(set(path_dates))
            or path_dates != holding_dates
            or path_dates[0] != entry_date
            or path_dates[-1] != exit_date
            or not np.isclose(
                path_returns[-1],
                live_return,
                rtol=0.0,
                atol=1e-9,
            )
        ):
            return invalid
        attempts.append(attempt)
        attempts_by_date[trade_date].append(attempt)

    trades: list[dict[str, Any]] = []
    trades_by_date: dict[str, dict[str, Any]] = {}
    for raw_trade in raw_trades:
        if not isinstance(raw_trade, Mapping):
            return invalid
        trade = dict(raw_trade)
        evaluable = trade.get("research_evaluable")
        if not isinstance(evaluable, bool):
            return invalid
        reason_key = "gate_reason" if evaluable else "skip_reason"
        if set(trade) != _EVIDENCE_TRADE_KEYS | {reason_key}:
            return invalid
        trade_date = _strict_iso_date(trade.get("trade_date"))
        codes = trade.get("codes")
        gate_pass = trade.get("gate_pass")
        if (
            trade_date not in attempts_by_date
            or not isinstance(trade.get("trade_date"), str)
            or trade_date in trades_by_date
            or not isinstance(codes, list)
            or any(
                not isinstance(code, str)
                or not code.strip()
                or code != code.strip()
                for code in codes
            )
            or len(codes) != len(set(codes))
            or not isinstance(gate_pass, bool)
            or trade.get("model_sha256") != expected_model_sha256
            or not isinstance(trade.get(reason_key), str)
            or not trade.get(reason_key)
        ):
            return invalid
        day_attempts = attempts_by_date[trade_date]
        attempt_codes = [
            str(attempt.get("ts_code") or "")
            for attempt in day_attempts
        ]
        if evaluable:
            raw_return = _bounded_evidence_return(
                trade.get("return_pct")
            )
            live_return = _bounded_evidence_return(
                trade.get("live_adjusted_return_pct")
            )
            expected_raw_return = round(
                sum(
                    float(attempt["forward_return_pct"])
                    for attempt in day_attempts
                )
                / float(top_k),
                4,
            )
            expected_live_return = round(
                sum(
                    float(attempt["live_adjusted_return_pct"])
                    for attempt in day_attempts
                    if not attempt["execution_blocked"]
                )
                / float(top_k),
                4,
            )
            if (
                raw_return is None
                or live_return is None
                or len(codes) != top_k
                or len(day_attempts) != top_k
                or codes != attempt_codes
                or any(
                    attempt.get("gate_pass") != gate_pass
                    for attempt in day_attempts
                )
                or not np.isclose(
                    raw_return,
                    expected_raw_return,
                    rtol=0.0,
                    atol=1e-9,
                )
                or not np.isclose(
                    live_return,
                    expected_live_return,
                    rtol=0.0,
                    atol=1e-9,
                )
            ):
                return invalid
        elif (
            day_attempts
            or len(codes) not in {0, top_k}
            or trade.get("return_pct") is not None
            or trade.get("live_adjusted_return_pct") is not None
            or gate_pass is not False
        ):
            return invalid
        trades_by_date[trade_date] = trade
        trades.append(trade)
    if sorted(trades_by_date) != session_dates:
        return invalid

    results = _build_preregistered_oos_results(
        session_dates=session_dates,
        trades=trades,
        attempts=attempts,
        model=model,
        test_days=window_sessions,
        top_k=top_k,
        holding_sessions=holding_sessions,
    )
    window_count = evidence.get("window_count")
    if (
        isinstance(window_count, bool)
        or not isinstance(window_count, int)
        or window_count != len(results["windows"])
        or not _canonical_json_equal(raw_windows, results["windows"])
        or not _canonical_json_equal(
            evidence.get("portfolio"),
            results["portfolio"],
        )
        or not _canonical_json_equal(
            evidence.get("gated_portfolio"),
            results["gated_portfolio"],
        )
    ):
        return invalid
    return {
        "ok": True,
        "session_dates": session_dates,
        "windows": results["windows"],
        "portfolio": results["portfolio"],
        "gated_portfolio": results["gated_portfolio"],
        "locking_protocol": locking_protocol,
        "calendar_snapshot": calendar_snapshot,
        "data_quality_assessment": data_quality_assessment,
    }


def assess_research_result(
    walk_forward: dict[str, Any],
    *,
    prospective_sessions: int = 0,
) -> dict[str, Any]:
    """Assess historical evidence without allowing it to grant deployment rights."""
    registration = load_model_preregistration()
    preregistered_supplied = "preregistered_oos" in walk_forward
    preregistered = walk_forward.get("preregistered_oos")
    preregistered = (
        preregistered if isinstance(preregistered, Mapping) else {}
    )
    registered_model = walk_forward.get("registered_model")
    try:
        evidence_validation = _validate_preregistered_oos_evidence(
            preregistered,
            model=registered_model,
            registration=registration,
        )
    except (TypeError, ValueError, KeyError):
        evidence_validation = {
            "ok": False,
            "session_dates": [],
            "windows": [],
            "portfolio": {},
            "gated_portfolio": {},
            "calendar_snapshot": {},
            "data_quality_assessment": {},
        }
    raw_windows = evidence_validation["windows"]
    expected_model_sha256 = registration.get("model_sha256")
    windows = [
        window
        for window in raw_windows
        if isinstance(window, Mapping)
        and _is_preregistered_oos_window(window)
        and window.get("model_sha256") == expected_model_sha256
    ]
    session_dates = evidence_validation["session_dates"]
    data_quality_assessment = evidence_validation.get(
        "data_quality_assessment"
    )
    data_quality_assessment = (
        data_quality_assessment
        if isinstance(data_quality_assessment, Mapping)
        else {}
    )
    preregistered_window_count = preregistered.get("window_count")
    preregistered_matches = (
        registration.get("ok")
        and evidence_validation["ok"]
        and preregistered.get("protocol_version")
        == registration["record"]["protocol_version"]
        and preregistered.get("calibrated_through")
        == registration["record"]["calibrated_through"]
        and preregistered.get("oos_not_before")
        == registration["record"]["oos_not_before"]
        and preregistered.get("registration_sha256")
        == registration.get("sha256")
        and preregistered.get("model_sha256")
        == expected_model_sha256
        and isinstance(preregistered_window_count, int)
        and not isinstance(preregistered_window_count, bool)
        and preregistered_window_count == len(windows)
        and len(windows) == len(raw_windows)
        and len(session_dates) == len(set(session_dates))
        and all(
            value
            >= str(registration["record"]["oos_not_before"])[:10]
            for value in session_dates
        )
    )
    if preregistered_matches:
        ungated_portfolio = evidence_validation["portfolio"]
        gated_portfolio = evidence_validation["gated_portfolio"]
    else:
        ungated_portfolio = {}
        gated_portfolio = {}
    frozen_model_eligible = _registered_frozen_model_is_eligible(
        registered_model,
        registration,
    )
    gate_evidence_eligible = all(
        bool(window.get("market_gate_evidence_eligible", True))
        for window in windows
    )
    if gate_evidence_eligible:
        portfolio = gated_portfolio or ungated_portfolio
        assessment_portfolio_source = "gated"
        window_return_field = "gated_live_total_return_pct"
    else:
        portfolio = ungated_portfolio
        assessment_portfolio_source = "ungated_due_to_gate_provenance"
        window_return_field = "live_total_return_pct"
    window_returns = [
        float(window.get(window_return_field, window.get("total_return_pct", 0.0)) or 0.0)
        for window in windows
    ]
    positive_window_rate = (
        sum(1 for value in window_returns if value > 0.0) / len(window_returns)
        if window_returns
        else 0.0
    )
    validation_flags = [
        bool(window.get("model_validation_stable"))
        for window in windows
    ]
    validation_acceptance_rate = (
        sum(validation_flags) / len(validation_flags)
        if validation_flags
        else 0.0
    )
    total_return = float(portfolio.get("total_return_pct") or 0.0)
    max_drawdown = float(portfolio.get("max_drawdown_pct") or 0.0)
    executed_trades = int(portfolio.get("executed_trade_count") or 0)
    confidence_interval = _bootstrap_total_return_ci(portfolio)
    lower_bound = confidence_interval.get("lower")
    rules = RESEARCH_VALIDATION_RULES
    unevaluable_oos_days = sum(
        int(window.get("unevaluable_oos_days") or 0)
        for window in windows
    )
    unevaluable_oos_slots = sum(
        int(window.get("unevaluable_oos_slots") or 0)
        for window in windows
    )

    failure_reasons = []
    if not registration.get("ok"):
        failure_reasons.append(
            str(
                registration.get("reason")
                or "preregistration_record_unavailable"
            )
        )
    if not preregistered_supplied or not preregistered_matches:
        failure_reasons.append("preregistered_oos_evidence_mismatch")
    if (
        evidence_validation["ok"]
        and data_quality_assessment.get("status") != "ok"
    ):
        failure_reasons.extend(
            str(value)
            for value in (
                data_quality_assessment.get("failure_reasons") or []
            )
            if str(value)
        )
    if not frozen_model_eligible:
        failure_reasons.append("preregistered_frozen_model_unavailable")
    if not windows:
        failure_reasons.append("no_preregistered_oos_windows")
    if not gate_evidence_eligible:
        failure_reasons.append("market_gate_historical_calibration_overlap")
    if unevaluable_oos_days or unevaluable_oos_slots:
        failure_reasons.append("incomplete_oos_outcomes")
    if len(windows) < int(rules["minimum_oos_windows"]):
        failure_reasons.append("insufficient_oos_windows")
    if total_return <= 0.0:
        failure_reasons.append("non_positive_oos_return")
    if max_drawdown < float(rules["maximum_drawdown_pct"]):
        failure_reasons.append("drawdown_limit_exceeded")
    if positive_window_rate < float(rules["minimum_positive_window_rate"]):
        failure_reasons.append("positive_window_rate_below_minimum")
    if validation_acceptance_rate < float(rules["minimum_validation_acceptance_rate"]):
        failure_reasons.append("validation_acceptance_rate_below_minimum")
    if executed_trades < int(rules["minimum_executed_trades"]):
        failure_reasons.append("insufficient_executed_trades")
    if lower_bound is None:
        failure_reasons.append("bootstrap_confidence_unavailable")
    elif float(lower_bound) <= float(rules["minimum_bootstrap_lower_return_pct"]):
        failure_reasons.append("bootstrap_lower_bound_not_positive")

    metric_gate_passed = not failure_reasons
    required_sessions = int(rules["prospective_sessions_required"])
    research_status = "exploratory" if metric_gate_passed else "failed"

    return {
        "research_status": research_status,
        "metric_gate_passed": metric_gate_passed,
        "lock_eligible": metric_gate_passed,
        "deployment_eligible": False,
        "research_only": True,
        "oos_consumed": bool(windows),
        "model_spec_protocol_version": MODEL_SPEC_PROTOCOL_VERSION,
        "model_spec_calibrated_through": MODEL_SPEC_CALIBRATED_THROUGH,
        "preregistration_record_sha256": registration.get("sha256"),
        "preregistration_oos_not_before": (
            registration["record"].get("oos_not_before")
            if registration.get("ok")
            else None
        ),
        "preregistered_frozen_model_eligible": frozen_model_eligible,
        "preregistered_oos_evidence_verified": bool(
            evidence_validation["ok"]
        ),
        "data_quality_assessment_verified": bool(
            evidence_validation["ok"]
            and data_quality_assessment
        ),
        "data_quality_status": data_quality_assessment.get("status"),
        "market_gate_evidence_eligible": gate_evidence_eligible,
        "assessment_portfolio_source": assessment_portfolio_source,
        "failure_reasons": failure_reasons,
        "oos_window_count": len(windows),
        "positive_window_rate": round(float(positive_window_rate), 4),
        "validation_acceptance_rate": round(float(validation_acceptance_rate), 4),
        "oos_total_return_pct": round(total_return, 4),
        "oos_max_drawdown_pct": round(max_drawdown, 4),
        "executed_trade_count": executed_trades,
        "unevaluable_oos_days": unevaluable_oos_days,
        "unevaluable_oos_slots": unevaluable_oos_slots,
        "bootstrap_total_return_ci_pct": confidence_interval,
        "prospective_sessions_observed": 0,
        "prospective_sessions_required": required_sessions,
        "prospective_sessions_source": "frozen_experiment_store_only",
        "thresholds": dict(rules),
    }


def build_frozen_experiment_manifest(report: dict[str, Any]) -> dict[str, Any]:
    """Build a lock manifest only from a trusted, server-produced research report."""
    if not isinstance(report, dict):
        raise ValueError("research report is required")
    if str(report.get("data_status") or report.get("status") or "") != "ok":
        raise ValueError("research report data is not trusted")
    assessment = report.get("research_assessment")
    if (
        not isinstance(assessment, dict)
        or assessment.get("metric_gate_passed") is not True
        or assessment.get("lock_eligible") is not True
    ):
        raise ValueError("historical research gate did not pass")
    registration = load_model_preregistration()
    model = report.get("model")
    if not _registered_frozen_model_is_eligible(model, registration):
        raise ValueError(
            "preregistered frozen factor model is unavailable"
        )
    raw_preregistered_oos = report.get("preregistered_oos")
    preregistered_oos = (
        raw_preregistered_oos
        if isinstance(raw_preregistered_oos, Mapping)
        else {}
    )
    recomputed_assessment = assess_research_result(
        {
            "windows": report.get("windows") or [],
            "portfolio": report.get("portfolio") or {},
            "gated_portfolio": report.get("gated_portfolio") or {},
            "preregistered_oos": preregistered_oos,
            "registered_model": model,
        }
    )
    if not bool(
        recomputed_assessment.get("metric_gate_passed")
        and recomputed_assessment.get("lock_eligible")
    ):
        raise ValueError("recomputed research evidence did not pass")
    assessment_keys = (
        "metric_gate_passed",
        "lock_eligible",
        "failure_reasons",
        "oos_window_count",
        "oos_total_return_pct",
        "oos_max_drawdown_pct",
        "executed_trade_count",
        "preregistration_record_sha256",
        "preregistered_frozen_model_eligible",
        "preregistered_oos_evidence_verified",
        "data_quality_assessment_verified",
        "data_quality_status",
    )
    stored_assessment = {
        key: assessment.get(key)
        for key in assessment_keys
    }
    expected_assessment = {
        key: recomputed_assessment.get(key)
        for key in assessment_keys
    }
    if not _canonical_json_equal(
        stored_assessment,
        expected_assessment,
    ):
        raise ValueError("stored research assessment does not match evidence")
    assessment = recomputed_assessment

    try:
        report_locking_protocol = _build_locking_protocol(
            config=report.get("config"),
            mining_rules=report.get("mining_rules"),
            execution_rules=report.get("execution_rules"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"locking protocol is invalid: {exc}") from None
    signed_locking_protocol = preregistered_oos.get("locking_protocol")
    if not _canonical_json_equal(
        report_locking_protocol,
        signed_locking_protocol,
    ):
        raise ValueError(
            "locking protocol does not match signed OOS evidence"
        )
    config = report_locking_protocol["config"]
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    latest_trade_date = summary.get("latest_trade_date")
    signed_observation_cutoff = preregistered_oos.get(
        "observation_cutoff"
    )
    if (
        not isinstance(latest_trade_date, str)
        or _strict_iso_date(latest_trade_date) is None
        or latest_trade_date != signed_observation_cutoff
    ):
        raise ValueError(
            "latest trade date does not match signed OOS evidence"
        )
    try:
        report_data_quality = _build_data_quality_assessment(
            **{
                key: summary.get(key)
                for key in (
                    "amount_data_coverage",
                    "latest_amount_data_coverage",
                    "st_status_coverage",
                    "latest_st_status_coverage",
                    "universe_eligibility_coverage",
                    "latest_universe_eligibility_coverage",
                    "panel_max_abs_close_return",
                    "panel_stock_count",
                    "universe_required_count",
                )
            }
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"data quality result is invalid: {exc}") from None
    signed_data_quality = preregistered_oos.get(
        "data_quality_assessment"
    )
    if (
        report_data_quality.get("status") != "ok"
        or not _canonical_json_equal(
            report_data_quality,
            signed_data_quality,
        )
    ):
        raise ValueError(
            "data quality result does not match signed OOS evidence"
        )
    mining_rules = report_locking_protocol["mining_rules"]
    execution_rules = report_locking_protocol["execution_rules"]
    backtest_policy = report_locking_protocol["backtest_policy"]
    return {
        "schema_version": 3,
        "strategy_key": STRATEGY_KEY,
        "protocol_binding": build_current_frozen_protocol_binding(),
        "model": json.loads(json.dumps(model, ensure_ascii=False)),
        "model_specification": {
            "protocol_version": MODEL_SPEC_PROTOCOL_VERSION,
            "registered_at": registration["record"]["registered_at"],
            "calibrated_through": MODEL_SPEC_CALIBRATED_THROUGH,
            "oos_not_before": registration["record"]["oos_not_before"],
            "registration_sha256": registration["sha256"],
            "model_sha256": registration["model_sha256"],
            "calibration_sha256": registration["record"][
                "calibration_evidence"
            ]["sha256"],
            "parameters": dict(FIXED_MODEL_PARAMETERS),
        },
        "preregistration": json.loads(
            json.dumps(
                {
                    **registration["record"],
                    "record_sha256": registration["sha256"],
                },
                ensure_ascii=False,
            )
        ),
        "config": json.loads(json.dumps(config, ensure_ascii=False)),
        "execution_rules": json.loads(
            json.dumps(execution_rules, ensure_ascii=False)
        ),
        "mining_rules": json.loads(json.dumps(mining_rules, ensure_ascii=False)),
        "market_gate": json.loads(
            json.dumps(
                model.get("selected_market_gate") or {"enabled": False},
                ensure_ascii=False,
            )
        ),
        "stock_pool": {
            "n_stocks": config.get("n_stocks"),
            "selection": config.get("universe_selection"),
        },
        "data_quality_policy": json.loads(
            json.dumps(config.get("data_quality_policy") or {}, ensure_ascii=False)
        ),
        "data_quality_assessment": json.loads(
            json.dumps(report_data_quality, ensure_ascii=False)
        ),
        "backtest_policy": json.loads(
            json.dumps(backtest_policy, ensure_ascii=False)
        ),
        "acceptance_rules": dict(PROSPECTIVE_ACCEPTANCE_RULES),
        "historical_evidence": {
            "scope": "preregistered_frozen_model_oos",
            "model_sha256": registration["model_sha256"],
            "oos_evidence_sha256": preregistered_oos.get(
                "evidence_sha256"
            ),
            "calendar_snapshot_sha256": (
                preregistered_oos.get("calendar_snapshot") or {}
            ).get("snapshot_sha256"),
            "generated_at": str(report.get("generated_at") or ""),
            "latest_trade_date": signed_observation_cutoff,
            "oos_total_return_pct": assessment.get("oos_total_return_pct"),
            "oos_max_drawdown_pct": assessment.get("oos_max_drawdown_pct"),
            "executed_trade_count": assessment.get("executed_trade_count"),
            "bootstrap_total_return_ci_pct": assessment.get(
                "bootstrap_total_return_ci_pct"
            )
            or {},
        },
    }


def _candidate_tier(score_percentile: float, risk_flags: Sequence[str], rules: dict[str, Any] | None = None) -> str:
    tiers = normalize_mining_rules(rules)["tier_thresholds"] if rules is None or "tier_thresholds" not in rules else rules["tier_thresholds"]
    if score_percentile >= tiers["strong_percentile"] and (not tiers["strong_requires_no_risk"] or not risk_flags):
        return "strong"
    if score_percentile >= tiers["watch_percentile"]:
        return "watch"
    return "observe"


def _candidate_pool_size(top_k: int, candidate_pool_size: int | None) -> int:
    if candidate_pool_size is not None:
        return max(top_k, int(candidate_pool_size))
    return max(top_k, min(120, max(80, top_k * 6)))


def _feature_snapshot(row: pd.Series) -> dict[str, float | None]:
    keys = [
        "ret_5",
        "ret_10",
        "ret_20",
        "volume_ratio_20",
        "amount_ratio_20",
        "near_high_20",
        "limit_count_20",
        "volatility_10",
        "ret_3",
        "market_emotion_score",
    ]
    return {key: _maybe_float(row.get(key)) for key in keys if key in row.index}


def _score_inputs(
    row: pd.Series,
    rules: dict[str, Any],
    factor_model: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    selected = list((factor_model or {}).get("selected_factors") or [])
    if selected and not bool((factor_model or {}).get("fallback")):
        weights = (factor_model or {}).get("normalized_weights") or {}
        coefficients = (factor_model or {}).get("coefficients") or {}
        training_ic = (factor_model or {}).get("training_ic") or {}
        for key in selected:
            weight = float(weights.get(key, 0.0) or 0.0)
            inputs.append(
                {
                    "key": key,
                    "label": _SIGNAL_LABELS.get(key, key),
                    "value": _maybe_float(row.get(key)),
                    "weight": round(weight, 6),
                    "coefficient": round(float(coefficients.get(key, 0.0) or 0.0), 6),
                    "training_ic": _maybe_float(training_ic.get(key)),
                    "direction": "penalty" if weight < 0 else "positive",
                    "source": "walk_forward_ridge",
                }
            )
        return inputs
    for key, weight in rules["weights"].items():
        source_key = "ret_3" if key == "overheat_ret_3" else key
        inputs.append(
            {
                "key": key,
                "label": _SIGNAL_LABELS.get(key, key),
                "value": _maybe_float(row.get(source_key)),
                "weight": round(float(weight), 4),
                "direction": "penalty" if float(weight) < 0 else "positive",
            }
        )
    return inputs


def _feedback_for_code(live_feedback: dict[str, dict[str, Any]] | None, ts_code: Any) -> dict[str, Any]:
    if not isinstance(live_feedback, dict) or not live_feedback:
        return {}
    code = str(ts_code or "").strip().upper()
    candidates = [code, code.split(".")[0]]
    if "." not in code and len(code) == 6:
        candidates.extend([f"{code}.SZ", f"{code}.SH"])
    for key in candidates:
        item = live_feedback.get(key)
        if isinstance(item, dict):
            return dict(item)
    return {}


def _candidate_live_readiness(
    row: pd.Series,
    risk_flags: Sequence[str],
    live_feedback: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    penalty = min(len(risk_flags), 4) * 0.06
    ret_3 = float(row.get("ret_3", 0.0) or 0.0)
    ret_5 = float(row.get("ret_5", 0.0) or 0.0)
    volatility = float(row.get("volatility_10", 0.0) or 0.0)
    volume_ratio = float(row.get("volume_ratio_20", 1.0) or 1.0)
    near_high = float(row.get("near_high_20", 0.0) or 0.0)
    emotion = _maybe_float(row.get("market_emotion_score"))
    st_status_unknown = "st_status_known" in row.index and not _is_trusted_marker(row.get("st_status_known"))
    known_st = (
        "st_status_known" in row.index
        and _is_trusted_marker(row.get("st_status_known"))
        and _is_trusted_marker(row.get("is_st"))
    )
    amount_data_untrusted = (
        "amount_data_real" in row.index
        and not _is_trusted_marker(row.get("amount_data_real"))
    )

    if ret_3 > 0.18:
        penalty += 0.18
        warnings.append("proxy_overextended")
    if volatility > 0.06:
        penalty += 0.14
        warnings.append("proxy_high_volatility")
    if volume_ratio < 1.0:
        penalty += 0.12
        warnings.append("proxy_weak_volume")
    if near_high >= 0.995 and ret_5 > 0.12:
        penalty += 0.12
        warnings.append("proxy_limit_queue")
    if emotion is not None and emotion < 50:
        penalty += 0.10
        warnings.append("proxy_market_weak")
    if st_status_unknown:
        penalty += 0.22
        warnings.append("st_status_unknown")
    if known_st:
        warnings.append("known_st")
    if amount_data_untrusted:
        warnings.append("real_amount_unavailable")

    feedback = _feedback_for_code(live_feedback, row.get("ts_code"))
    sample_count = int(feedback.get("sample_count") or 0) if feedback else 0
    buy_vote_count = int(feedback.get("buy_vote_count") or 0) if feedback else 0
    paper_trade_count = int(feedback.get("paper_trade_count") or 0) if feedback else 0
    blocked_rate = _maybe_float(feedback.get("blocked_rate")) if feedback else None
    execution_rate = _maybe_float(feedback.get("execution_rate")) if feedback else None
    near_limit_rate = _maybe_float(feedback.get("near_limit_rate")) if feedback else None
    paper_avg_pnl_pct = _maybe_float(feedback.get("paper_avg_pnl_pct")) if feedback else None
    paper_win_rate = _maybe_float(feedback.get("paper_win_rate")) if feedback else None

    if sample_count >= 3 and blocked_rate is not None and blocked_rate > 0.5:
        penalty += 0.18
        warnings.append("feedback_frequent_blocked")
    if buy_vote_count >= 3 and execution_rate is not None and execution_rate < 0.3:
        penalty += 0.16
        warnings.append("feedback_low_execution")
    if sample_count >= 3 and near_limit_rate is not None and near_limit_rate > 0.4:
        penalty += 0.10
        warnings.append("feedback_near_limit_queue")
    if paper_trade_count >= 3 and paper_avg_pnl_pct is not None and paper_avg_pnl_pct < 0:
        penalty += 0.18
        warnings.append("feedback_paper_loss")
    if paper_trade_count >= 5 and paper_win_rate is not None and paper_win_rate < 0.4:
        penalty += 0.10
        warnings.append("feedback_low_win_rate")

    for flag in risk_flags:
        if flag == "weak_volume" and "proxy_weak_volume" not in warnings:
            warnings.append("proxy_weak_volume")
        elif flag == "high_volatility" and "proxy_high_volatility" not in warnings:
            warnings.append("proxy_high_volatility")
        elif flag == "overextended_3d" and "proxy_overextended" not in warnings:
            warnings.append("proxy_overextended")

    score = max(0.0, min(1.0, 1.0 - penalty))
    if known_st:
        score = 0.0
    if amount_data_untrusted:
        score = 0.0
    if st_status_unknown:
        score = 0.0
    if score >= 0.78:
        status = "ready"
        label = "实盘可跟踪"
    elif score >= 0.58:
        status = "watch"
        label = "需竞价确认"
    else:
        status = "avoid"
        label = "仅观察"
    warning_texts = [_LIVE_WARNING_TEXT.get(key, key) for key in dict.fromkeys(warnings)]
    return {
        "score": round(score, 4),
        "status": status,
        "label": label,
        "warning_keys": list(dict.fromkeys(warnings)),
        "warnings": warning_texts,
        "summary": "；".join(warning_texts) if warning_texts else "收盘因子未发现明显实盘执行降级项，仍需次日竞价确认。",
        "feedback": feedback,
    }


def _candidate_factor_explain(
    row: pd.Series,
    rules: dict[str, Any],
    positive_factors: Sequence[str],
    risk_flags: Sequence[str],
    tier: str,
    score: float,
    confidence: float,
    position_pct: float,
    hold_days: int,
    stop_loss: float = -0.035,
    take_profit: float = 0.07,
    factor_model: dict[str, Any] | None = None,
    live_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    matched = []
    for key in positive_factors:
        label, text = _POSITIVE_FACTOR_TEXT.get(key, (key, key))
        matched.append({"key": key, "label": label, "text": text})
    risks = [
        {"key": key, "label": key, "text": _RISK_FACTOR_TEXT.get(key, key)}
        for key in risk_flags
    ]
    risk_summary = "无显性风险旗标" if not risks else "；".join(item["text"] for item in risks)
    tier_text = {"strong": "强候选", "watch": "观察候选", "observe": "仅参考候选"}.get(tier, tier)
    return {
        "factor_version": MODEL_VERSION,
        "factor_title": "二三板接力情绪强势延续",
        "summary": f"二三板接力/强势延续复合因子：{tier_text}，评分{round(float(score), 2)}，置信度{round(float(confidence), 4)}。",
        "matched_factors": matched,
        "risk_factors": risks,
        "risk_summary": risk_summary,
        "live_reliability": live_readiness or {},
        "score_inputs": _score_inputs(row, rules, factor_model=factor_model),
        "model": {
            "model_version": str((factor_model or {}).get("model_version") or MODEL_VERSION),
            "fallback": bool((factor_model or {}).get("fallback", True)),
            "fallback_reason": str((factor_model or {}).get("fallback_reason") or ""),
            "selected_factors": list((factor_model or {}).get("selected_factors") or []),
            "normalized_weights": dict((factor_model or {}).get("normalized_weights") or {}),
        },
        "feature_snapshot": _feature_snapshot(row),
        "market_context": {
            "regime": _market_regime(row),
            "emotion_score": _maybe_float(row.get("market_emotion_score")),
            "data_status": str(row.get("market_data_status", "not_checked")),
            "source": str(row.get("market_source", "")),
        },
        "risk_controls": [
            "只用于次日观察/竞价/开盘确认，不使用未来收益参与当日选股。",
            "避免高开过多、封单弱、反复开板或量能确认失败时追入。",
            "若市场情绪、两市成交额或涨跌结构转弱，降级为观察或放弃。",
        ],
        "trading_plan": {
            "entry_mode": "next_session_watch_or_limit_board",
            "entry_zone": "avoid_chasing_above_limit_queue_plan",
            "position_pct": round(float(position_pct), 4),
            "stop_loss": round(float(stop_loss), 4),
            "take_profit": round(float(take_profit), 4),
            "hold_days": int(hold_days),
            "cancel_conditions": ["market_emotion_drops", "limit_board_opens_repeatedly", "volume_confirm_fails"],
        },
        "update_policy": {
            "signal_refresh": "每个交易日收盘后刷新候选池",
            "auction_review": "次日9:15和9:25结合竞价/风控再决策",
            "factor_review": "每周复盘稳定性，每20个交易日或连续3-5天失效时评估权重",
        },
    }


def _resolve_stock_name(ts_code: Any) -> str:
    raw_code = str(ts_code or "").strip()
    code = raw_code.split(".")[0]
    try:
        from analytics import data_manager as dm

        name = str(dm.get_stock_name(code) or "").strip()
    except Exception:
        name = ""
    return name if name and name not in {raw_code, code} else (code or raw_code)


def _force_readiness_avoid(value: Any, warning_key: str, label: str) -> dict[str, Any]:
    readiness = dict(value or {})
    warning_keys = list(readiness.get("warning_keys") or [])
    warnings = list(readiness.get("warnings") or [])
    if warning_key not in warning_keys:
        warning_keys.append(warning_key)
        warnings.append(_LIVE_WARNING_TEXT[warning_key])
    readiness.update(
        {
            "score": 0.0,
            "status": "avoid",
            "label": label,
            "warning_keys": warning_keys,
            "warnings": warnings,
        }
    )
    return readiness


def build_latest_candidates(
    features: pd.DataFrame,
    top_k: int = 10,
    hold_days: int = 2,
    stop_loss: float = -0.035,
    take_profit: float = 0.07,
    candidate_pool_size: int | None = None,
    mining_rules: dict[str, Any] | None = None,
    live_feedback: dict[str, dict[str, Any]] | None = None,
    factor_model: dict[str, Any] | None = None,
    research_assessment: dict[str, Any] | None = None,
    strict_factor_model: bool = False,
) -> list[dict[str, Any]]:
    if features.empty:
        return []
    rules = normalize_mining_rules(mining_rules)
    active_filters = rules["filters"]
    latest_date = features["trade_date"].max()
    latest = features[features["trade_date"] == latest_date].copy()
    if "market_data_trusted" in latest.columns:
        trusted_values = latest["market_data_trusted"].map(_is_trusted_marker)
        if not bool(trusted_values.all()):
            return []
    if active_filters["min_relay_score"] is not None:
        latest = latest[latest["relay_score"] >= active_filters["min_relay_score"]]
    if active_filters["min_volume_ratio_20"] is not None and "volume_ratio_20" in latest.columns:
        latest = latest[latest["volume_ratio_20"] >= active_filters["min_volume_ratio_20"]]
    if active_filters["min_near_high_20"] is not None and "near_high_20" in latest.columns:
        latest = latest[latest["near_high_20"] >= active_filters["min_near_high_20"]]
    if active_filters["exclude_high_risk"] and "risk_flags" in latest.columns:
        latest = latest[latest["risk_flags"].fillna("").astype(str).str.len() == 0]
    latest["_risk_flags_list"] = latest["risk_flags"].fillna("").astype(str).map(lambda text: [flag for flag in text.split(",") if flag]) if "risk_flags" in latest.columns else [[] for _ in range(len(latest))]
    latest["_live_readiness"] = latest.apply(
        lambda row: _candidate_live_readiness(row, row.get("_risk_flags_list") or [], live_feedback=live_feedback),
        axis=1,
    )
    active_model = factor_model or {
        "model_version": MODEL_VERSION,
        "fallback": True,
        "fallback_reason": "factor_model_not_available",
        "selected_factors": [],
    }
    model_validation_rejected = (
        active_model.get("validation_stable") is False
        or active_model.get("validation_accepted") is False
    )
    if model_validation_rejected:
        latest["_live_readiness"] = latest["_live_readiness"].map(
            lambda value: _force_readiness_avoid(value, "model_validation_rejected", "验证未通过")
        )
    research_not_validated = (
        isinstance(research_assessment, dict)
        and not bool(research_assessment.get("deployment_eligible"))
    )
    if research_not_validated:
        latest["_live_readiness"] = latest["_live_readiness"].map(
            lambda value: _force_readiness_avoid(value, "research_not_validated", "研究期")
        )
    latest["_factor_model_score"] = score_factor_model(
        latest,
        active_model,
        strict=strict_factor_model,
    )
    if active_model.get("fallback"):
        latest["_factor_model_score"] *= int(active_model.get("fallback_direction") or 1)
    latest["_selection_score"] = latest["_factor_model_score"].rank(method="average", pct=True) * 100.0
    latest["_live_ready_score"] = latest.apply(
        lambda row: float(row.get("_selection_score", 0.0) or 0.0)
        * (0.65 + 0.35 * float((row.get("_live_readiness") or {}).get("score", 1.0))),
        axis=1,
    )
    latest = latest.sort_values(["_live_ready_score", "relay_score"], ascending=False).reset_index(drop=True)
    latest_count = len(latest)
    if latest_count == 0:
        return []
    pool_size = min(_candidate_pool_size(top_k, candidate_pool_size), latest_count)
    latest = latest.head(pool_size).copy()
    candidates: list[dict[str, Any]] = []
    for idx, row in latest.iterrows():
        score = float(row["relay_score"])
        selection_score = float(row.get("_selection_score", 0.0) or 0.0)
        risk_flags = list(row.get("_risk_flags_list") or [flag for flag in str(row.get("risk_flags", "")).split(",") if flag])
        live_readiness = dict(row.get("_live_readiness") or _candidate_live_readiness(row, risk_flags, live_feedback=live_feedback))
        rank = int(idx) + 1
        score_percentile = 1.0 - ((rank - 1) / max(latest_count - 1, 1))
        confidence = max(
            0.0,
            min(
                1.0,
                selection_score / 100.0 * 0.45
                + score_percentile * 0.55
                - min(len(risk_flags), 3) * 0.03,
            ),
        )
        live_confidence = max(0.0, min(1.0, confidence * (0.55 + 0.45 * float(live_readiness.get("score", 1.0)))))
        tier = _candidate_tier(score_percentile, risk_flags, rules)
        if live_readiness.get("status") == "avoid":
            tier = "observe"
        elif live_readiness.get("status") == "watch" and tier == "strong":
            tier = "watch"
        if tier == "strong":
            position_pct = 0.12 if not risk_flags else 0.08
        elif tier == "watch":
            position_pct = 0.08 if not risk_flags else 0.05
        else:
            position_pct = 0.0
        if live_readiness.get("status") == "watch":
            position_pct = min(position_pct, 0.05)
        elif live_readiness.get("status") == "avoid":
            position_pct = 0.0
        positive_factors = _positive_factors(row, rules)
        factor_explain = _candidate_factor_explain(
            row,
            rules,
            positive_factors=positive_factors,
            risk_flags=risk_flags,
            tier=tier,
            score=score,
            confidence=confidence,
            position_pct=position_pct,
            hold_days=hold_days,
            stop_loss=stop_loss,
            take_profit=take_profit,
            factor_model=active_model,
            live_readiness=live_readiness,
        )
        candidates.append(
            {
                "ts_code": str(row["ts_code"]),
                "code": str(row["ts_code"]).split(".")[0],
                "name": _resolve_stock_name(row["ts_code"]),
                "trade_date": str(row["trade_date"]),
                "is_st": _is_trusted_marker(row.get("is_st")),
                "st_status_known": _is_trusted_marker(row.get("st_status_known")),
                "amount_data_real": _is_trusted_marker(row.get("amount_data_real")),
                "strategy_key": STRATEGY_KEY,
                "model_version": str(active_model.get("model_version") or MODEL_VERSION),
                "validation_accepted": active_model.get("validation_accepted"),
                "validation_rejection_reason": active_model.get("validation_rejection_reason"),
                "validation_stable": active_model.get("validation_stable"),
                "validation_stability_reason": active_model.get(
                    "validation_stability_reason"
                ),
                "research_status": (
                    research_assessment.get("research_status")
                    if isinstance(research_assessment, dict)
                    else None
                ),
                "deployment_eligible": (
                    bool(research_assessment.get("deployment_eligible"))
                    if isinstance(research_assessment, dict)
                    else False
                ),
                "candidate_rank": rank,
                "candidate_tier": tier,
                "candidate_pool_size": pool_size,
                "latest_universe_size": latest_count,
                "score_percentile": round(score_percentile, 4),
                "relay_score": round(score, 4),
                "factor_model_score": round(float(row.get("_factor_model_score", score)), 6),
                "selection_score": round(selection_score, 4),
                "confidence": round(confidence, 4),
                "live_confidence": round(live_confidence, 4),
                "live_readiness_score": live_readiness.get("score"),
                "live_readiness_status": live_readiness.get("status"),
                "live_readiness_label": live_readiness.get("label"),
                "live_warnings": live_readiness.get("warnings", []),
                "live_warning_keys": live_readiness.get("warning_keys", []),
                "live_ready_score": round(float(row.get("_live_ready_score", score)), 4),
                "market_regime": _market_regime(row),
                "market_emotion_score": _maybe_float(row.get("market_emotion_score")),
                "market_data_status": str(row.get("market_data_status", "not_checked")),
                "market_source": str(row.get("market_source", "")),
                "positive_factors": positive_factors,
                "negative_factors": risk_flags,
                "risk_flags": risk_flags,
                "factor_explain": factor_explain,
                "active_filters": {key: value for key, value in active_filters.items() if value not in (None, False)},
                "entry_mode": "next_session_watch_or_limit_board",
                "entry_zone": "avoid_chasing_above_limit_queue_plan",
                "position_pct": position_pct,
                "stop_loss": round(float(stop_loss), 4),
                "take_profit": round(float(take_profit), 4),
                "cancel_conditions": ["market_emotion_drops", "limit_board_opens_repeatedly", "volume_confirm_fails"],
                "hold_days": int(hold_days),
                "available_at": str(row.get("available_at", "")),
                "research_only": not bool(
                    research_assessment.get("deployment_eligible")
                    if isinstance(research_assessment, dict)
                    else False
                ),
            }
        )
    return candidates


def build_frozen_shadow_snapshot(
    panel: dict[str, pd.DataFrame],
    manifest: dict[str, Any],
    *,
    market_signal_frame: pd.DataFrame | None = None,
    live_feedback: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score one close with a frozen model without exposing executable candidates."""
    protocol_valid, protocol_reason = validate_frozen_protocol_binding(manifest)
    if not protocol_valid:
        return {
            "ok": False,
            "data_status": "degraded",
            "reason": protocol_reason,
            "signal_date": None,
            "market_gate": {"enabled": False, "pass": False},
            "candidates": [],
        }
    model = dict(manifest.get("model") or {}) if isinstance(manifest, dict) else {}
    config = dict(manifest.get("config") or {}) if isinstance(manifest, dict) else {}
    rules = normalize_mining_rules(
        manifest.get("mining_rules") if isinstance(manifest, dict) else None
    )
    if (
        model.get("fallback") is not False
        or model.get("validation_accepted") is not True
        or model.get("validation_stable") is not True
    ):
        return {
            "ok": False,
            "data_status": "degraded",
            "reason": "frozen_model_is_not_validation_accepted",
            "signal_date": None,
            "market_gate": {"enabled": False, "pass": False},
            "candidates": [],
        }

    features_raw = build_feature_frame(panel, mining_rules=rules)
    if features_raw.empty:
        return {
            "ok": False,
            "data_status": "degraded",
            "reason": "frozen_shadow_feature_frame_empty",
            "signal_date": None,
            "market_gate": {"enabled": False, "pass": False},
            "candidates": [],
        }

    selected_gate = dict(model.get("selected_market_gate") or {"enabled": False})
    market_signals_required = bool(
        config.get("include_market_signals") or selected_gate.get("enabled")
    )
    if market_signal_frame is None and market_signals_required:
        market_signal_frame = build_market_signal_frame(
            features_raw["trade_date"].unique()
        )
    features = apply_market_signals(features_raw, market_signal_frame)
    signal_date = str(features["trade_date"].max())
    latest = features[features["trade_date"] == signal_date]

    amount_coverage = (
        float(latest["amount_data_real"].map(_is_trusted_marker).mean())
        if "amount_data_real" in latest.columns and not latest.empty
        else 0.0
    )
    st_coverage = (
        float(latest["st_status_known"].map(_is_trusted_marker).mean())
        if "st_status_known" in latest.columns and not latest.empty
        else 0.0
    )
    market_trusted = (
        bool(latest["market_data_trusted"].map(_is_trusted_marker).all())
        if market_signals_required
        and "market_data_trusted" in latest.columns
        and not latest.empty
        else not market_signals_required
    )
    if amount_coverage < MIN_REAL_AMOUNT_COVERAGE:
        data_status, reason = "degraded", "latest_real_amount_coverage_too_low"
    elif st_coverage < 0.98:
        data_status, reason = "degraded", "latest_st_status_coverage_too_low"
    elif not market_trusted:
        data_status, reason = "degraded", "latest_market_snapshot_not_trusted"
    else:
        data_status, reason = "ok", "ok"

    gate_result: dict[str, Any] = {
        "enabled": bool(selected_gate.get("enabled")),
        "pass": True,
        "reason": "gate_disabled",
    }
    if selected_gate.get("enabled"):
        latest_market = _latest_market_summary(features)
        emotion = latest_market.get("market_emotion_score")
        emotion_delta = _latest_emotion_delta(features)
        gate_pass, gate_reason = _gate_check(
            selected_gate,
            emotion,
            emotion_delta,
        )
        gate_result.update(
            {
                "pass": bool(gate_pass),
                "reason": gate_reason,
                "emotion": emotion,
                "emotion_delta": emotion_delta,
            }
        )

    candidates: list[dict[str, Any]] = []
    if data_status == "ok":
        candidate_features = features
        if (
            not market_signals_required
            and "market_data_trusted" in candidate_features.columns
        ):
            candidate_features = candidate_features.drop(
                columns=["market_data_trusted"]
            )
        try:
            candidates = build_latest_candidates(
                candidate_features,
                top_k=int(config.get("top_k") or 5),
                hold_days=int(config.get("hold_days") or 2),
                stop_loss=float(config.get("stop_loss") or -0.035),
                take_profit=float(config.get("take_profit") or 0.07),
                candidate_pool_size=int(
                    config.get("candidate_pool_size")
                    or config.get("top_k")
                    or 5
                ),
                mining_rules=rules,
                live_feedback=live_feedback,
                factor_model=model,
                research_assessment={
                    "research_status": "validated",
                    "deployment_eligible": True,
                    "research_only": False,
                },
                strict_factor_model=True,
            )
        except ValueError as exc:
            return {
                "ok": False,
                "data_status": "degraded",
                "reason": str(exc),
                "signal_date": signal_date,
                "market_gate": gate_result,
                "data_quality": {
                    "latest_real_amount_coverage": round(
                        amount_coverage,
                        4,
                    ),
                    "latest_st_status_coverage": round(st_coverage, 4),
                    "latest_market_data_trusted": market_trusted,
                },
                "candidates": [],
            }
        for candidate in candidates:
            ungated_target = float(candidate.get("position_pct") or 0.0)
            shadow_target = ungated_target if gate_result["pass"] else 0.0
            candidate.update(
                {
                    "experiment_id": str(manifest.get("experiment_id") or ""),
                    "model_instance_id": str(
                        manifest.get("model_instance_id") or ""
                    ),
                    "source_run_id": str(manifest.get("source_run_id") or ""),
                    "research_status": "locked_shadow",
                    "deployment_eligible": False,
                    "research_only": True,
                    "gate_pass": bool(gate_result["pass"]),
                    "ungated_shadow_target_position_pct": round(
                        ungated_target,
                        4,
                    ),
                    "shadow_target_position_pct": round(shadow_target, 4),
                    "position_pct": 0.0,
                }
            )
            factor_explain = candidate.get("factor_explain")
            if isinstance(factor_explain, dict):
                trading_plan = factor_explain.setdefault("trading_plan", {})
                trading_plan["shadow_target_position_pct"] = round(
                    shadow_target,
                    4,
                )
                trading_plan["position_pct"] = 0.0

    return {
        "ok": data_status == "ok",
        "data_status": data_status,
        "reason": reason,
        "signal_date": signal_date,
        "market_gate": gate_result,
        "data_quality": {
            "latest_real_amount_coverage": round(amount_coverage, 4),
            "latest_st_status_coverage": round(st_coverage, 4),
            "latest_market_data_trusted": market_trusted,
        },
        "candidates": candidates,
    }


def settle_frozen_shadow_candidate(
    panel: dict[str, pd.DataFrame],
    candidate: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Settle one frozen candidate only when its real entry and exit bars exist."""
    protocol_valid, protocol_reason = validate_frozen_protocol_binding(manifest)
    if not protocol_valid:
        return {
            "status": "rejected",
            "reason": protocol_reason,
        }
    if not bool(candidate.get("gate_pass", True)):
        return {
            "status": "settled",
            "execution_blocked": True,
            "block_reasons": ["market_gate_blocked"],
            "exit_reason": "market_gate_blocked",
            "realized_return_pct": 0.0,
            "valuation_path": [],
        }
    if float(candidate.get("shadow_target_position_pct") or 0.0) <= 0.0:
        return {
            "status": "settled",
            "execution_blocked": True,
            "block_reasons": ["zero_target_position"],
            "exit_reason": "zero_target_position",
            "realized_return_pct": 0.0,
            "valuation_path": [],
        }

    aligned = _aligned_panel(panel)
    code = str(candidate.get("ts_code") or candidate.get("code") or "").strip()
    code_key = code.split(".")[0]
    column = next(
        (
            item
            for item in aligned["close"].columns
            if str(item) == code or str(item).split(".")[0] == code_key
        ),
        None,
    )
    if column is None:
        return {"status": "pending", "reason": "candidate_bar_unavailable"}

    index = aligned["close"].index
    date_texts = pd.Index(
        pd.to_datetime(index).strftime("%Y-%m-%d"),
    )
    signal_date = str(
        candidate.get("signal_date") or candidate.get("trade_date") or ""
    )[:10]
    signal_matches = np.flatnonzero(date_texts == signal_date)
    if len(signal_matches) != 1:
        return {"status": "pending", "reason": "signal_bar_unavailable"}
    signal_index = int(signal_matches[0])
    entry_index = signal_index + 1
    if entry_index >= len(index):
        return {"status": "pending", "reason": "awaiting_entry_data"}

    close_series = aligned["close"][column]
    open_frame = aligned.get("open")
    if open_frame is None:
        return {"status": "pending", "reason": "entry_open_unavailable"}
    open_series = open_frame[column]
    bars = pd.DataFrame(
        {
            "open": open_series,
            "high": aligned.get("high", aligned["close"])[column],
            "low": aligned.get("low", aligned["close"])[column],
            "close": close_series,
            "volume": aligned["volume"][column],
        },
        index=index,
    )
    if "is_st" in aligned:
        bars["is_st"] = aligned["is_st"][column]

    entry_row = bars.iloc[entry_index]
    entry_open = _maybe_float(entry_row.get("open"))
    previous_close = _maybe_float(close_series.iloc[signal_index])
    entry_volume = _maybe_float(entry_row.get("volume"))
    entry_date = date_texts[entry_index]
    if entry_volume is not None and entry_volume <= 0:
        return {
            "status": "settled",
            "entry_date": entry_date,
            "entry_price": entry_open,
            "execution_blocked": True,
            "block_reasons": ["entry_suspended_or_zero_volume"],
            "exit_reason": "entry_suspended_or_zero_volume",
            "realized_return_pct": 0.0,
            "valuation_path": [],
        }
    if (
        entry_open is None
        or previous_close is None
        or previous_close <= 0
        or entry_volume is None
    ):
        return {"status": "pending", "reason": "entry_bar_unavailable"}

    st_known = (
        _is_trusted_marker(
            aligned["st_status_known"].iloc[entry_index].get(column)
        )
        if "st_status_known" in aligned
        else False
    )
    is_st = (
        _is_trusted_marker(aligned["is_st"].iloc[entry_index].get(column))
        if st_known and "is_st" in aligned
        else True
    )
    feature_snapshot = (
        (candidate.get("factor_explain") or {}).get("feature_snapshot") or {}
        if isinstance(candidate.get("factor_explain"), dict)
        else {}
    )
    amount_ratio = _maybe_float(
        candidate.get("signal_amount_ratio_20")
        or feature_snapshot.get("amount_ratio_20")
    )
    entry_gap_pct = (entry_open / previous_close - 1.0) * 100.0
    execution_rules = normalize_execution_rules(
        manifest.get("execution_rules")
        if isinstance(manifest, dict)
        else None
    )
    entry_flags = _entry_st_execution_flags(
        st_status_known=st_known,
        is_st=is_st,
    )
    entry_flags.extend(
        _entry_execution_flags(
            entry_gap_pct,
            amount_ratio,
            code=code,
            entry_date=entry_date,
            is_st=is_st,
            execution_rules=execution_rules,
        )
    )
    if entry_flags:
        return {
            "status": "settled",
            "entry_date": entry_date,
            "entry_price": entry_open,
            "execution_blocked": True,
            "block_reasons": entry_flags,
            "exit_reason": entry_flags[0],
            "realized_return_pct": 0.0,
            "valuation_path": [],
        }

    config = dict(manifest.get("config") or {}) if isinstance(manifest, dict) else {}
    path = build_trade_path(
        code=code,
        bars=bars,
        signal_index=signal_index,
        hold_days=int(config.get("hold_days") or 2),
        stop_loss=float(config.get("stop_loss") or -0.035),
        take_profit=float(config.get("take_profit") or 0.07),
        round_trip_cost_pct=float(execution_rules["round_trip_cost_pct"]),
        slippage_pct=float(execution_rules["slippage_pct"]),
        max_deferred_exit_sessions=MAX_DEFERRED_EXIT_SESSIONS,
    )
    if not path:
        return {"status": "pending", "reason": "awaiting_exit_data"}
    return {
        "status": "settled",
        "execution_blocked": False,
        "block_reasons": [],
        "entry_date": path["entry_date"],
        "exit_date": path["exit_date"],
        "entry_price": path["entry_price"],
        "exit_price": path["exit_price"],
        "exit_reason": path["exit_reason"],
        "realized_return_pct": path["live_adjusted_return_pct"],
        "valuation_path": path["valuation_path"],
        "path_policy": path["path_policy"],
    }


def _maybe_float(value) -> float | None:
    try:
        if value in (None, "") or pd.isna(value):
            return None
        return round(float(value), 4)
    except Exception:
        return None


def _is_trusted_marker(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "ok"}
    return bool(value)


def _market_regime(row: pd.Series) -> str:
    score = _maybe_float(row.get("market_emotion_score"))
    if score is None:
        return "unknown"
    if score >= 70:
        return "strong"
    if score <= 35:
        return "weak"
    return "neutral"

def _positive_factors(row: pd.Series, rules: dict[str, Any] | None = None) -> list[str]:
    active_rules = normalize_mining_rules(rules)
    volume_bar = active_rules["filters"].get("min_volume_ratio_20") or 1.2
    near_high_bar = active_rules["filters"].get("min_near_high_20") or 0.92
    factors = []
    if float(row.get("ret_5", 0.0) or 0.0) > 0:
        factors.append("positive_5d_momentum")
    if float(row.get("volume_ratio_20", 0.0) or 0.0) > volume_bar:
        factors.append("volume_confirmation")
    if float(row.get("near_high_20", 0.0) or 0.0) > near_high_bar:
        factors.append("near_20d_high")
    if float(row.get("limit_count_20", 0.0) or 0.0) >= 1:
        factors.append("recent_limit_strength")
    if float(row.get("market_emotion_score", 0.0) or 0.0) >= 65:
        factors.append("supportive_market_emotion")
    return factors


def _write_outputs(
    out_dir: Path,
    features: pd.DataFrame,
    labels: pd.DataFrame,
    report: dict[str, Any],
    candidates: list[dict[str, Any]],
    factor_model: dict[str, Any],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    features.to_csv(out_dir / "features.csv", index=False, encoding="utf-8-sig")
    labels.to_csv(out_dir / "labels.csv", index=False, encoding="utf-8-sig")
    (out_dir / "walkforward_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "latest_candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "factor_model.json").write_text(
        json.dumps(factor_model, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _universe_eligibility_audit(
    panel: Mapping[str, pd.DataFrame],
    features: pd.DataFrame,
    required_count: int,
) -> dict[str, Any]:
    close = panel.get("close")
    if not isinstance(close, pd.DataFrame) or close.empty:
        return {
            "required_count": max(0, int(required_count)),
            "required_date_count": 0,
            "sufficient_date_count": 0,
            "coverage": 0.0,
            "latest_coverage": 0.0,
            "count_min": 0,
            "count_max": 0,
            "latest_count": 0,
        }

    panel_dates = pd.Index(
        pd.to_datetime(close.index).strftime("%Y-%m-%d")
    )
    if not features.empty and "trade_date" in features.columns:
        first_feature_date = str(features["trade_date"].min())[:10]
        required_dates = panel_dates[panel_dates >= first_feature_date]
    else:
        required_dates = panel_dates
    required_dates = pd.Index(required_dates.unique())
    if required_dates.empty:
        required_dates = panel_dates[-1:]

    universe = panel.get("universe_eligible")
    if isinstance(universe, pd.DataFrame):
        aligned = universe.reindex(
            index=close.index,
            columns=close.columns,
        ).apply(lambda column: column.map(_is_trusted_marker))
        eligible_counts = pd.Series(
            aligned.sum(axis=1).to_numpy(dtype=int),
            index=panel_dates,
            dtype=int,
        )
    else:
        eligible_counts = pd.Series(0, index=panel_dates, dtype=int)

    if not features.empty and "trade_date" in features.columns:
        feature_counts = (
            features.assign(
                trade_date=features["trade_date"].astype(str).str[:10]
            )
            .groupby("trade_date", sort=False)
            .size()
            .astype(int)
        )
    else:
        feature_counts = pd.Series(dtype=int)
    usable_counts = pd.concat(
        [
            eligible_counts.reindex(required_dates, fill_value=0),
            feature_counts.reindex(required_dates, fill_value=0),
        ],
        axis=1,
    ).min(axis=1).astype(int)
    target = max(0, int(required_count))
    sufficient = usable_counts >= target if target > 0 else usable_counts >= 0
    coverage = round(float(sufficient.mean()), 4) if len(sufficient) else 0.0
    latest_count = int(usable_counts.iloc[-1]) if not usable_counts.empty else 0
    return {
        "required_count": target,
        "required_date_count": int(len(usable_counts)),
        "sufficient_date_count": int(sufficient.sum()),
        "coverage": coverage,
        "latest_coverage": 1.0 if target > 0 and latest_count >= target else 0.0,
        "count_min": int(usable_counts.min()) if not usable_counts.empty else 0,
        "count_max": int(usable_counts.max()) if not usable_counts.empty else 0,
        "latest_count": latest_count,
    }


def run_relay_research(
    panel: dict[str, pd.DataFrame] | None = None,
    out_dir: str | Path | None = None,
    n_stocks: int = 800,
    days: int = 180,
    train_days: int = 60,
    test_days: int = 10,
    step_days: int | None = None,
    hold_days: int = 2,
    stop_loss: float = -0.035,
    take_profit: float = 0.07,
    top_k: int = 5,
    candidate_pool_size: int | None = None,
    market_signal_frame: pd.DataFrame | None = None,
    include_market_signals: bool = False,
    mining_rules: dict[str, Any] | None = None,
    require_latest_data: bool = True,
    feedback_user_id: int | None = None,
    load_live_feedback: bool = True,
) -> dict[str, Any]:
    n_stocks = _strict_protocol_int(
        n_stocks,
        "config.n_stocks",
        minimum=1,
        maximum=20_000,
    )
    days = _strict_protocol_int(
        days,
        "config.days",
        minimum=1,
        maximum=10_000,
    )
    train_days = _strict_protocol_int(
        train_days,
        "config.train_days",
        minimum=1,
        maximum=10_000,
    )
    test_days = _strict_protocol_int(
        test_days,
        "config.test_days",
        minimum=1,
        maximum=10_000,
    )
    step_days = _strict_protocol_int(
        test_days if step_days is None else step_days,
        "config.step_days",
        minimum=1,
        maximum=10_000,
    )
    hold_days = _strict_protocol_int(
        hold_days,
        "config.hold_days",
        minimum=1,
        maximum=1_000,
    )
    stop_loss = _strict_protocol_float(
        stop_loss,
        "config.stop_loss",
    )
    take_profit = _strict_protocol_float(
        take_profit,
        "config.take_profit",
    )
    top_k = _strict_protocol_int(
        top_k,
        "config.top_k",
        minimum=1,
        maximum=10_000,
    )
    candidate_pool_size = (
        _candidate_pool_size(top_k, None)
        if candidate_pool_size is None
        else _strict_protocol_int(
            candidate_pool_size,
            "config.candidate_pool_size",
            minimum=1,
            maximum=20_000,
        )
    )
    if not isinstance(include_market_signals, bool):
        raise _locking_protocol_error(
            "config.include_market_signals must be boolean"
        )
    rules = normalize_mining_rules(mining_rules)
    execution_rules = normalize_execution_rules()
    report_config = {
        "n_stocks": n_stocks,
        "days": days,
        "train_days": train_days,
        "test_days": test_days,
        "step_days": step_days,
        "hold_days": hold_days,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "top_k": top_k,
        "candidate_pool_size": candidate_pool_size,
        "include_market_signals": bool(
            include_market_signals
            or market_signal_frame is not None
        ),
        "model_version": MODEL_VERSION,
        "universe_selection": (
            "point_in_time_eligible_stable_sha256_sample"
        ),
        "data_quality_policy": dict(FROZEN_DATA_QUALITY_POLICY),
    }
    locking_protocol = _build_locking_protocol(
        config=report_config,
        mining_rules=rules,
        execution_rules=execution_rules,
    )
    report_config = locking_protocol["config"]
    if panel is None:
        panel, _codes = build_panel_from_cache(
            n_stocks=research_candidate_pool_size(n_stocks),
            days=days,
            min_history=min(days, 60),
            require_latest=require_latest_data,
            point_in_time=True,
            require_security_master=True,
        )
    out_path = Path(out_dir) if out_dir is not None else _OUT_BASE / datetime.now().strftime("%Y%m%d-%H%M%S")
    close_frame = panel.get("close")
    panel_stock_count = int(
        close_frame.shape[1]
        if isinstance(close_frame, pd.DataFrame)
        else 0
    )
    universe_target_count = min(
        max(0, int(n_stocks)),
        panel_stock_count,
    )
    features_raw = build_feature_frame(panel, mining_rules=rules)
    if "universe_eligible" in panel:
        features_raw = _stable_daily_feature_sample(
            features_raw,
            universe_target_count,
        )
    label_panel = _panel_for_feature_universe(panel, features_raw)
    labels = build_label_frame(
        label_panel,
        hold_days=hold_days,
        stop_loss=stop_loss,
        take_profit=take_profit,
        compact_diagnostics=True,
    )
    del label_panel
    market_signals_required = bool(include_market_signals or market_signal_frame is not None)
    if market_signal_frame is None and include_market_signals:
        market_signal_frame = build_market_signal_frame(features_raw["trade_date"].unique())
    features = apply_market_signals(features_raw, market_signal_frame)
    if market_signals_required and "market_data_trusted" in features.columns:
        trusted_feature_rows = int(
            features["market_data_trusted"].map(_is_trusted_marker).sum()
        )
        training_features = features
    else:
        trusted_feature_rows = int(len(features))
    training_columns = [
        column
        for column in _RESEARCH_FEATURE_COLUMNS
        if column in features.columns
        and (
            market_signals_required
            or column != "market_data_trusted"
        )
    ]
    training_features = features.loc[:, training_columns].copy()
    del features_raw
    if "amount_data_real" in features.columns and not features.empty:
        amount_real_markers = features["amount_data_real"].map(_is_trusted_marker)
        amount_data_coverage = round(float(amount_real_markers.mean()), 4)
        latest_amount_date = features["trade_date"].max()
        latest_amount_markers = amount_real_markers.loc[features["trade_date"] == latest_amount_date]
        latest_amount_data_coverage = (
            round(float(latest_amount_markers.mean()), 4)
            if not latest_amount_markers.empty
            else 0.0
        )
    else:
        amount_data_coverage = 0.0
        latest_amount_data_coverage = 0.0
    amount_data_real = bool(
        amount_data_coverage >= MIN_REAL_AMOUNT_COVERAGE
        and latest_amount_data_coverage >= MIN_REAL_AMOUNT_COVERAGE
    )
    amount_data_sources = (
        sorted(features["amount_data_source"].dropna().astype(str).unique().tolist())
        if "amount_data_source" in features.columns
        else []
    )
    amount_data_source = ",".join(amount_data_sources) or "unknown"
    if "st_status_known" in features.columns and not features.empty:
        st_known = features["st_status_known"].map(_is_trusted_marker)
        st_status_known_rows = int(st_known.sum())
        st_status_coverage = round(float(st_known.mean()), 4)
        latest_feature_date = features["trade_date"].max()
        latest_st_known = st_known.loc[features["trade_date"] == latest_feature_date]
        latest_st_status_coverage = round(float(latest_st_known.mean()), 4) if not latest_st_known.empty else 0.0
    else:
        st_status_known_rows = 0
        st_status_coverage = 0.0
        latest_st_status_coverage = 0.0
    universe_audit = _universe_eligibility_audit(
        panel,
        features,
        required_count=universe_target_count,
    )
    universe_eligibility_coverage = universe_audit["coverage"]
    latest_universe_eligibility_coverage = universe_audit[
        "latest_coverage"
    ]
    panel_max_abs_close_return = None
    if isinstance(close_frame, pd.DataFrame) and not close_frame.empty:
        max_return = close_frame.pct_change(fill_method=None).abs().max().max()
        if pd.notna(max_return):
            panel_max_abs_close_return = round(float(max_return), 6)
    data_quality_assessment = _build_data_quality_assessment(
        amount_data_coverage=amount_data_coverage,
        latest_amount_data_coverage=latest_amount_data_coverage,
        st_status_coverage=st_status_coverage,
        latest_st_status_coverage=latest_st_status_coverage,
        universe_eligibility_coverage=universe_eligibility_coverage,
        latest_universe_eligibility_coverage=(
            latest_universe_eligibility_coverage
        ),
        panel_max_abs_close_return=panel_max_abs_close_return,
        panel_stock_count=panel_stock_count,
        universe_required_count=n_stocks,
    )
    wf = walk_forward_relay(
        training_features, labels,
        train_days=train_days, test_days=test_days, step_days=step_days, top_k=top_k,
        market_gate=rules.get("market_gate"),
        locking_protocol=locking_protocol,
        data_quality_assessment=data_quality_assessment,
    )
    research_assessment = assess_research_result(wf)
    if (
        universe_eligibility_coverage < 0.9999
        or latest_universe_eligibility_coverage < 0.9999
    ):
        research_assessment = dict(research_assessment)
        failure_reasons = list(
            research_assessment.get("failure_reasons") or []
        )
        if "universe_size_below_required" not in failure_reasons:
            failure_reasons.append("universe_size_below_required")
        research_assessment.update(
            {
                "research_status": "failed",
                "metric_gate_passed": False,
                "lock_eligible": False,
                "deployment_eligible": False,
                "research_only": True,
                "failure_reasons": failure_reasons,
            }
        )
    rolling_latest_model = dict(
        wf.get("latest_model")
        or fit_factor_model(
            pd.DataFrame(),
            target_col="model_target_pct",
        )
    )
    latest_model = dict(
        wf.get("registered_model")
        or _unavailable_preregistered_model(
            "preregistered_calibration_model_unavailable"
        )
    )
    model_runtime_diagnostics = {
        "research_status": research_assessment["research_status"],
        "deployment_eligible": research_assessment[
            "deployment_eligible"
        ],
        "research_failure_reasons": research_assessment[
            "failure_reasons"
        ],
        "amount_data_real": amount_data_real,
        "amount_data_source": amount_data_source,
        "amount_data_coverage": amount_data_coverage,
        "latest_amount_data_coverage": latest_amount_data_coverage,
        "st_status_coverage": st_status_coverage,
        "latest_st_status_coverage": latest_st_status_coverage,
    }
    if wf.get("windows"):
        latest_window = wf["windows"][-1]
        model_runtime_diagnostics["rolling_protocol_diagnostics"] = {
            "training_diagnostics": (
                latest_window.get("training_diagnostics") or {}
            ),
            "validation_diagnostics": (
                latest_window.get("validation_diagnostics") or {}
            ),
            "test_diagnostics": (
                latest_window.get("test_diagnostics") or {}
            ),
            "selection_turnover": latest_window.get(
                "selection_turnover"
            ),
            "test_start": latest_window.get("test_start"),
            "test_end": latest_window.get("test_end"),
            "purged_date_count": latest_window.get(
                "purged_date_count",
                0,
            ),
            "purged_sample_count": latest_window.get(
                "purged_sample_count",
                0,
            ),
        }
    live_feedback: dict[str, dict[str, Any]] = {}
    live_feedback_health: dict[str, Any] = {"ok": False, "reason": "not_loaded"}
    if feedback_user_id is None:
        live_feedback_health = {
            "ok": False,
            "reason": "feedback_owner_unavailable",
        }
    elif not load_live_feedback:
        live_feedback_health = {
            "ok": False,
            "reason": "feedback_loading_disabled",
        }
    else:
        try:
            from analytics import live_factor_feedback

            sync_result = live_factor_feedback.sync_paper_trades(
                days=max(days, 180)
            )
            live_feedback = live_factor_feedback.build_code_feedback(
                days=90,
                user_id=feedback_user_id,
                strategy_key=STRATEGY_KEY,
                model_version=MODEL_VERSION,
            )
            live_feedback_health = live_factor_feedback.feedback_health()
            live_feedback_health["sync"] = sync_result
        except Exception as exc:
            live_feedback_health = {"ok": False, "error": str(exc)[:160]}
    candidate_features = features
    if not market_signals_required and "market_data_trusted" in candidate_features.columns:
        candidate_features = candidate_features.drop(columns=["market_data_trusted"])
    candidates = build_latest_candidates(
        candidate_features,
        top_k=top_k,
        hold_days=hold_days,
        stop_loss=stop_loss,
        take_profit=take_profit,
        candidate_pool_size=candidate_pool_size,
        mining_rules=rules,
        live_feedback=live_feedback,
        factor_model=latest_model,
        research_assessment=research_assessment,
    )
    latest_market = _latest_market_summary(features)

    # ── 今日市场状态门：不过门 → 候选全体降级 + 明确"建议观望" ──
    gate = rules.get("market_gate") or {}
    gate_today: dict[str, Any] = {"enabled": bool(gate.get("enabled"))}
    if gate.get("enabled"):
        emotion_today = latest_market.get("market_emotion_score")
        delta_today = _latest_emotion_delta(features)
        gate_pass, gate_reason = _gate_check(gate, emotion_today, delta_today)
        gate_today.update({
            "pass": gate_pass,
            "reason": gate_reason,
            "reason_cn": _GATE_REASON_TEXT.get(gate_reason, gate_reason),
            "emotion": emotion_today,
            "emotion_delta": delta_today,
        })
        if not gate_pass:
            for candidate in candidates:
                if str(candidate.get("candidate_tier") or "") != "observe":
                    candidate["tier_downgraded_by_gate"] = True
                candidate["candidate_tier"] = "observe"
                candidate["position_pct"] = 0.0
                candidate["live_confidence"] = 0.0
                candidate["live_ready_score"] = 0.0
                candidate["live_readiness_score"] = 0.0
                candidate["live_readiness_status"] = "avoid"
                candidate["live_readiness_label"] = "市场状态门阻断"
                warns = candidate.setdefault("live_warnings", [])
                keys = candidate.setdefault("live_warning_keys", [])
                if "market_gate_blocked" not in keys:
                    keys.append("market_gate_blocked")
                    warns.append(_LIVE_WARNING_TEXT["market_gate_blocked"])
                factor_explain = candidate.get("factor_explain")
                if isinstance(factor_explain, dict):
                    factor_explain.setdefault("trading_plan", {})["position_pct"] = 0.0
                    factor_explain.setdefault("live_reliability", {}).update(
                        {
                            "score": 0.0,
                            "status": "avoid",
                            "label": "市场状态门阻断",
                            "warning_keys": list(keys),
                            "warnings": list(warns),
                            "summary": _LIVE_WARNING_TEXT["market_gate_blocked"],
                        }
                    )
    else:
        gate_today["pass"] = True
        gate_today["reason"] = "gate_disabled"
        gate_today["reason_cn"] = _GATE_REASON_TEXT["gate_disabled"]

    tier_counts: dict[str, int] = {}
    for candidate in candidates:
        tier = str(candidate.get("candidate_tier", "unknown"))
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
    summary = {
        **wf["summary"],
        "feature_rows": int(len(features)),
        "trusted_feature_rows": trusted_feature_rows,
        "label_rows": int(len(labels)),
        "market_signal_rows": int(0 if market_signal_frame is None else len(market_signal_frame)),
        "market_signals_required": market_signals_required,
        "amount_data_real": amount_data_real,
        "amount_data_source": amount_data_source,
        "amount_data_coverage": amount_data_coverage,
        "latest_amount_data_coverage": latest_amount_data_coverage,
        "amount_data_status": "ok" if amount_data_real else "degraded",
        "st_status_known_rows": st_status_known_rows,
        "st_status_coverage": st_status_coverage,
        "latest_st_status_coverage": latest_st_status_coverage,
        "st_status": "ok" if latest_st_status_coverage >= 0.9999 else "degraded",
        "universe_eligibility_coverage": universe_eligibility_coverage,
        "latest_universe_eligibility_coverage": (
            latest_universe_eligibility_coverage
        ),
        "universe_required_count": universe_audit["required_count"],
        "universe_required_date_count": universe_audit[
            "required_date_count"
        ],
        "universe_sufficient_date_count": universe_audit[
            "sufficient_date_count"
        ],
        "universe_eligible_count_min": universe_audit["count_min"],
        "universe_eligible_count_max": universe_audit["count_max"],
        "latest_universe_eligible_count": universe_audit["latest_count"],
        "panel_stock_count": panel_stock_count,
        "panel_max_abs_close_return": panel_max_abs_close_return,
        "data_quality_assessment": data_quality_assessment,
        "latest_market_data_status": latest_market.get("market_data_status"),
        "latest_market_data_reason": latest_market.get("market_data_reason"),
        "latest_market_emotion_score": latest_market.get("market_emotion_score"),
        "lookahead_policy": "features_current_or_past_labels_t_plus_1",
        "backtest_policy": dict(FROZEN_BACKTEST_POLICY),
        "latest_trade_date": features["trade_date"].max() if not features.empty else None,
        "latest_candidate_count": int(len(candidates)),
        "latest_candidate_tier_counts": tier_counts,
        "live_feedback_code_count": int(len(live_feedback)),
        "live_feedback_user_id": feedback_user_id,
        "live_feedback_updated_at": live_feedback_health.get("updated_at"),
        "live_feedback_status": "ok" if live_feedback_health.get("ok") else "degraded",
        "live_feedback_error": live_feedback_health.get("error") or live_feedback_health.get("reason") or "",
        "gate_today": gate_today,
        "research_status": research_assessment["research_status"],
        "research_deployment_eligible": research_assessment["deployment_eligible"],
        "research_failure_reasons": research_assessment["failure_reasons"],
        "bootstrap_total_return_ci_pct": research_assessment["bootstrap_total_return_ci_pct"],
    }
    summary["gate_note"] = (
        "市场门仅在每个滚动窗口的时间顺序验证集上做开启/关闭选择，"
        "阈值集合固定，外层测试标签不参与选择。"
    )
    min_dates = train_days + test_days
    unique_dates = int(training_features["trade_date"].nunique()) if not training_features.empty else 0
    window_count = int(wf["summary"].get("window_count", 0))
    if market_signals_required and latest_market.get("market_data_status") != "ok":
        status = "degraded"
        market_reason = str(latest_market.get("market_data_reason") or "unknown")
        reason = f"latest market snapshot is not trusted: {market_reason}"
    elif int(wf["summary"].get("untrusted_oos_sessions") or 0) > 0:
        status = "degraded"
        reason = (
            "historical OOS market sessions are untrusted: "
            f"{int(wf['summary']['untrusted_oos_sessions'])}"
        )
    elif int(wf["summary"].get("unevaluable_oos_slots") or 0) > 0:
        status = "degraded"
        reason = (
            "historical OOS outcomes are incomplete: "
            f"{int(wf['summary']['unevaluable_oos_slots'])} slots"
        )
    elif window_count <= 0:
        status = "degraded"
        reason = f"insufficient trusted samples: need {min_dates} feature dates, got {unique_dates}"
    elif st_status_coverage < 0.98 or latest_st_status_coverage < 0.98:
        status = "degraded"
        reason = (
            f"ST status coverage too low: history {st_status_coverage:.1%}, "
            f"latest {latest_st_status_coverage:.1%}"
        )
    elif not amount_data_real:
        status = "degraded"
        reason = (
            f"turnover amount coverage too low: history {amount_data_coverage:.1%}, "
            f"latest {latest_amount_data_coverage:.1%}"
        )
    elif (
        universe_eligibility_coverage < 0.9999
        or latest_universe_eligibility_coverage < 0.9999
    ):
        status = "degraded"
        reason = (
            "point-in-time universe size too low: "
            f"minimum {universe_audit['count_min']}/"
            f"{universe_audit['required_count']}, "
            f"latest {universe_audit['latest_count']}/"
            f"{universe_audit['required_count']}, "
            f"complete dates {universe_eligibility_coverage:.1%}"
        )
    elif data_quality_assessment["status"] != "ok":
        status = "degraded"
        reason = (
            "data quality assessment failed: "
            + ",".join(data_quality_assessment["failure_reasons"])
        )
    else:
        status = "ok"
        reason = "ok"
    report = {
        "status": status,
        "data_status": status,
        "reason": reason,
        "research_status": research_assessment["research_status"],
        "research_assessment": research_assessment,
        "config": report_config,
        "summary": summary,
        "model": latest_model,
        "model_runtime_diagnostics": model_runtime_diagnostics,
        "rolling_latest_model": rolling_latest_model,
        "execution_rules": execution_rules,
        "portfolio": wf.get("portfolio") or {},
        "gated_portfolio": wf.get("gated_portfolio") or {},
        "preregistered_oos": wf.get("preregistered_oos") or {},
        "mining_rules": rules,
        "condition_explain": condition_explain(
            rules,
            selected_market_gate=latest_model.get("selected_market_gate"),
        ),
        "windows": wf["windows"],
        "trades": wf["trades"],
        "latest_candidates": candidates,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_outputs(out_path, features, labels, report, candidates, latest_model)
    return report


def _latest_emotion_delta(features: pd.DataFrame) -> float | None:
    """最新交易日的市场情绪相对前一交易日的变化（升温为正）。"""
    if features.empty or "market_emotion_score" not in features.columns:
        return None
    _emotion_by_date, delta_by_date, _trusted_by_date = (
        _market_emotion_context(features)
    )
    dates = sorted(str(value) for value in features["trade_date"].unique())
    if not dates:
        return None
    delta = delta_by_date.get(dates[-1])
    return None if delta is None else round(float(delta), 4)


def _latest_market_summary(features: pd.DataFrame) -> dict[str, Any]:
    if features.empty or "market_data_status" not in features.columns:
        return {"market_data_status": "not_checked", "market_data_reason": "", "market_emotion_score": None}
    latest_date = features["trade_date"].max()
    latest = features[features["trade_date"] == latest_date].iloc[0]
    return {
        "market_data_status": str(latest.get("market_data_status", "not_checked")),
        "market_data_reason": str(latest.get("market_data_reason", "")),
        "market_emotion_score": _maybe_float(latest.get("market_emotion_score")),
    }

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Relay factor mining research")
    parser.add_argument("--n-stocks", type=int, default=800)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--train-days", type=int, default=60)
    parser.add_argument("--test-days", type=int, default=10)
    parser.add_argument("--hold-days", type=int, default=2)
    parser.add_argument("--stop-loss", type=float, default=-0.035)
    parser.add_argument("--take-profit", type=float, default=0.07)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--candidate-pool-size", type=int, default=None)
    parser.add_argument("--out-dir", type=str, default=None)
    parser.add_argument("--with-market-signals", action="store_true", help="gate research with market breadth and turnover quality signals")
    args = parser.parse_args(argv)
    report = run_relay_research(
        out_dir=args.out_dir,
        n_stocks=args.n_stocks,
        days=args.days,
        train_days=args.train_days,
        test_days=args.test_days,
        hold_days=args.hold_days,
        stop_loss=args.stop_loss,
        take_profit=args.take_profit,
        top_k=args.top_k,
        candidate_pool_size=args.candidate_pool_size,
        include_market_signals=args.with_market_signals,
    )
    print(json.dumps({"status": report["status"], "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"ok", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
