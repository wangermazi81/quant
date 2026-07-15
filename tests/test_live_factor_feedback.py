# -*- coding: utf-8 -*-
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics import live_factor_feedback as feedback


def test_record_ai_decision_and_build_code_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "FEEDBACK_PATH", tmp_path / "live_feedback.json")

    result = feedback.record_ai_decision(
        {
            "generated_at": "2026-07-15 09:31:00",
            "results": [
                {
                    "code": "000001",
                    "name": "Ping An Bank",
                    "action": "BUY",
                    "risk_ok": False,
                    "blocked": "near limit queue",
                    "executed": False,
                    "auction_snapshot": {
                        "price": 10.99,
                        "limit_up": 11.0,
                        "bid1_price": 10.98,
                        "ask1_price": 10.99,
                        "bid1_vol": 1200,
                    },
                    "source": {
                        "watchlist_id": "7",
                        "source_run_id": "run-1",
                        "trade_date": "2026-07-14",
                        "candidate_tier": "strong",
                        "live_readiness_status": "ready",
                        "live_readiness_score": 0.92,
                    },
                },
                {
                    "code": "000001.SZ",
                    "name": "Ping An Bank",
                    "action": "BUY",
                    "risk_ok": True,
                    "blocked": "",
                    "executed": True,
                    "source": {"watchlist_id": "7"},
                },
            ],
        }
    )
    store = feedback.load_store()
    store["paper_trades"] = [
        {"code": "000001", "buy_date": "2026-07-15", "sell_time": "2026-07-16 10:00:00", "pnl_pct": -1.5},
        {"code": "000001", "buy_date": "2026-07-15", "sell_time": "2026-07-17 10:00:00", "pnl_pct": 2.0},
    ]
    feedback._write_json(feedback.FEEDBACK_PATH, store)

    by_code = feedback.build_code_feedback(days=30)

    assert result["ok"] is True
    assert result["recorded"] == 2
    assert by_code["000001"]["decision_count"] == 2
    assert by_code["000001"]["blocked_rate"] == 0.5
    assert by_code["000001"]["execution_rate"] == 0.5
    assert by_code["000001"]["near_limit_rate"] == 0.5
    assert by_code["000001"]["paper_trade_count"] == 2
    assert by_code["000001"]["paper_avg_pnl_pct"] == 0.25
