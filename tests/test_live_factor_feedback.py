# -*- coding: utf-8 -*-
import sqlite3
import sys
import threading
import time
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


def test_record_ai_decision_is_idempotent_for_the_same_payload(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(feedback, "FEEDBACK_PATH", tmp_path / "live_feedback.json")
    payload = {
        "generated_at": "2026-07-15 09:31:00",
        "results": [
            {
                "id": "decision-1",
                "code": "000001",
                "action": "BUY",
                "risk_ok": True,
                "executed": False,
                "source": {
                    "user_id": 101,
                    "strategy_key": "relay_factor_mining_v2",
                    "watchlist_id": "watch-1",
                },
            }
        ],
    }

    first = feedback.record_ai_decision(payload)
    repeated = feedback.record_ai_decision(payload)

    assert first["recorded"] == 1
    assert repeated["recorded"] == 0
    assert repeated["idempotent"] is True
    decisions = feedback.load_store()["decisions"]
    assert len(decisions) == 1
    assert len(decisions[0]["decision_key"]) == 64


def test_build_code_feedback_excludes_same_code_from_other_strategies(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "FEEDBACK_PATH", tmp_path / "live_feedback.json")
    feedback._write_json(
        feedback.FEEDBACK_PATH,
        {
            "version": 1,
            "updated_at": "2026-07-29 10:00:00",
            "daily_snapshots": [],
            "decisions": [
                {
                    "recorded_at": "2026-07-29 09:31:00",
                    "decision_date": "2026-07-29",
                    "code": "000001",
                    "strategy_key": "relay_factor_mining_v2",
                    "model_version": "relay_factor_mining_v2_ridge",
                    "experiment_id": "experiment-v2-1",
                    "model_instance_id": "model-instance-v2-1",
                    "action": "BUY",
                    "risk_ok": False,
                    "blocked": "near limit queue",
                    "executed": False,
                },
                {
                    "recorded_at": "2026-07-29 09:31:30",
                    "decision_date": "2026-07-29",
                    "code": "000001",
                    "strategy_key": "relay_factor_mining_v2",
                    "model_version": "relay_factor_mining_v2_ridge",
                    "experiment_id": "experiment-v2-2",
                    "model_instance_id": "model-instance-v2-2",
                    "action": "BUY",
                    "risk_ok": True,
                    "blocked": "",
                    "executed": True,
                },
                {
                    "recorded_at": "2026-07-29 09:32:00",
                    "decision_date": "2026-07-29",
                    "code": "000001",
                    "strategy_key": "other_strategy",
                    "model_version": "other_model",
                    "action": "BUY",
                    "risk_ok": True,
                    "blocked": "",
                    "executed": True,
                },
                {
                    "recorded_at": "2026-07-29 09:33:00",
                    "decision_date": "2026-07-29",
                    "code": "000001",
                    "action": "BUY",
                    "risk_ok": True,
                    "blocked": "",
                    "executed": True,
                },
            ],
            "paper_trades": [
                {
                    "code": "000001",
                    "buy_date": "2026-07-29",
                    "sell_time": "2026-07-29 14:00:00",
                    "strategy_key": "relay_factor_mining_v2",
                    "model_version": "relay_factor_mining_v2_ridge",
                    "experiment_id": "experiment-v2-1",
                    "model_instance_id": "model-instance-v2-1",
                    "pnl_pct": 3.0,
                },
                {
                    "code": "000001",
                    "buy_date": "2026-07-29",
                    "sell_time": "2026-07-29 14:00:30",
                    "strategy_key": "relay_factor_mining_v2",
                    "model_version": "relay_factor_mining_v2_ridge",
                    "experiment_id": "experiment-v2-2",
                    "model_instance_id": "model-instance-v2-2",
                    "pnl_pct": -4.0,
                },
                {
                    "code": "000001",
                    "buy_date": "2026-07-29",
                    "sell_time": "2026-07-29 14:01:00",
                    "strategy_key": "other_strategy",
                    "model_version": "other_model",
                    "pnl_pct": -8.0,
                },
                {
                    "code": "000001",
                    "buy_date": "2026-07-29",
                    "sell_time": "2026-07-29 14:02:00",
                    "pnl_pct": -5.0,
                },
            ],
        },
    )

    by_code = feedback.build_code_feedback(
        days=30,
        strategy_key="relay_factor_mining_v2",
        model_version="relay_factor_mining_v2_ridge",
        experiment_id="experiment-v2-1",
        model_instance_id="model-instance-v2-1",
    )

    assert set(by_code) == {"000001"}
    assert by_code["000001"]["decision_count"] == 1
    assert by_code["000001"]["blocked_rate"] == 1.0
    assert by_code["000001"]["execution_rate"] == 0.0
    assert by_code["000001"]["paper_trade_count"] == 1
    assert by_code["000001"]["paper_avg_pnl_pct"] == 3.0


def test_build_code_feedback_isolates_user_strategy_and_model(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "FEEDBACK_PATH", tmp_path / "live_feedback.json")
    rows = []
    for user_id, blocked in ((101, "blocked for user 101"), (202, "")):
        rows.append(
            {
                "recorded_at": "2026-07-29 09:31:00",
                "decision_date": "2026-07-29",
                "code": "000001",
                "user_id": user_id,
                "strategy_key": "relay_factor_mining_v2",
                "model_version": "relay_factor_mining_v2_ridge",
                "action": "BUY",
                "risk_ok": not blocked,
                "blocked": blocked,
                "executed": not blocked,
            }
        )
    feedback._write_json(
        feedback.FEEDBACK_PATH,
        {
            "version": 1,
            "updated_at": "2026-07-29 10:00:00",
            "daily_snapshots": [],
            "decisions": rows,
            "paper_trades": [],
        },
    )

    by_code = feedback.build_code_feedback(
        days=30,
        user_id=101,
        strategy_key="relay_factor_mining_v2",
        model_version="relay_factor_mining_v2_ridge",
    )

    assert by_code["000001"]["decision_count"] == 1
    assert by_code["000001"]["blocked_rate"] == 1.0
    assert by_code["000001"]["execution_rate"] == 0.0


def test_concurrent_decision_records_do_not_overwrite_each_other(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "FEEDBACK_PATH", tmp_path / "live_feedback.json")
    original_read = feedback._read_json

    def slow_read(path, default):
        data = original_read(path, default)
        time.sleep(0.02)
        return data

    monkeypatch.setattr(feedback, "_read_json", slow_read)
    threads = []
    for index in range(10):
        thread = threading.Thread(
            target=feedback.record_ai_decision,
            args=(
                {
                    "generated_at": "2026-07-29 09:31:00",
                    "results": [
                        {
                            "code": f"{index:06d}",
                            "action": "HOLD",
                            "source": {
                                "user_id": 101,
                                "strategy_key": "relay_factor_mining_v2",
                                "model_version": "relay_factor_mining_v2_ridge",
                            },
                        }
                    ],
                },
            ),
        )
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join(3)

    assert all(not thread.is_alive() for thread in threads)
    assert len(feedback.load_store()["decisions"]) == 10


def test_record_ai_decision_preserves_attribution_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "FEEDBACK_PATH", tmp_path / "live_feedback.json")

    result = feedback.record_ai_decision(
        {
            "generated_at": "2026-07-29 09:31:00",
            "results": [
                {
                    "code": "000001",
                    "action": "BUY",
                    "source": {
                        "user_id": 101,
                        "strategy_key": "relay_factor_mining_v2",
                        "model_version": "relay_factor_mining_v2_ridge",
                        "experiment_id": "experiment-v2-1",
                        "model_instance_id": "model-instance-v2-1",
                        "watchlist_id": "2026-07-28-000001.SZ",
                        "source_run_id": "v2-run-1",
                    },
                },
            ],
        }
    )

    assert result["ok"] is True
    row = feedback.load_store()["decisions"][0]
    assert row["user_id"] == 101
    assert row["strategy_key"] == "relay_factor_mining_v2"
    assert row["model_version"] == "relay_factor_mining_v2_ridge"
    assert row["experiment_id"] == "experiment-v2-1"
    assert row["model_instance_id"] == "model-instance-v2-1"
    assert row["watchlist_id"] == "2026-07-28-000001.SZ"
    assert row["source_run_id"] == "v2-run-1"


