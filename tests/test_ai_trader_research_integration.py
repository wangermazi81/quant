# -*- coding: utf-8 -*-
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

import ai_trader


def test_prescreen_uses_research_ranker_when_available(monkeypatch):
    class FakeStrategyManager:
        @staticmethod
        def _full_market_universe_with_quotes(max_codes=1200):
            return ["000001", "000002"], {
                "000001": {"code": "000001", "name": "Slow", "price": 10, "change_pct": 1, "amount": 10},
                "000002": {"code": "000002", "name": "Fast", "price": 10, "change_pct": 2, "amount": 20},
            }

    fake_ranker = types.SimpleNamespace(
        rank_candidates=lambda universe, quotes, market_context=None, top=6, as_of_date=None: [
            {"code": "000002", "name": "Fast", "score": 99, "as_of_date": as_of_date, "factors": {}}
        ]
    )
    monkeypatch.setitem(sys.modules, "strategy_manager", FakeStrategyManager)
    monkeypatch.setattr(ai_trader, "ai_candidate_ranker", fake_ranker, raising=False)

    engine = ai_trader._Engine(101)
    picks = engine._prescreen({"prescreen_universe": 2, "prescreen_top": 1})

    assert picks == [("000002", "Fast")]


def test_prescreen_falls_back_when_ranker_fails(monkeypatch):
    class FakeStrategyManager:
        @staticmethod
        def _full_market_universe_with_quotes(max_codes=1200):
            return ["000001", "000002"], {
                "000001": {"code": "000001", "name": "Alpha", "price": 10, "change_pct": 4, "amount": 2_000_000_000},
                "000002": {"code": "000002", "name": "Beta", "price": 10, "change_pct": 1, "amount": 1},
            }

    def _boom(*args, **kwargs):
        raise RuntimeError("ranker unavailable")

    monkeypatch.setitem(sys.modules, "strategy_manager", FakeStrategyManager)
    monkeypatch.setattr(ai_trader, "ai_candidate_ranker", types.SimpleNamespace(rank_candidates=_boom), raising=False)

    engine = ai_trader._Engine(101)
    picks = engine._prescreen({"prescreen_universe": 2, "prescreen_top": 1})

    assert picks == [("000001", "Alpha")]


def test_handle_decision_records_research_memory_without_blocking(monkeypatch, tmp_path):
    recorded = []
    fake_store = types.SimpleNamespace(
        record_decision=lambda uid, decision: recorded.append((uid, dict(decision))) or 1
    )
    monkeypatch.setattr(ai_trader, "ai_research_store", fake_store, raising=False)
    monkeypatch.setattr(ai_trader._Engine, "_account", lambda self: {"positions": {}, "cash": 100000, "capital": 100000})
    monkeypatch.setattr(ai_trader._Engine, "_risk_gate", lambda self, d, st, acct=None: (True, ""))
    monkeypatch.setattr(ai_trader._Engine, "_broker_mode", lambda self: "live")

    engine = ai_trader._Engine(101)
    decision = {
        "id": "p1",
        "code": "000001",
        "name": "Alpha",
        "action": "BUY",
        "confidence": 90,
        "price": 10,
        "reason": "research test",
    }
    engine._handle_decision(decision, {"risk": {"min_confidence": 60}})

    assert recorded[0][0] == 101
    assert recorded[0][1]["code"] == "000001"
    assert recorded[0][1]["as_of_date"]
    assert engine.pending["p1"]["status"] == "pending"


def test_handle_decision_adds_plan_and_position_size_before_pending(monkeypatch):
    monkeypatch.setattr(ai_trader, "ai_research_store", None, raising=False)
    monkeypatch.setattr(ai_trader._Engine, "_account", lambda self: {"positions": {}, "cash": 100000, "capital": 100000})
    monkeypatch.setattr(ai_trader._Engine, "_risk_gate", lambda self, d, st, acct=None: (True, ""))
    monkeypatch.setattr(ai_trader._Engine, "_broker_mode", lambda self: "live")

    engine = ai_trader._Engine(101)
    decision = {
        "id": "p2",
        "code": "000002",
        "name": "Beta",
        "action": "BUY",
        "confidence": 90,
        "price": 10,
        "reason": "research sizing test",
    }
    st = {"risk": {"min_confidence": 60, "per_trade_ratio": 0.1, "single_max_ratio": 0.2}}

    engine._handle_decision(decision, st)

    pending = engine.pending["p2"]
    assert pending["plan"]["action"] == "BUY"
    assert pending["plan"]["stop_loss"] == 9.2
    assert pending["plan"]["take_profit"] == 11.2
    assert pending["position_size"]["shares"] == 900
    assert pending["position_size"]["notional"] == 9000


def test_execute_uses_research_position_size_for_buy(monkeypatch):
    captured = {}

    class FakeBroker:
        def submit_buy(self, code, name, price, shares, reason=""):
            captured.update({"code": code, "name": name, "price": price, "shares": shares, "reason": reason})
            return {"ok": True, "msg": "filled"}

        def get_positions(self, cur=None):
            return []

        def get_account(self, cur=None):
            return {"cash": 100000, "capital": 100000}

        def get_stats(self):
            return {}

    monkeypatch.setattr(ai_trader._Engine, "_broker_mode", lambda self: "paper")
    monkeypatch.setattr(ai_trader._Engine, "_ai_broker", lambda self: FakeBroker())

    engine = ai_trader._Engine(101)
    decision = {
        "code": "000003",
        "name": "Gamma",
        "action": "BUY",
        "confidence": 90,
        "price": 10,
        "reason": "sized",
        "position_size": {"shares": 300},
    }
    st = {"risk": {"per_trade_ratio": 0.2}}

    res = engine._execute(decision, st, acct={"positions": {}, "cash": 100000, "capital": 100000})

    assert res["ok"] is True
    assert captured["shares"] == 300
