# -*- coding: utf-8 -*-
"""影子账户分析器 v0 —— 真实成交流水 → 行为画像

目标：把同花顺/东财导出的"历史成交"CSV 喂进来，输出：
  - 标准化交易表（trades.csv）
  - FIFO 配对的 round-trip 表（round_trips.csv）
  - 行为统计（胜率/平均持仓/最大回撤/winner-loser 持仓时间差）
  - 人可读的 Markdown 报告
  - Run Card 快照（自动）

v0 主动 NOT 做的事（违反 Simplicity First）：
  - 影子回测（actual vs 规则化策略对比）—— 等 v1
  - 多券商/多账户合并 —— 等 v1
  - HTML/PDF 渲染 —— Markdown 已经够看
  - 严格的 disposition effect（要现仓数据）—— v0 用持仓时间代理

铁律：v0 不读 / 不写任何用户数据库；输入只读 CSV，输出全部在 logs/shadow/ 下。

跑法：
    cd D:\\同花顺量化交易\\ths_auto_trader
    python -m analytics.shadow_account --csv path\\to\\trades.csv
    # 默认输出: logs/shadow/<时间戳>/
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_OUT_BASE = _ROOT / "logs" / "shadow"

# 同花顺/东财/华泰/雪盈 等导出的列名别名 → 标准列名
_COL_ALIASES = {
    "date":   ["成交日期", "日期", "trade_date", "Date", "date"],
    "time":   ["成交时间", "时间", "Time", "time"],
    "code":   ["证券代码", "股票代码", "代码", "code", "Code"],
    "name":   ["证券名称", "股票名称", "名称", "name", "Name"],
    "side":   ["操作", "委托方向", "买卖方向", "买卖标志", "操作类型", "side"],
    "price":  ["成交价格", "成交均价", "成交价", "价格", "price", "Price"],
    "qty":    ["成交数量", "成交股数", "数量", "股数", "qty", "Qty"],
    "amount": ["成交金额", "金额", "amount", "Amount"],
    "fee":    ["手续费", "佣金", "fee", "commission"],
    "tax":    ["印花税", "tax"],
}

_BUY_TOKENS  = {"买入", "买", "BUY", "buy", "B"}
_SELL_TOKENS = {"卖出", "卖", "SELL", "sell", "S"}


# ============================== 解析 ==============================

def _detect_encoding(path: Path) -> str:
    """同花顺导出常见 GBK；东财有时 UTF-8 BOM。试两次。"""
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            with open(path, "r", encoding=enc) as f:
                f.read(4096)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


def _pick(df: pd.DataFrame, canonical: str) -> Optional[pd.Series]:
    for alias in _COL_ALIASES.get(canonical, [canonical]):
        if alias in df.columns:
            return df[alias]
    return None


def _normalize_side(s) -> Optional[str]:
    if pd.isna(s):
        return None
    s = str(s).strip()
    if s in _BUY_TOKENS:  return "BUY"
    if s in _SELL_TOKENS: return "SELL"
    # 兜底：包含子串
    if any(t in s for t in ("买", "BUY", "Buy")):  return "BUY"
    if any(t in s for t in ("卖", "SELL", "Sell")): return "SELL"
    return None


def _normalize_code(c) -> Optional[str]:
    if pd.isna(c): return None
    c = str(c).strip().replace(".SZ", "").replace(".SH", "").replace(".BJ", "")
    # 同花顺有时会写成 "='600611'" 形式
    c = c.strip("'\"= ")
    if not c or len(c) > 8:
        return None
    return c.zfill(6) if c.isdigit() and len(c) <= 6 else c


def parse_trades(csv_path: Path) -> pd.DataFrame:
    """读取券商 CSV，返回标准化交易表。

    返回列：date(datetime64), code(str), name(str), side(BUY/SELL),
            price(float), qty(int), amount(float), fee(float), tax(float)
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"找不到 CSV: {csv_path}")

    enc = _detect_encoding(csv_path)
    raw = pd.read_csv(csv_path, encoding=enc, dtype=str)
    if raw.empty:
        raise ValueError(f"CSV 为空: {csv_path}")

    out = pd.DataFrame()
    # 必需列
    date_s = _pick(raw, "date")
    code_s = _pick(raw, "code")
    side_s = _pick(raw, "side")
    price_s = _pick(raw, "price")
    qty_s  = _pick(raw, "qty")
    if any(x is None for x in (date_s, code_s, side_s, price_s, qty_s)):
        cols = list(raw.columns)
        raise ValueError(
            f"必需列缺失。当前 CSV 列：{cols}\n"
            f"需要至少能匹配到：date/code/side/price/qty 的别名"
        )

    out["date"]   = pd.to_datetime(date_s, errors="coerce")
    out["code"]   = code_s.map(_normalize_code)
    _name = _pick(raw, "name")
    out["name"]   = _name.astype(str).str.strip() if _name is not None else ""
    out["side"]   = side_s.map(_normalize_side)
    out["price"]  = pd.to_numeric(price_s.astype(str).str.replace(",", ""), errors="coerce")
    out["qty"]    = pd.to_numeric(qty_s.astype(str).str.replace(",", ""), errors="coerce").astype("Int64")
    # 可选列
    amt = _pick(raw, "amount")
    out["amount"] = pd.to_numeric(amt.astype(str).str.replace(",", ""), errors="coerce") if amt is not None \
                    else out["price"] * out["qty"].astype(float)
    fee = _pick(raw, "fee"); tax = _pick(raw, "tax")
    out["fee"] = pd.to_numeric(fee.astype(str).str.replace(",", ""), errors="coerce").fillna(0.0) if fee is not None else 0.0
    out["tax"] = pd.to_numeric(tax.astype(str).str.replace(",", ""), errors="coerce").fillna(0.0) if tax is not None else 0.0

    # 清洗：丢弃必需字段缺失的行
    before = len(out)
    out = out.dropna(subset=["date", "code", "side", "price", "qty"])
    out = out[out["qty"] > 0]
    dropped = before - len(out)
    if dropped:
        logger.warning(f"[shadow] 丢弃 {dropped} 条无效行（必需字段缺失或 qty<=0）")

    out = out.sort_values(["date", "code"]).reset_index(drop=True)
    return out


