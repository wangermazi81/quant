# -*- coding: utf-8 -*-
"""
tests/test_trading_integration.py
main.py 下单编排集成测试（execute_buy/execute_sell/do_scan）。

这是 keystone 重构（main.py 用 QUANT_TEST_IMPORT 守卫 import 时副作用）解锁的成果：
现在能安全 `import main` 并在临时库上端到端测真实下单入口，而不会启动调度线程/初始化
THSOperator/写真实库。

运行：
  cd ths_auto_trader
  python -m pytest tests/test_trading_integration.py -v
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

# ★ 必须在 import main 之前设：跳过 main 的 import 时副作用（建表/迁移/operator/调度线程）。
os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

import pytest
import user_manager as um
import db_migration
import paper_broker as pb
import main                                  # keystone 重构后可安全 import
from app_state import get_user_state, user_states


def test_import_had_no_side_effects():
    """守卫生效的证明：纯导入下 operator=None、未启动任何调度线程。"""
    import threading
    assert main.operator is None
    names = [t.name for t in threading.enumerate()]
    assert not any("scheduler" in n.lower() or n in ("vibe-scheduler", "news-center", "review-engine")
                   for n in names), names


@pytest.fixture
def uid(tmp_path, monkeypatch):
    """临时 users.db + paper_trading.db，建用户（默认模拟盘），清掉所有进程内缓存。"""
    udb = str(tmp_path / "users.db")
    monkeypatch.setattr(um, "DB_PATH", udb)
    monkeypatch.setattr(db_migration, "USERS_DB", udb)
    monkeypatch.setattr(pb, "DB_PATH", tmp_path / "paper.db")
    pb._brokers.clear()          # 清模拟盘 broker 实例缓存（否则用旧 DB_PATH）
    user_states.clear()          # 清内存用户运行态缓存
    um.init_db(); db_migration.migrate_users_db(); pb.init_paper_db()
    code = um.create_invite_code(creator_uid=1, max_uses=5, is_admin_code=True, reward=0)["code"]
    um.register("trader", "t@x.com", "pass123", code, ip="1.1.1.1")
    _uid = um.login("trader", "pass123")["user_id"]
    yield _uid
    pb._brokers.clear(); user_states.clear()


def _age_position_one_day(_uid, code):
    """把某持仓 buy_time 回拨一天，绕过 A 股 T+1（当天买入不可当天卖）。"""
    broker = pb.get_broker(_uid)
    con = broker._conn()
    yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute("UPDATE paper_positions SET buy_time=? WHERE user_id=? AND code=?",
                (yday, _uid, code))
    con.commit(); con.close()


class TestExecuteBuy:
    def test_buy_creates_position(self, uid):
        r = main.execute_buy(uid, "000001", "平安银行", 12.5, 400, reason="测试买入")
        assert r["ok"], r
        # 内存运行态更新
        s = get_user_state(uid)
        assert "000001" in s["positions"]
        assert s["positions"]["000001"]["shares"] == 400
        assert s["executed"] >= 1
        # 模拟盘 DB 确有持仓
        positions = pb.get_broker(uid).get_positions()
        assert any(p["code"] == "000001" for p in positions)

    def test_buy_deducts_cash(self, uid):
        broker = pb.get_broker(uid)
        before = broker._cash()
        main.execute_buy(uid, "600000", "浦发银行", 10.0, 1000)
        assert broker._cash() < before

    def test_buy_routes_to_paper_by_default(self, uid):
        # 新用户默认 broker_type=paper → 不应产生实盘信号，应即时成交进模拟盘
        r = main.execute_buy(uid, "000002", "万科A", 11.0, 300)
        assert r["ok"] and "signal_id" not in r          # 实盘路径才返回 signal_id


class TestExecuteSell:
    def test_buy_then_sell(self, uid):
        assert main.execute_buy(uid, "000001", "平安银行", 12.5, 400)["ok"]
        _age_position_one_day(uid, "000001")             # 绕过 T+1
        r = main.execute_sell(uid, "000001", 13.0, sell_reason="测试卖出")
        assert r["ok"], r
        s = get_user_state(uid)
        assert "000001" not in s["positions"]            # 卖出后持仓清除


class TestDoScanSmoke:
    def test_do_scan_callable_on_idle_user(self, uid):
        # 未运行的用户：do_scan 应安全早返回（is_trading_time/running 守卫），不抛异常
        s = get_user_state(uid)
        s["running"] = False
        main.do_scan(uid)                                 # 不抛即通过（证明 do_scan 可被测试驱动）
