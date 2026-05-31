# -*- coding: utf-8 -*-
"""打板专用回测 harness（日线近似）。

目的：用本地日线诚实量化「首板封板买入 + T+1 卖出规则」的胜率/盈亏比/期望。
聚焦主板（60/000/001/002/003，涨停 10% 全程不变，避开创业板 2020 改 20% 的历史断层）。

口径（重要，报告里要如实说明）：
  入场 = 当日首板封板（close==涨停价 且 昨日非涨停 且 非一字板 low<涨停）。
  成交 = 按涨停价买入（模拟封板当天买到封单价；一字板买不到→已排除）。
  ⚠ 乐观假设：强封单实盘未必买得到；这里假设非一字首板能买到封板价。
  卖出 = 可配置规则（次日开盘/止盈止损/连板持有/尾盘）。
  费用 = 买 0.03% + 卖 0.13%（佣金+印花税），round-trip ≈ 0.16%。
"""
import os
import glob
import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "daily")
BUY_FEE  = 0.0003
SELL_FEE = 0.0013
TOL      = 0.001   # 涨停价容差


def _is_mainboard(code: str) -> bool:
    return code.startswith(("60", "000", "001", "002", "003"))


CACHE = os.path.join(os.path.dirname(__file__), "..", "logs", "_dabang_events.pkl")


def build_events(min_rows=120, start_date="2018-01-01", end_date="2026-12-31",
                 price_min=3.0, price_max=200.0):
    """扫描主板日线，返回所有「首板封板」入场事件（带后续 bar + 入场日特征）。

    特征（入场日 T 收盘可知，无前视）：
      vol_ratio  当日量 / 20日均量（资金关注度）
      ma20_ratio close / MA20（趋势位置；>1 在均线上）
      ma60_ratio close / MA60
      gain_20d   近 20 日涨幅%（位置高低；高位首板易失败）
      gain_5d    近 5 日涨幅%
      open_pct   当日开盘相对昨收涨幅%（低开反包 vs 高开秒板）
      amount     当日成交额近似 = close*volume（流动性）
    future 含入场日之后最多 6 根 bar。
    """
    events = []
    files = glob.glob(os.path.join(DATA_DIR, "*.csv"))
    n_scanned = 0
    for fp in files:
        code = os.path.splitext(os.path.basename(fp))[0]
        if not _is_mainboard(code):
            continue
        try:
            df = pd.read_csv(fp)
        except Exception:
            continue
        if df is None or len(df) < min_rows:
            continue
        if not {"date", "open", "close", "high", "low", "volume"}.issubset(df.columns):
            continue
        df = df.sort_values("date").reset_index(drop=True)
        df = df[(df["date"] >= start_date) & (df["date"] <= end_date)].reset_index(drop=True)
        if len(df) < 65:
            continue
        n_scanned += 1
        c = df["close"].values
        o = df["open"].values
        h = df["high"].values
        lo = df["low"].values
        v = df["volume"].values
        prev_c = np.roll(c, 1)
        limit_up = np.round(prev_c * 1.10, 2)
        is_limit = c >= (limit_up - TOL)
        is_limit[0] = False
        prev_is_limit = np.roll(is_limit, 1)
        prev_is_limit[0] = False
        is_yizi = lo >= (limit_up - TOL)
        vol_ma20 = pd.Series(v).rolling(20).mean().values
        ma20 = pd.Series(c).rolling(20).mean().values
        ma60 = pd.Series(c).rolling(60).mean().values
        c20  = np.roll(c, 20)
        c5   = np.roll(c, 5)

        n = len(df)
        for i in range(60, n - 1):
            # 含连板：只要当日封板(收盘=涨停) 且非一字，不再排除昨日涨停
            if not is_limit[i] or is_yizi[i]:
                continue
            buy_price = limit_up[i]
            if buy_price < price_min or buy_price > price_max:
                continue
            # 连板数：从 i 往前数连续涨停（含今日）。1=首板, 2=2连板, ...
            consec = 1
            jj = i - 1
            while jj >= 0 and is_limit[jj]:
                consec += 1
                jj -= 1
            vr = (v[i] / vol_ma20[i]) if vol_ma20[i] > 0 else 0.0
            future = []
            for j in range(i + 1, min(i + 12, n)):   # 更多未来bar，供"持有吃连板"
                future.append({
                    "open": float(o[j]), "close": float(c[j]),
                    "high": float(h[j]), "low": float(lo[j]),
                    "volume": float(v[j]), "limit_up": float(limit_up[j]),
                    "is_limit": bool(is_limit[j]),
                })
            if not future:
                continue
            events.append({
                "code": code, "date": str(df["date"].iloc[i])[:10],
                "year": int(str(df["date"].iloc[i])[:4]),
                "consec": int(consec),
                "buy_price": float(buy_price), "prev_close": float(prev_c[i]),
                "vol_ratio": round(float(vr), 2),
                "ma20_ratio": round(float(c[i] / ma20[i]), 3) if ma20[i] > 0 else 1.0,
                "ma60_ratio": round(float(c[i] / ma60[i]), 3) if ma60[i] > 0 else 1.0,
                "gain_20d": round(float(c[i] / c20[i] - 1) * 100, 2) if c20[i] > 0 else 0.0,
                "gain_5d": round(float(c[i] / c5[i] - 1) * 100, 2) if c5[i] > 0 else 0.0,
                "open_pct": round(float(o[i] / prev_c[i] - 1) * 100, 2) if prev_c[i] > 0 else 0.0,
                "amount": round(float(c[i] * v[i]), 0),
                "future": future,
            })
    return events, n_scanned


