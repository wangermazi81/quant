# -*- coding: utf-8 -*-
"""阶段二·运行态持久化回归测试（2026-07-11）。

保护的不变量：模拟盘日内开仓计数(today_trades)持久化到 user_settings，
重启（清 user_states 内存缓存）后能恢复，使日内开仓限额护栏在重启当天依然准确。
此前 today_trades 纯内存，重启归零 → 当天可超限开仓（真 bug）。
"""
import os
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QUANT_TEST_IMPORT", "1")
os.environ.setdefault("THS_NO_SCHEDULER", "1")

from core import db_migration, user_manager as um
from broker import paper_broker as pb
from app_state import get_user_state, user_states
import core.trade_service as trade_service


@pytest.fixture
def uid(tmp_path, monkeypatch):
    users_db = str(tmp_path / "users.db")
    monkeypatch.setattr(um, "DB_PATH", users_db)
    monkeypatch.setattr(db_migration, "USERS_DB", users_db)
    monkeypatch.setattr(pb, "DB_PATH", tmp_path / "paper.db")
    pb._brokers.clear()
    user_states.clear()
    um.init_db()
    db_migration.migrate_users_db()
    pb.init_paper_db()
    code = um.create_invite_code(1)["code"] if hasattr(um, "create_invite_code") else None
    if code:
        um.register("rt", "rt@example.com", "pass123", code, ip="1.1.1.1")
        yield um.login("rt", "pass123")["user_id"]
    else:
        um.register("rt", "rt@example.com", "pass123", ip="1.1.1.1")
        yield um.login("rt", "pass123")["user_id"]
    pb._brokers.clear()
    user_states.clear()


def _simulate_restart():
    """清内存运行态缓存，模拟进程重启（下次 get_user_state 从 DB 重建）。"""
    user_states.clear()
    pb._brokers.clear()


def test_today_trades_persists_across_restart(uid):
    today = datetime.now().strftime("%Y-%m-%d")
    r = trade_service.execute_buy(uid, "000001", "平安银行", 10.0, 100, reason="测试")
    assert r["ok"], r
    s = get_user_state(uid)
    assert s["today_trades"] == 1
    assert s["today_date"] == today

    _simulate_restart()

    s2 = get_user_state(uid)
    assert s2["today_trades"] == 1, "重启后日内计数应从 DB 恢复"
    assert s2["today_date"] == today


def test_stale_day_count_not_restored(uid):
    # 手写一个昨天的计数进 DB，重启后应视为新的一天(0)，不恢复陈旧计数
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    um.save_user_settings(uid, today_trades=5, today_date=yesterday)
    _simulate_restart()
    s = get_user_state(uid)
    assert s["today_trades"] == 0, "隔日陈旧计数不应恢复"


def test_daily_reset_persists(uid):
    # 先制造一个"昨天有计数"的内存态，再触发 _reset_daily_if_needed
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    s = get_user_state(uid)
    s["today_date"] = yesterday
    s["today_trades"] = 3
    trade_service._reset_daily_if_needed(uid)
    today = datetime.now().strftime("%Y-%m-%d")
    assert s["today_trades"] == 0
    assert s["today_date"] == today
    # 落库验证：重启后仍是今天的 0
    _simulate_restart()
    s2 = get_user_state(uid)
    assert s2["today_date"] == today
    assert s2["today_trades"] == 0


def test_multiple_buys_accumulate_and_persist(uid):
    for i in range(3):
        trade_service.execute_buy(uid, f"00000{i+1}", f"票{i}", 10.0, 100, reason="测试")
    assert get_user_state(uid)["today_trades"] == 3
    _simulate_restart()
    assert get_user_state(uid)["today_trades"] == 3
