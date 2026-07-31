# -*- coding: utf-8 -*-
"""
tests/test_paper_broker.py
模拟盘引擎单元测试

运行方式：
  cd ths_auto_trader
  python -m pytest tests/ -v
"""

import sys
import sqlite3
import threading
import pytest
from pathlib import Path

# 把项目根目录加入 sys.path，使 _pathsetup 可被 import
sys.path.insert(0, str(Path(__file__).parent.parent))

# 使用临时数据库，不污染正式数据
@pytest.fixture
def broker(tmp_path, monkeypatch):
    """每个测试用独立临时数据库的 PaperBroker 实例。

    引擎已重构为多用户（PaperBroker(user_id)，数据按 user_id 隔离、共享 paper_trading.db）。
    单测隔离：monkeypatch 模块级 DB_PATH 到临时库，再建表 + 用专属 user_id 实例化。
    """
    from broker import paper_broker as pb
    from datetime import datetime, timedelta
    monkeypatch.setattr(pb, "DB_PATH", tmp_path / "test_paper.db")
    pb.init_paper_db()
    b = pb.PaperBroker(user_id=999)
    # 引擎含 A 股 T+1（当天买入不可当天卖出）。单测在同一天内买卖，
    # 故买入成功后把持仓 buy_time 回拨一天，使卖出/风控/统计用例验证的是 T+1 之后的正常流程。
    _orig_buy = b.submit_buy
    def _buy_then_age(*a, **kw):
        r = _orig_buy(*a, **kw)
        if isinstance(r, dict) and r.get("ok"):
            try:
                con = b._conn()
                yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
                con.execute("UPDATE paper_positions SET buy_time=? WHERE user_id=?", (yday, b._uid))
                con.commit(); con.close()
            except Exception:
                pass
        return r
    b.submit_buy = _buy_then_age
    return b


@pytest.fixture
def raw_broker(tmp_path, monkeypatch):
    """不做 buy_time 回拨的 PaperBroker —— 专门用来验 T+1 规则本身。

    上面的 broker fixture 为了让「卖出/风控/统计」用例能在同一天内跑完，
    统一把买入时间回拨了一天，于是 T+1 拒卖这条分支从来没有被任何用例走到过：
    真把日期比较改坏（比如时间格式变更让 strptime 抛异常而走「放行」分支），
    模拟盘会允许当日回转交易，而全套测试依然全绿。
    """
    from broker import paper_broker as pb
    monkeypatch.setattr(pb, "DB_PATH", tmp_path / "test_paper_t1.db")
    pb.init_paper_db()
    return pb.PaperBroker(user_id=998)


class TestLimitPriceBand:
    """A 股不可能以高于涨停价、低于跌停价成交——成交价必须夹在合法价带内。"""

    def test_buy_exec_price_never_exceeds_limit_up(self, broker):
        limit_up = 11.0
        exec_price, _amount, _comm, _slip = broker._calc_buy_cost(
            limit_up, 100, price_cap=limit_up)
        assert exec_price == limit_up, "封板按涨停价成交后再叠加滑点会越出合法价带"

    def test_sell_exec_price_never_below_limit_down(self, broker):
        limit_down = 9.0
        exec_price, _net, _comm, _tax = broker._calc_sell_income(
            limit_down, 100, price_floor=limit_down)
        assert exec_price == limit_down, "弱跌停按跌停价成交后再减滑点会跌穿合法价带"

    def test_normal_price_still_gets_slippage(self, broker):
        """不给价带时滑点照常生效——夹取不能把正常路径的滑点也吃掉。"""
        buy_price, _a, _c, _s = broker._calc_buy_cost(10.0, 100)
        sell_price, _n, _c2, _t = broker._calc_sell_income(10.0, 100)
        assert buy_price > 10.0
        assert sell_price < 10.0


class TestT1Rule:
    def test_same_day_sell_is_rejected(self, raw_broker):
        """★A 股 T+1：当天买入当天不可卖出"""
        assert raw_broker.submit_buy("000001", "平安银行", 10.0, 100)["ok"] is True
        result = raw_broker.submit_sell("000001", 11.0)
        assert result["ok"] is False
        assert "T+1" in result["msg"]

    def test_position_bought_yesterday_can_be_sold(self, raw_broker):
        """回拨到昨天即可正常卖出——确认拒卖不是无条件的"""
        from datetime import datetime, timedelta

        assert raw_broker.submit_buy("000001", "平安银行", 10.0, 100)["ok"] is True
        con = raw_broker._conn()
        yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute("UPDATE paper_positions SET buy_time=? WHERE user_id=?",
                    (yday, raw_broker._uid))
        con.commit()
        con.close()
        assert raw_broker.submit_sell("000001", 11.0)["ok"] is True

    def test_unparsable_buy_time_fails_closed_and_warns(self, raw_broker, caplog):
        """buy_time 格式非法时当前行为是「放行」，锁死该行为并要求留下 WARNING。

        放行是有意的：解析不出日期就无法判断是否当日，拒卖会让这笔持仓永远卖不掉。
        但这等于绕过了一条风控，必须在日志里看得见。
        """
        import logging

        assert raw_broker.submit_buy("000001", "平安银行", 10.0, 100)["ok"] is True
        con = raw_broker._conn()
        con.execute("UPDATE paper_positions SET buy_time=? WHERE user_id=?",
                    ("not-a-date", raw_broker._uid))
        con.commit()
        con.close()

        with caplog.at_level(logging.WARNING, logger="broker.paper_broker"):
            result = raw_broker.submit_sell("000001", 11.0)

        assert result["ok"] is False
        assert "T+1" in result["msg"]
        con = raw_broker._conn()
        remaining = con.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE user_id=? AND code=?",
            (raw_broker._uid, "000001"),
        ).fetchone()[0]
        con.close()
        assert remaining == 1
        assert any("T+1" in r.message for r in caplog.records), \
            "T+1 检查被跳过时必须留下 WARNING，否则风控静默失效无人知晓"


