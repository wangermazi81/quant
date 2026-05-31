# META: {"name": "连板2+策略", "desc": "盯昨日首板·今日不破位·盘口异动·秒级watch_list实时监控", "author": "用户", "builtin": true}
"""连板 2+ 策略
─────────────────────────────────────────────
设计思路：
  1. 启动时（或每隔 5 分钟）从全市场拉一次日线，找出"昨日涨停/今日有潜在连板可能"的股
     → 形成 watch_list（通常 30-100 只）
  2. 之后每次扫描**只对 watch_list 做 pytdx 实时查询**（80 只 ≈ 1.5 秒）
     → 真正秒级响应
  3. 信号条件（满足任一）：
     (a) 今日竞价高开 > 3% + 当前价 > 昨收
     (b) 盘中涨幅 ≥ 5% + 量比 > 2 + 卖一未挂涨停
     (c) 接近涨停（涨幅 ≥ limit_rate × 0.7）+ 未封板 → 二板冲击

依赖：
  - 本地 data/daily/<code>.csv 缓存（昨日涨停判断）
  - pytdx 实时盘口（5档）
"""
from __future__ import annotations
import logging
import os
import sys
import time
from datetime import datetime

# 确保能 import 项目模块
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "market"))

_logger = logging.getLogger("strategies.lianban_2plus")

# ── watch list 缓存 ──
# 每隔 _REFRESH_SEC 重新筛一次（不必每次扫描都重筛）
_watch_list: list = []
_watch_list_ts: float = 0.0
_REFRESH_SEC = 300   # 5 分钟


def _market_priority(code: str) -> int:
    """主板优先，连板策略主要打主板（创业板/科创板 20% 涨停太极端）"""
    if code.startswith("60") or code.startswith("00"):
        return 0
    if code.startswith("300") or code.startswith("301"):
        return 1
    if code.startswith("688") or code.startswith("689"):
        return 2
    return 9


def _was_limit_up_yesterday(code: str) -> tuple[bool, float]:
    """检查昨日是否涨停。返回 (是否涨停, 昨日收盘价)
    数据源：本地 data/daily/<code>.csv（factor_manager 已经在维护）
    """
    try:
        import pandas as pd
        proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        csv_path = os.path.join(proj_root, "data", "daily", f"{code}.csv")
        if not os.path.exists(csv_path):
            return False, 0.0
        df = pd.read_csv(csv_path)
        if len(df) < 2:
            return False, 0.0
        df = df.sort_values("date" if "date" in df.columns else df.columns[0]).reset_index(drop=True)
        # 取最后一根（即昨日）+ 倒数第二根（前日）
        last = df.iloc[-1]
        prev = df.iloc[-2]
        last_close = float(last.get("close") or 0)
        prev_close = float(prev.get("close") or 0)
        if last_close <= 0 or prev_close <= 0:
            return False, 0.0
        # 涨幅 ≥ 9.5% 视为涨停（主板 10% / 创业板 20% / ST 5% 都靠这个判断）
        pct = (last_close - prev_close) / prev_close * 100
        rate = 20.0 if code.startswith(("300", "301", "688", "689")) else (5.0 if code.startswith(("4", "8")) else 10.0)
        is_limit = pct >= rate * 0.95
        return is_limit, last_close
    except Exception as e:
        _logger.debug(f"_was_limit_up_yesterday({code}) 异常: {e}")
        return False, 0.0


def _refresh_watch_list(client) -> list:
    """从全市场行情筛出昨日涨停或近期强势的股，形成 watch list

    数据源：pytdx 全市场行情 + 本地 daily CSV 缓存
    返回：[code, code, ...] 通常 30-100 只
    """
    try:
        quotes = client.get_full_market_quotes()
    except Exception as e:
        _logger.error(f"拉全市场行情失败: {e}")
        return []
    if not quotes:
        return []

    candidates = []
    for q in quotes:
        try:
            code = q.get("code") or ""
            if not code:
                continue
            # 只看主板 + 创业板（北交所/科创太极端不打）
            if not (code.startswith(("60", "00", "300", "301"))):
                continue
            # 必要字段
            price = float(q.get("price") or 0)
            last_close = float(q.get("last_close") or 0)
            change_pct = float(q.get("change_pct") or 0)
            if price <= 0 or last_close <= 0:
                continue
            # 筛选 1：今日涨幅 ≥ 2%（开盘后表现）或 已经接近涨停
            if change_pct < 2.0:
                continue
            # 筛选 2：昨日是否涨停
            yest_limit_up, _yest_close = _was_limit_up_yesterday(code)
            if yest_limit_up:
                candidates.append(code)
                continue
            # 筛选 3：今日涨幅 ≥ 7% 即使昨日没涨停也盯一下（潜在首板）
            if change_pct >= 7.0:
                candidates.append(code)
        except Exception:
            continue
    # 按主板优先排序
    candidates.sort(key=_market_priority)
    # 限制最多 100 只（保证 pytdx 实时查询 < 2 秒）
    candidates = candidates[:100]
    _logger.info(f"[lianban_2plus] watch_list 刷新: {len(candidates)} 只候选")
    return candidates


