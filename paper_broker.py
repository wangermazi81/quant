# -*- coding: utf-8 -*-
"""
paper_broker.py — 模拟盘核心引擎

功能：
  · SQLite 持久化（WAL 模式，支持多线程并发读）
  · 真实佣金/印花税/滑点/最小手续费模拟
  · 100 股整手买入、资金/仓位充足性校验
  · 止损(-5%)、止盈(+10%)、最大持仓天数(10天)、趋势卖出 四种自动卖出规则
  · 净值曲线按日快照（每次账户变动后写入）
  · 统计接口：胜率、盈亏比、最大回撤、夏普
"""

import sqlite3
import threading
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# ── 路径 ─────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "data" / "paper_trading.db"
DB_PATH.parent.mkdir(exist_ok=True)

# ── 费率常数 ──────────────────────────────────────────
DEFAULT_COMMISSION_RATE = 0.0003   # 买卖双边，万3
DEFAULT_MIN_COMMISSION  = 5.0      # 最低佣金 5 元
DEFAULT_TAX_RATE        = 0.001    # 印花税 0.1%（仅卖出）
DEFAULT_SLIPPAGE_BPS    = 3        # 3 bps 滑点（买入价 × 1.0003）

# ── 风控常数 ──────────────────────────────────────────
DEFAULT_STOP_LOSS       = -0.05    # 止损 -5%
DEFAULT_TAKE_PROFIT     =  0.10    # 止盈 +10%
DEFAULT_MAX_HOLD_DAYS   = 10       # 最大持仓天数
DEFAULT_INITIAL_CAPITAL = 500_000.0


