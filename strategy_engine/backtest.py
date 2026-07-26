# -*- coding: utf-8 -*-
"""
回测引擎 v4 — Backtrader 架构启发
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
核心设计（参考 backtrader 源码架构）：

  Cerebro          — 总编排器，管理全生命周期
  BackBroker       — 模拟券商：订单状态机、佣金、持仓
  Order            — 9 状态机 (submitted→accepted→completed/rejected)
  Trade            — 一次完整买卖记录
  Analyzer(s)      — 可插拔指标收集器
    ├ SharpeAnalyzer    (ddof=1, rf=2.5%, 年化)
    ├ DrawDownAnalyzer  (逐日最大回撤序列)
    ├ TradeAnalyzer     (胜率/盈亏比/连胜/连败)
    └ MonthlyAnalyzer   (月度收益热力图)

关键改进（vs 简单 for 循环）：
  ✓ 信号日收盘生成 → 次日开盘成交（无前视偏差）
  ✓ 双边佣金（A股标准：买 0.03%、卖 0.13%）
  ✓ 按仓位百分比动态计算手数
  ✓ 止损/止盈在收盘触发，次日开盘执行
  ✓ 组合市值实时估值（持仓收盘价）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import math, os, tempfile, traceback, logging, importlib.util
import threading as _bt_threading
import uuid as _bt_uuid
import time as _bt_time
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent.parent / "data" / "daily"

# 全市场回测 TDX 补充标的上限（资居内存与此成正比）。小内存机器(如 1.6GB 云主机)
# 可设环境变量 BACKTEST_MAX_CODES=400 降低全市场回测内存峰值，避免 worker 被 OOM kill。
BACKTEST_MAX_CODES = int(os.environ.get("BACKTEST_MAX_CODES", "800") or "800")
logger   = logging.getLogger(__name__)

# ── 异步回测任务注册表 ──────────────────────────────────
# 只增不删会持续吃内存：每个 result 里带完整 equity_curve/trades/logs（MB 级），
# 而云端 worker 常驻数周、systemd 又限了 MemoryMax=512M → 迟早 cgroup OOM-kill 重启
#（表现同样是"重启才好"）。这里按「容量 + 存活时长」双上限淘汰已结束的任务。
_bt_tasks: dict = {}   # task_id -> {status, progress, result, created}
_BT_TASK_MAX      = int(os.environ.get("BACKTEST_TASK_MAX", "20") or "20")
_BT_TASK_TTL_SEC  = float(os.environ.get("BACKTEST_TASK_TTL", "3600") or "3600")


def _prune_bt_tasks() -> None:
    """淘汰过期/超量的**已结束**任务；运行中的任务永不淘汰。"""
    try:
        now = _bt_time.time()
        finished = [(tid, t) for tid, t in _bt_tasks.items()
                    if t.get("status") != "running"]
        for tid, t in finished:
            if now - float(t.get("created") or 0) > _BT_TASK_TTL_SEC:
                _bt_tasks.pop(tid, None)
        finished = [(tid, t) for tid, t in _bt_tasks.items()
                    if t.get("status") != "running"]
        if len(finished) > _BT_TASK_MAX:
            finished.sort(key=lambda kv: float(kv[1].get("created") or 0))
            for tid, _ in finished[:len(finished) - _BT_TASK_MAX]:
                _bt_tasks.pop(tid, None)
    except Exception as e:
        logger.warning(f"[backtest] 任务表清理失败（不影响本次回测）: {e}")


# ══════════════════════════════════════════════════
# Order — 完整状态机
# ══════════════════════════════════════════════════
class OrderStatus:
    SUBMITTED = "submitted"
    ACCEPTED  = "accepted"
    COMPLETED = "completed"
    CANCELED  = "canceled"
    REJECTED  = "rejected"

class Order:
    _id_seq = 0

    def __init__(self, code, action, budget=0.0, reason=""):
        Order._id_seq += 1
        self.id          = Order._id_seq
        self.code        = code
        self.action      = action        # "buy" | "sell"
        self.budget      = budget        # buy: 可用资金; sell: 忽略
        self.reason      = reason        # "signal" | "stop_loss" | "take_profit"
        self.status      = OrderStatus.SUBMITTED
        self.submit_date = None
        self.exec_date   = None
        self.exec_price  = None
        self.qty         = 0
        self.commission  = 0.0

    def to_dict(self):
        return {
            "id": self.id, "code": self.code, "action": self.action,
            "status": self.status, "exec_date": self.exec_date,
            "exec_price": self.exec_price, "qty": self.qty,
            "commission": round(self.commission, 2), "reason": self.reason,
        }


# ══════════════════════════════════════════════════
# Trade — 一次完整的买卖记录
# ══════════════════════════════════════════════════
class Trade:
    def __init__(self, code, buy_date, buy_price, sell_date, sell_price, qty, commission):
        self.code       = code
        self.buy_date   = buy_date
        self.buy_price  = buy_price
        self.sell_date  = sell_date
        self.sell_price = sell_price
        self.qty        = qty
        self.commission = commission
        self.pnl        = round((sell_price - buy_price) * qty - commission, 2)
        self.pnl_pct    = round((sell_price - buy_price) / buy_price * 100, 2)
        self.hold_days  = (pd.Timestamp(sell_date) - pd.Timestamp(buy_date)).days

    def to_dict(self):
        return {
            "code": self.code,
            "buy_date": self.buy_date, "buy_price": round(self.buy_price, 2),
            "sell_date": self.sell_date, "sell_price": round(self.sell_price, 2),
            "qty": self.qty, "commission": round(self.commission, 2),
            "pnl": self.pnl, "pnl_pct": self.pnl_pct, "hold_days": self.hold_days,
            # 兼容旧格式，方便前端 trades 表渲染
            "date": self.sell_date, "action": "sell",
            "price": round(self.sell_price, 2),
        }


# ══════════════════════════════════════════════════
# Position
# ══════════════════════════════════════════════════
class Position:
    def __init__(self, code, qty, cost, entry_date):
        self.code       = code
        self.qty        = qty
        self.cost       = cost        # 买入均价
        self.entry_date = entry_date


# ══════════════════════════════════════════════════
# BackBroker — 模拟券商
# ══════════════════════════════════════════════════
class BackBroker:
    """
    参考 backtrader brokers/brokerback.py 的佣金模型和订单执行逻辑。
    A 股双边佣金：
      买入：0.03% 印花税豁免 + 0.02% 过户费 ≈ 0.03%（取整）
      卖出：0.1% 印花税 + 0.02% + 0.03% ≈ 0.13%
    """
    BUY_COMMISSION  = 0.0003   # 0.03%
    SELL_COMMISSION = 0.0013   # 0.13%
    MIN_COMMISSION  = 5.0      # 最低 5 元

    def __init__(self, cash: float, slippage=None):
        """
        cash:     初始资金
        slippage: 可选的 broker.slippage.SlippageModel 实例；None=不加滑点（默认）
        """
        self.cash       = cash
        self.positions  = {}          # code -> Position
        self._pending   = []          # 待执行订单（下一个交易日开盘执行）
        self.all_orders = []
        self.trades     = []
        self._buy_commissions  = 0.0
        self._sell_commissions = 0.0
        # 滑点模型（None = NoSlippage 等价；保留 None 完全还原旧行为）
        self.slippage = slippage

    # ── 佣金计算 ──
    def _comm(self, value: float, action: str) -> float:
        rate = self.BUY_COMMISSION if action == "buy" else self.SELL_COMMISSION
        return max(value * rate, self.MIN_COMMISSION)

    # ── 应用滑点（默认透传开盘价，零行为变更）──
    def _exec_price(self, base_price: float, action: str, qty: int = 0) -> float:
        if self.slippage is None:
            return base_price
        try:
            return float(self.slippage.adjust(base_price, action, qty))
        except Exception:
            return base_price

    # ── 提交订单（当日信号，加入待执行队列）──
    def submit_buy(self, code: str, date, budget: float, reason="signal") -> Order:
        o = Order(code, "buy", budget=budget, reason=reason)
        o.status = OrderStatus.ACCEPTED
        o.submit_date = str(date)
        self._pending.append(o)
        return o

    def submit_sell(self, code: str, date, reason="signal") -> Order:
        if code not in self.positions:
            return None
        o = Order(code, "sell", reason=reason)
        o.status = OrderStatus.ACCEPTED
        o.submit_date = str(date)
        o.qty = self.positions[code].qty
        self._pending.append(o)
        return o

    # ── 次日开盘执行所有待定订单 ──
    def process_pending(self, day, open_prices: dict, logs: list):
        filled, rejected = [], []
        remaining = []

        # 先处理卖单（释放资金）
        sells = [o for o in self._pending if o.action == "sell"]
        buys  = [o for o in self._pending if o.action == "buy"]

        for o in sells:
            code = o.code
            if code not in open_prices or code not in self.positions:
                o.status = OrderStatus.CANCELED
                rejected.append(o)
                continue
            pos = self.positions.pop(code)
            px  = self._exec_price(open_prices[code], "sell", pos.qty)
            comm = self._comm(px * pos.qty, "sell")
            self._sell_commissions += comm
            self.cash += px * pos.qty - comm

            o.exec_price = px
            o.exec_date  = str(day.date())
            o.qty        = pos.qty
            o.commission = comm
            o.status     = OrderStatus.COMPLETED

            trade = Trade(
                code=code,
                buy_date=pos.entry_date,   buy_price=pos.cost,
                sell_date=str(day.date()), sell_price=px,
                qty=pos.qty,
                commission=self._comm(pos.cost * pos.qty, "buy") + comm,
            )
            self.trades.append(trade)
            self.all_orders.append(o)
            filled.append(o)

        # 再处理买单
        for o in buys:
            code = o.code
            if code not in open_prices or code in self.positions:
                o.status = OrderStatus.CANCELED
                rejected.append(o)
                continue
            # 先按未加滑点的开盘价估算手数，再用加滑点价结算
            _base_px = open_prices[code]
            qty = int(o.budget / _base_px / 100) * 100   # 整手
            px  = self._exec_price(_base_px, "buy", qty)
            if qty < 100:
                o.status = OrderStatus.REJECTED
                logs.append({"level": "WARNING",
                             "msg": f"{day.date()} {code}: 预算¥{o.budget:.0f} 不足买 100 股（¥{px*100:.0f}），跳过"})
                rejected.append(o)
                continue
            comm  = self._comm(px * qty, "buy")
            total = px * qty + comm
            if total > self.cash:
                o.status = OrderStatus.REJECTED
                logs.append({"level": "WARNING",
                             "msg": f"{day.date()} {code}: 现金¥{self.cash:.0f} 不足，需¥{total:.0f}，跳过"})
                rejected.append(o)
                continue
            self.cash -= total
            self._buy_commissions += comm
            self.positions[code] = Position(code, qty, px, str(day.date()))

            o.exec_price = px
            o.exec_date  = str(day.date())
            o.qty        = qty
            o.commission = comm
            o.status     = OrderStatus.COMPLETED
            self.all_orders.append(o)
            filled.append(o)

        self._pending = []
        return filled, rejected

    # ── 当前持仓市值 ──
    def portfolio_value(self, close_prices: dict) -> float:
        val = self.cash
        for code, pos in self.positions.items():
            val += close_prices.get(code, pos.cost) * pos.qty
        return val

    @property
    def total_commission(self):
        return self._buy_commissions + self._sell_commissions


# ══════════════════════════════════════════════════
# Analyzers — 可插拔指标收集器
# ══════════════════════════════════════════════════
class SharpeAnalyzer:
    """参考 backtrader analyzers/sharpe.py，年化 Sharpe（ddof=1）"""
    def __init__(self, rf_annual=0.025):
        self.rf_daily = rf_annual / 252
        self._equity  = []

    def notify_equity(self, equity: float):
        self._equity.append(equity)

    def get_analysis(self) -> dict:
        eq = self._equity
        if len(eq) < 2:
            return {"sharpe": 0.0}
        rets   = [(eq[i] - eq[i-1]) / eq[i-1] for i in range(1, len(eq)) if eq[i-1] > 0]
        excess = [r - self.rf_daily for r in rets]
        if len(excess) < 2:
            return {"sharpe": 0.0}
        std = float(np.std(excess, ddof=1))
        if std < 1e-8:
            return {"sharpe": 0.0}
        raw = float(np.mean(excess)) / std * math.sqrt(252)
        return {"sharpe": round(max(-99.0, min(99.0, raw)), 2)}


class DrawDownAnalyzer:
    """参考 backtrader analyzers/drawdown.py，逐日回撤序列 + 最大回撤"""
    def __init__(self):
        self._series = []   # list of {"date":str, "drawdown":float}
        self._peak   = None

    def notify_equity(self, date: str, equity: float):
        if self._peak is None or equity > self._peak:
            self._peak = equity
        dd = (self._peak - equity) / self._peak * 100 if self._peak > 0 else 0.0
        self._series.append({"date": date, "drawdown": round(dd, 2)})

    def get_analysis(self) -> dict:
        if not self._series:
            return {"max_drawdown": 0.0, "series": []}
        max_dd = max(s["drawdown"] for s in self._series)
        return {"max_drawdown": round(max_dd, 2), "series": self._series}


class TradeAnalyzer:
    """参考 backtrader analyzers/tradeanalyzer.py"""
    def __init__(self):
        self._trades = []

    def notify_trade(self, trade: Trade):
        self._trades.append(trade)

    def get_analysis(self) -> dict:
        trades = self._trades
        if not trades:
            return {
                "total_trades": 0, "win_rate": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0, "profit_factor": 0.0,
                "max_win_streak": 0, "max_loss_streak": 0,
                "avg_hold_days": 0.0, "total_pnl": 0.0,
                "pnl_dist": [], "hold_days_dist": [],
            }
        wins    = [t for t in trades if t.pnl_pct > 0]
        losses  = [t for t in trades if t.pnl_pct < 0]
        avg_win  = float(np.mean([t.pnl_pct for t in wins]))  if wins   else 0.0
        avg_loss = float(np.mean([t.pnl_pct for t in losses]))if losses else 0.0
        pf_denom = abs(avg_loss * len(losses))
        pf       = round(abs(avg_win * len(wins)) / pf_denom, 2) if pf_denom > 0 else 0.0

        # 连胜/连败
        max_win_streak = max_loss_streak = cur_win = cur_loss = 0
        for t in trades:
            if t.pnl_pct > 0:
                cur_win  += 1; cur_loss = 0
                max_win_streak = max(max_win_streak, cur_win)
            elif t.pnl_pct < 0:
                cur_loss += 1; cur_win  = 0
                max_loss_streak = max(max_loss_streak, cur_loss)
            else:
                cur_win = cur_loss = 0

        pnl_dist = [round(t.pnl_pct, 2) for t in trades]

        return {
            "total_trades":    len(trades),
            "win_rate":        round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
            "avg_win":         round(avg_win, 2),
            "avg_loss":        round(avg_loss, 2),
            "profit_factor":   pf,
            "max_win_streak":  max_win_streak,
            "max_loss_streak": max_loss_streak,
            "avg_hold_days":   round(float(np.mean([t.hold_days for t in trades])), 1),
            "total_pnl":       round(sum(t.pnl for t in trades), 2),
            "pnl_dist":        pnl_dist,
            "hold_days_dist":  [t.hold_days for t in trades],
        }


class MonthlyAnalyzer:
    """月度收益热力图数据"""
    def __init__(self):
        self._equity = []

    def notify_equity(self, date: str, equity: float):
        self._equity.append({"date": date, "equity": equity})

    def get_analysis(self) -> dict:
        if not self._equity:
            return {}
        df = pd.DataFrame(self._equity)
        df["date"]  = pd.to_datetime(df["date"])
        df["year"]  = df["date"].dt.year
        df["month"] = df["date"].dt.month
        monthly = {}
        for (yr, mo), grp in df.groupby(["year", "month"]):
            start = grp["equity"].iloc[0]
            end   = grp["equity"].iloc[-1]
            if start > 0:
                monthly.setdefault(str(yr), {})[str(mo)] = round((end - start) / start * 100, 2)
        return monthly


# ══════════════════════════════════════════════════
# Cerebro — 总编排器
# ══════════════════════════════════════════════════
class Cerebro:
    """
    Backtrader Cerebro 的轻量实现。
    负责：数据加载 → 逐日模拟 → 策略执行 → 指标收集 → 结果汇总
    """

    def __init__(self, initial_cash=100_000, max_positions=5,
                 stop_loss=-0.05, take_profit=0.10, slippage=None):
        """
        slippage: broker.slippage.SlippageModel 实例；None=不加滑点（默认，行为同重构前）
                  典型用法：Cerebro(slippage=PercentSlippage(0.002))
        """
        self.broker        = BackBroker(initial_cash, slippage=slippage)
        self.initial_cash  = initial_cash
        self.max_positions = max_positions
        self.stop_loss     = stop_loss
        self.take_profit   = take_profit
        self.slippage      = slippage

        # Analyzers（参考 backtrader addanalyzer 接口）
        self._sharpe   = SharpeAnalyzer()
        self._drawdown = DrawDownAnalyzer()
        self._trade    = TradeAnalyzer()
        self._monthly  = MonthlyAnalyzer()

        self.data_feeds  = {}   # code -> pd.DataFrame
        self._open_panel  = None    # date×code 宽表（P-5，懒构建）
        self._close_panel = None
        self._date_index  = {}      # code -> 已排序的 date 数组（二分用）
        self.scan_fn     = None
        self.logs        = []
        self.equity_curve= []
        self._signal_cnt = 0

    def add_data(self, code: str, df: pd.DataFrame):
        self.data_feeds[code] = df.sort_values("date").reset_index(drop=True)
        # 数据变了，之前建好的宽表/日期索引作废
        self._open_panel = None
        self._close_panel = None
        self._date_index = {}

    # ── P-5：价格查询从「逐日对每只股票做布尔掩码」改为宽表 + 二分 ──────
    # 原实现里 _open_prices/_close_prices 每天都对**每只**股票做一次
    # `df[df["date"] == day]`：250 交易日 × 4000 只 = 100 万次整列比较，
    # 每次还分配一个布尔数组。单次全市场回测就这样白烧掉数分钟 CPU（GIL 内，
    # 且与 Web 同进程）。改成一次性 pivot 出 date×code 宽表后按行标签 O(1) 取整日截面。
    #
    # ★语义必须保持：原来「当天没有这只票的数据就跳过」，宽表里对应的是 NaN，
    #   必须继续跳过 —— 绝不能把 NaN 当成 0 价喂进撮合。
    def _ensure_price_panels(self):
        if self._open_panel is not None:
            return
        opens, closes = {}, {}
        for code, df in self.data_feeds.items():
            if df is None or df.empty:
                continue
            # 同一天重复行取第一条，与原 `row.iloc[0]` 的取法一致
            s = df.drop_duplicates(subset="date", keep="first").set_index("date")
            opens[code] = s["open"]
            closes[code] = s["close"]
        self._open_panel = pd.DataFrame(opens) if opens else pd.DataFrame()
        self._close_panel = pd.DataFrame(closes) if closes else pd.DataFrame()

    @staticmethod
    def _row_to_prices(panel: pd.DataFrame, day) -> dict:
        if panel is None or panel.empty:
            return {}
        try:
            row = panel.loc[day]
        except KeyError:
            return {}          # 当天全市场都没有数据（非交易日等）
        if isinstance(row, pd.DataFrame):   # 万一 index 仍有重复，取第一行
            row = row.iloc[0]
        return {code: float(v) for code, v in row.items() if pd.notna(v)}

    def _rows_upto(self, code: str, day) -> int:
        """该股 date <= day 的行数（二分，替代整列布尔掩码）。"""
        df = self.data_feeds.get(code)
        if df is None or df.empty:
            return 0
        arr = self._date_index.get(code)
        if arr is None:
            arr = df["date"].to_numpy()
            self._date_index[code] = arr
        return int(np.searchsorted(arr, np.datetime64(day), side="right"))

    def set_scan(self, fn):
        """设置 scan() 策略函数"""
        self.scan_fn = fn

    # ── 数据辅助 ──
    def _open_prices(self, day) -> dict:
        self._ensure_price_panels()
        return self._row_to_prices(self._open_panel, day)

    def _close_prices(self, day) -> dict:
        self._ensure_price_panels()
        return self._row_to_prices(self._close_panel, day)

    def _kline(self, code: str, n: int, day) -> pd.DataFrame:
        df = self.data_feeds.get(code)
        if df is None:
            return pd.DataFrame()
        pos = self._rows_upto(code, day)
        if pos <= 0:
            return df.iloc[0:0].reset_index(drop=True)
        return df.iloc[max(0, pos - n):pos].reset_index(drop=True)

    def _quotes(self, codes: list, day) -> pd.DataFrame:
        rows = []
        for c in codes:
            df = self.data_feeds.get(c)
            if df is None:
                continue
            # 同 _kline：二分定位替代整列布尔掩码（P-5）
            pos = self._rows_upto(c, day)
            if pos > 0:
                last  = df.iloc[pos - 1]
                close = float(last["close"])
                # ★ 修复：K线 DataFrame 无 change_pct 列，旧代码 last.get("change_pct",0)
                #   恒为 0 → 任何按「涨跌幅」过滤的策略(如 MACD动量 chg>=1%)信号恒 0、
                #   回测永远 0 交易。改用前后两根收盘价实算涨跌幅，并补昨收/量比对齐实盘 quote。
                prev_close = float(df.iloc[pos - 2]["close"]) if pos >= 2 else close
                chg = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0
                rows.append({
                    "代码": c, "名称": "",
                    "最新价": close,
                    "涨跌幅": chg,
                    "昨收": prev_close,
                    "成交量": float(last["volume"]),
                    "量比": 1.0,
                    "换手率": 0,
                })
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    # ── 主运行循环 ──
    def run(self, start_date: str, end_date: str, _progress_fn=None, _cancel_fn=None) -> "Cerebro":
        start_ts = pd.Timestamp(start_date)
        end_ts   = pd.Timestamp(end_date)

        # 收集有效交易日
        days_set = set()
        for df in self.data_feeds.values():
            mask = (df["date"] >= start_ts) & (df["date"] <= end_ts)
            days_set.update(df[mask]["date"].tolist())
        days = sorted(days_set)

        if not days:
            self.logs.append({"level": "ERROR",
                               "msg": f"区间 {start_date}~{end_date} 无交易日数据"})
            return self

        self.logs.append({"level": "INFO",
                           "msg": f"Cerebro 启动: {len(days)} 交易日，"
                                  f"{len(self.data_feeds)} 只标的，"
                                  f"初始资金 ¥{self.initial_cash:,.0f}"})
        self.logs.append({"level": "INFO",
                           "msg": f"参数: 最大持仓={self.max_positions}，"
                                  f"止损={self.stop_loss*100}%，"
                                  f"止盈={self.take_profit*100}%"})

        for i, day in enumerate(days):
            # 终止检查（每5天检查一次）
            if _cancel_fn and i % 5 == 0 and _cancel_fn():
                break
            # 进度回调（每 5 天更新一次，减少开销）
            if _progress_fn and i % 5 == 0:
                _progress_fn(i + 1, len(days), f"模拟 {day.date()} ({i+1}/{len(days)}天)")

            # ── Step 1: 次日开盘执行昨日订单（backtrader 核心：无前视偏差）──
            open_px = self._open_prices(day)
            filled, rejected = self.broker.process_pending(day, open_px, self.logs)
            for o in filled:
                if o.action == "buy":
                    self.logs.append({"level": "INFO",
                                       "msg": f"[成交-买] {day.date()} {o.code} "
                                              f"¥{o.exec_price:.2f}×{o.qty}股 "
                                              f"佣金¥{o.commission:.2f}"})
                else:
                    # 卖出成交 → 通知 TradeAnalyzer
                    for t in self.broker.trades:
                        if t.sell_date == str(day.date()) and t.code == o.code:
                            self._trade.notify_trade(t)
                    self.logs.append({"level": "INFO",
                                       "msg": f"[成交-卖] {day.date()} {o.code} "
                                              f"¥{o.exec_price:.2f}×{o.qty}股 "
                                              f"原因={o.reason}"})

            # ── Step 2: 当日收盘价格 ──
            close_px = self._close_prices(day)

            # ── Step 3: 止损/止盈检查（收盘价触发，次日开盘执行）──
            for code, pos in list(self.broker.positions.items()):
                if code not in close_px:
                    continue
                cp  = close_px[code]
                pnl = (cp - pos.cost) / pos.cost
                if pnl <= self.stop_loss:
                    self.broker.submit_sell(code, day, "stop_loss")
                    self.logs.append({"level": "INFO",
                                       "msg": f"[止损] {day.date()} {code} "
                                              f"浮亏{pnl*100:.1f}%，次日卖出"})
                elif pnl >= self.take_profit:
                    self.broker.submit_sell(code, day, "take_profit")
                    self.logs.append({"level": "INFO",
                                       "msg": f"[止盈] {day.date()} {code} "
                                              f"浮盈{pnl*100:.1f}%，次日卖出"})

            # ── Step 4: 组合权益估值（参考 backtrader broker.getvalue）──
            equity = self.broker.portfolio_value(close_px)
            self.equity_curve.append({"date": str(day.date()), "equity": round(equity, 2)})
            self._sharpe.notify_equity(equity)
            self._drawdown.notify_equity(str(day.date()), equity)
            self._monthly.notify_equity(str(day.date()), equity)

            # ── Step 5: 收盘运行策略，生成次日信号 ──
            if i < len(days) - 1 and self.scan_fn:
                slots  = self.max_positions - len(self.broker.positions)
                # 排除已有待售单的标的
                pending_sell = {o.code for o in self.broker._pending if o.action == "sell"}
                if slots > 0:
                    try:
                        _day = day
                        sigs = self.scan_fn(
                            list(self.data_feeds.keys()),
                            lambda c, d=60, _d=_day: self._kline(c, d, _d),
                            lambda cs, _d=_day: self._quotes(cs, _d),
                        )
                        self._signal_cnt += len(sigs)
                        budget = self.broker.cash / max(slots, 1)
                        for sig in sigs[:slots]:
                            code = sig.get("code", "")
                            if code and code not in self.broker.positions \
                                    and code not in pending_sell:
                                self.broker.submit_buy(code, day, budget, "signal")
                    except Exception as e:
                        self.logs.append({"level": "WARNING",
                                           "msg": f"{day.date()} 策略出错: {e}"})

        # ── 回测结束统计 ──
        open_cnt = len(self.broker.positions)
        if open_cnt:
            self.logs.append({"level": "WARNING",
                               "msg": f"回测结束仍有 {open_cnt} 只未平仓: "
                                      f"{list(self.broker.positions.keys())}"})
        ta = self._trade.get_analysis()
        self.logs.append({"level": "INFO",
                           "msg": f"Cerebro 完成: "
                                  f"总交易={ta['total_trades']} "
                                  f"胜率={ta['win_rate']}% "
                                  f"信号={self._signal_cnt} "
                                  f"总佣金=¥{self.broker.total_commission:.0f}"})
        return self

    # ── 汇总结果 ──
    def get_results(self) -> dict:
        ta      = self._trade.get_analysis()
        sharpe  = self._sharpe.get_analysis()
        dd_data = self._drawdown.get_analysis()
        monthly = self._monthly.get_analysis()

        eq  = [e["equity"] for e in self.equity_curve]
        n   = len(eq)
        cap = self.initial_cash
        final = eq[-1] if eq else cap

        total_ret  = (final - cap) / cap
        annual_ret = (1 + total_ret) ** (252 / n) - 1 if n > 1 else 0.0
        calmar     = abs(annual_ret / (dd_data["max_drawdown"] / 100)) \
                     if dd_data["max_drawdown"] > 0 else 0.0

        metrics = {
            "total_return":    round(total_ret * 100, 2),
            "annual_return":   round(annual_ret * 100, 2),
            "max_drawdown":    dd_data["max_drawdown"],
            "sharpe":          sharpe["sharpe"],
            "calmar":          round(calmar, 2),
            "win_rate":        ta["win_rate"],
            "total_trades":    ta["total_trades"],
            "open_positions":  len(self.broker.positions),
            "avg_win":         ta["avg_win"],
            "avg_loss":        ta["avg_loss"],
            "profit_factor":   ta["profit_factor"],
            "max_win_streak":  ta["max_win_streak"],
            "max_loss_streak": ta["max_loss_streak"],
            "avg_hold_days":   ta["avg_hold_days"],
            "total_pnl":       ta["total_pnl"],
            "total_commission":round(self.broker.total_commission, 2),
            "final_equity":    round(final, 2),
            "total_days":      n,
        }

        # 兼容旧前端格式（trades 列表）
        all_trades = []
        for t in self.broker.trades:
            all_trades.append({
                "date": t.sell_date, "code": t.code, "action": "sell",
                "price": round(t.sell_price, 2), "qty": t.qty,
                "pnl_pct": t.pnl_pct, "hold_days": t.hold_days,
            })
            all_trades.append({
                "date": t.buy_date, "code": t.code, "action": "buy",
                "price": round(t.buy_price, 2), "qty": t.qty,
                "pnl_pct": 0, "hold_days": 0,
            })
        all_trades.sort(key=lambda x: x["date"])

        return {
            "metrics":         metrics,
            "equity_curve":    self.equity_curve,
            "drawdown_series": dd_data["series"],
            "monthly_returns": monthly,
            "pnl_dist":        ta["pnl_dist"],
            "hold_days_dist":  ta.get("hold_days_dist", []),
            "trades":          all_trades,
            "logs":            self.logs,
            "commission_total":round(self.broker.total_commission, 2),
        }


# ══════════════════════════════════════════════════
# 全市场数据加载（本地 CSV + TDX 活跃补充）
# ══════════════════════════════════════════════════
def _load_full_market_data(start_date: str, end_date: str, logs: list,
                            progress_fn=None) -> dict:
    """
    全市场回测数据两阶段加载：
      Phase 1: 本地 CSV 全量（data/daily/*.csv）— 快速
      Phase 2: TDX 实时行情预筛选 → 按成交量取前 800 只补充 K 线
    progress_fn(current, total, stage) 每步回调，供前端显示进度条。
    """
    from analytics import data_gateway as dg
    lookback_ts = pd.Timestamp(start_date) - pd.Timedelta(days=120)
    all_data: dict = {}

    # ── Phase 1: 本地 CSV 全量 ────────────────────────────────
    local_files = sorted(DATA_DIR.glob("*.csv"))
    total_local  = len(local_files)
    logs.append({"level": "INFO",
                 "msg": f"[全市场] 本地CSV {total_local} 只，开始加载..."})

    for i, f in enumerate(local_files):
        code = f.stem
        if progress_fn and i % 10 == 0:
            progress_fn(i + 1, total_local + 800, f"本地CSV {i+1}/{total_local}")
        try:
            df = dg.get_daily_kline(code, days=800, min_rows=10)
            if df is not None and not df.empty and len(df) >= 10:
                df = df[df["date"] >= lookback_ts].reset_index(drop=True)
                if not df.empty:
                    all_data[code] = df
        except Exception:
            pass

    logs.append({"level": "INFO",
                 "msg": f"[全市场] 本地加载完成: {len(all_data)} 只"})

    try:
        all_codes = dg.get_stock_universe()
        if not all_codes:
            return all_data

        logs.append({"level": "INFO",
                     "msg": f"[全市场] 统一数据源行情预筛选: {len(all_codes)} 只..."})
        if progress_fn:
            progress_fn(total_local, total_local + 800, "统一数据源行情预筛选中...")

        quotes = dg.get_realtime_quotes(all_codes)
        if not quotes:
            return all_data

        # 预筛选：价格 > 3 元、成交量 > 100 手、非停牌
        active = [q for q in quotes
                  if q.get("price", 0) > 3
                  and q.get("vol", 0) > 100
                  and q.get("last_close", 0) > 0]
        active.sort(key=lambda q: q.get("vol", 0), reverse=True)

        # 取前 N 只（BACKTEST_MAX_CODES，默认 800）中本地尚无数据的
        top_codes = [q["code"] for q in active[:BACKTEST_MAX_CODES] if q["code"] not in all_data]
        logs.append({"level": "INFO",
                     "msg": f"[全市场] TDX补充标的: {len(top_codes)} 只（排除本地已有）"})
        # ★ C-7：股票池是按**当前**实时行情筛出来的（今天仍活跃、价格>3元、按当前成交量排序），
        #   这带来两重偏差，回测收益会系统性高于实盘：
        #   ① 幸存者偏差——期间退市/仙股化/长期停牌的标的根本不在池子里；
        #   ② 前视偏差——用「今天的成交量排名」去选回测期初的标的。
        #   彻底修要落一份含退市股的历史股票宇宙表（独立工单）。在那之前必须让用户看见，
        #   否则会拿一个虚高的回测结果去决定要不要投实盘。
        logs.append({"level": "WARN",
                     "msg": "[偏差提示] 全市场股票池按『当前』行情筛选（今日仍活跃、价格>3元、"
                            "当前成交量排序），存在幸存者偏差与前视偏差，实盘表现通常低于此回测结果"})

        for i, code in enumerate(top_codes):
            if progress_fn and i % 10 == 0:
                progress_fn(total_local + i + 1,
                            total_local + len(top_codes),
                            f"统一数据源K线 {i+1}/{len(top_codes)}")
            try:
                df = dg.get_daily_kline(code, days=800, min_rows=10)
                if df is not None and not df.empty:
                    df["date"] = pd.to_datetime(df["date"])
                    df = df[df["date"] >= lookback_ts].sort_values("date").reset_index(drop=True)
                    if not df.empty:
                        all_data[code] = df
            except Exception:
                pass

        logs.append({"level": "INFO",
                     "msg": f"[全市场] 统一数据源补充完成，总计 {len(all_data)} 只数据"})

    except Exception as e:
        logs.append({"level": "WARNING", "msg": f"[全市场] 统一数据源补充失败: {e}"})

    return all_data


# ══════════════════════════════════════════════════
# 指定股票数据加载（本地 CSV + TDX）
# ══════════════════════════════════════════════════
def _load_data(codes: list, start_date: str, end_date: str, logs: list) -> dict:
    """
    数据加载：统一数据源优先，按后台配置自动降级。
    """
    from analytics import data_gateway as dg
    lookback_ts = pd.Timestamp(start_date) - pd.Timedelta(days=120)
    all_data    = {}

    for code in codes:
        # ── 统一数据源：第一源 Tushare，第二源按后台配置降级 ─────
        try:
            df = dg.get_daily_kline(code, days=800, min_rows=10)
            if df is not None and not df.empty and len(df) >= 10:
                df = df[df["date"] >= lookback_ts].reset_index(drop=True)
                if not df.empty:
                    all_data[code] = df
                    logs.append({"level": "INFO", "msg": f"[统一数据源] {code}: {len(df)} 行"})
                    continue
        except Exception as e:
            logs.append({"level": "WARNING", "msg": f"{code}: 统一数据源获取失败 — {e}"})

        logs.append({"level": "WARNING", "msg": f"{code}: 统一数据源无历史数据"})

    if not all_data:
        logs.append({"level": "ERROR",
                      "msg": "所有股票均无可用数据。请先在「数据管理」下载本地历史数据，"
                             "或确认 TDX 行情服务器已连接。"})
    return all_data


def _load_strategy(code_str: str, is_builtin: bool = False):
    """把策略源码落临时文件后 exec。

    这里必须自己跑一遍 ast_check：此前本路径一次都不检查，只靠「存盘时查过一次」
    的间接信任（TOCTOU）——策略文件被旁路改动、或将来新增导入/上传入口，
    都会在回测时被无差别执行。strategy_manager.run() 一直是查过的，两条执行路径
    的检查口径必须一致（含内置豁免：内置策略用 sys.path 做兼容导入）。
    """
    if not is_builtin:
        from strategy_engine.strategy_manager import ast_check
        chk = ast_check(code_str)
        if not chk["ok"]:
            raise ValueError(f"策略代码安全检查未通过: {chk['msg']}")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                     encoding="utf-8", delete=False) as f:
        f.write(code_str)
        tmp = f.name
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    try:
        spec = importlib.util.spec_from_file_location("_bt_mod", tmp)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.unlink(tmp)


# ══════════════════════════════════════════════════
# 公开入口（保持与 main.py 调用签名兼容）
# ══════════════════════════════════════════════════
def run_backtest(strategy_code: str, codes: list,
                 start_date: str, end_date: str,
                 initial_capital: float = 100_000,
                 max_positions: int = 5,
                 stop_loss: float = -0.05,
                 take_profit: float = 0.10,
                 is_builtin: bool = False,
                 _progress_fn=None,
                 _cancel_fn=None) -> dict:
    """
    Backtrader 风格回测入口。
    codes=[] → 全市场模式（本地CSV + TDX活跃前800只）
    _progress_fn(current, total, stage) → 供异步轮询使用
    """
    logs = []
    Order._id_seq = 0   # 每次回测重置 ID 计数器

    def _prog(cur, total, stage=""):
        if _progress_fn:
            _progress_fn(cur, total, stage)

    try:
        # 编译策略
        try:
            mod = _load_strategy(strategy_code, is_builtin=is_builtin)
        except Exception as e:
            return {"ok": False, "error": f"策略编译失败: {e}", "logs": logs}
        if not hasattr(mod, "scan"):
            return {"ok": False, "error": "策略必须包含 scan() 函数", "logs": logs}

        full_market = not codes   # codes=[] → 全市场模式
        logs.append({"level": "INFO",
                      "msg": f"策略加载成功，{'全市场' if full_market else f'{len(codes)}只'}回测模式，开始数据加载..."})

        # ── 数据加载（阶段1：占总进度 0-70%）──────────────────
        def _data_prog(cur, total, stage):
            _prog(int(cur / max(total, 1) * 70), 100, stage)

        if full_market:
            all_data = _load_full_market_data(start_date, end_date, logs, _data_prog)
        else:
            all_data = _load_data(codes, start_date, end_date, logs)
            _prog(70, 100, "数据加载完成")

        if not all_data:
            return {"ok": False,
                    "error": "无可用历史数据，请先在「数据管理」下载，或确认 TDX 已连接",
                    "logs": logs}

        actual_codes = list(all_data.keys())

        if not full_market:
            missing = [c for c in codes if c not in all_data]
            if missing:
                logs.append({"level": "WARNING",
                              "msg": f"以下股票无数据，跳过: {missing}"})

        logs.append({"level": "INFO",
                      "msg": f"数据就绪: {len(actual_codes)} 只，启动 Cerebro..."})
        _prog(70, 100, f"数据就绪 {len(actual_codes)} 只，启动模拟...")

        # ── 构建 Cerebro ─────────────────────────────────────
        cerebro = Cerebro(
            initial_cash  = initial_capital,
            max_positions = max_positions,
            stop_loss     = stop_loss,
            take_profit   = take_profit,
        )
        # 边加边释放：pop 出原 df 交给 add_data（内部排序成新副本存入 cerebro），
        # 原 df 随即无引用被回收 → 避免 all_data 与 cerebro.data_feeds 同时各存一份
        # （小内存机器上全市场回测的资居内存峰值约减半，是 OOM 的主因之一）。
        for code in actual_codes:
            cerebro.add_data(code, all_data.pop(code))
        all_data = None
        cerebro.logs = logs
        cerebro.set_scan(mod.scan)

        # ── 运行（阶段2：占总进度 70-100%）─────────────────────
        def _sim_prog(cur, total, stage):
            _prog(70 + int(cur / max(total, 1) * 30), 100, stage)

        cerebro.run(start_date, end_date, _progress_fn=_sim_prog, _cancel_fn=_cancel_fn)

        results = cerebro.get_results()
        m = results["metrics"]

        # 股票名称
        from analytics.data_manager import get_stock_name
        stock_names = {c: get_stock_name(c) for c in actual_codes}

        logs.append({"level": "INFO",
                      "msg": f"回测完成: "
                             f"收益={m['total_return']}% "
                             f"年化={m['annual_return']}% "
                             f"回撤={m['max_drawdown']}% "
                             f"Sharpe={m['sharpe']} "
                             f"胜率={m['win_rate']}% "
                             f"佣金=¥{m['total_commission']}"})

        _prog(100, 100, "完成")

        return {
            "ok":            True,
            "metrics":       results["metrics"],
            "equity_curve":  results["equity_curve"],
            "drawdown_series":results["drawdown_series"],
            "monthly_returns":results["monthly_returns"],
            "pnl_dist":      results["pnl_dist"],
            "hold_days_dist":results["hold_days_dist"],
            "trades":        results["trades"],
            "logs":          logs,
            "stock_names":   stock_names,
            "universe_size": len(actual_codes),
            "full_market":   full_market,
            "data_sources":  {c: "local" if (DATA_DIR / f"{c}.csv").exists() else "tdx"
                              for c in actual_codes},
        }

    except Exception as e:
        tb = traceback.format_exc()
        logs.append({"level": "ERROR", "msg": f"Cerebro 异常: {e}"})
        logs.append({"level": "ERROR", "msg": tb})
        logger.error(f"回测异常: {e}\n{tb}")
        _prog(-1, 100, f"错误: {e}")
        return {"ok": False, "error": str(e), "traceback": tb, "logs": logs}


# ══════════════════════════════════════════════════
# 异步回测 API（全市场需要60-120秒，前端需轮询）
# ══════════════════════════════════════════════════
def start_backtest_async(strategy_code: str, codes: list,
                          start_date: str, end_date: str,
                          initial_capital: float = 100_000,
                          max_positions: int = 5,
                          stop_loss: float = -0.05,
                          take_profit: float = 0.10,
                          is_builtin: bool = False,
                          uid: int | None = None) -> str:
    """在后台线程运行回测，立即返回 task_id（前端轮询 /api/backtest/task/<task_id>）"""
    _prune_bt_tasks()          # 新任务入表前先清理已结束的旧任务
    task_id = _bt_uuid.uuid4().hex
    _bt_tasks[task_id] = {
        "status":   "running",
        "uid":      uid,
        "progress": {"current": 0, "total": 100, "stage": "初始化..."},
        "result":   None,
        "created":  _bt_time.time(),
        "cancel":   False,   # 终止标志
    }

    def _worker():
        def _prog(cur, total, stage=""):
            _bt_tasks[task_id]["progress"] = {
                "current": cur, "total": total, "stage": stage
            }
        def _is_canceled():
            return _bt_tasks[task_id].get("cancel", False)

        try:
            result = run_backtest(
                strategy_code   = strategy_code,
                codes           = codes,
                start_date      = start_date,
                end_date        = end_date,
                initial_capital = initial_capital,
                max_positions   = max_positions,
                stop_loss       = stop_loss,
                take_profit     = take_profit,
                is_builtin      = is_builtin,
                _progress_fn    = _prog,
                _cancel_fn      = _is_canceled,
            )
            # 检查是否在运行中被取消
            if _bt_tasks[task_id].get("cancel"):
                _bt_tasks[task_id]["status"]   = "canceled"
                _bt_tasks[task_id]["result"]   = {"ok": False, "error": "用户已终止回测"}
                _bt_tasks[task_id]["progress"] = {"current": 0, "total": 100, "stage": "已终止"}
            else:
                _bt_tasks[task_id]["status"]   = "done"
                _bt_tasks[task_id]["result"]   = result
                _bt_tasks[task_id]["progress"] = {"current": 100, "total": 100, "stage": "完成"}
        except Exception as e:
            if _bt_tasks[task_id].get("cancel"):
                _bt_tasks[task_id]["status"]   = "canceled"
                _bt_tasks[task_id]["result"]   = {"ok": False, "error": "用户已终止回测"}
                _bt_tasks[task_id]["progress"] = {"current": 0, "total": 100, "stage": "已终止"}
            else:
                tb = traceback.format_exc()
                _bt_tasks[task_id]["status"]   = "error"
                _bt_tasks[task_id]["result"]   = {"ok": False, "error": str(e), "traceback": tb}
                _bt_tasks[task_id]["progress"] = {"current": -1, "total": 100, "stage": f"失败: {e}"}

    _bt_threading.Thread(target=_worker, daemon=True).start()
    return task_id


def cancel_backtest_task(task_id: str) -> dict:
    """请求终止一个正在运行的回测任务"""
    task = _bt_tasks.get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    if task["status"] != "running":
        return {"ok": False, "msg": f"任务状态为 {task['status']}，无法终止"}
    task["cancel"] = True
    task["status"] = "canceling"
    task["progress"]["stage"] = "正在终止..."
    return {"ok": True, "msg": "已发送终止信号"}


def get_backtest_task(task_id: str) -> dict:
    """查询异步回测任务的状态、进度与结果"""
    task = _bt_tasks.get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    return {
        "ok":       True,
        "status":   task["status"],
        "progress": task["progress"],
        # 只在完成/出错时返回 result（running 时不传，减少带宽）
        "result":   task["result"] if task["status"] not in ("running", "canceling") else None,
    }


# ══════════════════════════════════════════════════
# Qlib 风格扩展指标
# ══════════════════════════════════════════════════
def compute_metrics(equity_curve: list[dict],
                    benchmark: list[float] = None,
                    rf_annual: float = 0.025) -> dict:
    """
    计算 Qlib 风格回测指标:
      annual_return, calmar, sortino, alpha, beta, information_ratio

    参数:
      equity_curve  — [{"date": str, "equity": float}, ...]
      benchmark     — 基准每日净值序列（与 equity_curve 等长），默认沪深300
      rf_annual     — 无风险年化收益率（默认2.5%）

    返回 dict，所有值保留4位小数。
    """
    if not equity_curve or len(equity_curve) < 2:
        return {}

    try:
        equity = np.array([d["equity"] for d in equity_curve], dtype=float)
        daily_ret = np.diff(equity) / equity[:-1]
        n_days    = len(daily_ret)
        rf_daily  = rf_annual / 252

        # 年化收益
        total_ret   = equity[-1] / equity[0] - 1
        years       = n_days / 252
        annual_ret  = (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0

        # 最大回撤
        peak        = np.maximum.accumulate(equity)
        drawdown    = (equity - peak) / (peak + 1e-9)
        max_dd      = float(np.min(drawdown))

        # Calmar = 年化收益 / |最大回撤|
        calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

        # Sortino（只惩罚下行波动）
        excess      = daily_ret - rf_daily
        downside    = excess[excess < 0]
        down_std    = float(np.std(downside)) * np.sqrt(252) if len(downside) > 0 else 1e-9
        sortino     = float(annual_ret - rf_annual) / down_std if down_std > 0 else 0

        # Alpha / Beta（vs 基准）
        alpha = beta = information_ratio = None
        if benchmark and len(benchmark) == len(equity):
            bm     = np.array(benchmark, dtype=float)
            bm_ret = np.diff(bm) / (bm[:-1] + 1e-9)
            if len(bm_ret) == len(daily_ret) and np.std(bm_ret) > 1e-9:
                cov    = np.cov(daily_ret, bm_ret)
                beta   = round(float(cov[0, 1] / (np.var(bm_ret) + 1e-9)), 4)
                alpha  = round(float((annual_ret - rf_annual) - beta * (
                    (1 + np.mean(bm_ret)) ** 252 - 1 - rf_annual)), 4)
                # IR = mean(excess_vs_bm) / std(excess_vs_bm) * sqrt(252)
                excess_bm = daily_ret - bm_ret
                if np.std(excess_bm) > 0:
                    information_ratio = round(
                        float(np.mean(excess_bm) / np.std(excess_bm) * np.sqrt(252)), 4)

        return {
            "annual_return":      round(float(annual_ret), 4),
            "calmar":             round(float(calmar), 4),
            "sortino":            round(float(sortino), 4),
            "alpha":              alpha,
            "beta":               beta,
            "information_ratio":  information_ratio,
        }
    except Exception as e:
        logger.warning(f"[compute_metrics] {e}")
        return {}
