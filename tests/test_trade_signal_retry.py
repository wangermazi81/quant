import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))


def test_get_pending_signals_does_not_requeue_stale_sent(tmp_path, monkeypatch):
    from core import db_migration as dbm
    from core import user_manager as um

    db_path = str(tmp_path / "users_test.db")
    monkeypatch.setattr(um, "DB_PATH", db_path)
    monkeypatch.setattr(dbm, "USERS_DB", db_path)
    um.init_db()
    dbm.migrate_users_db()

    uid = 1
    stale_id = um.add_trade_signal(uid, "000001", "PingAn", "BUY", 10.0, 100, status="sent")
    fresh_id = um.add_trade_signal(uid, "000002", "Vanke", "BUY", 11.0, 100, status="sent")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE trade_signals SET updated_at=datetime('now','localtime','-3 minutes') WHERE id=?",
            (stale_id,),
        )

    signals = um.get_pending_signals(uid)

    assert signals == []
    assert um.get_trade_signal(stale_id)["status"] == "sent"
    assert um.get_trade_signal(fresh_id)["status"] == "sent"


def test_restore_stale_sent_signals_requires_broker_reconciliation(tmp_path, monkeypatch):
    from core import db_migration as dbm
    from core import user_manager as um

    db_path = str(tmp_path / "users_test.db")
    monkeypatch.setattr(um, "DB_PATH", db_path)
    monkeypatch.setattr(dbm, "USERS_DB", db_path)
    um.init_db()
    dbm.migrate_users_db()

    uid = 1
    stale_sent = um.add_trade_signal(uid, "000003", "OldSent", "BUY", 10.0, 100, status="sent")
    submitted = um.add_trade_signal(uid, "000004", "Submitted", "BUY", 10.0, 100, status="submitted")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE trade_signals SET updated_at=datetime('now','localtime','-3 minutes') WHERE id IN (?,?)",
            (stale_sent, submitted),
        )

    assert um.restore_stale_sent_signals(uid, older_than_sec=120) == 0
    assert um.get_trade_signal(stale_sent)["status"] == "sent"

    assert um.restore_stale_sent_signals(
        uid,
        older_than_sec=120,
        confirmed_not_submitted=True,
    ) == 1
    assert um.get_trade_signal(stale_sent)["status"] == "pending"
    assert um.get_trade_signal(submitted)["status"] == "submitted"