def test_sync_paper_trades_supports_legacy_table_without_attribution_columns(
    tmp_path, monkeypatch
):
    from broker import paper_broker

    db_path = tmp_path / "legacy_paper.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            buy_time TEXT NOT NULL,
            sell_time TEXT,
            buy_price REAL NOT NULL,
            sell_price REAL,
            shares INTEGER NOT NULL,
            pnl REAL,
            pnl_pct REAL,
            hold_days INTEGER,
            sell_reason TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO paper_trades
        (id,user_id,code,name,buy_time,sell_time,buy_price,sell_price,shares,pnl,pnl_pct,hold_days,sell_reason)
        VALUES(1,101,'000001','Legacy','2026-07-28 09:31:00','2026-07-29 14:00:00',10,11,100,95,9.5,1,'test')
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(paper_broker, "DB_PATH", db_path)
    monkeypatch.setattr(feedback, "FEEDBACK_PATH", tmp_path / "live_feedback.json")

    result = feedback.sync_paper_trades(days=30)

    assert result["ok"] is True
    assert result["synced"] == 1
    row = feedback.load_store()["paper_trades"][0]
    assert row["strategy_key"] == ""
    assert row["model_version"] == ""
    assert row["experiment_id"] == ""
    assert row["model_instance_id"] == ""
    assert row["watchlist_id"] == ""
    assert row["source_run_id"] == ""