# ============================== FIFO 配对 ==============================

def match_round_trips(trades: pd.DataFrame) -> pd.DataFrame:
    """按股票分组 FIFO 配对买卖，输出 round-trip 表。

    返回列：code, name, buy_date, sell_date, qty, buy_price, sell_price,
            hold_days, gross_pnl, net_pnl, return_pct
    """
    trips = []
    for code, grp in trades.groupby("code", sort=False):
        # 按时间排序，名称取最后一次出现
        grp = grp.sort_values("date")
        name = grp["name"].iloc[-1]
        # buy 队列：(date, qty_remaining, price, fee_per_share, tax_per_share)
        buy_q: deque = deque()
        for _, row in grp.iterrows():
            qty = int(row["qty"])
            if row["side"] == "BUY":
                fps = float(row["fee"]) / qty if qty else 0.0
                tps = float(row["tax"]) / qty if qty else 0.0
                buy_q.append([row["date"], qty, float(row["price"]), fps, tps])
            elif row["side"] == "SELL":
                sell_qty_remain = qty
                sell_fee_total  = float(row["fee"])
                sell_tax_total  = float(row["tax"])
                sell_price      = float(row["price"])
                # 按本次卖出占该笔总卖出比例摊销手续费
                total_sell_qty = qty
                while sell_qty_remain > 0 and buy_q:
                    lot = buy_q[0]
                    take = min(sell_qty_remain, lot[1])
                    portion = take / total_sell_qty
                    sell_fee_this = sell_fee_total * portion
                    sell_tax_this = sell_tax_total * portion
                    buy_fee_this  = lot[3] * take
                    buy_tax_this  = lot[4] * take

                    gross = (sell_price - lot[2]) * take
                    net   = gross - sell_fee_this - sell_tax_this - buy_fee_this - buy_tax_this
                    hold_days = max((row["date"] - lot[0]).days, 0)
                    trips.append({
                        "code":        code,
                        "name":        name,
                        "buy_date":    lot[0],
                        "sell_date":   row["date"],
                        "qty":         take,
                        "buy_price":   round(lot[2], 4),
                        "sell_price":  round(sell_price, 4),
                        "hold_days":   hold_days,
                        "gross_pnl":   round(gross, 2),
                        "net_pnl":     round(net, 2),
                        "return_pct":  round((sell_price / lot[2] - 1) * 100, 3) if lot[2] else None,
                    })
                    lot[1] -= take
                    sell_qty_remain -= take
                    if lot[1] == 0:
                        buy_q.popleft()
                if sell_qty_remain > 0:
                    logger.warning(
                        f"[shadow] {code} 卖出 {qty} 股但买入队列已空（裸卖 / 历史数据不全），"
                        f"剩 {sell_qty_remain} 股无法配对"
                    )
    if not trips:
        return pd.DataFrame(columns=[
            "code", "name", "buy_date", "sell_date", "qty",
            "buy_price", "sell_price", "hold_days", "gross_pnl", "net_pnl", "return_pct",
        ])
    return pd.DataFrame(trips)