def _from_tdx_realtime(results_out: list) -> bool:
    """主流程：对 watch list 做实时盘口扫描"""
    global _watch_list, _watch_list_ts
    try:
        import ths_quote as tq
        client = tq.get_client()
        if not client or not client._ok:
            _logger.error("[lianban_2plus] TDX 客户端不可用")
            return False

        # 1. 维护 watch list（每 5 分钟刷一次）
        now_ts = time.time()
        if not _watch_list or (now_ts - _watch_list_ts) > _REFRESH_SEC:
            _watch_list = _refresh_watch_list(client)
            _watch_list_ts = now_ts
        if not _watch_list:
            _logger.warning("[lianban_2plus] watch_list 为空 — 当前市场无昨日涨停股")
            return True   # 不算失败

        # 2. 只对 watch_list 拉实时盘口（80 只 < 2s）
        t0 = time.time()
        quotes = client.get_quotes(_watch_list) or []
        elapsed = time.time() - t0
        _logger.info(f"[lianban_2plus] watch_list 实时查询: {len(quotes)} 只完成，耗时 {elapsed:.2f}s")

        for q in quotes:
            try:
                code = q.get("code") or ""
                if not code:
                    continue
                price = float(q.get("price") or 0)
                last_close = float(q.get("last_close") or 0)
                limit_up = float(q.get("limit_up") or 0)
                change_pct = float(q.get("change_pct") or 0)
                limit_rate = float(q.get("limit_rate") or 10.0)
                asks = q.get("asks") or []
                bids = q.get("bids") or []

                if price <= 0 or last_close <= 0 or limit_up <= 0:
                    continue

                # 检查昨日是否真涨停（用本地 CSV）
                yest_limit_up, yest_close = _was_limit_up_yesterday(code)

                # ── 信号条件 ──
                # 条件 A：昨日涨停 + 今日涨幅 ≥ 4% + 价格 > 昨收（不破位）→ 二板冲击信号
                cond_a = yest_limit_up and change_pct >= 4.0 and price > last_close * 1.001

                # 条件 B：当前价接近涨停（涨幅 ≥ 7% 即 limit_rate × 0.7）+ 未封板
                cond_b = (change_pct >= limit_rate * 0.7) and (price < limit_up - 0.001)

                # 条件 C：盘口异动 — 买盘碾压（仅在已有候选时加权）
                bid1_vol = int(bids[0].get("vol", 0)) if bids else 0
                ask1_vol = int(asks[0].get("vol", 0)) if asks else 0
                cond_c = bids and asks and ask1_vol > 0 and bid1_vol > ask1_vol * 3

                if not (cond_a or cond_b):
                    continue

                # 拼信号原因
                reasons = []
                board_label = "2板冲击" if yest_limit_up else "首板冲击"
                if cond_a:
                    reasons.append(f"昨涨停{yest_close:.2f}→今+{change_pct:.1f}%")
                if cond_b:
                    reasons.append(f"接近涨停({change_pct:.1f}% < {limit_up:.2f})")
                if cond_c:
                    reasons.append(f"买盘×{bid1_vol/max(ask1_vol,1):.1f}")

                results_out.append({
                    "code": code,
                    "name": q.get("name", code),
                    "price": price,
                    "change": change_pct,
                    "signal": True,
                    "consec_limit": 2 if yest_limit_up else 1,
                    "consec_label": board_label,
                    "reason": f"连板{board_label} | " + " | ".join(reasons) + f" | 涨停={limit_up:.2f}",
                })
            except Exception as _ee:
                _logger.debug(f"watch 单只异常 {q.get('code')}: {_ee}")
                continue

        _logger.info(f"[lianban_2plus] watch={len(_watch_list)} 实查={len(quotes)} → 信号={len(results_out)} 只")
        return True

    except Exception as e:
        _logger.exception(f"[lianban_2plus] 异常: {e}")
        return False


def scan(codes: list, get_kline, get_quotes) -> list:
    """策略扫描入口。

    监控模式（codes 为空 or 全市场）：走 _from_tdx_realtime（watch list 秒级查询）
    回测模式（codes 非空 + 历史数据）：仅作粗筛降级
    """
    results = []

    # 优先：pytdx watch list 实时扫描
    if _from_tdx_realtime(results):
        # 排序：主板优先 + 涨幅高优先
        results.sort(key=lambda x: (_market_priority(x["code"]), -x.get("change", 0)))
        return results

    # 降级：用回测引擎的 get_quotes 拉一次（不推荐，仅兜底）
    if not codes:
        return []
    try:
        quotes = get_quotes(codes)
        if quotes is None or quotes.empty:
            return []
        for _, row in quotes.iterrows():
            code = str(row.get("代码", "")).strip()
            change_pct = float(row.get("涨跌幅", 0) or 0)
            price = float(row.get("最新价", 0) or 0)
            if not code or price <= 0:
                continue
            if change_pct < 7.0:
                continue
            yest_limit_up, _ = _was_limit_up_yesterday(code)
            if not yest_limit_up:
                continue
            results.append({
                "code": code,
                "name": str(row.get("名称", "")).strip(),
                "price": price,
                "change": round(change_pct, 2),
                "signal": True,
                "consec_limit": 2,
                "consec_label": "2板冲击(降级)",
                "reason": f"连板降级 | 昨涨停今+{change_pct:.1f}%",
            })
        results.sort(key=lambda x: -x.get("change", 0))
        return results
    except Exception as e:
        _logger.error(f"降级路径异常: {e}")
        return []
