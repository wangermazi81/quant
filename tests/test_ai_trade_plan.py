# -*- coding: utf-8 -*-
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics import ai_trade_plan as plans


def test_normalize_trade_plan_clamps_action_confidence_and_risk():
    plan = plans.normalize_trade_plan(
        {
            "action": "buy",
            "confidence": 140,
            "entry": 10.0,
            "stop_loss": 12.0,
            "take_profit": 9.0,
            "reason": "x" * 200,
        },
        code="000001",
        name="Alpha",
        current_price=10.0,
        as_of_date="2026-07-03",
        holding=False,
    )

    assert plan["action"] == "BUY"
    assert plan["confidence"] == 100
    assert plan["entry"] == 10.0
    assert plan["stop_loss"] < plan["entry"]
    assert plan["take_profit"] > plan["entry"]
    assert len(plan["reason"]) <= 80
    assert plan["as_of_date"] == "2026-07-03"


def test_invalid_buy_without_price_becomes_hold():
    plan = plans.normalize_trade_plan(
        {"action": "BUY", "confidence": 90, "reason": "missing price"},
        code="000001",
        name="Alpha",
        current_price=None,
        as_of_date="2026-07-03",
        holding=False,
    )

    assert plan["action"] == "HOLD"
    assert plan["confidence"] == 0
    assert "invalid" in plan["reason"].lower()