# ============================== 行为统计 ==============================

def _max_drawdown(cum_pnl: pd.Series) -> float:
    if cum_pnl.empty: return 0.0
    peak = cum_pnl.cummax()
    dd = cum_pnl - peak  # 负值
    return float(dd.min())


def compute_behavior(trades: pd.DataFrame, trips: pd.DataFrame) -> dict:
    """从 trades + trips 算行为画像。"""
    n_trades = len(trades)
    n_trips  = len(trips)
    if n_trips == 0:
        return {
            "n_trades": n_trades, "n_round_trips": 0,
            "first_date": str(trades["date"].min()) if n_trades else None,
            "last_date":  str(trades["date"].max()) if n_trades else None,
            "unique_codes": int(trades["code"].nunique()) if n_trades else 0,
        }

    winners = trips[trips["net_pnl"] > 0]
    losers  = trips[trips["net_pnl"] < 0]
    cum     = trips.sort_values("sell_date")["net_pnl"].cumsum()

    return {
        "n_trades":               n_trades,
        "n_round_trips":          n_trips,
        "first_date":             str(trades["date"].min()),
        "last_date":              str(trades["date"].max()),
        "unique_codes":           int(trades["code"].nunique()),
        "win_rate_pct":           round(len(winners) / n_trips * 100, 2),
        "avg_hold_days":          round(float(trips["hold_days"].mean()), 2),
        "median_hold_days":       int(trips["hold_days"].median()),
        "avg_winner_hold_days":   round(float(winners["hold_days"].mean()), 2) if len(winners) else None,
        "avg_loser_hold_days":    round(float(losers["hold_days"].mean()), 2) if len(losers)  else None,
        "disposition_proxy":      _disposition_proxy(winners, losers),
        "total_net_pnl":          round(float(trips["net_pnl"].sum()), 2),
        "avg_net_pnl_per_trip":   round(float(trips["net_pnl"].mean()), 2),
        "best_trip_pnl":          round(float(trips["net_pnl"].max()), 2),
        "worst_trip_pnl":         round(float(trips["net_pnl"].min()), 2),
        "max_drawdown":           round(_max_drawdown(cum), 2),
        "avg_return_pct":         round(float(trips["return_pct"].mean()), 3),
    }


def _disposition_proxy(winners: pd.DataFrame, losers: pd.DataFrame) -> str:
    """处置效应代理：赚钱的比亏钱的持仓时间更短 → "卖飞效应"（典型散户偏差）。"""
    if winners.empty or losers.empty:
        return "n/a (winners or losers empty)"
    w = float(winners["hold_days"].mean())
    l = float(losers["hold_days"].mean())
    if w < l - 1:
        return f"sell_winners_too_early (winners avg {w:.1f}d < losers {l:.1f}d)"
    if w > l + 1:
        return f"hold_winners_run (winners avg {w:.1f}d > losers {l:.1f}d) — 较少见，纪律强"
    return f"balanced (winners {w:.1f}d ≈ losers {l:.1f}d)"


# ============================== 报告渲染 ==============================

