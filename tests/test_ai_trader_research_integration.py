# -*- coding: utf-8 -*-
from tests._module_stubs import stub_project_module
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics import ai_trader


def test_watchlist_sanitization_preserves_model_identity():
    from core import factor_watchlist

    item = factor_watchlist.sanitize_watchlist_item(
        {
            "ts_code": "000001.SZ",
            "name": "Ping An Bank",
            "trade_date": "2026-07-28",
            "strategy_key": "relay_factor_mining_v2",
            "model_version": "relay_factor_mining_v2_ridge",
        }
    )

    assert item["strategy_key"] == "relay_factor_mining_v2"
    assert item["model_version"] == "relay_factor_mining_v2_ridge"


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
    stub_project_module(monkeypatch, "strategy_manager", FakeStrategyManager)
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

    stub_project_module(monkeypatch, "strategy_manager", FakeStrategyManager)
    monkeypatch.setattr(ai_trader, "ai_candidate_ranker", types.SimpleNamespace(rank_candidates=_boom), raising=False)

    engine = ai_trader._Engine(101)
    picks = engine._prescreen({"prescreen_universe": 2, "prescreen_top": 1})

    assert picks == [("000001", "Alpha")]


def test_build_candidates_prioritizes_strategy_library_results(monkeypatch):
    calls = []

    class FakeStrategyManager:
        @staticmethod
        def run(filename, codes, max_codes=0):
            calls.append((filename, tuple(codes), max_codes))
            return {
                "ok": True,
                "results": [
                    {"code": "000007", "name": "StrategyPick"},
                    {"code": "000008", "name": "SecondPick"},
                ],
            }

    stub_project_module(monkeypatch, "strategy_manager", FakeStrategyManager)
    monkeypatch.setattr(ai_trader._Engine, "_account", lambda self: {"positions": {}})
    monkeypatch.setattr(ai_trader._Engine, "_user_state", lambda self: {"queue": [{"code": "000009", "name": "Queued"}]})
    monkeypatch.setattr(ai_trader._Engine, "_prescreen", lambda self, st: [("000010", "Prescreen")])

    engine = ai_trader._Engine(101)
    picks = engine._build_candidates({
        "strategy_candidates_enabled": True,
        "strategy_candidate_strategies": ["daban_lianban_2plus_quality"],
        "strategy_candidate_max_codes": 500,
        "strategy_candidate_top": 2,
        "watchlist": ["000011"],
        "prescreen_enabled": True,
        "prescreen_fallback_only": True,
        "max_candidates": 6,
    })

    assert calls == [("daban_lianban_2plus_quality", tuple(), 500)]
    assert picks[:2] == [("000007", "StrategyPick"), ("000008", "SecondPick")]
    assert ("000009", "Queued") in picks
    assert ("000010", "Prescreen") not in picks


def test_strategy_candidates_expand_legacy_single_quality_strategy(monkeypatch):
    calls = []

    class FakeStrategyManager:
        @staticmethod
        def run(filename, codes, max_codes=0):
            calls.append(filename)
            if filename == "daban_lianban_2plus_quality":
                return {"ok": True, "results": []}
            if filename == "lianban_2plus":
                return {"ok": True, "results": [{"code": "600246", "name": "Relay"}]}
            return {"ok": True, "results": []}

    stub_project_module(monkeypatch, "strategy_manager", FakeStrategyManager)

    engine = ai_trader._Engine(101)
    picks = engine._strategy_candidates({
        "strategy_candidate_strategies": ["daban_lianban_2plus_quality"],
        "strategy_candidate_max_codes": 500,
        "strategy_candidate_top": 2,
    })

    assert picks == [("600246", "Relay")]
    assert calls[:2] == ["daban_lianban_2plus_quality", "lianban_2plus"]


