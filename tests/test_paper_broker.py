# -*- coding: utf-8 -*-
"""
tests/test_paper_broker.py
模拟盘引擎单元测试

运行方式：
  cd ths_auto_trader
  python -m pytest tests/ -v
"""

import sys
import os
import tempfile
import pytest
from pathlib import Path

# 把项目根目录加入 sys.path，使 import paper_broker 可以找到
sys.path.insert(0, str(Path(__file__).parent.parent))

# 使用临时数据库，不污染正式数据
@pytest.fixture
def broker(tmp_path):
    """每个测试用独立临时数据库的 PaperBroker 实例"""
    from paper_broker import PaperBroker
    b = PaperBroker(db_path=tmp_path / "test_paper.db")
    return b


# ══════════════════════════════════════════════════════
# 1. 买入测试
# ══════════════════════════════════════════════════════
class TestBuy:
    def test_buy_success(self, broker):
        """正常买入：资金充足，100整股"""
        result = broker.submit_buy("000001", "平安银行", 12.50, 400, reason="测试买入")
        assert result["ok"] is True
        assert result["shares"] == 400
        assert result["exec_price"] > 12.50   # 含滑点

    def test_buy_deducts_cash(self, broker):
        """买入后现金减少"""
        before = broker._cash()
        broker.submit_buy("000001", "平安银行", 10.0, 1000)
        after = broker._cash()
        assert after < before

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


# ══════════════════════════════════════════════════════
# 6. AST 安全检查测试
# ══════════════════════════════════════════════════════
class TestAstSafety:
    def _check(self, code):
        from strategy_manager import ast_check
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
class TestApiSmoke:
    """
    测试 main.py 路由不崩溃（通过 Flask test client）
    需要 mock TDX 和数据库，确保快速且无副作用
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

        import importlib
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
