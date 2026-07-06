# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date


VALID_ACTIONS = {"BUY", "SELL", "HOLD"}


def normalize_trade_plan(raw: dict, code: str, name: str, current_price=None, as_of_date: str | None = None, holding: bool = False) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    action = str(raw.get("action") or "HOLD").upper()
    if action not in VALID_ACTIONS:
        action = "HOLD"
    confidence = _clamp(_to_int(raw.get("confidence"), 0), 0, 100)
    entry = _to_float(raw.get("entry"), None)
    price = _to_float(current_price, None) or entry
    reason = str(raw.get("reason") or "")[:80]

    if action == "BUY" and (price is None or price <= 0 or holding):
        return _hold(code, name, as_of_date, "invalid BUY plan: missing price or already holding")
    if action == "SELL" and not holding:
        return _hold(code, name, as_of_date, "invalid SELL plan: no position")

    if price and not entry:
        entry = price
    stop_loss = _to_float(raw.get("stop_loss"), None)
    take_profit = _to_float(raw.get("take_profit"), None)
    if action == "BUY":
        if stop_loss is None or stop_loss >= entry:
            stop_loss = round(entry * 0.92, 3)
        if take_profit is None or take_profit <= entry:
            take_profit = round(entry * 1.12, 3)
    elif action == "SELL":
        stop_loss = None
        take_profit = None

    return {
        "code": str(code),
        "name": str(name or code),
        "action": action,
        "confidence": confidence,
        "entry": round(entry, 3) if entry else None,
        "price": round(price, 3) if price else None,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "reason": reason or ("rule normalized plan" if action != "HOLD" else "hold"),
        "holding": bool(holding),
        "as_of_date": as_of_date or date.today().isoformat(),
    }


def _hold(code: str, name: str, as_of_date: str | None, reason: str) -> dict:
    return {
        "code": str(code),
        "name": str(name or code),
        "action": "HOLD",
        "confidence": 0,
        "entry": None,
        "price": None,
        "stop_loss": None,
        "take_profit": None,
        "reason": reason[:80],
        "holding": False,
        "as_of_date": as_of_date or date.today().isoformat(),
    }


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