# ══════════════════════════════════════════════════════
# 1. 买入测试
# ══════════════════════════════════════════════════════
class TestStrictMarketQuoteGate:
    def test_strict_ai_buy_rejects_missing_quote(self, raw_broker, monkeypatch):
        from market import ths_quote

        monkeypatch.setattr(ths_quote, "get_client", lambda: None)
        raw_broker.set_config({"require_market_quote": 1})

        result = raw_broker.submit_buy("000001", "Ping An Bank", 10.0, 100)

        assert result["ok"] is False
        assert "quote" in result["msg"]

    def test_strict_ai_sell_rejects_missing_quote(self, raw_broker, monkeypatch):
        from datetime import datetime, timedelta
        from market import ths_quote

        assert raw_broker.submit_buy("000001", "Ping An Bank", 10.0, 100)["ok"] is True
        con = raw_broker._conn()
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute(
            "UPDATE paper_positions SET buy_time=? WHERE user_id=?",
            (yesterday, raw_broker._uid),
        )
        con.commit()
        con.close()
        monkeypatch.setattr(ths_quote, "get_client", lambda: None)
        raw_broker.set_config({"require_market_quote": 1})

        result = raw_broker.submit_sell("000001", 10.0)

        assert result["ok"] is False
        assert "quote" in result["msg"]


class TestBuy:
    def test_default_capital_is_one_million(self, broker):
        acct = broker.get_account()

        assert acct["initial_capital"] == 1_000_000.0
        assert acct["cash"] == 1_000_000.0

    def test_buy_success(self, broker):
        """正常买入：资金充足，100整股"""
        result = broker.submit_buy("000001", "平安银行", 12.50, 400, reason="测试买入")
        assert result["ok"] is True
        assert isinstance(result["trade_id"], int)
        assert result["shares"] == 400
        assert result["exec_price"] > 12.50   # 含滑点

    def test_buy_deducts_cash(self, broker):
        """买入后现金减少"""
        before = broker._cash()
        broker.submit_buy("000001", "平安银行", 10.0, 1000)
        after = broker._cash()
        assert after < before

    def test_buy_locks_the_cash_row_before_reading_available_funds(self, broker):
        traced_sql = []
        original_conn = broker._conn

        def traced_conn():
            con = original_conn()
            con.set_trace_callback(traced_sql.append)
            return con

        broker._conn = traced_conn

        result = broker.submit_buy("000001", "平安银行", 10.0, 100)

        assert result["ok"] is True
        begin_index = next(
            index
            for index, statement in enumerate(traced_sql)
            if statement.strip().upper() == "BEGIN IMMEDIATE"
        )
        cash_read_index = next(
            index
            for index, statement in enumerate(traced_sql)
            if "KEY='CASH'" in statement.upper()
        )
        assert begin_index < cash_read_index

    def test_buy_round_lot(self, broker):
        """非整百股自动向下取整"""
        result = broker.submit_buy("000001", "平安银行", 10.0, 150)
        assert result["ok"] is True
        assert result["shares"] == 100   # 150 → 100

    def test_buy_insufficient_cash(self, broker):
        """资金不足拒绝买入"""
        # 初始500000，买500000股@10 = 5000000元，远超资金
        result = broker.submit_buy("000001", "平安银行", 10.0, 500_000)
        assert result["ok"] is False
        assert "资金不足" in result["msg"]

    def test_buy_no_duplicate(self, broker):
        """同一标的不允许重复买入"""
        broker.submit_buy("000001", "平安银行", 10.0, 100)
        result = broker.submit_buy("000001", "平安银行", 10.5, 100)
        assert result["ok"] is False
        assert "已持有" in result["msg"]

    def test_buy_commission_min(self, broker):
        """小额买入佣金不低于最低5元"""
        result = broker.submit_buy("000001", "平安银行", 5.0, 100)
        assert result["ok"] is True
        assert result["commission"] >= 5.0

    def test_buy_zero_shares(self, broker):
        """0股拒绝"""
        result = broker.submit_buy("000001", "平安银行", 10.0, 0)
        assert result["ok"] is False

    def test_attribute_trade_persists_identity_and_updates_position(self, broker):
        from broker import paper_broker as pb

        result = broker.submit_buy("000001", "平安银行", 10.0, 100)
        attribution = {
            "user_id": 101,
            "strategy_key": "relay_factor_mining_v2",
            "model_version": "relay_factor_mining_v2_ridge",
            "experiment_id": "experiment-v2-1",
            "model_instance_id": "model-instance-v2-1",
            "watchlist_id": "2026-07-28-000001.SZ",
            "source_run_id": "v2-run-1",
        }

        broker.attribute_trade(result["trade_id"], attribution)
        pb.init_paper_db()

        trade = broker.get_trades()[0]
        assert trade["owner_user_id"] == attribution["user_id"]
        assert trade["strategy_key"] == attribution["strategy_key"]
        assert trade["model_version"] == attribution["model_version"]
        assert trade["experiment_id"] == attribution["experiment_id"]
        assert trade["model_instance_id"] == attribution["model_instance_id"]
        assert trade["watchlist_id"] == attribution["watchlist_id"]
        assert trade["source_run_id"] == attribution["source_run_id"]
        assert broker.get_positions()[0]["strategy"] == attribution["strategy_key"]

    def test_buy_persists_attribution_in_the_fill_transaction(self, broker):
        attribution = {
            "user_id": 101,
            "strategy_key": "relay_factor_mining_v2",
            "model_version": "relay_factor_mining_v2_ridge",
            "experiment_id": "experiment-v2-atomic",
            "model_instance_id": "model-instance-v2-atomic",
            "watchlist_id": "immutable-watchlist-id",
            "source_run_id": "v2-run-atomic",
        }

        result = broker.submit_buy(
            "000001",
            "平安银行",
            10.0,
            100,
            attribution=attribution,
        )

        assert result["ok"] is True
        trade = broker.get_trades()[0]
        assert trade["owner_user_id"] == attribution["user_id"]
        assert {
            key: trade[key]
            for key in (
                "strategy_key",
                "model_version",
                "experiment_id",
                "model_instance_id",
                "watchlist_id",
                "source_run_id",
            )
        } == {
            key: attribution[key]
            for key in (
                "strategy_key",
                "model_version",
                "experiment_id",
                "model_instance_id",
                "watchlist_id",
                "source_run_id",
            )
        }
        assert broker.get_positions()[0]["strategy"] == attribution["strategy_key"]


