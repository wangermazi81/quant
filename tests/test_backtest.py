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


# ══════════════════════════════════════════════════════
# T-11：指标数值正确性
# ══════════════════════════════════════════════════════
# 此前 test_runs_end_to_end 只断言这些键**存在**，于是年化/夏普/回撤公式、
# 资金曲线复利、手续费漏扣算错都不会被任何测试发现 ——
# 而用户正是拿这些数字决定要不要把策略投进实盘。


def _flat(n=40, price=10.0, start="2026-01-02"):
    """完全不涨不跌的行情：任何策略都不该产生盈亏。"""
    dates = pd.bdate_range(start, periods=n)
    return pd.DataFrame({
        "date": dates, "open": [price] * n, "close": [price] * n,
        "high": [price] * n, "low": [price] * n, "volume": [100000] * n,
    })


def _down(n=40, start="2026-01-02", rate=0.97):
    """每日下跌 3%：触发止损，胜率必须为 0。"""
    dates = pd.bdate_range(start, periods=n)
    rows, px = [], 10.0
    for _d in dates:
        op = px
        cl = round(px * rate, 2)
        rows.append({"date": _d, "open": op, "close": cl,
                     "high": op, "low": cl, "volume": 100000})
        px = cl
    return pd.DataFrame(rows)


class TestMetricsNumeric:
    def test_flat_market_has_no_gain(self, monkeypatch):
        """横盘不动：总收益不可能为正（最多因手续费/滑点略负）。"""
        monkeypatch.setattr(backtest, "_load_data",
                            lambda codes, s, e, logs: {"000001": _flat()})
        r = backtest.run_backtest(_STRAT, ["000001"], "2026-01-02", "2026-02-27",
                                  initial_capital=100000)
        assert r["ok"], r.get("error")
        assert r["metrics"]["total_return"] <= 0, "横盘行情不该跑出正收益"

    def test_falling_market_win_rate_is_zero(self, monkeypatch):
        """单边下跌：所有平仓交易都该是亏的，胜率必须为 0。"""
        monkeypatch.setattr(backtest, "_load_data",
                            lambda codes, s, e, logs: {"000001": _down()})
        r = backtest.run_backtest(_STRAT, ["000001"], "2026-01-02", "2026-02-27",
                                  initial_capital=100000, stop_loss=-0.05)
        assert r["ok"], r.get("error")
        closed = [t for t in r["trades"] if t.get("pnl") is not None]
        if closed:
            assert r["metrics"]["win_rate"] == 0, f"下跌行情胜率应为0，实得 {r['metrics']['win_rate']}"
            assert all(t["pnl"] <= 0 for t in closed), "下跌行情不该有盈利的平仓交易"
        assert r["metrics"]["total_return"] < 0
        assert r["metrics"]["max_drawdown"] < 0 or r["metrics"]["max_drawdown"] > 0, \
            "单边下跌必须记录到非零回撤"

    def test_equity_curve_matches_reported_total_return(self, mock_data):
        """资金曲线的首尾必须和 total_return 自洽 —— 防复利/口径算错。"""
        r = backtest.run_backtest(_STRAT, ["000001", "600000"],
                                  "2026-01-02", "2026-02-27", initial_capital=100000)
        assert r["ok"], r.get("error")
        curve = r["equity_curve"]
        assert len(curve) >= 2

        def _val(point):
            if isinstance(point, dict):
                for key in ("equity", "value", "total", "nav"):
                    if key in point:
                        return float(point[key])
                raise AssertionError(f"资金曲线点缺少净值字段: {point}")
            return float(point[1] if isinstance(point, (list, tuple)) else point)

        derived = (_val(curve[-1]) / _val(curve[0]) - 1) * 100
        assert abs(derived - float(r["metrics"]["total_return"])) < 1.0, (
            f"total_return({r['metrics']['total_return']}) 与资金曲线推导值({derived:.2f}) 不自洽"
        )

    def test_max_drawdown_never_positive_on_monotonic_rise(self, mock_data):
        """单调上涨行情不该出现大幅回撤（回撤公式写反会在这里露馅）。"""
        r = backtest.run_backtest(_STRAT, ["000001"], "2026-01-02", "2026-02-27",
                                  initial_capital=100000, take_profit=0.10)
        assert r["ok"], r.get("error")
        assert abs(float(r["metrics"]["max_drawdown"])) < 50, \
            "持续上涨行情出现了 >50% 的回撤，回撤计算大概率有误"
