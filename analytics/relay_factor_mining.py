# -*- coding: utf-8 -*-
"""Relay factor mining pipeline for AI trading research.

The module is research-only. Feature rows are computed from current and past
bars, while labels are generated separately from T+1/T+n prices. Candidate
output is suitable for AI-trading review and paper-trading shadow runs, not for
bypassing live risk controls.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from analytics.factors_zoo.scorer import MAX_ABS_DAILY_RETURN, build_panel_from_cache
from analytics.relay_factor_model import (
    MIN_REAL_AMOUNT_COVERAGE,
    MODEL_VERSION,
    factor_diagnostics,
    fit_factor_model,
    score_factor_model,
    select_factor_model,
    selection_turnover,
)
from analytics.market_signal_quality import apply_market_signals, build_market_signal_frame
from analytics.relay_portfolio import build_trade_path, limit_up_threshold, simulate_portfolio

_ROOT = Path(__file__).resolve().parent.parent
_OUT_BASE = _ROOT / "data" / "research" / "relay_factor"

_REQUIRED = ("close", "high", "volume")
STRATEGY_KEY = "relay_factor_mining_v2"

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

RESEARCH_VALIDATION_RULES: dict[str, float | int] = {
    "minimum_oos_windows": 12,
    "minimum_positive_window_rate": 0.50,
    "minimum_validation_acceptance_rate": 0.50,
    "minimum_executed_trades": 100,
    "maximum_drawdown_pct": -15.0,
    "minimum_bootstrap_lower_return_pct": 0.0,
    "prospective_sessions_required": 20,
}

PROSPECTIVE_ACCEPTANCE_RULES: dict[str, float | int] = {
    "required_sessions": 20,
    "minimum_total_return_pct": 0.0,
    "maximum_drawdown_pct": -10.0,
    "minimum_executed_trades": 20,
    "maximum_protocol_violations": 0,
}

_LIVE_WARNING_TEXT: dict[str, str] = {
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
    rules["market_gate"] = {
        "enabled": bool(gate_raw.get("enabled", gate_defaults["enabled"])),
        "emo_min": emo_min,
        "emo_max": emo_max,
        "require_cooling": bool(gate_raw.get("require_cooling", gate_defaults["require_cooling"])),
    }
    rules["descriptions"] = _RULE_DESCRIPTIONS
    return rules


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
        amount_real = pd.DataFrame("amount" in p, index=close.index, columns=close.columns)
    if "universe_eligible" in p:
        universe_eligible = p["universe_eligible"].fillna(0.0).astype(bool)
    else:
        universe_eligible = pd.DataFrame(True, index=close.index, columns=close.columns)
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
    limit_count_rank = _rank(limit_count_20).fillna(0.5)

    relay_score = (
        _rank(ret_5) * weights["ret_5"]
        + _rank(ret_10) * weights["ret_10"]
        + _rank(ret_20) * weights["ret_20"]
        + _rank(volume_ratio_20) * weights["volume_ratio_20"]
        + _rank(amount_ratio_20).fillna(0.5) * weights["amount_ratio_20"]
        + _rank(near_high_20) * weights["near_high_20"]
        + limit_count_rank * weights["limit_count_20"]
        + _rank(volatility_10) * weights["volatility_10"]
        + _rank(ret_3.clip(lower=0.0)) * weights["overheat_ret_3"]
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
    long = None
    for name, frame in fields.items():
        series = frame.stack(future_stack=True).rename(name)
        long = series.to_frame() if long is None else long.join(series, how="outer")
    assert long is not None
    long = long.join(amount_real.stack(future_stack=True).rename("amount_data_real"), how="left")
    long = long.join(universe_eligible.stack(future_stack=True).rename("universe_eligible"), how="left")
    long = long.join(is_st.stack(future_stack=True).rename("is_st"), how="left")
    long = long.join(st_status_known.stack(future_stack=True).rename("st_status_known"), how="left")
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
) -> pd.DataFrame:
    """Build forward labels from next-session entry to hold-period exit."""
    if hold_days < 1:
        raise ValueError("hold_days must be positive")
    p = _aligned_panel(panel)
    close = p["close"]
    open_price = p.get("open", close)
    high = p.get("high", close)
    low = p.get("low", close)
    amount = p.get("amount", close * p["volume"])
    raw_is_st = p.get("is_st")
    if raw_is_st is not None:
        if "st_status_known" in p:
            st_status_known = p["st_status_known"].fillna(0.0).astype(bool)
        else:
            st_status_known = raw_is_st.notna()
        is_st = raw_is_st.fillna(0.0).astype(bool) & st_status_known
    else:
        is_st = pd.DataFrame(False, index=close.index, columns=close.columns)
        st_status_known = pd.DataFrame(False, index=close.index, columns=close.columns)
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

    rows: list[dict[str, Any]] = []
    dates = list(close.index)
    for i, trade_date in enumerate(dates):
        max_exit_i = i + hold_days + 1
        if max_exit_i >= len(dates):
            continue
        for code in close.columns:
            path = build_trade_path(
                code=str(code),
                bars=bars_by_code[str(code)],
                signal_index=i,
                hold_days=hold_days,
                stop_loss=stop_loss,
                take_profit=take_profit,
                round_trip_cost_pct=LIVE_EXECUTION_RULES["round_trip_cost_pct"],
                slippage_pct=LIVE_EXECUTION_RULES["slippage_pct"],
            )
            if not path:
                continue
            entry_price_value = _maybe_float(entry.iloc[i].get(code))
            fixed_exit_price = _maybe_float(close.iloc[max_exit_i].get(code))
            if entry_price_value is None or fixed_exit_price is None or entry_price_value <= 0:
                continue
            forward_ret = (fixed_exit_price / entry_price_value - 1.0) * 100.0
            gap = _maybe_float(entry_gap.iloc[i].get(code))
            amount_ratio = _maybe_float(signal_amount_ratio.iloc[i].get(code))
            st_known_value = bool(st_status_known.iloc[i + 1].get(code))
            is_st_value = bool(is_st.iloc[i + 1].get(code)) if st_known_value else False
            execution_flags = _entry_execution_flags(
                gap,
                amount_ratio,
                code=str(code),
                entry_date=path["entry_date"],
                is_st=is_st_value if st_known_value else True,
            )
            blocked = bool(execution_flags)
            live_ret = float(path["live_adjusted_return_pct"])
            rows.append(
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
                    "execution_blocked": blocked,
                    "execution_flags": execution_flags,
                    "label_source": "t_plus_1_entry_to_trade_plan_exit",
                    "path_policy": path["path_policy"],
                }
            )
    return pd.DataFrame(rows)


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
    equity = np.cumprod(1.0 + arr / 100.0)
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


def _portfolio_attempt(row: pd.Series, gate_pass: bool) -> dict[str, Any]:
    trade_date = str(row.get("trade_date") or "")
    entry_date = str(row.get("entry_date") or trade_date)
    exit_date = str(row.get("exit_date") or entry_date)
    live_return = _maybe_float(row.get("live_adjusted_return_pct"))
    if live_return is None:
        live_return = float(row.get("forward_return_pct") or 0.0)
    blocked_value = row.get("execution_blocked", False)
    blocked = False if blocked_value is None or pd.isna(blocked_value) else bool(blocked_value)
    path = row.get("valuation_path")
    if not isinstance(path, list):
        path = [{"date": exit_date, "return_pct": live_return}]
    return {
        "trade_date": trade_date,
        "ts_code": str(row.get("ts_code") or ""),
        "entry_date": entry_date,
        "exit_date": exit_date,
        "execution_blocked": blocked,
        "live_adjusted_return_pct": float(live_return),
        "valuation_path": path,
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

    feature_data = features.copy()
    label_data = labels.copy()
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
        data.loc[missing_path, column] = 0.0

    if "execution_blocked" not in data.columns:
        data["execution_blocked"] = False
    data["execution_blocked"] = data["execution_blocked"].map(
        lambda value: bool(value) if value is not None and not pd.isna(value) else False
    )
    data.loc[missing_path, "execution_blocked"] = True

    for column in ("entry_date", "exit_date"):
        if column not in data.columns:
            data[column] = None
        data.loc[missing_path, column] = data.loc[missing_path, "trade_date"]

    if "exit_reason" not in data.columns:
        data["exit_reason"] = None
    data.loc[missing_path, "exit_reason"] = "missing_forward_execution_path"
    if "label_source" not in data.columns:
        data["label_source"] = None
    data.loc[missing_path, "label_source"] = "missing_forward_execution_path"

    if "execution_flags" not in data.columns:
        data["execution_flags"] = None
    if "valuation_path" not in data.columns:
        data["valuation_path"] = None
    for index in data.index[missing_path]:
        trade_date = str(data.at[index, "trade_date"])
        data.at[index, "execution_flags"] = ["missing_forward_execution_path"]
        data.at[index, "valuation_path"] = [{"date": trade_date, "return_pct": 0.0}]
    return _ensure_model_target(data)


def walk_forward_relay(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    train_days: int = 60,
    test_days: int = 10,
    step_days: int | None = None,
    top_k: int = 5,
    market_gate: dict[str, Any] | None = None,
    nested_gate_selection: bool = True,
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
    latest_model = fit_factor_model(pd.DataFrame(), target_col="model_target_pct")
    start = 0
    while start + train_days + test_days <= len(dates):
        raw_train_dates = dates[start : start + train_days]
        test_dates = dates[start + train_days : start + train_days + test_days]
        raw_train = data[data["trade_date"].isin(raw_train_dates)].copy()
        train = raw_train
        if "exit_date" in raw_train.columns:
            exits = pd.to_datetime(raw_train["exit_date"], errors="coerce")
            test_start_ts = pd.Timestamp(test_dates[0])
            train = raw_train[exits < test_start_ts].copy()
        train_dates = sorted(train["trade_date"].astype(str).unique().tolist())
        raw_fit_dates, validation_dates = _fit_validation_dates(train_dates)
        raw_fit_frame = train[train["trade_date"].isin(raw_fit_dates)].copy()
        fit_frame = raw_fit_frame
        if validation_dates and "exit_date" in raw_fit_frame.columns:
            fit_exits = pd.to_datetime(raw_fit_frame["exit_date"], errors="coerce")
            validation_start_ts = pd.Timestamp(validation_dates[0])
            fit_frame = raw_fit_frame[fit_exits < validation_start_ts].copy()
        fit_dates = sorted(fit_frame["trade_date"].astype(str).unique().tolist())
        validation_frame = train[train["trade_date"].isin(validation_dates)].copy()
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
                "validation_accepted": bool(selection["validation_accepted"]),
                "validation_rejection_reason": selection["validation_rejection_reason"],
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
                "refit_policy": "selected_hyperparameters_on_full_purged_train",
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
        for trade_date in test_dates:
            day = data[data["trade_date"] == trade_date].copy()
            if day.empty:
                continue
            day["factor_model_score"] = score_factor_model(day, model)
            day["rank_score"] = day["factor_model_score"]
            if model.get("fallback"):
                day["rank_score"] = day["rank_score"] * direction
            window_scored_rows.append(day)
            picks = day.sort_values("rank_score", ascending=False).head(top_k)
            if picks.empty:
                continue
            ret = round(float(picks["forward_return_pct"].mean()), 4)
            if "execution_blocked" in picks.columns:
                executable = picks[~picks["execution_blocked"].map(lambda value: bool(value))]
            else:
                executable = picks
            blocked_count = int(len(picks) - len(executable))
            live_field = "live_adjusted_return_pct" if "live_adjusted_return_pct" in executable.columns else "forward_return_pct"
            live_ret = round(float(executable[live_field].sum()) / float(top_k), 4) if not executable.empty else 0.0
            codes = picks["ts_code"].astype(str).tolist()
            picks_by_date.append(codes)
            emotion = emo_by_date.get(str(trade_date))
            delta = delta_by_date.get(str(trade_date))
            market_gate_pass, gate_reason = _gate_check(selected_market_gate, emotion, delta)
            validation_accepted = bool(model.get("validation_accepted"))
            gate_pass = market_gate_pass and validation_accepted
            if not validation_accepted:
                gate_reason = str(model.get("validation_rejection_reason") or "model_validation_rejected")
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
            }
        )
        start += step
    raw_returns = [float(t["return_pct"]) for t in trades]
    live_returns = [float(t.get("live_adjusted_return_pct", t["return_pct"])) for t in trades]
    blocked_counts = [int(t.get("blocked_count", 0)) for t in trades]
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
        if window_model.get("validation_accepted"):
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
    # 门轨总览
    gated_trades = [t for t in trades if t.get("gate_pass")]
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
    return {
        "windows": windows,
        "trades": trades,
        "summary": summary,
        "portfolio": portfolio,
        "gated_portfolio": gated_portfolio,
        "latest_model": latest_model,
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


def assess_research_result(
    walk_forward: dict[str, Any],
    *,
    prospective_sessions: int = 0,
) -> dict[str, Any]:
    """Assess historical evidence without allowing it to grant deployment rights."""
    windows = list(walk_forward.get("windows") or [])
    portfolio = walk_forward.get("gated_portfolio") or walk_forward.get("portfolio") or {}
    window_returns = [
        float(window.get("gated_live_total_return_pct", window.get("total_return_pct", 0.0)) or 0.0)
        for window in windows
    ]
    positive_window_rate = (
        sum(1 for value in window_returns if value > 0.0) / len(window_returns)
        if window_returns
        else 0.0
    )
    validation_flags = [
        bool((window.get("model") or {}).get("validation_accepted"))
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

    failure_reasons = []
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
        "oos_consumed": True,
        "failure_reasons": failure_reasons,
        "oos_window_count": len(windows),
        "positive_window_rate": round(float(positive_window_rate), 4),
        "validation_acceptance_rate": round(float(validation_acceptance_rate), 4),
        "oos_total_return_pct": round(total_return, 4),
        "oos_max_drawdown_pct": round(max_drawdown, 4),
        "executed_trade_count": executed_trades,
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
    if not isinstance(assessment, dict) or not bool(
        assessment.get("metric_gate_passed")
        and assessment.get("lock_eligible")
    ):
        raise ValueError("historical research gate did not pass")
    model = report.get("model")
    if (
        not isinstance(model, dict)
        or model.get("fallback") is not False
        or model.get("validation_accepted") is not True
        or not model.get("selected_factors")
    ):
        raise ValueError("validated frozen factor model is unavailable")

    config_raw = report.get("config") if isinstance(report.get("config"), dict) else {}
    config_keys = (
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
    config = {
        key: config_raw[key]
        for key in config_keys
        if key in config_raw
    }
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    mining_rules = (
        report.get("mining_rules")
        if isinstance(report.get("mining_rules"), dict)
        else {}
    )
    reported_execution_rules = (
        report.get("execution_rules")
        if isinstance(report.get("execution_rules"), dict)
        else None
    )
    return {
        "schema_version": 2,
        "strategy_key": STRATEGY_KEY,
        "model": json.loads(json.dumps(model, ensure_ascii=False)),
        "config": json.loads(json.dumps(config, ensure_ascii=False)),
        "execution_rules": normalize_execution_rules(reported_execution_rules),
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
        "backtest_policy": json.loads(
            json.dumps(summary.get("backtest_policy") or {}, ensure_ascii=False)
        ),
        "acceptance_rules": dict(PROSPECTIVE_ACCEPTANCE_RULES),
        "historical_evidence": {
            "generated_at": str(report.get("generated_at") or ""),
            "latest_trade_date": summary.get("latest_trade_date"),
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
    if st_status_unknown:
        score = min(score, 0.77)
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
    model_validation_rejected = active_model.get("validation_accepted") is False
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
    latest["_factor_model_score"] = score_factor_model(latest, active_model)
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
                "strategy_key": STRATEGY_KEY,
                "model_version": str(active_model.get("model_version") or MODEL_VERSION),
                "validation_accepted": active_model.get("validation_accepted"),
                "validation_rejection_reason": active_model.get("validation_rejection_reason"),
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
    model = dict(manifest.get("model") or {}) if isinstance(manifest, dict) else {}
    config = dict(manifest.get("config") or {}) if isinstance(manifest, dict) else {}
    rules = normalize_mining_rules(
        manifest.get("mining_rules") if isinstance(manifest, dict) else None
    )
    if model.get("fallback") is not False or model.get("validation_accepted") is not True:
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
        )
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
    open_series = aligned.get("open", aligned["close"])[column]
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
        bool(aligned["st_status_known"].iloc[entry_index].get(column))
        if "st_status_known" in aligned
        else False
    )
    is_st = (
        bool(aligned["is_st"].iloc[entry_index].get(column))
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
    entry_flags = _entry_execution_flags(
        entry_gap_pct,
        amount_ratio,
        code=code,
        entry_date=entry_date,
        is_st=is_st,
        execution_rules=execution_rules,
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
) -> dict[str, Any]:
    rules = normalize_mining_rules(mining_rules)
    if panel is None:
        panel, _codes = build_panel_from_cache(
            n_stocks=n_stocks,
            days=days,
            min_history=min(days, 60),
            require_latest=require_latest_data,
            point_in_time=True,
        )
    out_path = Path(out_dir) if out_dir is not None else _OUT_BASE / datetime.now().strftime("%Y%m%d-%H%M%S")
    features_raw = build_feature_frame(panel, mining_rules=rules)
    labels = build_label_frame(
        panel,
        hold_days=hold_days,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
    market_signals_required = bool(include_market_signals or market_signal_frame is not None)
    if market_signal_frame is None and include_market_signals:
        market_signal_frame = build_market_signal_frame(features_raw["trade_date"].unique())
    features = apply_market_signals(features_raw, market_signal_frame)
    training_features = (
        features[features["market_data_trusted"]]
        if market_signals_required and "market_data_trusted" in features.columns
        else features
    )
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
    close_frame = panel.get("close")
    panel_stock_count = int(close_frame.shape[1]) if isinstance(close_frame, pd.DataFrame) else 0
    panel_max_abs_close_return = None
    if isinstance(close_frame, pd.DataFrame) and not close_frame.empty:
        max_return = close_frame.pct_change(fill_method=None).abs().max().max()
        if pd.notna(max_return):
            panel_max_abs_close_return = round(float(max_return), 6)
    wf = walk_forward_relay(
        training_features, labels,
        train_days=train_days, test_days=test_days, step_days=step_days, top_k=top_k,
        market_gate=rules.get("market_gate"),
    )
    research_assessment = assess_research_result(wf)
    latest_model = dict(wf.get("latest_model") or fit_factor_model(pd.DataFrame(), target_col="model_target_pct"))
    latest_model.update(
        {
            "research_status": research_assessment["research_status"],
            "deployment_eligible": research_assessment["deployment_eligible"],
            "research_failure_reasons": research_assessment["failure_reasons"],
        }
    )
    if wf.get("windows"):
        latest_window = wf["windows"][-1]
        latest_model.update(
            {
                "training_diagnostics": latest_window.get("training_diagnostics") or {},
                "validation_diagnostics": latest_window.get("validation_diagnostics") or {},
                "test_diagnostics": latest_window.get("test_diagnostics") or {},
                "selection_turnover": latest_window.get("selection_turnover"),
                "test_start": latest_window.get("test_start"),
                "test_end": latest_window.get("test_end"),
                "purged_date_count": latest_window.get("purged_date_count", 0),
                "purged_sample_count": latest_window.get("purged_sample_count", 0),
                "amount_data_real": amount_data_real,
                "amount_data_source": amount_data_source,
                "amount_data_coverage": amount_data_coverage,
                "latest_amount_data_coverage": latest_amount_data_coverage,
                "st_status_coverage": st_status_coverage,
                "latest_st_status_coverage": latest_st_status_coverage,
            }
        )
    live_feedback: dict[str, dict[str, Any]] = {}
    live_feedback_health: dict[str, Any] = {"ok": False, "reason": "not_loaded"}
    try:
        from analytics import live_factor_feedback

        sync_result = live_factor_feedback.sync_paper_trades(days=max(days, 180))
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
        "trusted_feature_rows": int(len(training_features)),
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
        "panel_stock_count": panel_stock_count,
        "panel_max_abs_close_return": panel_max_abs_close_return,
        "latest_market_data_status": latest_market.get("market_data_status"),
        "latest_market_data_reason": latest_market.get("market_data_reason"),
        "latest_market_emotion_score": latest_market.get("market_emotion_score"),
        "lookahead_policy": "features_current_or_past_labels_t_plus_1",
        "backtest_policy": {
            "purge": "exit_date_before_test_start",
            "validation": "chronological_tail_20pct",
            "entry": "next_session_open",
            "same_bar_stop_target": "stop_first_conservative",
            "blocked_slots": "remain_cash_without_redistribution",
            "capital": "cash_aware_overlapping_positions",
            "compounding": True,
        },
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
    else:
        status = "ok"
        reason = "ok"
    report = {
        "status": status,
        "data_status": status,
        "reason": reason,
        "research_status": research_assessment["research_status"],
        "research_assessment": research_assessment,
        "config": {
            "n_stocks": n_stocks,
            "days": days,
            "train_days": train_days,
            "test_days": test_days,
            "step_days": step_days or test_days,
            "hold_days": hold_days,
            "stop_loss": round(float(stop_loss), 4),
            "take_profit": round(float(take_profit), 4),
            "top_k": top_k,
            "candidate_pool_size": _candidate_pool_size(top_k, candidate_pool_size),
            "include_market_signals": include_market_signals or market_signal_frame is not None,
            "model_version": MODEL_VERSION,
            "universe_selection": "point_in_time_eligible_stable_sha256_sample",
            "data_quality_policy": {
                "discontinuous_price_history": "quarantine_from_first_detectable_break",
                "max_abs_daily_return": MAX_ABS_DAILY_RETURN,
                "amount_without_real_cache": "proxy_for_legacy_rules_excluded_from_v2_model",
                "minimum_real_amount_coverage": MIN_REAL_AMOUNT_COVERAGE,
                "st_status_source": "point_in_time_cache_is_st_then_name",
                "st_status_unknown": "assume_5pct_limit_for_execution_and_cap_candidate_at_watch",
            },
        },
        "summary": summary,
        "model": latest_model,
        "execution_rules": normalize_execution_rules(),
        "portfolio": wf.get("portfolio") or {},
        "gated_portfolio": wf.get("gated_portfolio") or {},
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
