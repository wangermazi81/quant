# -*- coding: utf-8 -*-
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics import ai_research_store as store


def test_feature_snapshot_and_memory_are_isolated_by_user(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "ai_research.db")
    store.init_research_db()

    store.record_feature_snapshot(
        uid=101,
        as_of_date="2026-07-03",
        code="000001",
        name="Ping An",
        features={"change_pct": 4.2, "amount": 1200000000, "score": 78},
    )
    decision_id = store.record_decision(
        101,
        {
            "as_of_date": "2026-07-03",
            "code": "000001",
            "name": "Ping An",
            "action": "BUY",
            "confidence": 82,
            "features": {"change_pct": 4.0, "amount": 1000000000},
        },
    )
    assert decision_id > 0
    store.record_trade_outcome(
        101,
        "000001",
        {"as_of_date": "2026-07-04", "pnl_pct": 3.5, "hold_days": 1, "exit_reason": "target"},
    )
    store.record_decision(
        202,
        {
            "as_of_date": "2026-07-03",
            "code": "000001",
            "action": "SELL",
            "confidence": 10,
            "features": {"change_pct": -8.0},
        },
    )

    memories = store.find_similar_memories(101, "000001", {"change_pct": 4.1, "amount": 900000000}, limit=3)

    assert len(memories) == 1
    assert memories[0]["uid"] == 101
    assert memories[0]["decision"]["action"] == "BUY"
    assert memories[0]["outcome"]["pnl_pct"] == 3.5
    assert memories[0]["as_of_date"] == "2026-07-03"


def test_memory_lookup_requires_as_of_date(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "ai_research.db")
    store.init_research_db()

    try:
        store.record_decision(101, {"code": "000001", "action": "BUY"})
    except ValueError as exc:
        assert "as_of_date" in str(exc)
    else:
        raise AssertionError("record_decision should reject samples without as_of_date")


def test_backtest_summary_uses_only_user_and_date_range(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "ai_research.db")
    store.init_research_db()

    store.record_decision(101, {"as_of_date": "2026-07-01", "code": "000001", "action": "BUY", "confidence": 80})
    store.record_trade_outcome(101, "000001", {"as_of_date": "2026-07-02", "pnl_pct": 4.0})
    store.record_decision(101, {"as_of_date": "2026-07-03", "code": "000002", "action": "BUY", "confidence": 70})
    store.record_trade_outcome(101, "000002", {"as_of_date": "2026-07-04", "pnl_pct": -2.0})
    store.record_decision(202, {"as_of_date": "2026-07-03", "code": "000003", "action": "BUY", "confidence": 99})
    store.record_trade_outcome(202, "000003", {"as_of_date": "2026-07-04", "pnl_pct": 20.0})

    summary = store.backtest_summary(101, start="2026-07-01", end="2026-07-03", top=10)

    assert summary["decision_count"] == 2
    assert summary["evaluated_count"] == 2
    assert summary["win_rate"] == 0.5
    assert summary["avg_pnl_pct"] == 1.0
    assert summary["total_pnl_pct"] == 2.0
    assert summary["max_loss_pct"] == -2.0
    assert {r["code"] for r in summary["rows"]} == {"000001", "000002"}