def render_report(behavior: dict, trades: pd.DataFrame, trips: pd.DataFrame, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "trades":      out_dir / "trades.csv",
        "round_trips": out_dir / "round_trips.csv",
        "report":      out_dir / "behavior_report.md",
        "report_json": out_dir / "behavior_report.json",
    }
    trades.to_csv(paths["trades"], index=False, encoding="utf-8-sig")
    trips.to_csv(paths["round_trips"], index=False, encoding="utf-8-sig")
    import json
    paths["report_json"].write_text(json.dumps(behavior, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 影子账户行为画像", "", f"_生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}_", ""]
    lines.append("## 基本统计")
    for k in ("n_trades", "n_round_trips", "unique_codes", "first_date", "last_date"):
        lines.append(f"- **{k}**: {behavior.get(k)}")
    if behavior.get("n_round_trips", 0) > 0:
        lines += ["", "## 盈亏与持仓"]
        for k in ("win_rate_pct", "total_net_pnl", "avg_net_pnl_per_trip",
                  "best_trip_pnl", "worst_trip_pnl", "max_drawdown",
                  "avg_return_pct"):
            lines.append(f"- **{k}**: {behavior.get(k)}")
        lines += ["", "## 行为偏差"]
        for k in ("avg_hold_days", "median_hold_days",
                  "avg_winner_hold_days", "avg_loser_hold_days",
                  "disposition_proxy"):
            lines.append(f"- **{k}**: {behavior.get(k)}")
        # Top winners / losers
        lines += ["", "## Top 5 盈利单"]
        lines.append("| code | name | buy_date | sell_date | hold_days | net_pnl | return_pct |")
        lines.append("|---|---|---|---|---:|---:|---:|")
        for _, r in trips.nlargest(5, "net_pnl").iterrows():
            lines.append(f"| {r['code']} | {r['name']} | {r['buy_date'].date()} | "
                         f"{r['sell_date'].date()} | {int(r['hold_days'])} | "
                         f"{r['net_pnl']:.2f} | {r['return_pct']:.2f}% |")
        lines += ["", "## Top 5 亏损单"]
        lines.append("| code | name | buy_date | sell_date | hold_days | net_pnl | return_pct |")
        lines.append("|---|---|---|---|---:|---:|---:|")
        for _, r in trips.nsmallest(5, "net_pnl").iterrows():
            lines.append(f"| {r['code']} | {r['name']} | {r['buy_date'].date()} | "
                         f"{r['sell_date'].date()} | {int(r['hold_days'])} | "
                         f"{r['net_pnl']:.2f} | {r['return_pct']:.2f}% |")
    paths["report"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {k: str(v) for k, v in paths.items()}


# ============================== CLI ==============================

def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="影子账户分析器 v0")
    p.add_argument("--csv", required=True, help="券商导出的成交流水 CSV 路径")
    args = p.parse_args(argv)

    csv_path = Path(args.csv).resolve()
    try:
        from run_card import RunCard
        card = RunCard.start("shadow_account", inputs={"csv": str(csv_path)})
    except Exception:
        card = None

    print(f"[shadow] 解析 {csv_path}")
    try:
        trades = parse_trades(csv_path)
    except Exception as exc:
        print(f"[shadow] 解析失败: {exc}", file=sys.stderr)
        if card: card.finish(outputs={"ok": False, "error": str(exc)}, status="error")
        return 1
    print(f"[shadow] 标准化后 {len(trades)} 条成交，{trades['code'].nunique()} 只股票")

    trips = match_round_trips(trades)
    print(f"[shadow] FIFO 配对得 {len(trips)} 个 round-trip")

    behavior = compute_behavior(trades, trips)
    stamp   = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = _OUT_BASE / stamp
    paths   = render_report(behavior, trades, trips, out_dir)
    print(f"[shadow] 完成。胜率 {behavior.get('win_rate_pct')}% "
          f"净盈亏 {behavior.get('total_net_pnl')} "
          f"最大回撤 {behavior.get('max_drawdown')}")
    print(f"  Report: {paths['report']}")

    if card:
        card.finish(outputs={"ok": True, **behavior, "paths": paths})
    return 0


if __name__ == "__main__":
    sys.exit(main())