def test_init_paper_db_migrates_legacy_trade_table_idempotently(tmp_path, monkeypatch):
    from broker import paper_broker as pb

    db_path = tmp_path / "legacy_paper.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            code TEXT NOT NULL,
            name TEXT DEFAULT '',
            buy_time TEXT NOT NULL,
            buy_price REAL NOT NULL,
            shares INTEGER NOT NULL,
            buy_cost REAL NOT NULL
        )
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(pb, "DB_PATH", db_path)

    pb.init_paper_db()
    pb.init_paper_db()

    con = sqlite3.connect(db_path)
    columns = {row[1] for row in con.execute("PRAGMA table_info(paper_trades)")}
    con.close()
    assert {
        "owner_user_id",
        "strategy_key",
        "model_version",
        "experiment_id",
        "model_instance_id",
        "watchlist_id",
        "source_run_id",
    }.issubset(columns)


def test_init_paper_db_serializes_concurrent_legacy_migrations(tmp_path, monkeypatch):
    from broker import paper_broker as pb
    import threading

    db_path = tmp_path / "concurrent_legacy_paper.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            code TEXT NOT NULL,
            name TEXT DEFAULT '',
            buy_time TEXT NOT NULL,
            buy_price REAL NOT NULL,
            shares INTEGER NOT NULL,
            buy_cost REAL NOT NULL
        )
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(pb, "DB_PATH", db_path)
    start = threading.Barrier(8)
    errors = []

    def migrate():
        try:
            start.wait()
            pb.init_paper_db()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=migrate) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    con = sqlite3.connect(db_path)
    columns = [row[1] for row in con.execute("PRAGMA table_info(paper_trades)")]
    con.close()
    for column in (
        "strategy_key",
        "model_version",
        "experiment_id",
        "model_instance_id",
        "watchlist_id",
        "source_run_id",
    ):
        assert columns.count(column) == 1


# ══════════════════════════════════════════════════════
# 2. 卖出测试
# ══════════════════════════════════════════════════════
class TestSell:
    def test_sell_success(self, broker):
        """先买后卖，正常流程"""
        broker.submit_buy("000001", "平安银行", 10.0, 1000)
        result = broker.submit_sell("000001", 12.0, "止盈")
        assert result["ok"] is True
        assert result["pnl"] > 0    # 买10卖12，盈利

    def test_sell_no_position(self, broker):
        """无持仓卖出拒绝"""
        result = broker.submit_sell("999999", 10.0)
        assert result["ok"] is False
        assert "无持仓" in result["msg"]

    def test_sell_tax(self, broker):
        """卖出含印花税"""
        broker.submit_buy("000001", "平安银行", 10.0, 1000)
        result = broker.submit_sell("000001", 10.0)
        assert result["ok"] is True
        assert result["tax"] > 0

    def test_default_stamp_tax_is_half_per_thousand_and_zero_is_respected(
        self,
        broker,
    ):
        from broker import paper_broker as pb

        assert pb.DEFAULT_TAX_RATE == 0.0005
        broker.set_config({"tax_rate": 0})

        _price, _income, _commission, tax = broker._calc_sell_income(
            10.0,
            1000,
        )

        assert tax == 0.0

    def test_sell_adds_cash(self, broker):
        """卖出后现金增加"""
        broker.submit_buy("000001", "平安银行", 10.0, 1000)
        cash_after_buy = broker._cash()
        broker.submit_sell("000001", 10.0)
        cash_after_sell = broker._cash()
        assert cash_after_sell > cash_after_buy

    def test_sell_removes_position(self, broker):
        """卖出后持仓清空"""
        broker.submit_buy("000001", "平安银行", 10.0, 1000)
        broker.submit_sell("000001", 10.0)
        positions = broker.get_positions()
        assert not any(p["code"] == "000001" for p in positions)

    def test_sell_records_trade(self, broker):
        """成交后 paper_trades 有完整记录"""
        broker.submit_buy("000001", "平安银行", 10.0, 1000)
        broker.submit_sell("000001", 11.0, "手动卖出")
        trades = broker.get_trades()
        closed = [t for t in trades if t["sell_time"] is not None]
        assert len(closed) >= 1
        t = closed[0]
        assert t["sell_reason"] == "手动卖出"
        assert t["pnl"] is not None