def test_strategy_candidates_records_trace_for_ui(monkeypatch):
    class FakeStrategyManager:
        @staticmethod
        def run(filename, codes, max_codes=0):
            if filename == "empty_strategy":
                return {"ok": True, "results": []}
            return {
                "ok": True,
                "results": [
                    {"code": "000111", "name": "TracePick"},
                    {"code": "000222", "name": "TraceSecond"},
                ],
            }

    stub_project_module(monkeypatch, "strategy_manager", FakeStrategyManager)

    engine = ai_trader._Engine(101)
    picks = engine._strategy_candidates({
        "strategy_candidate_strategies": ["empty_strategy", "winner_strategy"],
        "strategy_candidate_max_codes": 500,
        "strategy_candidate_top": 1,
    })

    assert picks == [("000111", "TracePick")]
    assert engine.candidate_trace["strategies"][0]["file"] == "empty_strategy"
    assert engine.candidate_trace["strategies"][0]["count"] == 0
    assert engine.candidate_trace["strategies"][1]["file"] == "winner_strategy"
    assert engine.candidate_trace["strategies"][1]["picked"] == [{"code": "000111", "name": "TracePick"}]


def test_llm_updates_estimated_token_usage(monkeypatch):
    monkeypatch.setattr(ai_trader._ai, "_call_llm", lambda *args, **kwargs: "abcd" * 12)

    engine = ai_trader._Engine(101)
    reply = engine._llm(
        {"provider": "test"},
        "system prompt",
        "user prompt",
        max_tokens=64,
    )

    assert reply == "abcd" * 12
    usage = engine.token_usage
    assert usage["calls"] == 1
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert usage["estimated"] is True
    assert usage["last_call"]["total_tokens"] > 0


def test_llm_prefers_provider_usage_when_available(monkeypatch):
    def fake_call(*args, **kwargs):
        assert kwargs.get("return_usage") is True
        return {
            "content": "真实 usage 回复",
            "usage": {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168},
            "provider": "deepseek",
            "model": "deepseek-chat",
        }

    monkeypatch.setattr(ai_trader._ai, "_call_llm", fake_call)

    engine = ai_trader._Engine(101)
    reply = engine._llm(
        {"provider": "deepseek", "model": "deepseek-chat"},
        "system prompt",
        "user prompt",
        max_tokens=64,
    )

    assert reply == "真实 usage 回复"
    assert engine.token_usage["prompt_tokens"] == 123
    assert engine.token_usage["completion_tokens"] == 45
    assert engine.token_usage["total_tokens"] == 168
    assert engine.token_usage["calls"] == 1
    assert engine.token_usage["estimated"] is False
    assert engine.token_usage["last_call"]["provider"] == "deepseek"
    assert engine.token_usage["last_call"]["model"] == "deepseek-chat"


def test_prescreen_rejects_stub_market_quotes(monkeypatch):
    class FakeStrategyManager:
        @staticmethod
        def _full_market_universe_with_quotes(max_codes=1200):
            return ["000001", "000002", "000003"], {
                "000001": {"code": "000001", "name": "000001", "price": 10.0, "change_pct": 0.0, "vol": 1000},
                "000002": {"code": "000002", "name": "000002", "price": 10.0, "change_pct": 0.0, "vol": 1000},
                "000003": {"code": "000003", "name": "000003", "price": 10.0, "change_pct": 0.0, "vol": 1000},
            }

    stub_project_module(monkeypatch, "strategy_manager", FakeStrategyManager)

    engine = ai_trader._Engine(101)
    assert engine._prescreen({"prescreen_universe": 3, "prescreen_top": 2}) == []
    assert engine.market_data_status["degraded"] is True


def test_risk_gate_blocks_buy_when_market_data_is_degraded():
    engine = ai_trader._Engine(101)
    engine.market_data_status = {"degraded": True, "reason": "stub quotes"}
    st = {
        "block_buy_on_degraded_data": True,
        "risk": {
            "min_confidence": 60,
            "daily_trade_limit": 10,
            "max_positions": 5,
            "per_trade_ratio": 0.2,
        },
    }
    acct = {"positions": {"000002": {"name": "Held"}}, "cash": 100000, "capital": 100000}

    buy_ok, buy_reason = engine._risk_gate(
        {"code": "000001", "action": "BUY", "confidence": 90, "price": 10}, st, acct
    )
    sell_ok, sell_reason = engine._risk_gate(
        {"code": "000002", "action": "SELL", "confidence": 90, "price": 10}, st, acct
    )

    assert buy_ok is False
    assert "market data degraded" in buy_reason
    assert sell_ok is True
    assert sell_reason == ""


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


