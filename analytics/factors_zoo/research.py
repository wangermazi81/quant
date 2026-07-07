# -*- coding: utf-8 -*-
"""Walk-forward factor research helpers.

This module is intentionally research-only: it reads historical panels, chooses
factor direction from the training window, and evaluates only out-of-sample
dates with next-day execution. It does not place orders or change live strategy
state.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from analytics.factors_zoo.scorer import build_panel_from_cache, rank_ic_series, forward_returns

_ROOT = Path(__file__).resolve().parent.parent.parent
_OUT_BASE = _ROOT / "logs" / "factor_research"


def _date_key(value) -> pd.Timestamp:
    return pd.Timestamp(value).normalize()


def _as_dates(values: Iterable) -> list[pd.Timestamp]:
    return [_date_key(v) for v in values]


def backtest_rank_strategy(
    factor: pd.DataFrame,
    close: pd.DataFrame,
    signal_dates: Sequence,
    direction: int,
    hold_days: int = 5,
    rebalance_days: int | None = None,
    top_k: int = 10,
    fee_rate: float = 0.0005,
) -> list[dict[str, Any]]:
    """Backtest cross-sectional top-k selection with next-day execution.

    ``direction`` is selected outside this function. ``1`` means buy high factor
    values; ``-1`` means buy low factor values. Returns are measured from
    signal day + 1 to signal day + 1 + hold_days, so factors computed with the
    signal-day close cannot see their own entry price.
    """
    if direction not in (1, -1):
        raise ValueError("direction must be 1 or -1")
    if hold_days < 1 or top_k < 1:
        raise ValueError("hold_days and top_k must be positive")
    rebalance_days = int(rebalance_days or hold_days)
    if rebalance_days < 1:
        raise ValueError("rebalance_days must be positive")

    common_cols = factor.columns.intersection(close.columns)
    idx = close.index.intersection(factor.index)
    factor = factor.loc[idx, common_cols]
    close = close.loc[idx, common_cols]
    date_pos = {_date_key(dt): pos for pos, dt in enumerate(close.index)}

    trades: list[dict[str, Any]] = []
    last_signal_pos = -rebalance_days
    for raw_dt in _as_dates(signal_dates):
        if raw_dt not in date_pos:
            continue
        i = date_pos[raw_dt]
        if i - last_signal_pos < rebalance_days:
            continue
        open_idx = i + 1
        close_idx = open_idx + hold_days
        if close_idx >= len(close.index):
            continue
        scores = (factor.iloc[i] * direction).dropna()
        if scores.empty:
            continue
        selected = scores.nlargest(min(top_k, len(scores))).index.tolist()
        returns = []
        for code in selected:
            p0 = close.iloc[open_idx][code]
            p1 = close.iloc[close_idx][code]
            if pd.notna(p0) and pd.notna(p1) and float(p0) > 0:
                returns.append(float(p1 / p0 - 1.0))
        if not returns:
            continue
        ret = float(np.mean(returns) - 2 * fee_rate)
        trades.append(
            {
                "signal_date": close.index[i].strftime("%Y-%m-%d"),
                "open_date": close.index[open_idx].strftime("%Y-%m-%d"),
                "close_date": close.index[close_idx].strftime("%Y-%m-%d"),
                "direction": int(direction),
                "codes": selected,
                "return_pct": round(ret * 100, 4),
            }
        )
        last_signal_pos = i
    return trades


def summarize_trades(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    returns = np.array([float(t.get("return_pct", 0.0)) / 100.0 for t in trades], dtype=float)
    if returns.size == 0:
        return {
            "trade_count": 0,
            "hit_rate": 0.0,
            "avg_return_pct": 0.0,
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "annual_return_pct": 0.0,
        }
    equity = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(equity)
    drawdown = equity / peak - 1.0
    total = float(equity[-1] - 1.0)
    elapsed_days = len(returns)
    try:
        first_open = pd.Timestamp(min(t["open_date"] for t in trades if t.get("open_date")))
        last_close = pd.Timestamp(max(t["close_date"] for t in trades if t.get("close_date")))
        elapsed_days = max((last_close - first_open).days, 1)
    except Exception:
        pass
    annual = float(equity[-1] ** (365 / max(elapsed_days, 1)) - 1.0)
    return {
        "trade_count": int(returns.size),
        "hit_rate": round(float((returns > 0).mean()), 4),
        "avg_return_pct": round(float(returns.mean() * 100), 4),
        "total_return_pct": round(total * 100, 4),
        "max_drawdown_pct": round(float(drawdown.min() * 100), 4),
        "annual_return_pct": round(annual * 100, 4),
    }


def _date_slice(index: pd.Index, start: int, length: int) -> pd.Index:
    return index[start : start + length]


def walk_forward_factor(
    alpha_id: str,
    factor: pd.DataFrame,
    close: pd.DataFrame,
    horizon: int = 5,
    train_days: int = 252,
    test_days: int = 63,
    step_days: int | None = None,
    top_k: int = 10,
    rebalance_days: int | None = None,
    fee_rate: float = 0.0005,
) -> dict[str, Any]:
    """Evaluate one factor with expanding-style rolling train/test windows."""
    if train_days <= horizon or test_days <= 0:
        raise ValueError("train_days must exceed horizon and test_days must be positive")
    step_days = int(step_days or test_days)
    rebalance_days = int(rebalance_days or horizon)
    if step_days <= 0:
        raise ValueError("step_days must be positive")

    common_cols = factor.columns.intersection(close.columns)
    idx = factor.index.intersection(close.index)
    factor = factor.loc[idx, common_cols]
    close = close.loc[idx, common_cols]
    dates = close.index
    min_cross_section = max(2, min(5, len(common_cols)))

    windows: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    start = 0
    while start + train_days + test_days <= len(dates):
        train_idx = _date_slice(dates, start, train_days)
        test_idx = _date_slice(dates, start + train_days, test_days)
        train_close = close.loc[train_idx]
        train_fwd = forward_returns(train_close, horizon)
        train_ic = rank_ic_series(factor.loc[train_idx], train_fwd, min_cross_section=min_cross_section)
        ic_mean = float(train_ic.mean()) if not train_ic.empty else 0.0
        direction = 1 if ic_mean >= 0 else -1
        trades = backtest_rank_strategy(
            factor,
            close,
            signal_dates=list(test_idx),
            direction=direction,
            hold_days=horizon,
            rebalance_days=rebalance_days,
            top_k=top_k,
            fee_rate=fee_rate,
        )
        test_close = close.loc[dates[start + train_days : min(start + train_days + test_days + horizon, len(dates))]]
        test_fwd = forward_returns(test_close, horizon)
        test_ic = rank_ic_series(factor.loc[test_idx], test_fwd, min_cross_section=min_cross_section)
        window_summary = summarize_trades(trades)
        windows.append(
            {
                "train_start": train_idx[0].strftime("%Y-%m-%d"),
                "train_end": train_idx[-1].strftime("%Y-%m-%d"),
                "test_start": test_idx[0].strftime("%Y-%m-%d"),
                "test_end": test_idx[-1].strftime("%Y-%m-%d"),
                "direction": direction,
                "train_ic_mean": round(ic_mean, 6),
                "test_ic_mean": round(float(test_ic.mean()), 6) if not test_ic.empty else None,
                **window_summary,
            }
        )
        all_trades.extend(trades)
        start += step_days

    summary = summarize_trades(all_trades)
    if windows:
        summary.update(
            {
                "train_end_first": windows[0]["train_end"],
                "test_start": windows[0]["test_start"],
                "window_count": len(windows),
                "positive_window_rate": round(sum(1 for w in windows if w["total_return_pct"] > 0) / len(windows), 4),
            }
        )
    else:
        summary.update({"window_count": 0, "positive_window_rate": 0.0})
    return {"alpha_id": alpha_id, "horizon": horizon, "summary": summary, "windows": windows, "trades": all_trades}


def _compute_builtin_factors(panel: dict[str, pd.DataFrame], alpha_ids: Sequence[str]) -> dict[str, pd.DataFrame]:
    from analytics.factors_zoo import registry

    reg = registry()
    out = {}
    for alpha_id in alpha_ids:
        try:
            out[alpha_id] = reg.compute(alpha_id, panel)
        except Exception as exc:
            out[alpha_id] = pd.DataFrame()
            out[f"{alpha_id}__error"] = pd.DataFrame({"error": [str(exc)]})
    return out


def _rank_pct(df: pd.DataFrame, ascending: bool = True) -> pd.DataFrame:
    return df.rank(axis=1, pct=True, ascending=ascending)


def build_composite_factors(factors: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    composites: dict[str, pd.DataFrame] = {}
    ma20 = factors.get("rh158_ma20")
    std5 = factors.get("rh158_std5")
    roc10 = factors.get("rh158_roc10")
    if ma20 is not None and std5 is not None and roc10 is not None and not ma20.empty and not std5.empty and not roc10.empty:
        common_idx = ma20.index.intersection(std5.index).intersection(roc10.index)
        common_cols = ma20.columns.intersection(std5.columns).intersection(roc10.columns)
        ma20 = ma20.loc[common_idx, common_cols]
        std5 = std5.loc[common_idx, common_cols]
        roc10 = roc10.loc[common_idx, common_cols]
        composites["jw_steady_trend_low_vol"] = (
            _rank_pct(ma20, ascending=True) * 0.45
            + _rank_pct(std5, ascending=False) * 0.35
            + _rank_pct(roc10.abs(), ascending=False) * 0.20
        )
    return composites


def run_research(
    n_stocks: int = 800,
    days: int = 700,
    horizon: int = 5,
    top_k: int = 10,
    rebalance_days: int | None = None,
    train_days: int = 252,
    test_days: int = 63,
) -> dict[str, Any]:
    panel, codes = build_panel_from_cache(n_stocks=n_stocks, days=days, min_history=days)
    if not panel or "close" not in panel:
        raise RuntimeError("no close panel loaded from data/daily")
    alpha_ids = ["rh158_ma20", "rh158_std5", "rh158_std10", "rh158_roc10", "gtja191_005"]
    factors = _compute_builtin_factors(panel, alpha_ids)
    factors.update(build_composite_factors(factors))
    results = []
    for alpha_id, factor in factors.items():
        if alpha_id.endswith("__error") or factor.empty:
            continue
        results.append(
            walk_forward_factor(
                alpha_id,
                factor,
                panel["close"],
                horizon=horizon,
                train_days=train_days,
                test_days=test_days,
                top_k=top_k,
                rebalance_days=rebalance_days,
            )
        )
    results.sort(key=lambda r: (r["summary"].get("positive_window_rate", 0), r["summary"].get("total_return_pct", 0)), reverse=True)
    return {"codes": codes, "config": {"n_stocks": n_stocks, "days": days, "horizon": horizon, "top_k": top_k, "rebalance_days": rebalance_days or horizon, "train_days": train_days, "test_days": test_days}, "results": results}


def write_report(payload: dict[str, Any], out_dir: Path | None = None) -> Path:
    out_dir = out_dir or (_OUT_BASE / datetime.now().strftime("%Y%m%d-%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "research.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 因子 Walk-Forward 研究报告", "", f"生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}", "", "| 因子 | 窗口 | 正收益窗口 | 交易数 | 胜率 | 总收益% | 平均单次% | 最大回撤% |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for result in payload.get("results", []):
        s = result["summary"]
        lines.append(
            f"| {result['alpha_id']} | {s.get('window_count', 0)} | {s.get('positive_window_rate', 0):.2%} | "
            f"{s.get('trade_count', 0)} | {s.get('hit_rate', 0):.2%} | {s.get('total_return_pct', 0):.2f} | "
            f"{s.get('avg_return_pct', 0):.2f} | {s.get('max_drawdown_pct', 0):.2f} |"
        )
    (out_dir / "research.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward factor research")
    parser.add_argument("--n-stocks", type=int, default=800)
    parser.add_argument("--days", type=int, default=700)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--rebalance-days", type=int, default=None)
    parser.add_argument("--train-days", type=int, default=252)
    parser.add_argument("--test-days", type=int, default=63)
    args = parser.parse_args(argv)

    payload = run_research(
        n_stocks=args.n_stocks,
        days=args.days,
        horizon=args.horizon,
        top_k=args.top_k,
        rebalance_days=args.rebalance_days,
        train_days=args.train_days,
        test_days=args.test_days,
    )
    out_dir = write_report(payload)
    print(f"[research] wrote {out_dir}")
    if payload["results"]:
        best = payload["results"][0]
        print(f"[research] best {best['alpha_id']}: {best['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