# ══════════════════════════════════════════════════════
# 3. 风控规则测试
# ══════════════════════════════════════════════════════
class TestRiskRules:
    def test_stop_loss_triggered(self, broker):
        """跌超止损线(-5%)自动卖出"""
        broker.submit_buy("000001", "平安银行", 10.0, 1000)
        # 模拟价格跌到9.0 → 亏损-10%，超过止损-5%
        triggered = broker.check_risk_rules({"000001": 9.0})
        assert any(t["code"] == "000001" for t in triggered)

    def test_take_profit_triggered(self, broker):
        """涨超止盈线(+10%)自动卖出"""
        broker.submit_buy("000001", "平安银行", 10.0, 1000)
        triggered = broker.check_risk_rules({"000001": 11.5})
        assert any(t["code"] == "000001" for t in triggered)

    def test_no_trigger_within_range(self, broker):
        """在止损/止盈范围内不触发"""
        broker.submit_buy("000001", "平安银行", 10.0, 1000)
        triggered = broker.check_risk_rules({"000001": 10.3})
        assert len(triggered) == 0

    def test_no_trigger_missing_price(self, broker):
        """无实时价格时不触发"""
        broker.submit_buy("000001", "平安银行", 10.0, 1000)
        triggered = broker.check_risk_rules({})
        assert len(triggered) == 0


# ══════════════════════════════════════════════════════
# 4. 统计测试
# ══════════════════════════════════════════════════════
class TestStats:
    def _make_trades(self, broker):
        """构造2盈1亏的成交记录"""
        # 盈利1
        broker.submit_buy("000001", "A", 10.0, 1000)
        broker.submit_sell("000001", 11.0)
        # 盈利2
        broker.submit_buy("000002", "B", 20.0, 500)
        broker.submit_sell("000002", 22.0)
        # 亏损
        broker.submit_buy("000003", "C", 15.0, 500)
        broker.submit_sell("000003", 13.0)

    def test_win_rate(self, broker):
        self._make_trades(broker)
        stats = broker.get_stats()
        # 2胜1负 → 胜率约66.7%
        assert stats["trade_count"] == 3
        assert abs(stats["win_rate"] - 66.67) < 1.0

    def test_profit_factor(self, broker):
        self._make_trades(broker)
        stats = broker.get_stats()
        assert stats["profit_factor"] > 1.0   # 盈多亏少

    def test_stats_no_trades(self, broker):
        """无成交时统计全为0"""
        stats = broker.get_stats()
        assert stats["trade_count"] == 0
        assert stats["win_rate"] == 0.0

    def test_account_equity(self, broker):
        """账户净资产 = 现金 + 持仓市值"""
        broker.submit_buy("000001", "A", 10.0, 1000)
        acct = broker.get_account()
        assert acct["equity"] == pytest.approx(acct["cash"] + acct["market_value"], abs=1)