def test_execute_caps_watchlist_buy_at_frozen_position_pct(monkeypatch):
    captured = {}

    class FakeBroker:
        def submit_buy(self, code, name, price, shares, reason=""):
            captured["shares"] = shares
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
        "reason": "frozen position cap",
        "position_size": {"shares": 5000},
        "source": {"position_pct": 0.1},
    }
    st = {"risk": {"per_trade_ratio": 0.5}}

    result = engine._execute(
        decision,
        st,
        source="watchlist_ai",
        acct={"positions": {}, "cash": 100000, "capital": 100000},
    )

    assert result["ok"] is True
    assert captured["shares"] == 1000


def test_hard_buy_filter_rejects_missing_or_zero_volume_quote(monkeypatch):
    engine = ai_trader._Engine(101)
    decision = {
        "code": "000003",
        "name": "Gamma",
        "action": "BUY",
        "price": 10,
    }

    monkeypatch.setattr(engine, "_quote", lambda code: {})
    missing_ok, missing_reason = engine._hard_buy_filter(dict(decision))
    monkeypatch.setattr(
        engine,
        "_quote",
        lambda code: {
            "price": 10,
            "open": 10,
            "last_close": 10,
            "change_pct": 0,
            "volume": 0,
            "amount": 0,
        },
    )
    zero_ok, zero_reason = engine._hard_buy_filter(dict(decision))

    assert missing_ok is False
    assert "quote" in missing_reason
    assert zero_ok is False
    assert "volume" in zero_reason


def test_execute_attributes_filled_paper_buy_without_new_submit_arguments(monkeypatch):
    captured = {}

    class FakeBroker:
        def submit_buy(self, code, name, price, shares, reason=""):
            captured["submit"] = {
                "code": code,
                "name": name,
                "price": price,
                "shares": shares,
                "reason": reason,
            }
            return {"ok": True, "msg": "filled", "trade_id": 42}

        def attribute_trade(self, trade_id, attribution):
            captured["trade_id"] = trade_id
            captured["attribution"] = dict(attribution)

        def get_positions(self, cur=None):
            return []

        def get_account(self, cur=None):
            return {"cash": 100000, "capital": 100000}

        def get_stats(self):
            return {}

    broker = FakeBroker()
    monkeypatch.setattr(ai_trader._Engine, "_broker_mode", lambda self: "paper")
    monkeypatch.setattr(ai_trader._Engine, "_ai_broker", lambda self: broker)

    engine = ai_trader._Engine(101)
    identity = {
        "user_id": 101,
        "strategy_key": "relay_factor_mining_v2",
        "model_version": "relay_factor_mining_v2_ridge",
        "watchlist_id": "2026-07-28-000004.SZ",
        "source_run_id": "v2-run-1",
    }
    decision = {
        "code": "000004",
        "name": "Delta",
        "action": "BUY",
        "confidence": 90,
        "price": 10,
        "reason": "attributed",
        "position_size": {"shares": 300},
        "source": identity,
    }
    st = {"risk": {"per_trade_ratio": 0.2}}

    result = engine._execute(
        decision,
        st,
        acct={"positions": {}, "cash": 100000, "capital": 100000},
    )

    assert result["ok"] is True
    assert captured["trade_id"] == 42
    assert {key: captured["attribution"][key] for key in identity} == identity


