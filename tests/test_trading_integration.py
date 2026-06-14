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


class TestDoScanQueue:
    """do_scan 选股→入队全流程集成测试（验证已部署的 order_sizing 接入 + 仓位护栏在实跑时的行为）。"""

    @pytest.fixture
    def scan(self, uid, monkeypatch):
        import strategy_manager as sm
        import notification_manager as nm
        import ths_quote
        # 交易时段=True；无行情客户端(避网络)；通知 no-op(避线程)；策略仓位配置取默认(走面板比例路径)
        monkeypatch.setattr(main, "is_trading_time", lambda: True)
        monkeypatch.setattr(ths_quote, "get_client", lambda *a, **k: None)
        monkeypatch.setattr(nm, "send_notification", lambda *a, **k: {"ok": True})
        monkeypatch.setattr(sm, "get_position_config", lambda *a, **k: {})
        return uid, sm, monkeypatch

    @staticmethod
    def _state(_uid, capital=500000, ratio=0.2, sp=0.2, maxpos=5):
        s = get_user_state(_uid)
        s.update(running=True, active_strategy="test_strat", capital=capital,
                 position_ratio=ratio, single_position_max_ratio=sp,
                 max_positions=maxpos, auto_execute=False, queue=[], positions={})
        return s

    @staticmethod
    def _mock_results(sm, mp, cands):
        mp.setattr(sm, "run", lambda *a, **k: {"results": cands, "scanned": len(cands)})

    def test_scan_queues_correctly_sized_orders(self, scan):
        uid, sm, mp = scan
        cands = [
            {"code": "000001", "name": "A", "price": 10.0, "change": 3.0},
            {"code": "600000", "name": "B", "price": 12.5, "change": 2.0},
            {"code": "000002", "name": "C", "price": 20.0, "change": 4.0},
        ]
        self._mock_results(sm, mp, cands)
        s = self._state(uid, capital=500000, ratio=0.2, sp=0.2, maxpos=5)
        main.do_scan(uid)
        from order_sizing import lot_shares
        q = {it["code"]: it for it in s["queue"]}
        assert set(q) == {"000001", "600000", "000002"}
        # 每笔手数 = lot_shares(全仓 × 比例, 价)，验证 order_sizing 在 do_scan 实跑中生效
        assert q["000001"]["shares"] == lot_shares(100000, 10.0)   # 10000
        assert q["600000"]["shares"] == lot_shares(100000, 12.5)   # 8000
        assert q["000002"]["shares"] == lot_shares(100000, 20.0)   # 5000

    def test_max_positions_guardrail(self, scan):
        uid, sm, mp = scan
        cands = [{"code": f"00000{i}", "name": str(i), "price": 10.0, "change": 3.0}
                 for i in range(1, 5)]                       # 4 只候选
        self._mock_results(sm, mp, cands)
        s = self._state(uid, capital=500000, ratio=0.1, sp=0.2, maxpos=2)
        main.do_scan(uid)
        assert len(s["queue"]) == 2                          # 持仓名额上限 2，多余信号不入队

    def test_available_cash_gate(self, scan):
        uid, sm, mp = scan
        cands = [{"code": f"00000{i}", "name": str(i), "price": 10.0, "change": 3.0}
                 for i in range(1, 4)]                       # 3 只候选
        self._mock_results(sm, mp, cands)
        # 每仓=200000×0.5=100000，可用 200000 → 只够 2 仓，第 3 只被可用资金闸门挡下
        s = self._state(uid, capital=200000, ratio=0.5, sp=0.5, maxpos=5)
        main.do_scan(uid)
        assert len(s["queue"]) == 2

    def test_single_position_guardrail_caps_ratio(self, scan):
        uid, sm, mp = scan
        cands = [{"code": "000001", "name": "A", "price": 10.0, "change": 3.0}]
        self._mock_results(sm, mp, cands)
        # 面板仓位 50% 但单笔护栏 20% → 实际按 20% 建仓
        s = self._state(uid, capital=500000, ratio=0.5, sp=0.2, maxpos=5)
        main.do_scan(uid)
        from order_sizing import lot_shares
        assert s["queue"][0]["shares"] == lot_shares(500000 * 0.2, 10.0)   # 护栏夹到 20%