# ══════════════════════════════════════════════════════
# 5. 重置测试
# ══════════════════════════════════════════════════════
class TestLegacyCapitalMigration:
    @staticmethod
    def _set_config(con, user_id, values):
        for key, value in values.items():
            con.execute(
                "INSERT OR REPLACE INTO paper_config(user_id,key,value)"
                " VALUES(?,?,?)",
                (user_id, key, str(value)),
            )

    @staticmethod
    def _insert_closed_trade(con, user_id):
        con.execute(
            "INSERT INTO paper_trades("
            "user_id,code,name,buy_time,sell_time,buy_price,sell_price,"
            "shares,buy_cost,sell_income,pnl,pnl_pct,hold_days"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id,
                "000001",
                "A",
                "2026-07-01 09:30:00",
                "2026-07-02 10:00:00",
                10.0,
                10.01,
                100,
                1000.0,
                1001.0,
                1.0,
                0.1,
                1,
            ),
        )

    def test_ensure_starts_immediate_transaction_before_reading_config(
        self,
        broker,
    ):
        traced_sql = []
        original_conn = broker._conn

        def traced_conn():
            con = original_conn()
            con.set_trace_callback(traced_sql.append)
            return con

        broker._conn = traced_conn
        broker._ensure_user_config()

        normalized = [statement.strip().upper() for statement in traced_sql]
        begin_indexes = [
            index
            for index, statement in enumerate(normalized)
            if statement == "BEGIN IMMEDIATE"
        ]
        assert begin_indexes, traced_sql
        config_read_index = next(
            index
            for index, statement in enumerate(normalized)
            if "FROM PAPER_CONFIG" in statement
        )
        assert begin_indexes[0] < config_read_index

    def test_concurrent_migration_is_idempotent_and_preserves_position_accounting(
        self,
        broker,
    ):
        from broker import paper_broker as pb

        con = broker._conn()
        self._set_config(
            con,
            broker._uid,
            {
                "initial_capital": 500_000.0,
                "cash": 400_000.0,
                "peak_equity": 500_000.0,
            },
        )
        con.execute(
            "DELETE FROM paper_config WHERE user_id=? AND key='pnl_base_capital'",
            (broker._uid,),
        )
        con.execute(
            "INSERT INTO paper_positions("
            "user_id,code,name,shares,cost_price,buy_time,buy_amount"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                broker._uid,
                "000001",
                "Legacy",
                10_000,
                10.0,
                "2026-07-01 09:30:00",
                100_000.0,
            ),
        )
        con.commit()
        con.close()

        start = threading.Barrier(3)
        failures = []

        def migrate(candidate):
            start.wait()
            try:
                candidate._ensure_user_config()
            except Exception as exc:
                failures.append(exc)

        candidates = []
        for _ in range(2):
            candidate = object.__new__(pb.PaperBroker)
            candidate._uid = broker._uid
            candidate._lock = threading.Lock()
            candidates.append(candidate)
        threads = [
            threading.Thread(target=migrate, args=(candidate,))
            for candidate in candidates
        ]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=15)

        assert all(not thread.is_alive() for thread in threads)
        assert failures == []
        broker._ensure_user_config()

        con = broker._conn()
        flows = con.execute(
            "SELECT amount,flow_type FROM paper_capital_flows"
            " WHERE user_id=?",
            (broker._uid,),
        ).fetchall()
        position = con.execute(
            "SELECT shares,cost_price,buy_amount,display_pnl_scale"
            " FROM paper_positions WHERE user_id=? AND code=?",
            (broker._uid, "000001"),
        ).fetchone()
        con.close()

        assert len(flows) == 1
        assert float(flows[0]["amount"]) == 500_000.0
        assert flows[0]["flow_type"] == "deposit"
        assert broker._cash() == 900_000.0
        assert tuple(position[:3]) == (10_000, 10.0, 100_000.0)
        assert float(position["display_pnl_scale"]) == 2.0

        account = broker.get_account(cur_prices={"000001": 11.0})
        displayed_position = broker.get_positions(
            cur_prices={"000001": 11.0}
        )[0]
        assert account["equity"] == 1_010_000.0
        assert displayed_position["pnl_amount"] == 20_000.0

    def test_stats_remove_migration_deposit_from_returns_and_drawdown(
        self,
        broker,
    ):
        from datetime import datetime, timedelta

        flow_date = datetime.now().date()
        first_date = (flow_date - timedelta(days=2)).isoformat()
        loss_date = (flow_date - timedelta(days=1)).isoformat()
        final_date = (flow_date + timedelta(days=1)).isoformat()

        con = broker._conn()
        self._set_config(
            con,
            broker._uid,
            {
                "initial_capital": 500_000.0,
                "cash": 490_000.0,
                "peak_equity": 500_000.0,
            },
        )
        con.execute(
            "DELETE FROM paper_config WHERE user_id=? AND key='pnl_base_capital'",
            (broker._uid,),
        )
        con.execute(
            "INSERT INTO paper_equity("
            "user_id,snap_date,cash,market_val,equity,drawdown"
            ") VALUES(?,?,?,?,?,?)",
            (broker._uid, first_date, 500_000.0, 0.0, 500_000.0, 0.0),
        )
        con.execute(
            "INSERT INTO paper_equity("
            "user_id,snap_date,cash,market_val,equity,drawdown"
            ") VALUES(?,?,?,?,?,?)",
            (broker._uid, loss_date, 490_000.0, 0.0, 490_000.0, -2.0),
        )
        con.commit()
        con.close()

        broker._ensure_user_config()

        con = broker._conn()
        con.execute(
            "INSERT INTO paper_equity("
            "user_id,snap_date,cash,market_val,equity,drawdown"
            ") VALUES(?,?,?,?,?,?)",
            (
                broker._uid,
                flow_date.isoformat(),
                990_000.0,
                0.0,
                990_000.0,
                0.0,
            ),
        )
        con.execute(
            "INSERT INTO paper_equity("
            "user_id,snap_date,cash,market_val,equity,drawdown"
            ") VALUES(?,?,?,?,?,?)",
            (broker._uid, final_date, 980_100.0, 0.0, 980_100.0, -1.0),
        )
        self._insert_closed_trade(con, broker._uid)
        con.commit()
        con.close()

        stats = broker.get_stats()

        assert stats["total_return"] == -2.98
        assert stats["max_drawdown"] == -2.98
        assert stats["sharpe"] == -15.8745

    def test_backfilled_flow_uses_the_historical_injection_jump(
        self,
        broker,
    ):
        from datetime import datetime, timedelta

        current_date = datetime.now().date()
        before_date = (current_date - timedelta(days=2)).isoformat()
        injection_date = (current_date - timedelta(days=1)).isoformat()

        con = broker._conn()
        self._set_config(
            con,
            broker._uid,
            {
                "initial_capital": 1_000_000.0,
                "cash": 1_000_000.0,
                "peak_equity": 1_000_000.0,
                "pnl_base_capital": 500_000.0,
            },
        )
        for snap_date, equity in (
            (before_date, 500_000.0),
            (injection_date, 1_000_000.0),
            (current_date.isoformat(), 990_000.0),
        ):
            con.execute(
                "INSERT INTO paper_equity("
                "user_id,snap_date,cash,market_val,equity,drawdown"
                ") VALUES(?,?,?,?,?,?)",
                (broker._uid, snap_date, equity, 0.0, equity, 0.0),
            )
        self._insert_closed_trade(con, broker._uid)
        con.commit()
        con.close()

        broker._ensure_user_config()

        con = broker._conn()
        flow = con.execute(
            "SELECT flow_date,amount FROM paper_capital_flows"
            " WHERE user_id=?",
            (broker._uid,),
        ).fetchone()
        con.close()
        stats = broker.get_stats()

        assert flow["flow_date"] == injection_date
        assert float(flow["amount"]) == 500_000.0
        assert stats["total_return"] == -1.0
        assert stats["max_drawdown"] == -1.0
        assert stats["sharpe"] == -11.225

    def test_same_day_pre_migration_snapshot_is_advanced_with_the_deposit(
        self,
        broker,
    ):
        from datetime import datetime

        flow_date = datetime.now().strftime("%Y-%m-%d")
        con = broker._conn()
        self._set_config(
            con,
            broker._uid,
            {
                "initial_capital": 500_000.0,
                "cash": 500_000.0,
                "peak_equity": 500_000.0,
            },
        )
        con.execute(
            "DELETE FROM paper_config WHERE user_id=? AND key='pnl_base_capital'",
            (broker._uid,),
        )
        con.execute(
            "INSERT INTO paper_equity("
            "user_id,snap_date,cash,market_val,equity,drawdown"
            ") VALUES(?,?,?,?,?,?)",
            (broker._uid, flow_date, 500_000.0, 0.0, 500_000.0, 0.0),
        )
        self._insert_closed_trade(con, broker._uid)
        con.commit()
        con.close()

        broker._ensure_user_config()

        con = broker._conn()
        snapshot = con.execute(
            "SELECT cash,equity,drawdown FROM paper_equity"
            " WHERE user_id=? AND snap_date=?",
            (broker._uid, flow_date),
        ).fetchone()
        con.close()
        stats = broker.get_stats()

        assert tuple(snapshot) == (1_000_000.0, 1_000_000.0, 0.0)
        assert stats["total_return"] == 0.0
        assert stats["max_drawdown"] == 0.0

    def test_reset_starts_a_new_capital_flow_epoch(self, broker):
        con = broker._conn()
        self._set_config(
            con,
            broker._uid,
            {
                "initial_capital": 500_000.0,
                "cash": 500_000.0,
                "peak_equity": 500_000.0,
            },
        )
        con.execute(
            "DELETE FROM paper_config WHERE user_id=? AND key='pnl_base_capital'",
            (broker._uid,),
        )
        con.commit()
        con.close()
        broker._ensure_user_config()

        broker.reset(initial_capital=200_000.0)

        con = broker._conn()
        flow_count = con.execute(
            "SELECT COUNT(*) FROM paper_capital_flows WHERE user_id=?",
            (broker._uid,),
        ).fetchone()[0]
        con.close()
        assert flow_count == 0