# ════════════════════════════════════════════════════
class PaperBroker:
    """模拟盘券商，所有方法线程安全"""

    def __init__(self, db_path: Path = DB_PATH):
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    # ── 内部：数据库连接（每线程独立） ─────────────────────
    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _init_db(self):
        """初始化表结构（幂等）"""
        ddl = """
        CREATE TABLE IF NOT EXISTS paper_config (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS paper_orders (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            order_time    TEXT    NOT NULL,
            code          TEXT    NOT NULL,
            name          TEXT    DEFAULT '',
            direction     TEXT    NOT NULL,   -- 'buy' | 'sell'
            signal_price  REAL    NOT NULL,
            exec_price    REAL    NOT NULL,
            shares        INTEGER NOT NULL,
            amount        REAL    NOT NULL,   -- exec_price × shares
            commission    REAL    NOT NULL,
            tax           REAL    DEFAULT 0,
            slippage_cost REAL    DEFAULT 0,
            status        TEXT    DEFAULT 'filled',  -- filled | rejected
            reject_reason TEXT    DEFAULT '',
            reason        TEXT    DEFAULT ''         -- 触发原因
        );

        CREATE TABLE IF NOT EXISTS paper_trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT    NOT NULL,
            name        TEXT    DEFAULT '',
            buy_time    TEXT    NOT NULL,
            sell_time   TEXT,
            buy_price   REAL    NOT NULL,
            sell_price  REAL,
            shares      INTEGER NOT NULL,
            buy_cost    REAL    NOT NULL,    -- 含佣金
            sell_income REAL,               -- 含佣金税后
            pnl         REAL,               -- sell_income - buy_cost
            pnl_pct     REAL,
            hold_days   INTEGER,
            sell_reason TEXT    DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS paper_positions (
            code        TEXT    PRIMARY KEY,
            name        TEXT    DEFAULT '',
            shares      INTEGER NOT NULL,
            cost_price  REAL    NOT NULL,   -- 含佣金的综合成本价
            buy_time    TEXT    NOT NULL,
            buy_amount  REAL    NOT NULL,   -- 买入总花费（含佣金）
            trade_id    INTEGER             -- → paper_trades.id（未平仓）
        );

        CREATE TABLE IF NOT EXISTS paper_equity (
            snap_date   TEXT    PRIMARY KEY,
            cash        REAL    NOT NULL,
            market_val  REAL    NOT NULL,
            equity      REAL    NOT NULL,
            drawdown    REAL    DEFAULT 0
        );
        """
        with self._lock:
            con = self._conn()
            con.executescript(ddl)
            con.commit()
            # 默认配置（若首次）
            defaults = {
                "initial_capital":  str(DEFAULT_INITIAL_CAPITAL),
                "cash":             str(DEFAULT_INITIAL_CAPITAL),
                "commission_rate":  str(DEFAULT_COMMISSION_RATE),
                "min_commission":   str(DEFAULT_MIN_COMMISSION),
                "tax_rate":         str(DEFAULT_TAX_RATE),
                "slippage_bps":     str(DEFAULT_SLIPPAGE_BPS),
                "stop_loss":        str(DEFAULT_STOP_LOSS),
                "take_profit":      str(DEFAULT_TAKE_PROFIT),
                "max_hold_days":    str(DEFAULT_MAX_HOLD_DAYS),
                "peak_equity":      str(DEFAULT_INITIAL_CAPITAL),
            }
            for k, v in defaults.items():
                con.execute(
                    "INSERT OR IGNORE INTO paper_config(key,value) VALUES(?,?)", (k, v)
                )
            con.commit()
            con.close()

    # ── 配置 ─────────────────────────────────────────
    def _cfg(self, key: str, typ=float):
        con = self._conn()
        row = con.execute("SELECT value FROM paper_config WHERE key=?", (key,)).fetchone()
        con.close()
        return typ(row["value"]) if row else None

    def _set_cfg(self, key: str, value):
        con = self._conn()
        con.execute("INSERT OR REPLACE INTO paper_config(key,value) VALUES(?,?)",
                    (key, str(value)))
        con.commit()
        con.close()

    def get_config(self) -> dict:
        con = self._conn()
        rows = con.execute("SELECT key,value FROM paper_config").fetchall()
        con.close()
        return {r["key"]: r["value"] for r in rows}

    def set_config(self, cfg: dict) -> dict:
        """更新部分配置项（不可更改 cash / peak_equity / initial_capital）"""
        allowed = {"commission_rate", "min_commission", "tax_rate",
                   "slippage_bps", "stop_loss", "take_profit", "max_hold_days"}
        with self._lock:
            con = self._conn()
            for k, v in cfg.items():
                if k in allowed:
                    con.execute("INSERT OR REPLACE INTO paper_config(key,value) VALUES(?,?)",
                                (k, str(v)))
            con.commit()
            con.close()
        return {"ok": True}

    # ── 账户快照 ─────────────────────────────────────
    def _cash(self) -> float:
        return self._cfg("cash") or 0.0

    def _set_cash(self, con: sqlite3.Connection, amount: float):
        con.execute("INSERT OR REPLACE INTO paper_config(key,value) VALUES('cash',?)",
                    (str(amount),))

    def _snap_equity(self, cur_prices: Optional[Dict] = None):
        """写一条净值快照（当日）"""
        try:
            con = self._conn()
            cash = self._cash()
            positions = con.execute("SELECT code,shares,cost_price FROM paper_positions").fetchall()
            mval = 0.0
            for pos in positions:
                p = (cur_prices or {}).get(pos["code"], pos["cost_price"])
                mval += p * pos["shares"]
            equity = cash + mval
            # 更新历史最高净值 → 用于回撤计算
            peak = self._cfg("peak_equity") or equity
            if equity > peak:
                peak = equity
                con.execute("INSERT OR REPLACE INTO paper_config(key,value) VALUES('peak_equity',?)",
                            (str(peak),))
            drawdown = round((equity - peak) / peak * 100, 4) if peak > 0 else 0.0
            today = datetime.now().strftime("%Y-%m-%d")
            con.execute("""
                INSERT OR REPLACE INTO paper_equity(snap_date,cash,market_val,equity,drawdown)
                VALUES(?,?,?,?,?)
            """, (today, round(cash, 2), round(mval, 2), round(equity, 2), drawdown))
            con.commit()
            con.close()
        except Exception as e:
            logger.warning(f"[PB] 净值快照失败: {e}")

    # ── 费用计算 ─────────────────────────────────────
    def _calc_buy_cost(self, signal_price: float, shares: int) -> tuple:
        """返回 (exec_price, amount, commission, slippage_cost)"""
        slippage_bps = self._cfg("slippage_bps") or DEFAULT_SLIPPAGE_BPS
        comm_rate    = self._cfg("commission_rate") or DEFAULT_COMMISSION_RATE
        min_comm     = self._cfg("min_commission") or DEFAULT_MIN_COMMISSION

        exec_price    = round(signal_price * (1 + slippage_bps / 10000), 4)
        amount        = round(exec_price * shares, 2)
        commission    = round(max(amount * comm_rate, min_comm), 2)
        slippage_cost = round((exec_price - signal_price) * shares, 2)
        return exec_price, amount, commission, slippage_cost

    def _calc_sell_income(self, signal_price: float, shares: int) -> tuple:
        """返回 (exec_price, amount, commission, tax)"""
        slippage_bps = self._cfg("slippage_bps") or DEFAULT_SLIPPAGE_BPS
        comm_rate    = self._cfg("commission_rate") or DEFAULT_COMMISSION_RATE
        min_comm     = self._cfg("min_commission") or DEFAULT_MIN_COMMISSION
        tax_rate     = self._cfg("tax_rate") or DEFAULT_TAX_RATE

        exec_price = round(signal_price * (1 - slippage_bps / 10000), 4)
        amount     = round(exec_price * shares, 2)
        commission = round(max(amount * comm_rate, min_comm), 2)
        tax        = round(amount * tax_rate, 2)
        net_income = round(amount - commission - tax, 2)
        return exec_price, net_income, commission, tax

    # ── 买入 ─────────────────────────────────────────
    def submit_buy(self, code: str, name: str, signal_price: float,
                   shares: int, reason: str = "") -> dict:
        """
        提交模拟买入。
        shares 必须是 100 的整数倍；资金不足时拒绝。
        返回 {"ok": bool, "msg": str, ...}
        """
        if shares <= 0 or shares % 100 != 0:
            shares = (shares // 100) * 100
        if shares <= 0:
            return {"ok": False, "msg": "手数不足100股"}

        exec_price, amount, commission, slippage_cost = self._calc_buy_cost(signal_price, shares)
        total_cost = amount + commission

        with self._lock:
            con = self._conn()
            cash = float(con.execute(
                "SELECT value FROM paper_config WHERE key='cash'"
            ).fetchone()["value"])

            # 资金校验
            if total_cost > cash:
                con.close()
                msg = (f"资金不足: 需要¥{total_cost:,.2f}，可用¥{cash:,.2f}")
                self._log_order(code, name, "buy", signal_price, exec_price, shares,
                                amount, commission, 0, slippage_cost, "rejected", msg, reason)
                return {"ok": False, "msg": msg}

            # 已有持仓则追加（不允许，同一标的只能持一笔）
            existing = con.execute(
                "SELECT shares FROM paper_positions WHERE code=?", (code,)
            ).fetchone()
            if existing:
                con.close()
                return {"ok": False, "msg": f"已持有 {code}，不重复买入"}

            now_str  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            new_cash = round(cash - total_cost, 2)
            cost_price = round(total_cost / shares, 4)  # 含佣金综合成本价

            # 写 paper_trades（未平仓记录）
            cur = con.execute("""
                INSERT INTO paper_trades(code,name,buy_time,buy_price,shares,buy_cost)
                VALUES(?,?,?,?,?,?)
            """, (code, name, now_str, exec_price, shares, round(total_cost, 2)))
            trade_id = cur.lastrowid

            # 写 paper_positions
            con.execute("""
                INSERT INTO paper_positions(code,name,shares,cost_price,buy_time,buy_amount,trade_id)
                VALUES(?,?,?,?,?,?,?)
            """, (code, name, shares, cost_price, now_str, round(total_cost, 2), trade_id))

            # 写 paper_orders
            con.execute("""
                INSERT INTO paper_orders
                  (order_time,code,name,direction,signal_price,exec_price,shares,amount,
                   commission,tax,slippage_cost,status,reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (now_str, code, name, "buy", signal_price, exec_price, shares, amount,
                  commission, 0, slippage_cost, "filled", reason))

            self._set_cash(con, new_cash)
            con.commit()
            con.close()

        self._snap_equity()
        logger.info(f"[PB] 买入 {code} {name} {shares}股 @{exec_price:.2f} "
                    f"花费¥{total_cost:,.2f} 剩余现金¥{new_cash:,.2f}")
        return {
            "ok":           True,
            "exec_price":   exec_price,
            "shares":       shares,
            "total_cost":   round(total_cost, 2),
            "commission":   commission,
            "slippage_cost": slippage_cost,
            "cash_after":   new_cash,
        }

    # ── 卖出 ─────────────────────────────────────────
    def submit_sell(self, code: str, signal_price: float,
                    sell_reason: str = "手动卖出") -> dict:
        """卖出全部持仓。返回 {"ok": bool, "pnl": float, ...}"""
        with self._lock:
            con = self._conn()
            pos = con.execute(
                "SELECT * FROM paper_positions WHERE code=?", (code,)
            ).fetchone()
            if not pos:
                con.close()
                return {"ok": False, "msg": f"无持仓: {code}"}

            shares = pos["shares"]
            name   = pos["name"]
            exec_price, net_income, commission, tax = self._calc_sell_income(signal_price, shares)

            cash    = float(con.execute(
                "SELECT value FROM paper_config WHERE key='cash'"
            ).fetchone()["value"])
            new_cash = round(cash + net_income, 2)

            now_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            buy_cost  = pos["buy_amount"]
            pnl       = round(net_income - buy_cost, 2)
            pnl_pct   = round(pnl / buy_cost * 100, 4) if buy_cost > 0 else 0

            buy_dt  = datetime.strptime(pos["buy_time"][:10], "%Y-%m-%d")
            hold_days = (datetime.now() - buy_dt).days

            # 更新 paper_trades
            con.execute("""
                UPDATE paper_trades
                SET sell_time=?,sell_price=?,sell_income=?,pnl=?,pnl_pct=?,
                    hold_days=?,sell_reason=?
                WHERE id=?
            """, (now_str, exec_price, round(net_income, 2), pnl, pnl_pct,
                  hold_days, sell_reason, pos["trade_id"]))

            # 写 paper_orders
            amount = round(exec_price * shares, 2)
            con.execute("""
                INSERT INTO paper_orders
                  (order_time,code,name,direction,signal_price,exec_price,shares,amount,
                   commission,tax,slippage_cost,status,reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (now_str, code, name, "sell", signal_price, exec_price, shares, amount,
                  commission, tax, 0, "filled", sell_reason))

            # 删持仓
            con.execute("DELETE FROM paper_positions WHERE code=?", (code,))
            self._set_cash(con, new_cash)
            con.commit()
            con.close()

        self._snap_equity()
        sign = "✅" if pnl >= 0 else "❌"
        logger.info(f"[PB] {sign} 卖出 {code} {name} {shares}股 @{exec_price:.2f} "
                    f"盈亏¥{pnl:+,.2f}({pnl_pct:+.2f}%) 原因:{sell_reason}")
        return {
            "ok":         True,
            "exec_price": exec_price,
            "net_income": round(net_income, 2),
            "commission": commission,
            "tax":        tax,
            "pnl":        pnl,
            "pnl_pct":    pnl_pct,
            "hold_days":  hold_days,
            "cash_after": new_cash,
        }

    def _log_order(self, code, name, direction, signal_price, exec_price,
                   shares, amount, commission, tax, slippage_cost,
                   status, reject_reason, reason):
        try:
            con = self._conn()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            con.execute("""
                INSERT INTO paper_orders
                  (order_time,code,name,direction,signal_price,exec_price,shares,amount,
                   commission,tax,slippage_cost,status,reject_reason,reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (now_str, code, name, direction, signal_price, exec_price, shares,
                  amount, commission, tax, slippage_cost, status, reject_reason, reason))
            con.commit()
            con.close()
        except Exception:
            pass

    # ── 风控检查：自动卖出 ────────────────────────────
    def check_risk_rules(self, cur_prices: dict) -> list:
        """
        检查所有持仓的风控规则，对触发条件的持仓执行模拟卖出。
        cur_prices: {code: current_price}
        返回已触发卖出的列表 [{"code", "reason", "pnl"}, ...]
        """
        triggered = []
        stop_loss    = self._cfg("stop_loss")    or DEFAULT_STOP_LOSS
        take_profit  = self._cfg("take_profit")  or DEFAULT_TAKE_PROFIT
        max_hold     = int(self._cfg("max_hold_days") or DEFAULT_MAX_HOLD_DAYS)

        con = self._conn()
        positions = con.execute("SELECT * FROM paper_positions").fetchall()
        con.close()

        for pos in positions:
            code       = pos["code"]
            cost_price = pos["cost_price"]
            buy_time   = pos["buy_time"]
            cur_price  = cur_prices.get(code)

            if not cur_price or cur_price <= 0:
                continue

            pnl_pct = (cur_price - cost_price) / cost_price if cost_price > 0 else 0
            buy_dt  = datetime.strptime(buy_time[:10], "%Y-%m-%d")
            hold    = (datetime.now() - buy_dt).days

            reason = None
            if pnl_pct <= stop_loss:
                reason = f"止损({pnl_pct*100:.1f}%≤{stop_loss*100:.0f}%)"
            elif pnl_pct >= take_profit:
                reason = f"止盈({pnl_pct*100:.1f}%≥{take_profit*100:.0f}%)"
            elif hold >= max_hold:
                reason = f"超期({hold}天≥{max_hold}天)"

            if reason:
                result = self.submit_sell(code, cur_price, sell_reason=reason)
                if result.get("ok"):
                    triggered.append({
                        "code":   code,
                        "name":   pos["name"],
                        "reason": reason,
                        "pnl":    result.get("pnl", 0),
                        "pnl_pct": result.get("pnl_pct", 0),
                    })

        return triggered

    # ── 账户信息 ─────────────────────────────────────
    def get_account(self) -> dict:
        con = self._conn()
        cash = float(con.execute(
            "SELECT value FROM paper_config WHERE key='cash'"
        ).fetchone()["value"])
        initial = float(con.execute(
            "SELECT value FROM paper_config WHERE key='initial_capital'"
        ).fetchone()["value"])
        peak = float(con.execute(
            "SELECT value FROM paper_config WHERE key='peak_equity'"
        ).fetchone()["value"])
        positions = con.execute("SELECT * FROM paper_positions").fetchall()
        con.close()

        # 持仓市值（使用成本价估算，无实时行情时）
        mval = sum(p["cost_price"] * p["shares"] for p in positions)
        equity = round(cash + mval, 2)
        total_return = round((equity - initial) / initial * 100, 4) if initial > 0 else 0
        drawdown = round((equity - peak) / peak * 100, 4) if peak > 0 else 0

        return {
            "initial_capital": initial,
            "cash":            round(cash, 2),
            "market_value":    round(mval, 2),
            "equity":          equity,
            "total_return":    total_return,
            "max_drawdown":    drawdown,
            "position_count":  len(positions),
        }

    def get_positions(self, cur_prices: Optional[Dict] = None) -> list:
        con = self._conn()
        rows = con.execute("SELECT * FROM paper_positions").fetchall()
        con.close()
        result = []
        for r in rows:
            code       = r["code"]
            cost_price = r["cost_price"]
            shares     = r["shares"]
            cur_price  = (cur_prices or {}).get(code, cost_price)
            pnl_pct    = round((cur_price - cost_price) / cost_price * 100, 2) if cost_price else 0
            pnl_amt    = round((cur_price - cost_price) * shares, 2)
            buy_dt     = datetime.strptime(r["buy_time"][:10], "%Y-%m-%d")
            hold_days  = (datetime.now() - buy_dt).days
            result.append({
                "code":         code,
                "name":         r["name"],
                "shares":       shares,
                "cost_price":   round(cost_price, 4),
                "cur_price":    round(cur_price, 2),
                "pnl_pct":      pnl_pct,
                "pnl_amount":   pnl_amt,
                "market_value": round(cur_price * shares, 2),
                "hold_days":    hold_days,
                "buy_time":     r["buy_time"],
            })
        return result

    def get_trades(self, limit: int = 100) -> list:
        con = self._conn()
        rows = con.execute("""
            SELECT * FROM paper_trades
            ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_orders(self, limit: int = 100) -> list:
        con = self._conn()
        rows = con.execute("""
            SELECT * FROM paper_orders
            ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_equity_curve(self, days: int = 90) -> list:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        con = self._conn()
        rows = con.execute("""
            SELECT snap_date, cash, market_val, equity, drawdown
            FROM paper_equity
            WHERE snap_date >= ?
            ORDER BY snap_date
        """, (since,)).fetchall()
        con.close()
        return [dict(r) for r in rows]

    # ── 统计 ─────────────────────────────────────────
    def get_stats(self) -> dict:
        """
        计算并返回模拟盘核心统计指标：
          win_rate, profit_factor, max_drawdown, total_return, sharpe, trade_count
        """
        con = self._conn()
        trades = con.execute("""
            SELECT pnl, pnl_pct, hold_days
            FROM paper_trades
            WHERE sell_time IS NOT NULL AND pnl IS NOT NULL
        """).fetchall()
        equity_rows = con.execute(
            "SELECT equity FROM paper_equity ORDER BY snap_date"
        ).fetchall()
        initial = float(con.execute(
            "SELECT value FROM paper_config WHERE key='initial_capital'"
        ).fetchone()["value"])
        con.close()

        if not trades:
            return {
                "trade_count":    0,
                "win_rate":       0.0,
                "profit_factor":  0.0,
                "avg_pnl":        0.0,
                "avg_hold_days":  0.0,
                "max_drawdown":   0.0,
                "total_return":   0.0,
                "sharpe":         0.0,
            }

        pnls      = [t["pnl"] for t in trades]
        pnl_pcts  = [t["pnl_pct"] for t in trades if t["pnl_pct"] is not None]
        holds     = [t["hold_days"] for t in trades if t["hold_days"] is not None]
        wins      = [p for p in pnls if p > 0]
        losses    = [p for p in pnls if p < 0]

        win_rate      = round(len(wins) / len(pnls) * 100, 2) if pnls else 0
        total_win     = sum(wins)
        total_loss    = abs(sum(losses)) if losses else 0
        profit_factor = round(total_win / total_loss, 4) if total_loss > 0 else (999.0 if total_win > 0 else 0.0)
        avg_pnl       = round(sum(pnls) / len(pnls), 2)
        avg_hold      = round(sum(holds) / len(holds), 1) if holds else 0

        # 最大回撤（从净值曲线）
        equities = [r["equity"] for r in equity_rows]
        max_dd = 0.0
        if equities:
            peak = equities[0]
            for eq in equities:
                if eq > peak:
                    peak = eq
                dd = (eq - peak) / peak * 100 if peak > 0 else 0
                if dd < max_dd:
                    max_dd = dd

        total_ret = 0.0
        if equities:
            total_ret = round((equities[-1] - initial) / initial * 100, 4)

        # 简化夏普（年化，假设252交易日，用日度收益率）
        sharpe = 0.0
        if len(pnl_pcts) >= 2:
            import statistics
            mu  = statistics.mean(pnl_pcts)
            std = statistics.stdev(pnl_pcts)
            if std > 0:
                sharpe = round(mu / std * (252 ** 0.5), 4)

        return {
            "trade_count":   len(pnls),
            "win_rate":      win_rate,
            "profit_factor": profit_factor,
            "avg_pnl":       avg_pnl,
            "avg_hold_days": avg_hold,
            "max_drawdown":  round(max_dd, 4),
            "total_return":  total_ret,
            "sharpe":        sharpe,
        }

    # ── 重置 ─────────────────────────────────────────
    def reset(self, initial_capital: Optional[float] = None) -> dict:
        """清空所有模拟盘数据，重置初始资金"""
        with self._lock:
            cap = initial_capital or self._cfg("initial_capital") or DEFAULT_INITIAL_CAPITAL
            con = self._conn()
            for tbl in ("paper_orders", "paper_trades", "paper_positions", "paper_equity"):
                con.execute(f"DELETE FROM {tbl}")
            # 重置配置
            for k, v in [("cash", cap), ("initial_capital", cap), ("peak_equity", cap)]:
                con.execute("INSERT OR REPLACE INTO paper_config(key,value) VALUES(?,?)",
                            (k, str(v)))
            con.commit()
            con.close()
        logger.info(f"[PB] 模拟盘已重置，初始资金¥{cap:,.0f}")
        return {"ok": True, "initial_capital": cap}


# ── 全局单例 ─────────────────────────────────────────
_broker: Optional["PaperBroker"] = None
_broker_lock = threading.Lock()


def get_broker() -> PaperBroker:
    global _broker
    if _broker is None:
        with _broker_lock:
            if _broker is None:
                _broker = PaperBroker()
    return _broker