def load_events(rebuild=False):
    """带缓存的事件加载（首次扫描后 pickle，之后秒读）。"""
    import pickle
    if not rebuild and os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    events, n = build_events()
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "wb") as f:
        pickle.dump((events, n), f)
    return events, n


# ── 卖出规则：给 (buy_price, future_bars) → (sell_price, hold_days, exit_reason) ──

def sell_next_open(buy, fut):
    """S1：次日开盘无脑卖。"""
    return fut[0]["open"], 1, "次日开盘"


def sell_next_close(buy, fut):
    """S2：次日收盘卖。"""
    return fut[0]["close"], 1, "次日收盘"


def sell_t1_gap_or_close(buy, fut, gap=0.03, stop=-0.05):
    """S3：次日——高开≥gap→开盘止盈；跌破 stop→止损(近似按 stop 价)；否则尾盘卖。"""
    f = fut[0]
    op = f["open"]
    if op >= buy * (1 + gap):
        return op, 1, "次日高开止盈"
    stop_px = buy * (1 + stop)
    if f["low"] <= stop_px:
        return stop_px, 1, "次日破位止损"
    return f["close"], 1, "次日尾盘"


def sell_hold_while_limit(buy, fut, stop=-0.06, max_days=4):
    """S4：连板持有——次日还涨停就继续持有；某日不涨停则当日收盘卖；破 stop 止损。"""
    for k in range(min(max_days, len(fut))):
        f = fut[k]
        stop_px = buy * (1 + stop)
        # 破位止损（任意持有日）
        if f["low"] <= stop_px and not f["is_limit"]:
            return stop_px, k + 1, f"第{k+1}日破位止损"
        if f["is_limit"]:
            # 仍涨停，继续持有（除非到 max_days）
            if k == min(max_days, len(fut)) - 1:
                return f["close"], k + 1, f"连板到第{k+1}日尾盘"
            continue
        # 不涨停 → 当日收盘卖
        return f["close"], k + 1, f"第{k+1}日开板尾盘"
    return fut[-1]["close"], len(fut), "兜底尾盘"


def sell_t1_open_if_up_else_hold(buy, fut, stop=-0.06, max_days=3):
    """S5：次日高开(>0)就开盘卖锁利；否则按连板持有逻辑(S4)。"""
    f0 = fut[0]
    if f0["open"] > buy * 1.001:
        return f0["open"], 1, "次日高开锁利"
    return sell_hold_while_limit(buy, fut, stop=stop, max_days=max_days)


