# -*- coding: utf-8 -*-
from __future__ import annotations


def suggest_position_size(cash, equity, price, confidence, risk: dict | None = None) -> dict:
    risk = risk or {}
    cash = _to_float(cash, 0.0)
    equity = _to_float(equity, 0.0)
    price = _to_float(price, 0.0)
    confidence = _to_float(confidence, 0.0)
    if cash <= 0 or equity <= 0 or price <= 0 or confidence < 50:
        return _empty("invalid input or confidence below trading threshold")

    per_trade = _to_float(risk.get("per_trade_ratio"), 0.12)
    single_max = _to_float(risk.get("single_max_ratio"), 0.2)
    per_trade = max(0.01, min(0.5, per_trade))
    single_max = max(0.01, min(0.8, single_max))
    confidence_scale = max(0.35, min(1.0, confidence / 100.0))
    budget = min(cash, equity * single_max, equity * per_trade * confidence_scale)
    shares = int(budget / price / 100) * 100
    if shares < 100:
        return _empty("budget below one round lot")
    notional = round(shares * price, 2)
    return {
        "shares": shares,
        "notional": notional,
        "position_pct": round(notional / equity, 6),
        "cash_after": round(cash - notional, 2),
        "reason": "deterministic research sizing",
    }


def _empty(reason: str) -> dict:
    return {"shares": 0, "notional": 0.0, "position_pct": 0.0, "cash_after": None, "reason": reason}


def _to_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