def test_watchlist_decision_returns_identity_at_source_and_result_top_level(monkeypatch, tmp_path):
    import app_state
    from core import factor_watchlist as fw

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    monkeypatch.setattr(
        fw,
        "_expected_scope_trade_date",
        lambda: "2026-07-28",
    )
    monkeypatch.setattr(
        ai_trader,
        "load_user_trader_config",
        lambda uid: {"enabled": True, "api_key": "test"},
    )
    monkeypatch.setattr(ai_trader, "load_settings", lambda uid: {"risk": {}})
    monkeypatch.setattr(ai_trader._Engine, "_agent_market", lambda self, cfg: {})
    monkeypatch.setattr(
        ai_trader._Engine,
        "_team_decide",
        lambda self, cfg, code, name, market_view, st: {
            "id": "decision-1",
            "code": code,
            "name": name,
            "action": "HOLD",
            "confidence": 80,
            "price": 10,
            "reason": "identity test",
        },
    )
    monkeypatch.setattr(ai_trader._Engine, "_hard_buy_filter", lambda self, decision: (True, ""))
    monkeypatch.setattr(
        ai_trader._Engine,
        "_risk_gate",
        lambda self, decision, st, acct=None: (True, ""),
    )
    monkeypatch.setattr(
        ai_trader._Engine,
        "_account",
        lambda self: {"positions": {}, "cash": 100000, "capital": 100000},
    )
    monkeypatch.setattr(ai_trader._Engine, "_push_status", lambda self: None)

    engine = ai_trader._Engine(101)
    stored = fw.import_factor_candidates_to_watchlist(
        [
            {
                "ts_code": "000005.SZ",
                    "name": "Epsilon",
                    "trade_date": "2026-07-28",
                    "strategy_key": "relay_factor_mining_v2",
                    "model_version": "relay_factor_mining_v3_nested_ridge",
                    "experiment_id": "experiment-v2-1",
                    "model_instance_id": "model-instance-v2-1",
                    "validation_accepted": True,
                    "research_status": "validated",
                    "deployment_eligible": True,
                    "research_only": False,
                    "live_readiness_status": "ready",
                    "position_pct": 0.1,
                }
            ],
            run_id="v2-run-1",
    )["items"][0]
    identity = {
            "user_id": 101,
            "strategy_key": "relay_factor_mining_v2",
            "model_version": "relay_factor_mining_v3_nested_ridge",
            "experiment_id": "experiment-v2-1",
            "model_instance_id": "model-instance-v2-1",
            "watchlist_id": stored["id"],
            "source_run_id": "v2-run-1",
        }
    result = engine.decide_watchlist_candidates(
        [{"id": identity["watchlist_id"]}],
        max_candidates=1,
    )

    row = result["results"][0]
    assert {key: row["source"][key] for key in identity} == identity
    assert {key: row[key] for key in identity} == identity
    assert row["source"]["position_pct"] == 0.1


def test_watchlist_decision_rejects_direct_market_gate_bypass(monkeypatch):
    monkeypatch.setattr(
        ai_trader,
        "load_user_trader_config",
        lambda uid: {"enabled": True, "api_key": "test"},
    )

    engine = ai_trader._Engine(101)
    result = engine.decide_watchlist_candidates(
        [
            {
                "ts_code": "000005.SZ",
                "name": "Epsilon",
                "strategy_key": "relay_factor_mining_v2",
                "live_warning_keys": ["market_gate_blocked"],
            }
        ],
        max_candidates=1,
    )

    assert result == {"ok": False, "error": "no usable watchlist candidates"}


def test_watchlist_decision_reloads_authoritative_gate_state(monkeypatch, tmp_path):
    import app_state
    from core import factor_watchlist as fw

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    monkeypatch.setattr(
        ai_trader,
        "load_user_trader_config",
        lambda uid: {"enabled": True, "api_key": "test"},
    )
    stored = fw.import_factor_candidates_to_watchlist(
        [
            {
                "ts_code": "000005.SZ",
                "name": "Epsilon",
                "trade_date": "2026-07-28",
                "strategy_key": "relay_factor_mining_v2",
                "model_version": "relay_factor_mining_v2_ridge",
                "live_warning_keys": ["market_gate_blocked"],
            }
        ],
        run_id="run-blocked",
    )["items"][0]
    tampered = {
        "id": stored["id"],
        "ts_code": stored["ts_code"],
        "name": stored["name"],
        "strategy_key": stored["strategy_key"],
        "live_warning_keys": [],
    }

    engine = ai_trader._Engine(101)
    result = engine.decide_watchlist_candidates([tampered], max_candidates=1)

    assert result == {"ok": False, "error": "no usable watchlist candidates"}