def sell_ride(buy, fut, stop=-0.05, trail=0.06, max_days=10):
    """龙头打板：让利润奔跑（吃连板）+ 紧止损 + 回落才走。盈亏比做成正非对称。
      - 任一日跳空开盘≤止损 → 开盘止损（认亏，砍得快）
      - 当日涨停(连板) → 继续持有（让利润奔跑，吃下一个板）
      - 当日未涨停：从持有期最高回落≥trail → 回落卖；否则收盘卖
      - 盘中破初始止损(且非涨停) → 止损
    stop 初始止损(相对买价)，trail 从峰值回落幅度，max_days 最长持有。
    """
    peak = buy
    stop_px = buy * (1 + stop)
    nd = min(max_days, len(fut))
    for k in range(nd):
        f = fut[k]
        if f["open"] <= stop_px:                       # 跳空跌破初始止损
            return f["open"], k + 1, "跳空止损"
        peak = max(peak, f["high"])
        if f["is_limit"]:                              # 连板：持有，让利润奔跑
            if k == nd - 1:
                return f["close"], k + 1, "到期仍封板"
            continue
        # 非涨停日
        if f["low"] <= stop_px and k == 0:             # 次日就破初始止损
            return stop_px, k + 1, "破位止损"
        trail_px = peak * (1 - trail)
        if f["low"] <= trail_px:                        # 从峰值回落超过 trail
            return min(f["open"], trail_px) if f["open"] < trail_px else trail_px, k + 1, "高位回落卖"
        return f["close"], k + 1, "开板收盘卖"
    return fut[nd - 1]["close"], nd, "到期卖"


def sell_small_tp_wide_stop(buy, fut, tp=0.015, stop=-0.08, max_days=3):
    """S6（高胜率向）：任一持有日触及 +tp 锁小利；触 stop 止损；否则到期尾盘卖。
    ⚠ 同日既触 tp 又触 stop 时**保守按先触 stop**（不高估胜率）。"""
    tp_px, stop_px = buy * (1 + tp), buy * (1 + stop)
    nd = min(max_days, len(fut))
    for k in range(nd):
        f = fut[k]
        if f["open"] <= stop_px:
            return f["open"], k + 1, "跳空止损"
        if f["open"] >= tp_px:
            return f["open"], k + 1, "高开锁利"
        if f["low"] <= stop_px:          # 保守：先判止损
            return stop_px, k + 1, "盘中止损"
        if f["high"] >= tp_px:
            return tp_px, k + 1, "盘中锁利"
    return fut[nd - 1]["close"], nd, "到期尾盘"


def sell_tp_then_trail(buy, fut, tp=0.01, stop=-0.07, max_days=2):
    """S7：更激进锁利，tp 更小、持有更短（冲高胜率，看期望是否还正）。"""
    return sell_small_tp_wide_stop(buy, fut, tp=tp, stop=stop, max_days=max_days)


def evaluate(events, sell_fn, label, quiet=False):
    """对所有事件套卖出规则，算 round-trip 净收益统计。"""
    rets = []
    holds = []
    for e in events:
        sp, hd, _ = sell_fn(e["buy_price"], e["future"])
        gross = sp / e["buy_price"] - 1.0
        net = (sp * (1 - SELL_FEE)) / (e["buy_price"] * (1 + BUY_FEE)) - 1.0
        rets.append(net)
        holds.append(hd)
    rets = np.array(rets)
    holds = np.array(holds)
    n = len(rets)
    if n == 0:
        if not quiet:
            print(f"{label}: 无事件")
        return None
    wins = rets > 0
    win_rate = wins.mean() * 100
    avg_win = rets[wins].mean() * 100 if wins.any() else 0.0
    avg_loss = rets[~wins].mean() * 100 if (~wins).any() else 0.0
    gross_win = rets[wins].sum()
    gross_loss = -rets[~wins].sum()
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    exp = rets.mean() * 100
    rr = (avg_win / abs(avg_loss)) if avg_loss != 0 else float("inf")   # 盈亏比(单笔均值)
    worst = rets.min() * 100
    total = rets.sum() * 100   # 等权累计(近似，单位%)
    if not quiet:
        print(f"{label}  n={n:<4} 胜率={win_rate:.0f}%  期望={exp:+.2f}%  盈亏比={rr:.2f}  "
              f"盈{avg_win:+.1f}/亏{avg_loss:+.1f}  最差{worst:+.0f}%  累计{total:+.0f}%  持{holds.mean():.1f}天")
    return {"n": n, "win_rate": win_rate, "exp": exp, "pf": pf, "rr": rr,
            "avg_win": avg_win, "avg_loss": avg_loss, "worst": worst, "total": total}