class TestReset:
    def test_reset_clears_data(self, broker):
        """重置后持仓、成交全部清空"""
        broker.submit_buy("000001", "A", 10.0, 1000)
        broker.reset(initial_capital=300_000)
        assert broker.get_positions() == []
        assert broker.get_trades() == []
        assert broker._cash() == 300_000.0

    def test_reset_custom_capital(self, broker):
        """重置可指定新初始资金"""
        broker.reset(initial_capital=200_000)
        acct = broker.get_account()
        assert acct["cash"] == 200_000.0
        assert acct["initial_capital"] == 200_000.0

    def test_reset_clamps_capital_to_allowed_range(self, broker):
        """本金越界被夹回区间，防止极端值配合 pnl 归一化放大盈亏"""
        from broker.paper_broker import MIN_RESET_CAPITAL, MAX_RESET_CAPITAL

        assert broker.reset(initial_capital=1.0)["initial_capital"] == MIN_RESET_CAPITAL
        assert broker.reset(initial_capital=10**12)["initial_capital"] == MAX_RESET_CAPITAL

    def test_reset_never_scales_pnl_even_with_small_capital(self, broker):
        """★安全回归：reset 成小额本金不得放大盈亏。

        曾经 reset 把 pnl_base_capital 写成用户指定的 cap，_pnl_scale() = 100万/本金，
        于是 reset 成 1 万后每笔盈亏 ×100 计入现金 —— 可凭空刷出天文收益率。
        """
        broker.reset(initial_capital=10_000)
        # 核心断言：小额本金也不得产生放大系数（曾经是 100万/1万 = 100 倍）
        assert broker._pnl_scale() == 1.0

        assert broker.submit_buy("000001", "A", 10.0, 100)["ok"] is True
        con = broker._conn()
        buy_amount = float(con.execute(
            "SELECT buy_amount FROM paper_positions WHERE user_id=? AND code=?",
            (broker._uid, "000001"),
        ).fetchone()["buy_amount"])
        con.close()

        _, net_income, _, _ = broker._calc_sell_income(11.0, 100)
        raw_pnl = round(net_income - buy_amount, 2)
        cash_before = broker._cash()
        result = broker.submit_sell("000001", 11.0)
        assert result["ok"] is True
        # 记入现金与成交记录的都必须是真实盈亏，不是被放大的
        assert result["pnl"] == raw_pnl
        assert result["cash_after"] == round(cash_before + buy_amount + raw_pnl, 2)

    def test_legacy_position_scales_display_pnl_without_scaling_cash(self, broker):
        con = broker._conn()
        for key, value in {
            "initial_capital": 500_000.0,
            "cash": 500_000.0,
            "peak_equity": 500_000.0,
            "commission_rate": 0,
            "min_commission": 0,
            "tax_rate": 0,
            "slippage_bps": 0,
        }.items():
            con.execute(
                "INSERT OR REPLACE INTO paper_config(user_id,key,value) VALUES(?,?,?)",
                (broker._uid, key, str(value)),
            )
        con.execute(
            "DELETE FROM paper_config WHERE user_id=? AND key=?",
            (broker._uid, "pnl_base_capital"),
        )
        con.commit()
        con.close()

        assert broker.submit_buy("000001", "Legacy", 10.0, 1000)["ok"] is True
        broker._ensure_user_config()

        positions = broker.get_positions(cur_prices={"000001": 12.0})
        cost_price = positions[0]["cost_price"]
        raw_open_pnl = round((12.0 - cost_price) * 1000, 2)
        assert positions[0]["shares"] == 1000
        assert positions[0]["cost_price"] == cost_price
        assert positions[0]["pnl_pct"] == round(raw_open_pnl / (cost_price * 1000) * 100, 2)
        assert positions[0]["pnl_amount"] == round(raw_open_pnl * 2, 2)
        assert broker._pnl_scale() == 1.0

        con = broker._conn()
        row = con.execute(
            "SELECT buy_amount,display_pnl_scale FROM paper_positions"
            " WHERE user_id=? AND code=?",
            (broker._uid, "000001"),
        ).fetchone()
        con.close()
        buy_amount = float(row["buy_amount"])
        assert float(row["display_pnl_scale"]) == 2.0
        _, net_income, _, _ = broker._calc_sell_income(12.0, 1000)
        raw_sell_pnl = round(net_income - buy_amount, 2)
        cash_before_sell = broker._cash()
        result = broker.submit_sell("000001", 12.0)
        assert result["ok"] is True
        assert result["pnl_pct"] == round(raw_sell_pnl / buy_amount * 100, 4)
        assert result["pnl"] == raw_sell_pnl
        assert result["cash_after"] == round(cash_before_sell + net_income, 2)

        assert broker.submit_buy("000002", "New", 10.0, 1000)["ok"] is True
        new_position = broker.get_positions(cur_prices={"000002": 12.0})[0]
        assert new_position["pnl_amount"] == round(
            (12.0 - new_position["cost_price"]) * 1000,
            2,
        )


