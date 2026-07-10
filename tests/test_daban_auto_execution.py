import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

import pytest

from core import db_migration
import main
from broker import paper_broker as pb
from core import notification_manager as nm
from core import trade_service
from strategy_engine import scan_service
from strategy_engine import strategy_manager as sm
from market import ths_quote
from core import user_manager as um
from app_state import get_user_state, user_states


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
    code = um.create_invite_code(
        creator_uid=1,
        max_uses=5,
        is_admin_code=True,
        reward=0,
    )["code"]
    um.register("daban", "daban@example.com", "pass123", code, ip="1.1.1.1")
    yield um.login("daban", "pass123")["user_id"]
    pb._brokers.clear()
    user_states.clear()


@pytest.fixture
def scan_env(uid, monkeypatch):
    # 工单4 后 do_scan 在 scan_service，is_trading_time/nm 绑定在该模块
    monkeypatch.setattr(scan_service, "is_trading_time", lambda: True)
    monkeypatch.setattr(ths_quote, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(nm, "send_notification", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(sm, "get_position_config", lambda *a, **k: {
        "max_daily": 5,
        "max_hold": 5,
        "sizing": "equal",
    })
    return uid, monkeypatch


def _state(user_id, *, auto_execute):
    state = get_user_state(user_id)
    state.update(
        running=True,
        active_strategy="lianban_2plus",
        capital=100000,
        position_ratio=0.2,
        single_position_max_ratio=0.2,
        max_positions=5,
        auto_execute=auto_execute,
        queue=[],
        positions={},
        today_trades=0,
    )
    return state


def _mock_scan(monkeypatch, position_ratio=0.15):
    monkeypatch.setattr(sm, "run", lambda *a, **k: {
        "results": [{
            "code": "000003",
            "name": "Tiered",
            "price": 10.0,
            "change": 9.9,
            "signal": True,
            "position_ratio": position_ratio,
            "reason": "2-board tier",
        }],
        "scanned": 1,
    })


def test_signal_position_ratio_drives_auto_paper_buy(scan_env):
    user_id, monkeypatch = scan_env
    _mock_scan(monkeypatch, position_ratio=0.15)
    state = _state(user_id, auto_execute=True)

    main.do_scan(user_id)

    assert state["queue"][0]["shares"] == 1500
    assert state["queue"][0]["status"] == "done"
    positions = {p["code"]: p for p in pb.get_broker(user_id).get_positions()}
    assert positions["000003"]["shares"] == 1500
    assert positions["000003"]["strategy"] == "lianban_2plus"


def test_auto_live_buy_creates_bridge_signal(scan_env, monkeypatch):
    user_id, _ = scan_env
    # 工单4 后 DRY_RUN 分别绑定在 trade_service(买卖) 与 scan_service(扫描)
    monkeypatch.setattr(trade_service, "DRY_RUN", False)
    monkeypatch.setattr(scan_service, "DRY_RUN", False)
    um.save_user_broker_config(user_id, "live", {"live_cash": 100000})
    _mock_scan(monkeypatch, position_ratio=0.15)
    state = _state(user_id, auto_execute=True)

    main.do_scan(user_id)

    item = state["queue"][0]
    assert item["shares"] == 1500
    assert item["status"] == "submitted"
    assert item["signal_id"] > 0
    signal = um.get_trade_signal(item["signal_id"])
    assert signal["action"] == "BUY"
    assert signal["status"] == "pending"
    assert signal["shares"] == 1500


def test_lianban_ride_auto_paper_sell_on_stop_loss(scan_env, monkeypatch):
    user_id, _ = scan_env
    state = _state(user_id, auto_execute=False)
    state["active_strategy"] = "lianban_2plus"
    assert main.execute_buy(user_id, "000005", "RideSell", 10.0, 1000)["ok"]

    broker = pb.get_broker(user_id)
    conn = broker._conn()
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE paper_positions SET buy_time=? WHERE user_id=? AND code=?",
        (yesterday, user_id, "000005"),
    )
    conn.commit()
    conn.close()

    class QuoteClient:
        _ok = True

        def get_quotes(self, codes):
            return [{"code": "000005", "price": 9.4, "last_close": 9.5}]

    monkeypatch.setattr(ths_quote, "get_client", lambda *a, **k: QuoteClient())

    main._check_paper_risk(user_id)

    assert "000005" not in state["positions"]
    assert not any(p["code"] == "000005" for p in broker.get_positions())


def test_do_scan_cloud_path_auto_sells_paper_stop_loss(scan_env, monkeypatch):
    user_id, _ = scan_env
    state = _state(user_id, auto_execute=False)
    state["active_strategy"] = "ma_cross"
    assert main.execute_buy(user_id, "000006", "CloudStop", 10.0, 1000)["ok"]

    broker = pb.get_broker(user_id)
    conn = broker._conn()
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE paper_positions SET buy_time=? WHERE user_id=? AND code=?",
        (yesterday, user_id, "000006"),
    )
    conn.commit()
    conn.close()

    class QuoteClient:
        _ok = True

        def get_quotes(self, codes):
            return [{"code": "000006", "price": 9.4, "last_close": 9.5}]

    monkeypatch.setattr(ths_quote, "get_client", lambda *a, **k: QuoteClient())
    monkeypatch.setattr(sm, "run", lambda *a, **k: {"results": [], "scanned": 0})

    main.do_scan(user_id)

    assert "000006" not in state["positions"]
    assert not any(p["code"] == "000006" for p in broker.get_positions())


def test_paper_position_keeps_original_strategy_after_switch(scan_env, monkeypatch):
    user_id, _ = scan_env
    _mock_scan(monkeypatch, position_ratio=0.15)
    state = _state(user_id, auto_execute=True)
    state["active_strategy"] = "lianban_2plus"
    main.do_scan(user_id)

    broker = pb.get_broker(user_id)
    conn = broker._conn()
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE paper_positions SET buy_time=? WHERE user_id=? AND code=?",
        (yesterday, user_id, "000003"),
    )
    conn.commit()
    conn.close()

    from core.daban_state import _DABAN_PEAKS
    _DABAN_PEAKS[(user_id, "000003")] = 11.3
    state["active_strategy"] = "ma_cross"

    class QuoteClient:
        _ok = True

        def get_quotes(self, codes):
            return [{"code": "000003", "price": 10.8, "last_close": 10.0}]

    monkeypatch.setattr(ths_quote, "get_client", lambda *a, **k: QuoteClient())

    main._check_paper_risk(user_id)

    assert "000003" not in state["positions"]
    assert not any(p["code"] == "000003" for p in broker.get_positions())


def test_cancel_pending_signals_releases_today_unfinished_signals(uid):
    sid1 = um.add_trade_signal(uid, "000001", "A", "BUY", 10.0, 100, status="pending")
    sid2 = um.add_trade_signal(uid, "000002", "B", "SELL", 11.0, 200, status="sent")
    sid3 = um.add_trade_signal(uid, "000003", "C", "BUY", 12.0, 300, status="executed")

    assert um.cancel_pending_signals(uid) == 2

    assert um.get_trade_signal(sid1)["status"] == "cancelled"
    assert um.get_trade_signal(sid2)["status"] == "cancelled"
    assert um.get_trade_signal(sid3)["status"] == "executed"
