# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date
from math import log10


def rank_candidates(universe, quotes: dict, market_context: dict | None = None, top: int = 6, as_of_date: str | None = None) -> list[dict]:
    as_of = as_of_date or date.today().isoformat()
    market_context = market_context or {}
    emotion = _to_float(market_context.get("emotion_score"), 50.0)
    rows = []
    for code in universe or []:
        q = dict((quotes or {}).get(code) or {})
        q.setdefault("code", code)
        item = score_candidate(q, emotion_score=emotion, as_of_date=as_of)
        if item:
            rows.append(item)
    rows.sort(key=lambda x: (x["score"], x["factors"].get("amount", 0), x["code"]), reverse=True)
    return rows[: max(0, int(top or 0))]


def score_candidate(quote: dict, emotion_score: float = 50.0, as_of_date: str | None = None) -> dict | None:
    code = str(quote.get("code") or "").strip()
    name = str(quote.get("name") or code).strip()
    price = _to_float(quote.get("price"), 0.0)
    change = _to_float(quote.get("change_pct"), None)
    amount = _to_float(quote.get("amount"), _to_float(quote.get("成交额"), 0.0)) or 0.0
    limit_up = _to_float(quote.get("limit_up"), 0.0) or 0.0
    upper_name = name.upper()

    if not code or "ST" in upper_name or "退" in name:
        return None
    if price < 2 or price > 300 or change is None:
        return None
    if limit_up and price >= limit_up * 0.999:
        return None
    if change < -3 or change > 9.3:
        return None

    momentum_score = max(0.0, min(35.0, change * 4.0 + 8.0))
    liquidity_score = max(0.0, min(30.0, log10(max(amount, 1.0)) * 4.0 - 20.0))
    emotion_score = max(-8.0, min(8.0, (float(emotion_score) - 50.0) / 6.0))
    penalty = 0.0
    if change < 0:
        penalty += abs(change) * 3.0
    if change > 8.5:
        penalty += (change - 8.5) * 8.0

    score = round(45.0 + momentum_score + liquidity_score + emotion_score - penalty, 4)
    return {
        "code": code,
        "name": name,
        "score": score,
        "as_of_date": as_of_date or date.today().isoformat(),
        "factors": {
            "price": price,
            "change_pct": change,
            "amount": amount,
            "momentum_score": round(momentum_score, 4),
            "liquidity_score": round(liquidity_score, 4),
            "emotion_score": round(emotion_score, 4),
            "penalty": round(penalty, 4),
        },
    }


def _to_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