# ══════════════════════════════════════════════════════
# 6. AST 安全检查测试
# ══════════════════════════════════════════════════════
class TestAstSafety:
    def _check(self, code):
        from strategy_engine.strategy_manager import ast_check
        return ast_check(code)

    def test_safe_code_passes(self):
        """合法策略代码通过"""
        code = """
import pandas as pd
import numpy as np

def scan(codes, get_kline, get_quotes):
    results = []
    for code in codes:
        df = get_kline(code, 60)
        if df.empty:
            continue
        close = df['close']
        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        if ma5.iloc[-1] > ma20.iloc[-1]:
            results.append({"code": code, "score": 10})
    return results
"""
        result = self._check(code)
        assert result["ok"] is True

    def test_os_import_blocked(self):
        """import os 被拦截"""
        result = self._check("import os\nos.system('rm -rf /')")
        assert result["ok"] is False
        assert "os" in result["msg"]

    def test_subprocess_blocked(self):
        result = self._check("import subprocess\nsubprocess.run(['ls'])")
        assert result["ok"] is False

    def test_eval_blocked(self):
        result = self._check("eval('print(1)')")
        assert result["ok"] is False
        assert "eval" in result["msg"]

    def test_exec_blocked(self):
        result = self._check("exec('import os')")
        assert result["ok"] is False

    # ── 逃逸回归：以下写法此前全部能通过检查并在 Web 进程内执行 ──────────
    # 攻击链：任意受邀用户 /api/strategies/save 存盘 → 点激活 → 调度器周期性
    # exec_module → 读 data/.secret_key 伪造 admin session、偷 users.db 与 .env。

    def test_importlib_escape_blocked(self):
        """import importlib 再 import_module('os') —— 原黑名单两层都不命中"""
        result = self._check(
            "import importlib\n"
            "importlib.import_module('os').system('whoami')"
        )
        assert result["ok"] is False

    def test_pathlib_secret_read_blocked(self):
        """pathlib 读密钥文件（open 被禁但 Path.read_text 等价）"""
        result = self._check(
            "import pathlib\n"
            "k = pathlib.Path('data/.secret_key').read_text()"
        )
        assert result["ok"] is False

    def test_dunder_class_traversal_blocked(self):
        """().__class__.__bases__[0].__subclasses__() 经典逃逸链"""
        result = self._check("x = ().__class__.__bases__[0].__subclasses__()")
        assert result["ok"] is False

    def test_builtins_access_blocked(self):
        result = self._check("__builtins__['eval']('1')")
        assert result["ok"] is False

    def test_ctypes_blocked(self):
        result = self._check("import ctypes")
        assert result["ok"] is False

    def test_pickle_blocked(self):
        result = self._check("import pickle\npickle.loads(b'')")
        assert result["ok"] is False

    def test_module_hop_to_os_blocked(self):
        """经允许的模块中转访问 os：模块名与调用名都不在原名单里"""
        result = self._check(
            "import data_manager as dm\n"
            "dm.os.system('whoami')"
        )
        assert result["ok"] is False

    def test_threading_blocked(self):
        result = self._check("import threading\nthreading.Thread(target=print).start()")
        assert result["ok"] is False

    def test_legit_strategy_imports_still_allowed(self):
        """加固不得误伤正常策略：常用数据/数学库与项目内扁平别名必须仍可用"""
        code = (
            "from __future__ import annotations\n"
            "import pandas as pd\n"
            "import numpy as np\n"
            "import math\n"
            "from datetime import datetime\n"
            "from collections import Counter\n"
            "from decimal import Decimal\n"
            "from indicators import sma, macd, cross_up\n"
            "from base import BaseStrategy, Signal\n"
            "\n"
            "def scan(codes, get_kline, get_quotes):\n"
            "    out = []\n"
            "    for c in codes:\n"
            "        df = get_kline(c, 60)\n"
            "        if df.empty:\n"
            "            continue\n"
            "        if sma(df['close'], 5).iloc[-1] > sma(df['close'], 20).iloc[-1]:\n"
            "            out.append({'code': c, 'score': math.log(2), 'name': str(datetime.now())})\n"
            "    return sorted(out, key=lambda r: r['score'], reverse=True)\n"
        )
        assert self._check(code)["ok"] is True

    def test_open_blocked(self):
        result = self._check("f = open('/etc/passwd','r')")
        assert result["ok"] is False
        assert "open" in result["msg"]

    def test_syntax_error_caught(self):
        result = self._check("def scan(: pass")
        assert result["ok"] is False
        assert "语法错误" in result["msg"]

    def test_from_import_blocked(self):
        result = self._check("from sys import argv")
        assert result["ok"] is False


