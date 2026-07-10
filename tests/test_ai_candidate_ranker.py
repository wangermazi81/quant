# -*- coding: utf-8 -*-
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics import ai_candidate_ranker as ranker


def test_rank_candidates_filters_untradable_names_and_prices():
    universe = ["000001", "000002", "000003", "000004", "000005"]
    quotes = {
        "000001": {"code": "000001", "name": "Alpha", "price": 10.0, "change_pct": 4.0, "amount": 2_000_000_000},
        "000002": {"code": "000002", "name": "ST Bad", "price": 8.0, "change_pct": 5.0, "amount": 9_000_000_000},
        "000003": {"code": "000003", "name": "Limit", "price": 11.0, "change_pct": 9.8, "amount": 8_000_000_000, "limit_up": 11.0},
        "000004": {"code": "000004", "name": "Penny", "price": 1.5, "change_pct": 5.0, "amount": 9_000_000_000},
        "000005": {"code": "000005", "name": "Beta", "price": 12.0, "change_pct": 2.5, "amount": 3_000_000_000},
    }

    picks = ranker.rank_candidates(universe, quotes, top=5, as_of_date="2026-07-03")

    assert [p["code"] for p in picks] == ["000001", "000005"]
    assert all(p["as_of_date"] == "2026-07-03" for p in picks)
    assert picks[0]["score"] > picks[1]["score"]
    assert "factors" in picks[0]


def test_rank_candidates_uses_market_context_without_requiring_it():
    universe = ["000001", "000002"]
    quotes = {
        "000001": {"code": "000001", "name": "Calm", "price": 10.0, "change_pct": 1.0, "amount": 1_000_000_000},
        "000002": {"code": "000002", "name": "Momentum", "price": 10.0, "change_pct": 6.0, "amount": 1_000_000_000},
    }

    weak = ranker.rank_candidates(universe, quotes, market_context={"emotion_score": 20}, top=2)
    strong = ranker.rank_candidates(universe, quotes, market_context={"emotion_score": 80}, top=2)

    assert weak[0]["code"] == "000002"
    assert strong[0]["score"] > weak[0]["score"]