def test_sync_paper_trades_maps_ai_virtual_account_to_real_owner(
    tmp_path, monkeypatch
):
    from broker import paper_broker

    db_path = tmp_path / "ai_paper.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            owner_user_id INTEGER,
            code TEXT NOT NULL,
            name TEXT,
            buy_time TEXT NOT NULL,
            sell_time TEXT,
            buy_price REAL NOT NULL,
            sell_price REAL,
            shares INTEGER NOT NULL,
            pnl REAL,
            pnl_pct REAL,
            hold_days INTEGER,
            sell_reason TEXT,
            strategy_key TEXT,
            model_version TEXT,
            experiment_id TEXT,
            model_instance_id TEXT,
            watchlist_id TEXT,
            source_run_id TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO paper_trades
        (id,user_id,owner_user_id,code,name,buy_time,sell_time,buy_price,
         sell_price,shares,pnl,pnl_pct,hold_days,sell_reason,strategy_key)
        VALUES(1,9000101,101,'000001','AI Paper','2026-07-28 09:31:00',
               '2026-07-29 14:00:00',10,11,100,95,9.5,1,'target',
               'relay_factor_mining_v2')
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(paper_broker, "DB_PATH", db_path)
    monkeypatch.setattr(feedback, "FEEDBACK_PATH", tmp_path / "live_feedback.json")

    result = feedback.sync_paper_trades(days=30)
    by_code = feedback.build_code_feedback(
        days=30,
        user_id=101,
        strategy_key="relay_factor_mining_v2",
    )

    assert result["ok"] is True
    row = feedback.load_store()["paper_trades"][0]
    assert row["user_id"] == 101
    assert row["paper_account_user_id"] == 9000101
    assert by_code["000001"]["paper_trade_count"] == 1
    assert by_code["000001"]["paper_avg_pnl_pct"] == 9.5
