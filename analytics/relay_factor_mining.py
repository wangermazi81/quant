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
from typing import Any, Sequence

import numpy as np
import pandas as pd

from analytics.factors_zoo.scorer import build_panel_from_cache
from analytics.market_signal_quality import apply_market_signals, build_market_signal_frame

_ROOT = Path(__file__).resolve().parent.parent
_OUT_BASE = _ROOT / "data" / "research" / "relay_factor"

_REQUIRED = ("close", "high", "volume")

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

_LIVE_WARNING_TEXT: dict[str, str] = {
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
}

_GATE_REASON_TEXT: dict[str, str] = {
    "pass": "通过：市场情绪处于可接力区间且未在升温追高",
    "gate_disabled": "状态门未启用",
    "gate_data_missing": "市场情绪数据缺失，门保守放行",
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


def condition_explain(rules: dict[str, Any]) -> list[str]:
    weights = rules["weights"]
    risk = rules["risk_thresholds"]
    tiers = rules["tier_thresholds"]
    filters = rules["filters"]
    active_filters = [f"{key}>={value}" for key, value in filters.items() if value not in (None, False)]
    gate = rules.get("market_gate") or {}
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
    amount = p.get("amount", close * volume)

    ret_1 = _safe_div(close, close.shift(1)) - 1.0
    ret_3 = _safe_div(close, close.shift(3)) - 1.0
    ret_5 = _safe_div(close, close.shift(5)) - 1.0
    ret_10 = _safe_div(close, close.shift(10)) - 1.0
    ret_20 = _safe_div(close, close.shift(20)) - 1.0
    volume_ratio_20 = _safe_div(volume, volume.rolling(20, min_periods=10).mean())
    amount_ratio_20 = _safe_div(amount, amount.rolling(20, min_periods=10).mean())
    near_high_20 = _safe_div(close, high.rolling(20, min_periods=10).max())
    drawdown_20 = near_high_20 - 1.0
    volatility_10 = close.pct_change(fill_method=None).rolling(10, min_periods=5).std(ddof=1)
    limit_count_20 = (ret_1 >= 0.095).rolling(20, min_periods=5).sum()

    relay_score = (
        _rank(ret_5) * weights["ret_5"]
        + _rank(ret_10) * weights["ret_10"]
        + _rank(ret_20) * weights["ret_20"]
        + _rank(volume_ratio_20) * weights["volume_ratio_20"]
        + _rank(amount_ratio_20) * weights["amount_ratio_20"]
        + _rank(near_high_20) * weights["near_high_20"]
        + _rank(limit_count_20) * weights["limit_count_20"]
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
    long.index.names = ["trade_date", "ts_code"]
    out = long.reset_index()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y-%m-%d")
    out = out.dropna(subset=["relay_score"]).reset_index(drop=True)
    out["available_at"] = out["trade_date"] + " 15:00:00"
    out["feature_as_of"] = out["trade_date"]
    out["decision_time"] = "next_session"
    out["risk_flags"] = out.apply(lambda row: _risk_flags(row, rules), axis=1)
    return out


def build_label_frame(panel: dict[str, pd.DataFrame], hold_days: int = 2) -> pd.DataFrame:
    """Build forward labels from next-session entry to hold-period exit."""
    if hold_days < 1:
        raise ValueError("hold_days must be positive")
    p = _aligned_panel(panel)
    close = p["close"]
    entry_source = p.get("open", close)
    amount = p.get("amount", close * p["volume"])
    entry = entry_source.shift(-1)
    exit_price = close.shift(-(hold_days + 1))
    prev_close = close
    entry_gap = (_safe_div(entry, prev_close) - 1.0) * 100.0
    entry_amount_ratio = _safe_div(amount, amount.rolling(20, min_periods=5).mean()).shift(-1)
    forward = (_safe_div(exit_price, entry) - 1.0) * 100.0

    rows: list[dict[str, Any]] = []
    dates = list(close.index)
    for i, trade_date in enumerate(dates):
        entry_i = i + 1
        exit_i = i + hold_days + 1
        if exit_i >= len(dates):
            continue
        values = forward.iloc[i].dropna()
        for code, ret in values.items():
            gap = _maybe_float(entry_gap.iloc[i].get(code))
            amount_ratio = _maybe_float(entry_amount_ratio.iloc[i].get(code))
            execution_flags = _entry_execution_flags(gap, amount_ratio)
            blocked = bool(execution_flags)
            live_ret = float(ret) - LIVE_EXECUTION_RULES["round_trip_cost_pct"] - LIVE_EXECUTION_RULES["slippage_pct"]
            rows.append(
                {
                    "trade_date": trade_date.strftime("%Y-%m-%d"),
                    "ts_code": str(code),
                    "entry_date": dates[entry_i].strftime("%Y-%m-%d"),
                    "exit_date": dates[exit_i].strftime("%Y-%m-%d"),
                    "hold_days": int(hold_days),
                    "forward_return_pct": round(float(ret), 4),
                    "live_adjusted_return_pct": round(float(live_ret), 4),
                    "entry_gap_pct": gap,
                    "entry_amount_ratio_20": amount_ratio,
                    "execution_blocked": blocked,
                    "execution_flags": execution_flags,
                    "label_source": "t_plus_1_entry_to_hold_exit",
                }
            )
    return pd.DataFrame(rows)


def _entry_execution_flags(entry_gap_pct: float | None, entry_amount_ratio_20: float | None) -> list[str]:
    flags: list[str] = []
    if entry_gap_pct is not None:
        if entry_gap_pct >= LIVE_EXECUTION_RULES["near_limit_gap_pct"]:
            flags.append("entry_near_limit_up")
        elif entry_gap_pct > LIVE_EXECUTION_RULES["high_open_block_pct"]:
            flags.append("entry_high_open")
        elif entry_gap_pct < LIVE_EXECUTION_RULES["weak_open_block_pct"]:
            flags.append("entry_weak_open")
    if entry_amount_ratio_20 is not None and entry_amount_ratio_20 < LIVE_EXECUTION_RULES["low_amount_ratio"]:
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


def _gate_check(
    gate: dict[str, Any] | None,
    emotion: float | None,
    delta: float | None,
) -> tuple[bool, str]:
    """市场状态门判定。返回 (是否放行, 原因键)。

    数据缺失时保守放行（gate_data_missing）——门只在有据可依时收紧，
    绝不因数据缺口静默把整段回测清零。
    """
    if not gate or not gate.get("enabled"):
        return True, "gate_disabled"
    if emotion is None:
        return True, "gate_data_missing"
    if emotion < float(gate.get("emo_min", 0.0)):
        return False, "emotion_too_cold"
    if emotion >= float(gate.get("emo_max", 100.0)):
        return False, "emotion_overheated"
    if gate.get("require_cooling"):
        if delta is None:
            return True, "gate_data_missing"
        if delta > 0:
            return False, "emotion_heating_up"
    return True, "pass"


def walk_forward_relay(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    train_days: int = 60,
    test_days: int = 10,
    step_days: int | None = None,
    top_k: int = 5,
    market_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """滚动窗口回测。双轨输出：

    - 原轨（ungated）：每个测试日照旧成篮，与历史报告可比；
    - 门轨（gated_*）：仅统计通过市场状态门的日子——这才是建议实际执行的口径。
    每笔 trade 记录当日情绪/情绪变化/门判定，复盘归因不再需要回拼 features.csv。
    """
    if train_days <= 0 or test_days <= 0 or top_k <= 0:
        raise ValueError("train_days, test_days and top_k must be positive")
    data = features.merge(labels, on=["trade_date", "ts_code"], how="inner")
    empty_summary = {
        **_trade_summary([]), **_live_summary_fields([], [], []),
        "window_count": 0, "positive_window_rate": 0.0,
        **{f"gated_{k}": v for k, v in _trade_summary([]).items()},
        "gated_live_total_return_pct": 0.0, "gated_skipped_days": 0,
        "market_gate_applied": bool(market_gate and market_gate.get("enabled")),
    }
    if data.empty:
        return {"windows": [], "trades": [], "summary": empty_summary}

    # 每个交易日的市场情绪与前日差（列缺失=门自动失效并如实标注）
    has_emotion = "market_emotion_score" in data.columns
    emo_by_date: dict[str, float] = {}
    delta_by_date: dict[str, float] = {}
    if has_emotion:
        emo_series = (
            data.groupby("trade_date")["market_emotion_score"].first().sort_index()
        )
        prev = emo_series.shift(1)
        for d in emo_series.index:
            v = emo_series.get(d)
            p = prev.get(d)
            if v is not None and not pd.isna(v):
                emo_by_date[str(d)] = float(v)
                if p is not None and not pd.isna(p):
                    delta_by_date[str(d)] = float(v) - float(p)

    dates = sorted(data["trade_date"].unique().tolist())
    step = int(step_days or test_days)
    windows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    start = 0
    while start + train_days + test_days <= len(dates):
        train_dates = dates[start : start + train_days]
        test_dates = dates[start + train_days : start + train_days + test_days]
        train = data[data["trade_date"].isin(train_dates)]
        corr = train[["relay_score", "forward_return_pct"]].corr().iloc[0, 1]
        direction = 1 if pd.isna(corr) or corr >= 0 else -1
        window_returns: list[float] = []
        window_live_returns: list[float] = []
        window_blocked_counts: list[int] = []
        gated_window_returns: list[float] = []
        gated_window_live_returns: list[float] = []
        gated_skipped = 0
        for trade_date in test_dates:
            day = data[data["trade_date"] == trade_date].copy()
            if day.empty:
                continue
            day["rank_score"] = day["relay_score"] * direction
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
            live_ret = round(float(executable[live_field].mean()), 4) if not executable.empty else 0.0
            codes = picks["ts_code"].astype(str).tolist()
            emotion = emo_by_date.get(str(trade_date))
            delta = delta_by_date.get(str(trade_date))
            gate_pass, gate_reason = _gate_check(market_gate, emotion, delta)
            trades.append({
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
            })
            window_returns.append(ret)
            window_live_returns.append(live_ret)
            window_blocked_counts.append(blocked_count)
            if gate_pass:
                gated_window_returns.append(ret)
                gated_window_live_returns.append(live_ret)
            else:
                gated_skipped += 1
        ws = _trade_summary(window_returns)
        live_ws = _live_summary_fields(window_returns, window_live_returns, window_blocked_counts, top_k=top_k)
        gated_ws = {f"gated_{k}": v for k, v in _trade_summary(gated_window_returns).items()}
        windows.append(
            {
                "train_start": train_dates[0],
                "train_end": train_dates[-1],
                "test_start": test_dates[0],
                "test_end": test_dates[-1],
                "direction": direction,
                "train_corr": None if pd.isna(corr) else round(float(corr), 6),
                **ws,
                **live_ws,
                **gated_ws,
                "gated_live_total_return_pct": round(float(sum(gated_window_live_returns)), 4),
                "gated_skipped_days": gated_skipped,
            }
        )
        start += step
    raw_returns = [float(t["return_pct"]) for t in trades]
    live_returns = [float(t.get("live_adjusted_return_pct", t["return_pct"])) for t in trades]
    blocked_counts = [int(t.get("blocked_count", 0)) for t in trades]
    summary = _trade_summary(raw_returns)
    summary.update(_live_summary_fields(raw_returns, live_returns, blocked_counts, top_k=top_k))
    summary["window_count"] = len(windows)
    summary["positive_window_rate"] = round(sum(1 for w in windows if w["total_return_pct"] > 0) / len(windows), 4) if windows else 0.0
    # 门轨总览
    gated_trades = [t for t in trades if t.get("gate_pass")]
    gated_raw = [float(t["return_pct"]) for t in gated_trades]
    gated_live = [float(t.get("live_adjusted_return_pct", t["return_pct"])) for t in gated_trades]
    summary.update({f"gated_{k}": v for k, v in _trade_summary(gated_raw).items()})
    summary["gated_live_total_return_pct"] = round(float(sum(gated_live)), 4)
    summary["gated_live_hit_rate"] = round(sum(1 for r in gated_live if r > 0) / len(gated_live), 4) if gated_live else 0.0
    summary["gated_skipped_days"] = int(len(trades) - len(gated_trades))
    summary["market_gate_applied"] = bool(market_gate and market_gate.get("enabled") and has_emotion)
    if market_gate and market_gate.get("enabled") and not has_emotion:
        summary["market_gate_note"] = "market_emotion_score column missing; gate inactive"
    return {"windows": windows, "trades": trades, "summary": summary}


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


def _score_inputs(row: pd.Series, rules: dict[str, Any]) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
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
        "factor_version": "relay_factor_mining_v1",
        "factor_title": "二三板接力情绪强势延续",
        "summary": f"二三板接力/强势延续复合因子：{tier_text}，评分{round(float(score), 2)}，置信度{round(float(confidence), 4)}。",
        "matched_factors": matched,
        "risk_factors": risks,
        "risk_summary": risk_summary,
        "live_reliability": live_readiness or {},
        "score_inputs": _score_inputs(row, rules),
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
            "stop_loss": -0.035,
            "take_profit": 0.07,
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


def build_latest_candidates(
    features: pd.DataFrame,
    top_k: int = 10,
    hold_days: int = 2,
    candidate_pool_size: int | None = None,
    mining_rules: dict[str, Any] | None = None,
    live_feedback: dict[str, dict[str, Any]] | None = None,
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
    latest["_live_ready_score"] = latest.apply(
        lambda row: float(row.get("relay_score", 0.0) or 0.0) * (0.65 + 0.35 * float((row.get("_live_readiness") or {}).get("score", 1.0))),
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
        risk_flags = list(row.get("_risk_flags_list") or [flag for flag in str(row.get("risk_flags", "")).split(",") if flag])
        live_readiness = dict(row.get("_live_readiness") or _candidate_live_readiness(row, risk_flags, live_feedback=live_feedback))
        rank = int(idx) + 1
        score_percentile = 1.0 - ((rank - 1) / max(latest_count - 1, 1))
        confidence = max(0.0, min(1.0, score / 100.0 * 0.45 + score_percentile * 0.55 - min(len(risk_flags), 3) * 0.03))
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
            live_readiness=live_readiness,
        )
        candidates.append(
            {
                "ts_code": str(row["ts_code"]),
                "code": str(row["ts_code"]).split(".")[0],
                "name": _resolve_stock_name(row["ts_code"]),
                "trade_date": str(row["trade_date"]),
                "strategy_key": "relay_factor_mining_v1",
                "candidate_rank": rank,
                "candidate_tier": tier,
                "candidate_pool_size": pool_size,
                "latest_universe_size": latest_count,
                "score_percentile": round(score_percentile, 4),
                "relay_score": round(score, 4),
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
                "stop_loss": -0.035,
                "take_profit": 0.07,
                "cancel_conditions": ["market_emotion_drops", "limit_board_opens_repeatedly", "volume_confirm_fails"],
                "hold_days": int(hold_days),
                "available_at": str(row.get("available_at", "")),
                "research_only": True,
            }
        )
    return candidates


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


def _write_outputs(out_dir: Path, features: pd.DataFrame, labels: pd.DataFrame, report: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    features.to_csv(out_dir / "features.csv", index=False, encoding="utf-8-sig")
    labels.to_csv(out_dir / "labels.csv", index=False, encoding="utf-8-sig")
    (out_dir / "walkforward_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "latest_candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")


def run_relay_research(
    panel: dict[str, pd.DataFrame] | None = None,
    out_dir: str | Path | None = None,
    n_stocks: int = 800,
    days: int = 180,
    train_days: int = 60,
    test_days: int = 10,
    step_days: int | None = None,
    hold_days: int = 2,
    top_k: int = 5,
    candidate_pool_size: int | None = None,
    market_signal_frame: pd.DataFrame | None = None,
    include_market_signals: bool = False,
    mining_rules: dict[str, Any] | None = None,
    require_latest_data: bool = True,
) -> dict[str, Any]:
    rules = normalize_mining_rules(mining_rules)
    if panel is None:
        panel, _codes = build_panel_from_cache(
            n_stocks=n_stocks,
            days=days,
            min_history=min(days, 60),
            require_latest=require_latest_data,
        )
    out_path = Path(out_dir) if out_dir is not None else _OUT_BASE / datetime.now().strftime("%Y%m%d-%H%M%S")
    features_raw = build_feature_frame(panel, mining_rules=rules)
    labels = build_label_frame(panel, hold_days=hold_days)
    if market_signal_frame is None and include_market_signals:
        market_signal_frame = build_market_signal_frame(features_raw["trade_date"].unique())
    features = apply_market_signals(features_raw, market_signal_frame)
    training_features = features[features["market_data_trusted"]] if "market_data_trusted" in features.columns else features
    wf = walk_forward_relay(
        training_features, labels,
        train_days=train_days, test_days=test_days, step_days=step_days, top_k=top_k,
        market_gate=rules.get("market_gate"),
    )
    live_feedback: dict[str, dict[str, Any]] = {}
    live_feedback_health: dict[str, Any] = {"ok": False, "reason": "not_loaded"}
    try:
        from analytics import live_factor_feedback

        sync_result = live_factor_feedback.sync_paper_trades(days=max(days, 180))
        live_feedback = live_factor_feedback.build_code_feedback(days=90)
        live_feedback_health = live_factor_feedback.feedback_health()
        live_feedback_health["sync"] = sync_result
    except Exception as exc:
        live_feedback_health = {"ok": False, "error": str(exc)[:160]}
    candidates = build_latest_candidates(
        features,
        top_k=top_k,
        hold_days=hold_days,
        candidate_pool_size=candidate_pool_size,
        mining_rules=rules,
        live_feedback=live_feedback,
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
            downgrade = {"strong": "watch", "watch": "observe"}
            for candidate in candidates:
                tier = str(candidate.get("candidate_tier") or "")
                if tier in downgrade:
                    candidate["candidate_tier"] = downgrade[tier]
                    candidate["tier_downgraded_by_gate"] = True
                warns = candidate.setdefault("live_warnings", [])
                keys = candidate.setdefault("live_warning_keys", [])
                if "market_gate_blocked" not in keys:
                    keys.append("market_gate_blocked")
                    warns.append(_LIVE_WARNING_TEXT["market_gate_blocked"])
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
        "latest_market_data_status": latest_market.get("market_data_status"),
        "latest_market_data_reason": latest_market.get("market_data_reason"),
        "latest_market_emotion_score": latest_market.get("market_emotion_score"),
        "lookahead_policy": "features_current_or_past_labels_t_plus_1",
        "latest_trade_date": features["trade_date"].max() if not features.empty else None,
        "latest_candidate_count": int(len(candidates)),
        "latest_candidate_tier_counts": tier_counts,
        "live_feedback_code_count": int(len(live_feedback)),
        "live_feedback_updated_at": live_feedback_health.get("updated_at"),
        "live_feedback_status": "ok" if live_feedback_health.get("ok") else "degraded",
        "live_feedback_error": live_feedback_health.get("error") or live_feedback_health.get("reason") or "",
        "gate_today": gate_today,
        "gate_note": (
            "市场状态门参数在历史样本内校准（in-sample）；报告同时保留 门前/门后 双轨指标，"
            "若未来窗口门后持续跑输门前，应重新校准或停用该门。"
        ) if gate.get("enabled") else "",
    }
    min_dates = train_days + test_days
    unique_dates = int(training_features["trade_date"].nunique()) if not training_features.empty else 0
    status = "ok" if wf["summary"].get("window_count", 0) > 0 else "degraded"
    reason = "ok" if status == "ok" else f"insufficient trusted samples: need {min_dates} feature dates, got {unique_dates}"
    report = {
        "status": status,
        "reason": reason,
        "config": {
            "n_stocks": n_stocks,
            "days": days,
            "train_days": train_days,
            "test_days": test_days,
            "step_days": step_days or test_days,
            "hold_days": hold_days,
            "top_k": top_k,
            "candidate_pool_size": _candidate_pool_size(top_k, candidate_pool_size),
            "include_market_signals": include_market_signals or market_signal_frame is not None,
        },
        "summary": summary,
        "mining_rules": rules,
        "condition_explain": condition_explain(rules),
        "windows": wf["windows"],
        "trades": wf["trades"],
        "latest_candidates": candidates,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_outputs(out_path, features, labels, report, candidates)
    return report


def _latest_emotion_delta(features: pd.DataFrame) -> float | None:
    """最新交易日的市场情绪相对前一交易日的变化（升温为正）。"""
    if features.empty or "market_emotion_score" not in features.columns:
        return None
    series = features.groupby("trade_date")["market_emotion_score"].first().sort_index()
    if len(series) < 2:
        return None
    last, prev = series.iloc[-1], series.iloc[-2]
    if pd.isna(last) or pd.isna(prev):
        return None
    return round(float(last) - float(prev), 4)


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
        top_k=args.top_k,
        candidate_pool_size=args.candidate_pool_size,
        include_market_signals=args.with_market_signals,
    )
    print(json.dumps({"status": report["status"], "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"ok", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
