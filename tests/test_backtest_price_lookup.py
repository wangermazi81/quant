# -*- coding: utf-8 -*-
"""P-5：价格查询从布尔掩码换成宽表+二分后，语义必须逐点等价。

这不是「跑通就行」的冒烟测试——回测结果直接决定用户要不要把策略投实盘，
换实现必须证明**取到的价格和原来一模一样**，尤其是这三种边界：
  · 停牌/缺失交易日 → 原来是「取不到就跳过」，宽表里是 NaN，绝不能当成 0 价；
  · 各股上市日不同 → 早期日期只有部分股票有数据；
  · day 落在两根 K 线之间 → _kline/_quotes 必须取到 <= day 的最后一根。
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy_engine.backtest import Cerebro


def _mk(dates, closes=None):
    closes = closes or [10.0 + i for i in range(len(dates))]
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "open": [c - 0.5 for c in closes],
        "close": closes,
        "high": [c + 0.2 for c in closes],
        "low": [c - 0.8 for c in closes],
        "volume": [1000 * (i + 1) for i in range(len(dates))],
    })


# ── 原实现（布尔掩码），作为等价性基准 ─────────────────────────
def _ref_open_prices(feeds, day):
    out = {}
    for code, df in feeds.items():
        row = df[df["date"] == day]
        if not row.empty:
            out[code] = float(row.iloc[0]["open"])
    return out


def _ref_close_prices(feeds, day):
    out = {}
    for code, df in feeds.items():
        row = df[df["date"] == day]
        if not row.empty:
            out[code] = float(row.iloc[0]["close"])
    return out


def _ref_kline(feeds, code, n, day):
    df = feeds.get(code)
    if df is None:
        return pd.DataFrame()
    return df[df["date"] <= day].tail(n).reset_index(drop=True)


def _build():
    """三只票故意错开：B 缺 01-03（停牌），C 晚上市。"""
    c = Cerebro()
    c.add_data("A", _mk(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06"]))
    c.add_data("B", _mk(["2026-01-01", "2026-01-02", "2026-01-06"]))          # 缺 01-03
    c.add_data("C", _mk(["2026-01-03", "2026-01-06"]))                        # 晚上市
    return c


_DAYS = [pd.Timestamp(d) for d in
         ["2025-12-31", "2026-01-01", "2026-01-02", "2026-01-03",
          "2026-01-04", "2026-01-05", "2026-01-06", "2026-01-07"]]


def test_open_close_prices_match_boolean_mask_implementation():
    c = _build()
    for day in _DAYS:
        assert c._open_prices(day) == _ref_open_prices(c.data_feeds, day), f"open 不一致 @{day}"
        assert c._close_prices(day) == _ref_close_prices(c.data_feeds, day), f"close 不一致 @{day}"


def test_missing_day_never_yields_zero_price():
    """★停牌/无数据的票必须被跳过，绝不能出现 0 价（会直接毒害撮合）。"""
    c = _build()
    prices = c._close_prices(pd.Timestamp("2026-01-03"))
    assert "B" not in prices, "B 当天停牌，不该出现在价格表里"
    assert set(prices) == {"A", "C"}
    assert all(v > 0 for v in prices.values())


def test_non_trading_day_returns_empty():
    c = _build()
    assert c._open_prices(pd.Timestamp("2026-01-04")) == {}


def test_kline_matches_boolean_mask_implementation():
    c = _build()
    for code in ("A", "B", "C"):
        for n in (1, 2, 5, 100):
            for day in _DAYS:
                got = c._kline(code, n, day)
                ref = _ref_kline(c.data_feeds, code, n, day)
                assert list(got.columns) == list(ref.columns) or (got.empty and ref.empty)
                assert len(got) == len(ref), f"{code} n={n} @{day} 行数不一致"
                if not ref.empty:
                    pd.testing.assert_frame_equal(got, ref, check_dtype=False)


def test_quotes_uses_last_bar_at_or_before_day():
    c = _build()
    # 01-04 非交易日：应取到各股 <= 01-04 的最后一根
    q = c._quotes(["A", "B", "C"], pd.Timestamp("2026-01-04"))
    by_code = {r["代码"]: r for r in q.to_dict("records")}
    assert by_code["A"]["最新价"] == float(c.data_feeds["A"].iloc[2]["close"])   # 01-03
    assert by_code["B"]["最新价"] == float(c.data_feeds["B"].iloc[1]["close"])   # 01-02
    assert by_code["C"]["最新价"] == float(c.data_feeds["C"].iloc[0]["close"])   # 01-03


def test_quotes_change_pct_uses_previous_bar():
    c = _build()
    q = c._quotes(["A"], pd.Timestamp("2026-01-03"))
    row = q.to_dict("records")[0]
    prev = float(c.data_feeds["A"].iloc[1]["close"])
    close = float(c.data_feeds["A"].iloc[2]["close"])
    assert row["昨收"] == prev
    assert row["涨跌幅"] == round((close - prev) / prev * 100, 2)


def test_first_bar_has_no_previous_close():
    """只有一根 K 线时，昨收退化为当日收盘、涨跌幅 0（与原实现一致）。"""
    c = _build()
    q = c._quotes(["C"], pd.Timestamp("2026-01-03"))
    row = q.to_dict("records")[0]
    assert row["昨收"] == row["最新价"]
    assert row["涨跌幅"] == 0.0


def test_panels_rebuild_after_add_data():
    """加新数据后宽表必须失效重建，否则新票永远查不到价。"""
    c = _build()
    c._close_prices(pd.Timestamp("2026-01-02"))       # 先触发一次构建
    c.add_data("D", _mk(["2026-01-02", "2026-01-06"]))
    prices = c._close_prices(pd.Timestamp("2026-01-02"))
    assert "D" in prices, "新加入的标的没有反映到价格宽表"