# ══════════════════════════════════════════════════════
# 7. API Smoke Tests（轻量集成测试，不依赖 Flask 运行）
# ══════════════════════════════════════════════════════
@pytest.mark.skip(reason="集成冒烟已迁移到 tests/baseline_test.py（对运行中的 5001 服务测试）。"
                         "此处导入完整 main 过重、需登录态、且 reset 会触碰真实模拟盘数据，故跳过。")
class TestApiSmoke:
    """
    集成冒烟测试 —— 已迁移到 tests/baseline_test.py（对运行中服务做，更真实安全）。
    pytest 跳过此类，避免导入完整 main / 触碰真实数据。
    """
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        """创建 Flask 测试客户端"""
        # 重定向数据库到临时路径
        monkeypatch.setenv("PAPER_DB_PATH", str(tmp_path / "test.db"))
        # mock ths_quote 防止真实网络连接
        import types
        mock_tq = types.ModuleType("ths_quote")
        mock_tq.get_client = lambda: None
        mock_tq.get_stock_name = lambda c: c
        mock_tq.get_scan_progress = lambda: {}
        sys.modules["ths_quote"] = mock_tq

        # mock ths_operator
        mock_op = types.ModuleType("ths_operator")
        class MockOperator:
            def buy(self, *a, **kw): return True
            def sell(self, *a, **kw): return True
        mock_op.THSOperator = MockOperator
        sys.modules["ths_operator"] = mock_op

        if "main" in sys.modules:
            del sys.modules["main"]

        try:
            import main as app_module
            app_module.app.config["TESTING"] = True
            return app_module.app.test_client()
        except Exception:
            pytest.skip("main.py 导入失败（依赖不满足），跳过 smoke tests")

    def test_status_endpoint(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200

    def test_paper_account_endpoint(self, client):
        resp = client.get("/api/paper/account")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "cash" in data

    def test_paper_positions_empty(self, client):
        resp = client.get("/api/paper/positions")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_paper_stats_endpoint(self, client):
        resp = client.get("/api/paper/stats")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "win_rate" in data

    def test_paper_reset_endpoint(self, client):
        resp = client.post("/api/paper/reset",
                           json={"initial_capital": 300000},
                           content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("ok") is True