def filt(events, **conds):
    """按区间过滤事件。conds 形如 vol_ratio=(lo,hi), ma20_ratio=(lo,hi), gain_20d=(lo,hi)...
    None 表示该侧不限。"""
    out = []
    for e in events:
        ok = True
        for k, (lo, hi) in conds.items():
            x = e.get(k)
            if x is None:
                ok = False; break
            if lo is not None and x < lo:
                ok = False; break
            if hi is not None and x > hi:
                ok = False; break
        if ok:
            out.append(e)
    return out


def by_year(events, sell_fn, label):
    print(f"  [{label}] 分年:")
    for yr in sorted(set(e["year"] for e in events)):
        sub = [e for e in events if e["year"] == yr]
        r = evaluate(sub, sell_fn, f"    {yr}", quiet=True)
        if r:
            print(f"    {yr}: n={r['n']:<4} 胜率={r['win_rate']:.1f}%  期望={r['exp']:+.2f}%  PF={r['pf']:.2f}")


if __name__ == "__main__":
    import sys, time
    rebuild = "--rebuild" in sys.argv
    t0 = time.time()
    events, n_scanned = load_events(rebuild=rebuild)
    ev = [e for e in events if e["year"] >= 2025]
    n_first = sum(1 for e in ev if e.get("consec", 1) == 1)
    print(f"主板有效个股 {n_scanned} 只，封板事件 {len(events)} 个（2025+ 共 {len(ev)}："
          f"首板 {n_first} / 连板 {len(ev)-n_first}，{time.time()-t0:.1f}s）\n")

    print("=" * 78)
    print("【对比：错误的对称(S6) vs 让利润奔跑(Ride)】(全部封板, 2025+)")
    print("=" * 78)
    evaluate(ev, lambda b, f: sell_small_tp_wide_stop(b, f), "❌S6 小止盈+宽止损(旧)   ")
    evaluate(ev, lambda b, f: sell_hold_while_limit(b, f, stop=-0.05, max_days=10), "S4 连板持有/-5%止损      ")
    print("-" * 78)
    print("【Ride 让利润奔跑：紧止损 + 吃连板 + 回落才走】参数网格")
    for stop in (-0.04, -0.05, -0.06):
        for trail in (0.05, 0.07, 0.09):
            evaluate(ev, lambda b, f, s=stop, t=trail: sell_ride(b, f, stop=s, trail=t),
                     f"Ride 止损{stop*100:.0f}%/回落{trail*100:.0f}%   ")

    # 选一个稳健 Ride 参数，看分连板数表现（首板 vs 2板 vs 3板+）
    ride = lambda b, f: sell_ride(b, f, stop=-0.05, trail=0.07, max_days=10)
    print("\n" + "=" * 78)
    print("【Ride(-5%/回落7%) 分连板数】(看右侧尾部在哪)")
    print("=" * 78)
    for lab, lo, hi in [("首板(1)", 1, 1), ("2连板", 2, 2), ("3连板", 3, 3), ("4连板+", 4, 99)]:
        sub = [e for e in ev if lo <= e.get("consec", 1) <= hi]
        evaluate(sub, ride, f"{lab:<10}")

    print("\n" + "=" * 78)
    print("【Ride(-5%/回落7%) 分年稳定性】")
    print("=" * 78)
    by_year(ev, ride, "Ride")
