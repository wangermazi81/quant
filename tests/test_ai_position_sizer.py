# -*- coding: utf-8 -*-
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

import ai_position_sizer as sizer


def test_position_suggestion_is_bounded_by_cash_risk_and_lot_size():
    suggestion = sizer.suggest_position_size(
        cash=50_000,
        equity=100_000,
        price=12.3,
        confidence=90,
        risk={"per_trade_ratio": 0.2, "single_max_ratio": 0.25},
    )

    assert suggestion["shares"] % 100 == 0
    assert suggestion["shares"] >= 100
    assert suggestion["notional"] <= 25_000
    assert suggestion["notional"] <= 50_000
    assert 0 < suggestion["position_pct"] <= 0.25


def test_low_confidence_or_invalid_price_returns_zero():
    assert sizer.suggest_position_size(50_000, 100_000, 10.0, 20, {})["shares"] == 0
    assert sizer.suggest_position_size(50_000, 100_000, 0, 90, {})["shares"] == 0
