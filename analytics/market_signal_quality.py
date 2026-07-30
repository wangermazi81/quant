# -*- coding: utf-8 -*-
"""Market data quality gate and short-term sentiment features.

This module turns market-wide snapshots into research-ready rows. It is designed
for factor mining: snapshots that are incomplete, stale, or internally degraded
are marked untrusted so training and candidate generation can skip them.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

import pandas as pd

TRUSTED_SOURCES = {
    "eastmoney_three_market_verified",
    "tushare_replay_rt_k",
    "tushare_replay_daily",
    "tushare_replay_daily_info",
}
MIN_BREADTH_COVERAGE = 0.98
MIN_TURNOVER_AMOUNT_YI = 1.0
SnapshotProvider = Callable[[str], dict]


def _date_iso(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return default


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _trusted_bool(value: Any) -> bool:
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


def quality_row_from_snapshot(snapshot: dict | None, requested_date: str | None = None) -> dict[str, Any]:
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    breadth = snapshot.get("breadth") if isinstance(snapshot.get("breadth"), dict) else {}
    turnover = snapshot.get("turnover") if isinstance(snapshot.get("turnover"), dict) else {}
    trade_date = _date_iso(snapshot.get("stat_date") or requested_date)
    source = str(snapshot.get("source") or breadth.get("source") or turnover.get("source") or "none")
    breadth_source = str(breadth.get("source") or "none")
    turnover_source = str(turnover.get("source") or "none")

    up = _safe_int(breadth.get("up"))
    down = _safe_int(breadth.get("down"))
    flat = _safe_int(breadth.get("flat"))
    total = _safe_int(breadth.get("total"), up + down + flat)
    listed_total = _safe_int(breadth.get("listed_total"), total)
    coverage = _safe_float(breadth.get("coverage"), 1.0 if total > 0 else 0.0)
    limit_up = _safe_int(breadth.get("limit_up"))
    limit_down = _safe_int(breadth.get("limit_dn", breadth.get("limit_down")))
    amount = _safe_float(turnover.get("amount", turnover.get("today")))
    avg_60d = _safe_float(turnover.get("avg_60d"), 0.0)
    turnover_vs_60d_pct = round((amount / avg_60d - 1.0) * 100.0, 4) if amount > 0 and avg_60d > 0 else None

    reasons: list[str] = []
    if not trade_date:
        reasons.append("missing_date")
    if breadth_source not in TRUSTED_SOURCES:
        reasons.append("untrusted_breadth_source")
    if turnover_source not in TRUSTED_SOURCES:
        reasons.append("untrusted_turnover_source")
    if breadth.get("degraded"):
        reasons.append("breadth_degraded")
    if turnover.get("degraded"):
        reasons.append("turnover_degraded")
    if coverage < MIN_BREADTH_COVERAGE:
        reasons.append("low_breadth_coverage")
    if total <= 0:
        reasons.append("empty_breadth")
    if amount < MIN_TURNOVER_AMOUNT_YI:
        reasons.append("invalid_turnover")
    requested_iso = _date_iso(requested_date) if requested_date else ""
    if requested_iso and trade_date and requested_iso != trade_date:
        reasons.append("date_mismatch")

    up_down_ratio = round(up / max(down, 1), 4) if up or down else 0.0
    limit_up_ratio = round(limit_up / max(total, 1), 6)
    limit_down_ratio = round(limit_down / max(total, 1), 6)
    breadth_balance = (up - down) / max(total, 1)
    limit_balance = (limit_up - limit_down) / max(total, 1)
    turnover_boost = 0.0 if turnover_vs_60d_pct is None else max(-15.0, min(15.0, turnover_vs_60d_pct * 0.35))
    emotion = _clip(50.0 + breadth_balance * 42.0 + limit_balance * 180.0 + turnover_boost)

    trusted = len(reasons) == 0
    return {
        "trade_date": trade_date or _date_iso(requested_date),
        "market_data_trusted": bool(trusted),
        "market_data_status": "ok" if trusted else "degraded",
        "market_data_reason": ",".join(dict.fromkeys(reasons)),
        "market_source": source,
        "breadth_source": breadth_source,
        "turnover_source": turnover_source,
        "breadth_total": total,
        "listed_total": listed_total,
        "breadth_coverage": round(coverage, 4),
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "limit_up_count": limit_up,
        "limit_down_count": limit_down,
        "up_down_ratio": up_down_ratio,
        "limit_up_ratio": limit_up_ratio,
        "limit_down_ratio": limit_down_ratio,
        "turnover_amount_yi": round(amount, 4),
        "turnover_vs_60d_pct": turnover_vs_60d_pct,
        "market_emotion_score": round(emotion, 4),
    }


def build_market_signal_frame(dates: Iterable[Any], snapshot_provider: SnapshotProvider | None = None) -> pd.DataFrame:
    if snapshot_provider is None:
        from market import market_stats

        snapshot_provider = market_stats.get_market_snapshot_metrics
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    if dates is None:
        return pd.DataFrame()
    for value in dates:
        requested = _date_iso(value)
        if not requested or requested in seen:
            continue
        seen.add(requested)
        try:
            rows.append(quality_row_from_snapshot(snapshot_provider(requested), requested_date=requested))
        except Exception as exc:
            rows.append(
                {
                    "trade_date": requested,
                    "market_data_trusted": False,
                    "market_data_status": "degraded",
                    "market_data_reason": str(exc),
                    "market_source": "none",
                    "breadth_source": "none",
                    "turnover_source": "none",
                    "breadth_total": 0,
                    "listed_total": 0,
                    "breadth_coverage": 0.0,
                    "up_count": 0,
                    "down_count": 0,
                    "flat_count": 0,
                    "limit_up_count": 0,
                    "limit_down_count": 0,
                    "up_down_ratio": 0.0,
                    "limit_up_ratio": 0.0,
                    "limit_down_ratio": 0.0,
                    "turnover_amount_yi": 0.0,
                    "turnover_vs_60d_pct": None,
                    "market_emotion_score": 0.0,
                }
            )
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)
    frame["market_data_trusted"] = frame["market_data_trusted"].map(_trusted_bool).astype(object)
    return frame


def apply_market_signals(features: pd.DataFrame, market_signal_frame: pd.DataFrame | None) -> pd.DataFrame:
    if features is None or features.empty or market_signal_frame is None or market_signal_frame.empty:
        out = features.copy() if isinstance(features, pd.DataFrame) else pd.DataFrame()
        if not out.empty and "market_data_trusted" not in out.columns:
            out["market_data_trusted"] = pd.Series(
                [False] * len(out),
                index=out.index,
                dtype=object,
            )
            out["market_data_status"] = "not_checked"
            out["market_data_reason"] = "market_signal_not_checked"
        return out
    signals = market_signal_frame.copy()
    signals["trade_date"] = signals["trade_date"].map(_date_iso)
    out = features.merge(signals, on="trade_date", how="left")
    out["market_data_trusted"] = out["market_data_trusted"].map(_trusted_bool).astype(object)
    out["market_data_status"] = out["market_data_status"].fillna("missing")
    out["market_data_reason"] = out["market_data_reason"].fillna("missing_market_signal")
    out["market_emotion_score"] = out["market_emotion_score"].fillna(0.0)
    return out


__all__ = ["quality_row_from_snapshot", "build_market_signal_frame", "apply_market_signals"]
