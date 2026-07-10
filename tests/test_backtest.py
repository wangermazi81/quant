# -*- coding: utf-8 -*-
"""
tests/test_backtest.py
回测引擎端到端冒烟/回归测试。

重点保护本轮的内存优化（run_backtest 改为 all_data.pop 边加边释放）——
证明改动后引擎仍能完整跑通并产出正确结构的结果。用 mock 的合成行情，
不依赖磁盘 CSV / TDX 网络。

运行：
  cd ths_auto_trader
  python -m pytest tests/test_backtest.py -v
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy_engine import backtest


def _synth(n=40, start="2026-01-02"):
    """生成一段每日上涨 ~1% 的合成 K 线（会触发止盈→产生交易）。"""
    dates = pd.bdate_range(start, periods=n)
    rows, px = [], 10.0
    for d in dates:
        op = px
        cl = round(px * 1.012, 2)
        rows.append({"date": d, "open": op, "close": cl,
                     "high": round(cl * 1.005, 2), "low": round(op * 0.995, 2),
                     "volume": 100000})
        px = cl
    return pd.DataFrame(rows)


# 简单策略：每天对头两只候选发买入信号
_STRAT = """
def scan(codes, get_kline, get_quotes):
    return [{"code": c} for c in codes[:2]]
"""


@pytest.fixture
def mock_data(monkeypatch):
    data = {"000001": _synth(), "600000": _synth(), "000002": _synth()}
    monkeypatch.setattr(backtest, "_load_data", lambda codes, s, e, logs: {
        c: data[c].copy() for c in codes if c in data})
    return data


class TestRunBacktest:
    def test_runs_end_to_end(self, mock_data):
        r = backtest.run_backtest(_STRAT, ["000001", "600000", "000002"],
                                  "2026-01-02", "2026-02-27", initial_capital=100000)
        assert r["ok"], r.get("error")
        assert "metrics" in r
        for k in ("total_return", "annual_return", "max_drawdown", "sharpe", "win_rate"):
            assert k in r["metrics"]
        assert r["universe_size"] == 3
        assert len(r["equity_curve"]) > 0

    def test_generates_trades(self, mock_data):
        # 持续上涨 + 10% 止盈 → 应当产生已平仓交易
        r = backtest.run_backtest(_STRAT, ["000001", "600000"],
                                  "2026-01-02", "2026-02-27", initial_capital=100000,
                                  take_profit=0.10)
        assert r["ok"]
        assert isinstance(r["trades"], list)
        assert r["metrics"]["total_return"] != 0   # 有交易→收益非零

    def test_missing_codes_skipped(self, mock_data):
        # 不存在的票应被跳过，不崩溃
        r = backtest.run_backtest(_STRAT, ["000001", "NOPE99"],
                                  "2026-01-02", "2026-02-27")
        assert r["ok"]
        assert r["universe_size"] == 1             # 只有 000001 有数据

    def test_bad_strategy_returns_error(self, mock_data):
        r = backtest.run_backtest("def not_scan(): pass", ["000001"],
                                  "2026-01-02", "2026-02-27")
        assert r["ok"] is False
        assert "scan" in r["error"]

    def test_empty_data_returns_error(self, monkeypatch):
        monkeypatch.setattr(backtest, "_load_data", lambda codes, s, e, logs: {})
        r = backtest.run_backtest(_STRAT, ["000001"], "2026-01-02", "2026-02-27")
        assert r["ok"] is False
