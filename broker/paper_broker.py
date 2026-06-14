# -*- coding: utf-8 -*-
"""
paper_broker.py — 多用户模拟盘引擎
每个用户拥有完全独立的持仓/资金/记录（通过 user_id 隔离）。
所有 SQL 都包含 WHERE user_id=? 条件，数据永久持久化到 paper_trading.db。
"""

import sqlite3
import threading
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict

import sell_rules   # 账户默认止损/止盈/超期决策（纯函数，与 main 实盘共用，测试见 tests/test_sell_rules.py）

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "paper_trading.db"
DB_PATH.parent.mkdir(exist_ok=True)

DEFAULT_COMMISSION_RATE = 0.0003
DEFAULT_MIN_COMMISSION  = 5.0
DEFAULT_TAX_RATE        = 0.001
DEFAULT_SLIPPAGE_BPS    = 3
DEFAULT_STOP_LOSS       = -0.05
DEFAULT_TAKE_PROFIT     = 0.10
DEFAULT_MAX_HOLD_DAYS   = 10
DEFAULT_INITIAL_CAPITAL = 500_000.0
# 封板/跌停封单金额阈值（元）：仅在 SealTracker 不可用时作回退兜底使用。
# 主路径走综合强弱评分（见 analytics/seal_tracker.py）。
DEFAULT_SEAL_BAN_THRESHOLD = 50_000_000.0
# 综合强度评分阈值：score > 阈值视为强封拒买，≤ 阈值允许"试排板"成交（按涨停价）
# 实测分布参考：103 只追踪 → 0-30 占 12 只 / 31-50 占 89 只 / 51+ 占 2 只
# 阈值 45 = 中等严：约 1 亿+封单的会被拒，5000 万以下能上
DEFAULT_SEAL_SCORE_THRESHOLD = 45


def init_paper_db():
    """创建/迁移 paper_trading.db 表结构（幂等，含 user_id 列）"""
    ddl = """
    CREATE TABLE IF NOT EXISTS paper_config (
        user_id  INTEGER NOT NULL DEFAULT 0,
        key      TEXT    NOT NULL,
        value    TEXT,
        PRIMARY KEY (user_id, key)
    );

    CREATE TABLE IF NOT EXISTS paper_orders (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL DEFAULT 0,
        order_time    TEXT    NOT NULL,
        code          TEXT    NOT NULL,
        name          TEXT    DEFAULT '',
        direction     TEXT    NOT NULL,
        signal_price  REAL    NOT NULL,
        exec_price    REAL    NOT NULL,
        shares        INTEGER NOT NULL,
        amount        REAL    NOT NULL,
        commission    REAL    NOT NULL,
        tax           REAL    DEFAULT 0,
        slippage_cost REAL    DEFAULT 0,
        status        TEXT    DEFAULT 'filled',
        reject_reason TEXT    DEFAULT '',
        reason        TEXT    DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_po_uid ON paper_orders(user_id);

    CREATE TABLE IF NOT EXISTS paper_trades (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL DEFAULT 0,
        code        TEXT    NOT NULL,
        name        TEXT    DEFAULT '',
        buy_time    TEXT    NOT NULL,
        sell_time   TEXT,
        buy_price   REAL    NOT NULL,
        sell_price  REAL,
        shares      INTEGER NOT NULL,
        buy_cost    REAL    NOT NULL,
        sell_income REAL,
        pnl         REAL,
        pnl_pct     REAL,
        hold_days   INTEGER,
        sell_reason TEXT    DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_pt_uid ON paper_trades(user_id);

    CREATE TABLE IF NOT EXISTS paper_positions (
        user_id     INTEGER NOT NULL DEFAULT 0,
        code        TEXT    NOT NULL,
        name        TEXT    DEFAULT '',
        shares      INTEGER NOT NULL,
        cost_price  REAL    NOT NULL,
        buy_time    TEXT    NOT NULL,
        buy_amount  REAL    NOT NULL,
        trade_id    INTEGER,
        PRIMARY KEY (user_id, code)
    );

    CREATE TABLE IF NOT EXISTS paper_equity (
        user_id    INTEGER NOT NULL DEFAULT 0,
        snap_date  TEXT    NOT NULL,
        cash       REAL    NOT NULL,
        market_val REAL    NOT NULL,
        equity     REAL    NOT NULL,
        drawdown   REAL    DEFAULT 0,
        PRIMARY KEY (user_id, snap_date)
    );
    """
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(ddl)
    con.commit()
    con.close()


