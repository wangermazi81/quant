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

_ROOT = Path(__file__).resolve().parent.parent
_OUT_BASE = _ROOT / "data" / "research" / "relay_factor"

_REQUIRED = ("close", "high", "volume")


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


def _risk_flags(row: pd.Series) -> str:
    flags: list[str] = []
    if float(row.get("ret_3", 0.0) or 0.0) > 0.24:
        flags.append("overextended_3d")
    if float(row.get("volatility_10", 0.0) or 0.0) > 0.08:
        flags.append("high_volatility")
    if float(row.get("volume_ratio_20", 1.0) or 1.0) < 0.75:
        flags.append("weak_volume")
    if float(row.get("drawdown_20", 0.0) or 0.0) < -0.18:
        flags.append("deep_drawdown")
    return ",".join(flags)


def build_feature_frame(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Build point-in-time relay features as a long DataFrame.

    Every feature uses only the current row and historical rows. The row is
    marked available at the signal date close (15:00), so consumers must use it
    for next-session decisions unless they have intraday equivalents.
    """
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
        _rank(ret_5) * 0.18
        + _rank(ret_10) * 0.12
        + _rank(ret_20) * 0.12
        + _rank(volume_ratio_20) * 0.16
        + _rank(amount_ratio_20) * 0.10
        + _rank(near_high_20) * 0.16
        + _rank(limit_count_20) * 0.10
        - _rank(volatility_10) * 0.08
        - _rank(ret_3.clip(lower=0.0)) * 0.08
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
    out["risk_flags"] = out.apply(_risk_flags, axis=1)
    return out


def build_label_frame(panel: dict[str, pd.DataFrame], hold_days: int = 2) -> pd.DataFrame:
    """Build forward labels from next-session entry to hold-period exit."""
    if hold_days < 1:
        raise ValueError("hold_days must be positive")
    p = _aligned_panel(panel)
    close = p["close"]
    entry_source = p.get("open", close)
    entry = entry_source.shift(-1)
    exit_price = close.shift(-(hold_days + 1))
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
            rows.append(
                {
                    "trade_date": trade_date.strftime("%Y-%m-%d"),
                    "ts_code": str(code),
                    "entry_date": dates[entry_i].strftime("%Y-%m-%d"),
                    "exit_date": dates[exit_i].strftime("%Y-%m-%d"),
                    "hold_days": int(hold_days),
                    "forward_return_pct": round(float(ret), 4),
                    "label_source": "t_plus_1_entry_to_hold_exit",
                }
            )
    return pd.DataFrame(rows)


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


def walk_forward_relay(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    train_days: int = 60,
    test_days: int = 10,
    step_days: int | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    if train_days <= 0 or test_days <= 0 or top_k <= 0:
        raise ValueError("train_days, test_days and top_k must be positive")
    data = features.merge(labels, on=["trade_date", "ts_code"], how="inner")
    if data.empty:
        return {"windows": [], "trades": [], "summary": {**_trade_summary([]), "window_count": 0, "positive_window_rate": 0.0}}
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
        for trade_date in test_dates:
            day = data[data["trade_date"] == trade_date].copy()
            if day.empty:
                continue
            day["rank_score"] = day["relay_score"] * direction
            picks = day.sort_values("rank_score", ascending=False).head(top_k)
            if picks.empty:
                continue
            ret = round(float(picks["forward_return_pct"].mean()), 4)
            codes = picks["ts_code"].astype(str).tolist()
            trades.append({"trade_date": trade_date, "direction": direction, "codes": codes, "return_pct": ret})
            window_returns.append(ret)
        ws = _trade_summary(window_returns)
        windows.append(
            {
                "train_start": train_dates[0],
                "train_end": train_dates[-1],
                "test_start": test_dates[0],
                "test_end": test_dates[-1],
                "direction": direction,
                "train_corr": None if pd.isna(corr) else round(float(corr), 6),
                **ws,
            }
        )
        start += step
    summary = _trade_summary([float(t["return_pct"]) for t in trades])
    summary["window_count"] = len(windows)
    summary["positive_window_rate"] = round(sum(1 for w in windows if w["total_return_pct"] > 0) / len(windows), 4) if windows else 0.0
    return {"windows": windows, "trades": trades, "summary": summary}


def build_latest_candidates(features: pd.DataFrame, top_k: int = 10, hold_days: int = 2) -> list[dict[str, Any]]:
    if features.empty:
        return []
    latest_date = features["trade_date"].max()
    latest = features[features["trade_date"] == latest_date].copy()
    latest = latest.sort_values("relay_score", ascending=False).head(top_k)
    candidates: list[dict[str, Any]] = []
    for _, row in latest.iterrows():
        score = float(row["relay_score"])
        risk_flags = [flag for flag in str(row.get("risk_flags", "")).split(",") if flag]
        confidence = max(0.0, min(1.0, score / 100.0))
        position_pct = 0.08 if risk_flags else 0.12
        candidates.append(
            {
                "ts_code": str(row["ts_code"]),
                "name": str(row["ts_code"]),
                "trade_date": str(row["trade_date"]),
                "strategy_key": "relay_factor_mining_v1",
                "relay_score": round(score, 4),
                "confidence": round(confidence, 4),
                "market_regime": "unknown",
                "positive_factors": _positive_factors(row),
                "negative_factors": risk_flags,
                "risk_flags": risk_flags,
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


def _positive_factors(row: pd.Series) -> list[str]:
    factors = []
    if float(row.get("ret_5", 0.0) or 0.0) > 0:
        factors.append("positive_5d_momentum")
    if float(row.get("volume_ratio_20", 0.0) or 0.0) > 1.2:
        factors.append("volume_confirmation")
    if float(row.get("near_high_20", 0.0) or 0.0) > 0.92:
        factors.append("near_20d_high")
    if float(row.get("limit_count_20", 0.0) or 0.0) >= 1:
        factors.append("recent_limit_strength")
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
) -> dict[str, Any]:
    if panel is None:
        panel, _codes = build_panel_from_cache(n_stocks=n_stocks, days=days, min_history=min(days, 60))
    out_path = Path(out_dir) if out_dir is not None else _OUT_BASE / datetime.now().strftime("%Y%m%d-%H%M%S")
    features = build_feature_frame(panel)
    labels = build_label_frame(panel, hold_days=hold_days)
    wf = walk_forward_relay(features, labels, train_days=train_days, test_days=test_days, step_days=step_days, top_k=top_k)
    candidates = build_latest_candidates(features, top_k=top_k, hold_days=hold_days)
    summary = {
        **wf["summary"],
        "feature_rows": int(len(features)),
        "label_rows": int(len(labels)),
        "lookahead_policy": "features_current_or_past_labels_t_plus_1",
        "latest_trade_date": features["trade_date"].max() if not features.empty else None,
    }
    min_dates = train_days + test_days
    unique_dates = int(features["trade_date"].nunique()) if not features.empty else 0
    status = "ok" if wf["summary"].get("window_count", 0) > 0 else "degraded"
    reason = "ok" if status == "ok" else f"insufficient samples: need {min_dates} feature dates, got {unique_dates}"
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
        },
        "summary": summary,
        "windows": wf["windows"],
        "trades": wf["trades"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_outputs(out_path, features, labels, report, candidates)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Relay factor mining research")
    parser.add_argument("--n-stocks", type=int, default=800)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--train-days", type=int, default=60)
    parser.add_argument("--test-days", type=int, default=10)
    parser.add_argument("--hold-days", type=int, default=2)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--out-dir", type=str, default=None)
    args = parser.parse_args(argv)
    report = run_relay_research(
        out_dir=args.out_dir,
        n_stocks=args.n_stocks,
        days=args.days,
        train_days=args.train_days,
        test_days=args.test_days,
        hold_days=args.hold_days,
        top_k=args.top_k,
    )
    print(json.dumps({"status": report["status"], "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"ok", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