# ════════════════════════════════════════════════════
class PaperBroker:
    """多用户模拟盘，每个 user_id 数据完全隔离"""

    def __init__(self, user_id: int):
        self._uid  = user_id
        self._lock = threading.Lock()
        self._ensure_user_config()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(DB_PATH), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _ensure_user_config(self):
        """首次使用时为该用户初始化默认配置"""
        con = self._conn()
        existing = con.execute(
            "SELECT key FROM paper_config WHERE user_id=? AND key='cash'",
            (self._uid,),
        ).fetchone()
        if not existing:
            defaults = {
                "initial_capital": str(DEFAULT_INITIAL_CAPITAL),
                "cash":            str(DEFAULT_INITIAL_CAPITAL),
                "commission_rate": str(DEFAULT_COMMISSION_RATE),
                "min_commission":  str(DEFAULT_MIN_COMMISSION),
                "tax_rate":        str(DEFAULT_TAX_RATE),
                "slippage_bps":    str(DEFAULT_SLIPPAGE_BPS),
                "stop_loss":       str(DEFAULT_STOP_LOSS),
                "take_profit":     str(DEFAULT_TAKE_PROFIT),
                "max_hold_days":   str(DEFAULT_MAX_HOLD_DAYS),
                "peak_equity":     str(DEFAULT_INITIAL_CAPITAL),
            }
            for k, v in defaults.items():
                con.execute(
                    "INSERT OR IGNORE INTO paper_config(user_id,key,value) VALUES(?,?,?)",
                    (self._uid, k, v),
                )
            con.commit()
        con.close()

    # ── 配置 ─────────────────────────────────────────
    def _cfg(self, key: str, typ=float):
        con = self._conn()
        row = con.execute(
            "SELECT value FROM paper_config WHERE user_id=? AND key=?",
            (self._uid, key),
        ).fetchone()
        con.close()
        return typ(row["value"]) if row else None

    def _set_cfg(self, con: sqlite3.Connection, key: str, value):
        con.execute(
            "INSERT OR REPLACE INTO paper_config(user_id,key,value) VALUES(?,?,?)",
            (self._uid, key, str(value)),
        )

    def get_config(self) -> dict:
        con = self._conn()
        rows = con.execute(
            "SELECT key,value FROM paper_config WHERE user_id=?", (self._uid,)
        ).fetchall()
        con.close()
        return {r["key"]: r["value"] for r in rows}

    def set_config(self, cfg: dict) -> dict:
        allowed = {"commission_rate", "min_commission", "tax_rate",
                   "slippage_bps", "stop_loss", "take_profit", "max_hold_days"}
        with self._lock:
            con = self._conn()
            for k, v in cfg.items():
                if k in allowed:
                    self._set_cfg(con, k, v)
            con.commit()
            con.close()
        return {"ok": True}

    # ── 账户 ─────────────────────────────────────────
    def _cash(self) -> float:
        return self._cfg("cash") or 0.0

    def _snap_equity(self, cur_prices: Optional[Dict] = None):
        try:
            con = self._conn()
            cash = self._cash()
            positions = con.execute(
                "SELECT code,shares,cost_price FROM paper_positions WHERE user_id=?",
                (self._uid,),
            ).fetchall()
            # 未传实时价时主动拉一次 TDX 行情。否则快照按成本价计市值，
            # 净值曲线永远不含浮动盈亏（且会覆盖当天已有的正确快照）。
            if not cur_prices and positions:
                try:
                    import sys, os
                    _mkt = os.path.join(os.path.dirname(os.path.dirname(__file__)), "market")
                    if _mkt not in sys.path:
                        sys.path.insert(0, _mkt)
                    import ths_quote as _tq
                    _cli = _tq.get_client()
                    if _cli and getattr(_cli, "_ok", False):
                        _qs = _cli.get_quotes([p["code"] for p in positions])
                        cur_prices = {q["code"]: float(q.get("price") or 0)
                                      for q in _qs if float(q.get("price") or 0) > 0}
                except Exception:
                    pass
            mval = sum(
                (cur_prices or {}).get(p["code"], p["cost_price"]) * p["shares"]
                for p in positions
            )
            equity = cash + mval
            peak = self._cfg("peak_equity") or equity
            if equity > peak:
                peak = equity
                con.execute(
                    "INSERT OR REPLACE INTO paper_config(user_id,key,value) VALUES(?,?,?)",
                    (self._uid, "peak_equity", str(peak)),
                )
            drawdown = round((equity - peak) / peak * 100, 4) if peak > 0 else 0.0
            today = datetime.now().strftime("%Y-%m-%d")
            con.execute(
                "INSERT OR REPLACE INTO paper_equity"
                "(user_id,snap_date,cash,market_val,equity,drawdown) VALUES(?,?,?,?,?,?)",
                (self._uid, today, round(cash, 2), round(mval, 2), round(equity, 2), drawdown),
            )
            con.commit()
            con.close()
        except Exception as e:
            logger.warning(f"[PB uid={self._uid}] 净值快照失败: {e}")

    # ── 费用计算 ─────────────────────────────────────
    def _calc_buy_cost(self, signal_price: float, shares: int):
        slip = self._cfg("slippage_bps") or DEFAULT_SLIPPAGE_BPS
        comm = self._cfg("commission_rate") or DEFAULT_COMMISSION_RATE
        minc = self._cfg("min_commission") or DEFAULT_MIN_COMMISSION
        exec_price    = round(signal_price * (1 + slip / 10000), 4)
        amount        = round(exec_price * shares, 2)
        commission    = round(max(amount * comm, minc), 2)
        slippage_cost = round((exec_price - signal_price) * shares, 2)
        return exec_price, amount, commission, slippage_cost

    def _calc_sell_income(self, signal_price: float, shares: int):
        slip = self._cfg("slippage_bps") or DEFAULT_SLIPPAGE_BPS
        comm = self._cfg("commission_rate") or DEFAULT_COMMISSION_RATE
        minc = self._cfg("min_commission") or DEFAULT_MIN_COMMISSION
        tax  = self._cfg("tax_rate") or DEFAULT_TAX_RATE
        exec_price = round(signal_price * (1 - slip / 10000), 4)
        amount     = round(exec_price * shares, 2)
        commission = round(max(amount * comm, minc), 2)
        tax_amt    = round(amount * tax, 2)
        net_income = round(amount - commission - tax_amt, 2)
        return exec_price, net_income, commission, tax_amt

    # ── 买入 ─────────────────────────────────────────
    def submit_buy(self, code: str, name: str, signal_price: float,
                   shares: int, reason: str = "") -> dict:
        if shares <= 0 or shares % 100 != 0:
            shares = (shares // 100) * 100
        if shares <= 0:
            return {"ok": False, "msg": "手数不足100股"}
        # 防御：如果 name 是空 / 含问号（PowerShell GBK 转码丢失中文等场景）
        # 从 stock_names.json 重新查正确名称
        if not name or "?" in name or "??" in name or name == code or len(name.strip()) < 2:
            try:
                from pathlib import Path
                import json as _json
                f = Path(__file__).parent.parent / "data" / "stock_names.json"
                if f.exists():
                    names = _json.loads(f.read_text(encoding="utf-8"))
                    name = names.get(code, name) or code
            except Exception:
                name = name or code

        # ── 封板成交检查（综合强弱评分）─────────────────────────
        # 现实规则：能不能排板买到，要看封单金额 + 趋势 + 开板次数 + 连板 + 封板时间，
        # 综合评分 0–100，**分越高越强势（越买不到）**。
        # 阈值默认 60：score > 60 拒买，≤ 60 允许（按涨停价成交模拟"试排板"）。
        try:
            import sys, os
            _mkt = os.path.join(os.path.dirname(os.path.dirname(__file__)), "market")
            if _mkt not in sys.path:
                sys.path.insert(0, _mkt)
            import ths_quote as _tq
            _cli = _tq.get_client()
            if _cli and getattr(_cli, "_ok", False):
                _qs = _cli.get_quotes([code])
                if _qs:
                    _q = _qs[0]
                    _lu = float(_q.get("limit_up", 0) or 0)
                    _bids = _q.get("bids") or []
                    _bid1_vol = float(_bids[0].get("vol", 0)) if _bids else 0
                    _cur_price = float(_q.get("price", 0) or 0)
                    seal_amount = _bid1_vol * 100 * _lu
                    is_sealed = _lu > 0 and _cur_price >= _lu - 0.001
                    # 接近涨停（封板 或 价 ≥ 涨停-0.02）才走综合评分逻辑
                    near_or_at_limit = _lu > 0 and _cur_price >= _lu - 0.02
                    if near_or_at_limit:
                        # 喂一笔最新样本（同时累计历史）
                        try:
                            from analytics.seal_tracker import get_tracker
                            tracker = get_tracker()
                            tracker.update(code, seal_amount, _cur_price, _lu, is_sealed)
                            score = tracker.score(code)
                            threshold = int(self._cfg("seal_score_threshold")
                                            or DEFAULT_SEAL_SCORE_THRESHOLD)
                            status = tracker.status(code)
                            if is_sealed and score > threshold:
                                return {
                                    "ok": False,
                                    "msg": (f"强封拒买: {code} 综合强度 {score}/100 > 阈值 {threshold}"
                                            f"（封单¥{seal_amount/10000:,.0f}万、"
                                            f"开板{status.get('open_count',0)}次、"
                                            f"{status.get('consec_limit',0)}连板）"),
                                }
                            # 通过：若当前封板，成交价 = 涨停价（扫货式）
                            if is_sealed:
                                signal_price = _lu
                                logger.info(
                                    f"[PB] {code} 强度评分 {score}≤{threshold}，按涨停 {_lu:.2f} 试排板成交"
                                    f"（开板{status.get('open_count',0)}次、封单¥{seal_amount/10000:,.0f}万）"
                                )
                        except Exception as _e:
                            logger.debug(f"[PB] 综合评分异常，回退单点判定: {_e}")
                            # 回退到原来的"5000 万单点阈值"
                            if is_sealed:
                                fallback = self._cfg("seal_ban_threshold") or DEFAULT_SEAL_BAN_THRESHOLD
                                if seal_amount >= fallback:
                                    return {"ok": False,
                                            "msg": f"封板拒买（回退）: {code} 封单¥{seal_amount/10000:,.0f}万 ≥ ¥{fallback/10000:,.0f}万"}
                                signal_price = _lu
        except Exception as _e:
            logger.debug(f"[PB] 封板检查异常（跳过）: {_e}")

        exec_price, amount, commission, slippage_cost = self._calc_buy_cost(signal_price, shares)
        total_cost = amount + commission

        with self._lock:
            con = self._conn()
            cash_row = con.execute(
                "SELECT value FROM paper_config WHERE user_id=? AND key='cash'",
                (self._uid,),
            ).fetchone()
            cash = float(cash_row["value"]) if cash_row else DEFAULT_INITIAL_CAPITAL

            if total_cost > cash:
                con.close()
                msg = f"资金不足: 需要¥{total_cost:,.2f}，可用¥{cash:,.2f}"
                self._log_order(code, name, "buy", signal_price, exec_price, shares,
                                amount, commission, 0, slippage_cost, "rejected", msg, reason)
                return {"ok": False, "msg": msg}

            existing = con.execute(
                "SELECT shares FROM paper_positions WHERE user_id=? AND code=?",
                (self._uid, code),
            ).fetchone()
            if existing:
                con.close()
                return {"ok": False, "msg": f"已持有 {code}，不重复买入"}

            now_str    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            new_cash   = round(cash - total_cost, 2)
            cost_price = round(total_cost / shares, 4)

            cur = con.execute(
                "INSERT INTO paper_trades"
                "(user_id,code,name,buy_time,buy_price,shares,buy_cost) VALUES(?,?,?,?,?,?,?)",
                (self._uid, code, name, now_str, exec_price, shares, round(total_cost, 2)),
            )
            trade_id = cur.lastrowid

            con.execute(
                "INSERT INTO paper_positions"
                "(user_id,code,name,shares,cost_price,buy_time,buy_amount,trade_id)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (self._uid, code, name, shares, cost_price, now_str, round(total_cost, 2), trade_id),
            )
            con.execute(
                "INSERT INTO paper_orders"
                "(user_id,order_time,code,name,direction,signal_price,exec_price,"
                "shares,amount,commission,tax,slippage_cost,status,reason)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self._uid, now_str, code, name, "buy", signal_price, exec_price,
                 shares, amount, commission, 0, slippage_cost, "filled", reason),
            )
            self._set_cfg(con, "cash", new_cash)
            con.commit()
            con.close()

        self._snap_equity()
        logger.info(f"[PB uid={self._uid}] 买入 {code} {shares}股 @{exec_price:.2f} "
                    f"花费¥{total_cost:,.2f} 余¥{new_cash:,.2f}")
        return {
            "ok": True, "exec_price": exec_price, "shares": shares,
            "total_cost": round(total_cost, 2), "commission": commission,
            "slippage_cost": slippage_cost, "cash_after": new_cash,
        }

    # ── 卖出 ─────────────────────────────────────────
    def submit_sell(self, code: str, signal_price: float,
                    sell_reason: str = "手动卖出") -> dict:
        with self._lock:
            con = self._conn()
            pos = con.execute(
                "SELECT * FROM paper_positions WHERE user_id=? AND code=?",
                (self._uid, code),
            ).fetchone()
            if not pos:
                con.close()
                return {"ok": False, "msg": f"无持仓: {code}"}

            # ── T+1 拒卖（A 股真实规则：当天买入不可当天卖出）──
            try:
                _buy_dt = datetime.strptime(pos["buy_time"][:10], "%Y-%m-%d").date()
                if _buy_dt == datetime.now().date():
                    con.close()
                    return {"ok": False,
                            "msg": f"T+1 拒卖: {code} 当天买入（{pos['buy_time']}）不可当天卖出，请明日再试"}
            except Exception as _e:
                logger.debug(f"[PB] T+1 检查异常（放行）: {_e}")

            # ── 跌停封单检查（已跌停时，看卖出封单是否砸得出去）──
            # 卖出封单金额 = ask1 卖盘手数 × 100 × 跌停价
            #   封单 ≥ 阈值（默认 5000 万）：跌停封死，砸不出去，拒卖
            #   封单 < 阈值                ：弱跌停，允许成交（按跌停价）
            try:
                import sys, os
                _mkt = os.path.join(os.path.dirname(os.path.dirname(__file__)), "market")
                if _mkt not in sys.path:
                    sys.path.insert(0, _mkt)
                import ths_quote as _tq
                _cli = _tq.get_client()
                if _cli and getattr(_cli, "_ok", False):
                    _qs = _cli.get_quotes([code])
                    if _qs:
                        _q = _qs[0]
                        _ld = float(_q.get("limit_down", 0) or 0)
                        _asks = _q.get("asks") or []
                        _ask1_vol = float(_asks[0].get("vol", 0)) if _asks else 0
                        if _ld > 0 and signal_price <= _ld + 0.001:
                            seal_amount = _ask1_vol * 100 * _ld
                            threshold   = self._cfg("seal_ban_threshold") or DEFAULT_SEAL_BAN_THRESHOLD
                            if seal_amount >= threshold:
                                con.close()
                                return {
                                    "ok": False,
                                    "msg": (f"跌停拒卖: {code} 跌停={_ld:.2f} 卖出封单¥{seal_amount/10000:,.0f}万"
                                            f"（≥¥{threshold/10000:,.0f}万阈值），砸不出去"),
                                }
                            else:
                                signal_price = _ld   # 实际成交价 = 跌停价
                                logger.info(
                                    f"[PB] 弱跌停成交 {code} 卖出封单¥{seal_amount/10000:,.0f}万 "
                                    f"< 阈值¥{threshold/10000:,.0f}万，按跌停 {_ld:.2f} 成交"
                                )
            except Exception as _e:
                logger.debug(f"[PB] 跌停检查异常（放行）: {_e}")

            shares = pos["shares"]
            name   = pos["name"]
            exec_price, net_income, commission, tax = self._calc_sell_income(signal_price, shares)

            cash_row = con.execute(
                "SELECT value FROM paper_config WHERE user_id=? AND key='cash'",
                (self._uid,),
            ).fetchone()
            cash     = float(cash_row["value"]) if cash_row else 0.0
            new_cash = round(cash + net_income, 2)

            now_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            buy_cost  = pos["buy_amount"]
            pnl       = round(net_income - buy_cost, 2)
            pnl_pct   = round(pnl / buy_cost * 100, 4) if buy_cost > 0 else 0
            buy_dt    = datetime.strptime(pos["buy_time"][:10], "%Y-%m-%d")
            hold_days = (datetime.now() - buy_dt).days

            con.execute(
                "UPDATE paper_trades SET sell_time=?,sell_price=?,sell_income=?,"
                "pnl=?,pnl_pct=?,hold_days=?,sell_reason=? WHERE id=? AND user_id=?",
                (now_str, exec_price, round(net_income, 2), pnl, pnl_pct,
                 hold_days, sell_reason, pos["trade_id"], self._uid),
            )
            amount = round(exec_price * shares, 2)
            con.execute(
                "INSERT INTO paper_orders"
                "(user_id,order_time,code,name,direction,signal_price,exec_price,"
                "shares,amount,commission,tax,slippage_cost,status,reason)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self._uid, now_str, code, name, "sell", signal_price, exec_price,
                 shares, amount, commission, tax, 0, "filled", sell_reason),
            )
            con.execute(
                "DELETE FROM paper_positions WHERE user_id=? AND code=?",
                (self._uid, code),
            )
            self._set_cfg(con, "cash", new_cash)
            con.commit()
            con.close()

        self._snap_equity()
        sign = "✅" if pnl >= 0 else "❌"
        logger.info(f"[PB uid={self._uid}] {sign} 卖出 {code} @{exec_price:.2f} "
                    f"盈亏¥{pnl:+,.2f}({pnl_pct:+.2f}%) 原因:{sell_reason}")
        return {
            "ok": True, "exec_price": exec_price, "net_income": round(net_income, 2),
            "commission": commission, "tax": tax,
            "pnl": pnl, "pnl_pct": pnl_pct, "hold_days": hold_days, "cash_after": new_cash,
        }

    def _log_order(self, code, name, direction, signal_price, exec_price,
                   shares, amount, commission, tax, slippage_cost,
                   status, reject_reason, reason):
        try:
            con = self._conn()
            con.execute(
                "INSERT INTO paper_orders"
                "(user_id,order_time,code,name,direction,signal_price,exec_price,"
                "shares,amount,commission,tax,slippage_cost,status,reject_reason,reason)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self._uid, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 code, name, direction, signal_price, exec_price, shares,
                 amount, commission, tax, slippage_cost, status, reject_reason, reason),
            )
            con.commit()
            con.close()
        except Exception:
            pass

    # ── 风控检查 ─────────────────────────────────────
    def check_risk_rules(self, cur_prices: dict,
                         stop_loss=None, take_profit=None, max_hold=None) -> list:
        """止损/止盈/超期检查。stop_loss/take_profit/max_hold 显式传入时覆盖账户默认配置，
        供策略级风控用（如打板 S6：止损-8% 锁利+1.5% 持有3天）。"""
        triggered = []
        if stop_loss is None:
            stop_loss = self._cfg("stop_loss") or DEFAULT_STOP_LOSS
        if take_profit is None:
            take_profit = self._cfg("take_profit") or DEFAULT_TAKE_PROFIT
        if max_hold is None:
            max_hold = int(self._cfg("max_hold_days") or DEFAULT_MAX_HOLD_DAYS)

        con = self._conn()
        positions = con.execute(
            "SELECT * FROM paper_positions WHERE user_id=?", (self._uid,)
        ).fetchall()
        con.close()

        for pos in positions:
            code       = pos["code"]
            cost_price = pos["cost_price"]
            cur_price  = cur_prices.get(code)
            if not cur_price or cur_price <= 0:
                continue
            pnl_pct = (cur_price - cost_price) / cost_price if cost_price > 0 else 0
            buy_dt  = datetime.strptime(pos["buy_time"][:10], "%Y-%m-%d")
            hold    = (datetime.now() - buy_dt).days

            _, reason = sell_rules.account_exit(
                pnl_pct, hold, stop_loss, take_profit, max_hold, thr_decimals=1)

            if reason:
                result = self.submit_sell(code, cur_price, sell_reason=reason)
                if result.get("ok"):
                    triggered.append({
                        "code": code, "name": pos["name"], "reason": reason,
                        "pnl": result.get("pnl", 0), "pnl_pct": result.get("pnl_pct", 0),
                    })
        return triggered

    # ── 账户信息 ─────────────────────────────────────
    def get_account(self, cur_prices: Optional[Dict] = None) -> dict:
        con = self._conn()
        cash_row = con.execute(
            "SELECT value FROM paper_config WHERE user_id=? AND key='cash'", (self._uid,)
        ).fetchone()
        init_row = con.execute(
            "SELECT value FROM paper_config WHERE user_id=? AND key='initial_capital'", (self._uid,)
        ).fetchone()
        peak_row = con.execute(
            "SELECT value FROM paper_config WHERE user_id=? AND key='peak_equity'", (self._uid,)
        ).fetchone()
        positions = con.execute(
            "SELECT * FROM paper_positions WHERE user_id=?", (self._uid,)
        ).fetchall()
        con.close()

        cash    = float(cash_row["value"])    if cash_row else DEFAULT_INITIAL_CAPITAL
        initial = float(init_row["value"])    if init_row else DEFAULT_INITIAL_CAPITAL
        peak    = float(peak_row["value"])    if peak_row else cash
        # 市值优先用实时价（传入 cur_prices 时），否则退回成本价。
        # 这样账户权益/收益率能体现浮动盈亏，实现实时收益跟踪。
        cur_prices = cur_prices or {}
        mval    = sum((cur_prices.get(p["code"]) or p["cost_price"]) * p["shares"] for p in positions)
        equity  = round(cash + mval, 2)
        return {
            "initial_capital": initial,
            "cash":            round(cash, 2),
            "market_value":    round(mval, 2),
            "equity":          equity,
            "total_return":    round((equity - initial) / initial * 100, 4) if initial > 0 else 0,
            "max_drawdown":    round((equity - peak) / peak * 100, 4) if peak > 0 else 0,
            "position_count":  len(positions),
        }

    def get_positions(self, cur_prices: Optional[Dict] = None) -> list:
        con = self._conn()
        rows = con.execute(
            "SELECT * FROM paper_positions WHERE user_id=?", (self._uid,)
        ).fetchall()
        con.close()
        # 防御：构造名称查询表（处理历史脏数据中的 "????" / 空名 / 与 code 相同等情况）
        _names_map = {}
        try:
            from pathlib import Path as _P
            import json as _json
            _f = _P(__file__).parent.parent / "data" / "stock_names.json"
            if _f.exists():
                _names_map = _json.loads(_f.read_text(encoding="utf-8"))
        except Exception:
            _names_map = {}

        result = []
        rewrite_needed = []  # 收集需要回写修复的脏记录
        for r in rows:
            code       = r["code"]
            cost_price = r["cost_price"]
            shares     = r["shares"]
            cur_price  = (cur_prices or {}).get(code, cost_price)
            pnl_pct    = round((cur_price - cost_price) / cost_price * 100, 2) if cost_price else 0
            pnl_amt    = round((cur_price - cost_price) * shares, 2)
            hold_days  = (datetime.now() - datetime.strptime(r["buy_time"][:10], "%Y-%m-%d")).days
            # 防御：名称含 ? / 空 / 与 code 相同 / 长度 <2，则从 stock_names.json 重新查询
            nm = r["name"]
            if (not nm) or ("?" in nm) or (nm == code) or (len(nm.strip()) < 2):
                fixed = _names_map.get(code)
                if fixed:
                    nm = fixed
                    rewrite_needed.append((fixed, self._uid, code))
                else:
                    nm = code
            result.append({
                "code":         code,
                "name":         nm,
                "shares":       shares,
                "cost_price":   round(cost_price, 4),
                "cur_price":    round(cur_price, 2),
                "pnl_pct":      pnl_pct,
                "pnl_amount":   pnl_amt,
                "market_value": round(cur_price * shares, 2),
                "hold_days":    hold_days,
                "buy_time":     r["buy_time"],
            })

        # 一次性回写脏数据（仅在确实找到正确名称时）
        if rewrite_needed:
            try:
                con = self._conn()
                con.executemany(
                    "UPDATE paper_positions SET name=? WHERE user_id=? AND code=?",
                    rewrite_needed,
                )
                con.commit()
                con.close()
                logger.info(f"[PB uid={self._uid}] 修复持仓名称: {len(rewrite_needed)} 条")
            except Exception as e:
                logger.warning(f"[PB uid={self._uid}] 修复持仓名称失败: {e}")
        return result

    def get_trades(self, limit: int = 100) -> list:
        con = self._conn()
        rows = con.execute(
            "SELECT * FROM paper_trades WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (self._uid, limit),
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_orders(self, limit: int = 100) -> list:
        con = self._conn()
        rows = con.execute(
            "SELECT * FROM paper_orders WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (self._uid, limit),
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_equity_curve(self, days: int = 90) -> list:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        con = self._conn()
        rows = con.execute(
            "SELECT snap_date,cash,market_val,equity,drawdown FROM paper_equity"
            " WHERE user_id=? AND snap_date>=? ORDER BY snap_date",
            (self._uid, since),
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        con = self._conn()
        trades = con.execute(
            "SELECT pnl,pnl_pct,hold_days FROM paper_trades"
            " WHERE user_id=? AND sell_time IS NOT NULL AND pnl IS NOT NULL",
            (self._uid,),
        ).fetchall()
        equity_rows = con.execute(
            "SELECT equity FROM paper_equity WHERE user_id=? ORDER BY snap_date",
            (self._uid,),
        ).fetchall()
        init_row = con.execute(
            "SELECT value FROM paper_config WHERE user_id=? AND key='initial_capital'",
            (self._uid,),
        ).fetchone()
        con.close()

        initial = float(init_row["value"]) if init_row else DEFAULT_INITIAL_CAPITAL
        if not trades:
            return {"trade_count": 0, "win_rate": 0.0, "profit_factor": 0.0,
                    "avg_pnl": 0.0, "avg_hold_days": 0.0, "max_drawdown": 0.0,
                    "total_return": 0.0, "sharpe": 0.0}

        pnls     = [t["pnl"] for t in trades]
        pnl_pcts = [t["pnl_pct"] for t in trades if t["pnl_pct"] is not None]
        holds    = [t["hold_days"] for t in trades if t["hold_days"] is not None]
        wins     = [p for p in pnls if p > 0]
        losses   = [p for p in pnls if p < 0]

        win_rate      = round(len(wins) / len(pnls) * 100, 2)
        total_win     = sum(wins)
        total_loss    = abs(sum(losses)) if losses else 0
        profit_factor = round(total_win / total_loss, 4) if total_loss > 0 else (999.0 if total_win > 0 else 0.0)
        avg_pnl       = round(sum(pnls) / len(pnls), 2)
        avg_hold      = round(sum(holds) / len(holds), 1) if holds else 0

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
        total_ret = round((equities[-1] - initial) / initial * 100, 4) if equities else 0.0

        # Sharpe：优先用净值曲线的日收益序列年化（正确口径）；
        # 净值点不足时退回单笔收益近似（仅作参考）
        sharpe = 0.0
        import statistics
        if len(equities) >= 3:
            rets = [(equities[i] - equities[i-1]) / equities[i-1]
                    for i in range(1, len(equities)) if equities[i-1] > 0]
            if len(rets) >= 2:
                mu, std = statistics.mean(rets), statistics.stdev(rets)
                if std > 0:
                    sharpe = round(mu / std * (252 ** 0.5), 4)
        elif len(pnl_pcts) >= 2:
            mu  = statistics.mean(pnl_pcts)
            std = statistics.stdev(pnl_pcts)
            if std > 0:
                sharpe = round(mu / std * (252 ** 0.5), 4)

        return {
            "trade_count": len(pnls), "win_rate": win_rate,
            "profit_factor": profit_factor, "avg_pnl": avg_pnl,
            "avg_hold_days": avg_hold, "max_drawdown": round(max_dd, 4),
            "total_return": total_ret, "sharpe": sharpe,
        }

    def reset(self, initial_capital: Optional[float] = None) -> dict:
        with self._lock:
            cap = initial_capital or self._cfg("initial_capital") or DEFAULT_INITIAL_CAPITAL
            con = self._conn()
            for tbl in ("paper_orders", "paper_trades", "paper_positions", "paper_equity"):
                con.execute(f"DELETE FROM {tbl} WHERE user_id=?", (self._uid,))
            for k, v in [("cash", cap), ("initial_capital", cap), ("peak_equity", cap)]:
                con.execute(
                    "INSERT OR REPLACE INTO paper_config(user_id,key,value) VALUES(?,?,?)",
                    (self._uid, k, str(v)),
                )
            # 立刻写入重置当天的起点快照：净值曲线从新本金重新开始，
            # 而不是空表（前端旧版对空数据不重绘，曲线看起来"没重置"）
            today = datetime.now().strftime("%Y-%m-%d")
            con.execute(
                "INSERT OR REPLACE INTO paper_equity"
                "(user_id,snap_date,cash,market_val,equity,drawdown) VALUES(?,?,?,?,?,?)",
                (self._uid, today, round(float(cap), 2), 0.0, round(float(cap), 2), 0.0),
            )
            con.commit()
            con.close()
        logger.info(f"[PB uid={self._uid}] 模拟盘已重置，初始资金¥{cap:,.0f}")
        return {"ok": True, "initial_capital": cap}


# ── 多用户实例池 ─────────────────────────────────────────────
_brokers: Dict[int, PaperBroker] = {}
_brokers_lock = threading.Lock()


def get_broker(user_id: int = 0) -> PaperBroker:
    """按 user_id 返回对应的 PaperBroker 实例（懒加载）"""
    if user_id not in _brokers:
        with _brokers_lock:
            if user_id not in _brokers:
                _brokers[user_id] = PaperBroker(user_id)
    return _brokers[user_id]
