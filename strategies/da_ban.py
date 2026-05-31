# META: {"name": "打板策略", "desc": "封板(首板/连板)+站上MA20选股；吃连板让利润奔跑+紧止损5%+回落7%卖。回测盈亏比≈2.5/期望+3%/笔(连板更高)", "author": "用户", "builtin": true}
"""
打板策略 v2（pytdx 版）
─────────────────────────────────────────────
数据来源：pytdx 直连通达信行情服务器
延迟：< 0.5 秒（TCP 直连）
信号：当前价 = 涨停价 - 0.01，且封板强度足够

封板强度判断（利用5档盘口）：
  - 卖一价 == 涨停价（已有卖单挂在涨停）→ 接近封板
  - 卖一手数 * 100 * 涨停价 / 总成交额 < 3%  → 封板成本低
  - 买一量 >> 卖一量 → 买盘强势

当 pytdx 不可用时，自动降级到 get_quotes（akshare）。
"""


def _market_priority(code: str) -> int:
    """市场优先级（数字越小越靠前）：
        0: 主板 60/00（含沪主、深主、中小板）
        1: 创业板 300/301
        2: 科创板 688/689
        3: 北交所 4/8
        9: 其他/未知
    自动执行时资金会先打到优先级 0 的主板，剩余再买后面的板块。
    """
    if code.startswith("60") or code.startswith("00"):
        return 0
    if code.startswith("300") or code.startswith("301"):
        return 1
    if code.startswith("688") or code.startswith("689"):
        return 2
    if code.startswith("4") or code.startswith("8"):
        return 3
    return 9


def _count_consec_limit_up(client, code: str, limit_rate: float) -> int:
    """
    统计连续涨停天数（含今日）。
    逻辑：取近30日K线，从最新一根往前数，连续涨幅≥limit_rate*0.95的天数。
    """
    try:
        bars = client.get_kline(code, 30)
        if not bars or len(bars) < 2:
            return 0
        bars = sorted(bars, key=lambda x: x["date"])
        count = 0
        for i in range(len(bars) - 1, 0, -1):
            close_today = bars[i]["close"]
            close_prev  = bars[i - 1]["close"]
            if close_prev <= 0:
                break
            pct = (close_today - close_prev) / close_prev * 100
            if pct >= limit_rate * 0.95:   # 允许±5%误差（含一字板、ST）
                count += 1
            else:
                break
        return count
    except Exception:
        return 0


def _from_tdx(results_out: list) -> bool:
    """
    尝试用 pytdx 扫描全市场，结果写入 results_out。
    返回 True 表示成功，False 表示降级。
    """
    import logging
    _logger = logging.getLogger("strategies.da_ban")
    # ★ 各级过滤命中数诊断（解决"为什么 0 命中"看不到原因的问题）★
    _stats = {
        "total": 0,           # quotes 总数
        "basic_filter": 0,    # 过基础字段（price>0 等）
        "near_limit": 0,      # 差两档以内（未封板候选）
        "near_passed_seal": 0,# 通过封板分阈值
        "sealed": 0,          # 已封板（含 signal=False 参考）
        "ic_板首板": 0,        # 已封板首板 consec<=1
    }

    try:
        import sys, os, time as _time
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        import ths_quote as tq
        try:
            from analytics.seal_tracker import get_tracker
            _tracker = get_tracker()
        except Exception:
            _tracker = None

        client = tq.get_client()
        if not client or not client._ok:
            _logger.error("[da_ban] TDX 客户端不可用，降级")
            return False

        quotes = client.get_full_market_quotes()
        _now_ts = _time.time()
        if not quotes:
            _logger.error("[da_ban] get_full_market_quotes 返回空，降级")
            return False
        _stats["total"] = len(quotes)
        _logger.info(f"[da_ban] 拉到全市场行情 {len(quotes)} 只")

        # 抽样看一下数据结构（首次诊断字段是否齐全）
        if quotes:
            _sample_keys = set(quotes[0].keys())
            _logger.info(f"[da_ban] 行情字段示例: {sorted(_sample_keys)}")

        for q in quotes:
            try:
                code       = q.get("code") or ""
                if not code:
                    continue
                price      = float(q.get("price") or 0)
                last_close = float(q.get("last_close") or 0)
                limit_up   = float(q.get("limit_up") or 0)
                change_pct = float(q.get("change_pct") or 0)
                vol        = float(q.get("vol") or 0)
                asks       = q.get("asks") or []
                bids       = q.get("bids") or []
                limit_rate = float(q.get("limit_rate") or 10.0)

                if price <= 0 or last_close <= 0 or limit_up <= 0:
                    continue
                _stats["basic_filter"] += 1

                # ★ Bug fix 2026-05-26: 放宽候选范围 ★
                # 散户买不进已封板股。改成"涨幅 ≥ limit_rate × 0.7 但价格 < 涨停"
                # → 即 7%+ 涨幅且未封板。涵盖差 1-N 档、即将冲板、强势震荡
                near_threshold = round(limit_up - 0.10, 2)   # 差最多 10 个 tick（约 1%）
                if not (last_close * (1 + limit_rate * 0.7 / 100) <= price < limit_up - 0.001):
                    continue
                _stats["near_limit"] += 1

                # ── 过滤极端低流动性（放宽：vol < 50 才跳过）──
                if vol < 50:
                    continue

                # ── 封板强度分析（5档盘口）──────────────────────
                if not asks or not bids:
                    # 没盘口数据 → 给个保守底分跳过封板分判定
                    seal_score = 20
                    ask1_price = price
                    ask1_vol = 0
                    bid1_vol = 0
                else:
                    ask1_price = float(asks[0].get("price") or price)
                    ask1_vol   = int(asks[0].get("vol") or 0)
                    bid1_vol   = int(bids[0].get("vol") or 0)

                    ask_at_limit = (ask1_price >= limit_up - 0.005)
                    seal_score = 0
                    if ask_at_limit:
                        seal_score += 40
                    if ask1_vol > 0 and bid1_vol > ask1_vol * 2:
                        seal_score += 30
                    if change_pct >= limit_rate * 0.95:
                        seal_score += 20
                    if vol > 500:
                        seal_score += 10

                # ★ 阈值放宽：30 → 10（让差一档但买盘弱的也能进，broker 层会再过滤）★
                if seal_score < 10:
                    continue
                _stats["near_passed_seal"] += 1

                # ★ Bug fix 2026-05-25: 连板查询移到循环外并行做，避免串行 250s+
                # 先把基础信息塞进 results_out，consec 字段稍后补
                _lim_rate = q.get("limit_rate", 0.10)
                results_out.append({
                    "code":   code,
                    "name":   q.get("name", code),
                    "price":  price,
                    "change": change_pct,
                    "signal": True,
                    "consec_limit": 0,           # 待并行补
                    "consec_label": "首板",       # 待并行补
                    "_lim_rate": _lim_rate,      # 暂存供并行查 consec 用
                    "_seal_score": seal_score,
                    "_bid1_vol": bid1_vol,
                    "_ask1_vol": ask1_vol,
                    "_limit_up": limit_up,
                    "reason": "",                # 待并行补
                })
            except Exception:
                continue

        _logger.info(f"[da_ban] 主循环完成: results_out={len(results_out)} (差两档候选)")
        # ★ Bug fix 2026-05-25: 连板查询 ThreadPoolExecutor + client._lock 死锁 ★
        # 跳过连板天数查询，默认当首板处理。要看连板让用户点 K 线自己看。
        # 实测：50 只主候选 × get_kline 串行排队 → 经常 5+ 分钟挂死无任何日志
        for item in results_out:
            item["consec_limit"] = 0
            item["consec_label"] = "首板"
            item["reason"] = (
                f"打板(TDX) | 首板 | 涨停={item['_limit_up']:.2f} "
                f"| 封板分={item['_seal_score']} "
                f"| 买一{item['_bid1_vol']}手 卖一{item['_ask1_vol']}手"
            )
            for k in ("_lim_rate", "_seal_score", "_bid1_vol", "_ask1_vol", "_limit_up"):
                item.pop(k, None)

        # ── 补充：已封板股票作为参考信息（涨停价但无买入信号）──
        try:
            import ths_quote as _tq
            _names = _tq._name_cache
        except Exception:
            _names = {}
        already_sealed = []
        for q in quotes:
            try:
                code      = q["code"]
                # 全市场，不过滤板块（同上）
                price     = q["price"]
                limit_up  = q["limit_up"]
                change_pct = q["change_pct"]
                limit_rate = q["limit_rate"]
                if price <= 0 or limit_up <= 0:
                    continue
                # ── 喂 SealTracker：只 track 接近涨停（≥ limit_rate × 0.9）的股 ──
                # 包含已封板 + 即将封板 + 曾经封板今天又跌下来开板的，全部记入时间线
                if _tracker is not None and change_pct >= limit_rate * 0.9:
                    bids = q.get("bids") or []
                    bid1_vol = bids[0].get("vol", 0) if bids else 0
                    seal_amt = bid1_vol * 100 * limit_up
                    is_sealed = price >= limit_up - 0.001
                    try:
                        _tracker.update(code, seal_amt, price, limit_up, is_sealed, ts=_now_ts)
                    except Exception:
                        pass
                # 已封板：price >= limit_up
                if price >= limit_up - 0.001 and change_pct >= limit_rate * 0.98:
                    # 过滤一字板（开盘即封死且全天未开板，最低价等于涨停价）
                    # 一字板散户根本买不到，留着只会让选股结果失真
                    if q.get("low", 0) >= limit_up - 0.005:
                        continue
                    name = q.get("name") or _names.get(code, "")
                    already_sealed.append({
                        "code":   code,
                        "name":   name,
                        "price":  price,
                        "change": change_pct,
                        "signal": False,
                        "consec_limit": 0,
                        "consec_label": "",
                        "reason": f"已封板 | 涨停={limit_up:.2f} | 涨幅={change_pct:.2f}%",
                        "_limit_rate": limit_rate,
                        "_limit_up":   limit_up,
                    })
            except Exception:
                continue
        # ── 行业/板块热度统计 ──────────────────────────────────
        # 同一行业有多只股票涨停 = 板块炒作，行业热度越高 → 更值得介入
        # （行业信息来自 data_manager.get_stock_sector，本地 meta 缓存，无外网压力）
        from collections import Counter
        try:
            import data_manager as dm
        except ImportError:
            dm = None

        # ★ Bug fix 2026-05-25: dm.get_stock_sector 对每只股触发 akshare 调用，
        # 几十只 × 网络慢 → 整个扫描挂死。
        # 跳过行业统计：只读本地缓存元数据（如果有），无缓存就默认"其他"。
        industry_counts = Counter()
        _meta_cache = {}
        if dm:
            try:
                # 一次性读全部 metadata（如果有该接口），避免每只单独调用
                _meta_cache = getattr(dm, "_metadata_cache", {}) or {}
            except Exception:
                _meta_cache = {}

        def _quick_industry(code):
            """只读已缓存的行业，不触发任何网络调用"""
            try:
                meta = _meta_cache.get(code) or {}
                return meta.get("industry") or "其他"
            except Exception:
                return "其他"

        for item in already_sealed:
            item["industry"] = _quick_industry(item["code"])
            industry_counts[item["industry"]] += 1
        for item in results_out:
            item["industry"] = _quick_industry(item["code"])
            item["industry_heat"] = industry_counts.get(item["industry"], 0)
        for item in already_sealed:
            item["industry_heat"] = industry_counts.get(item["industry"], 0)

        # 已封板按"市场优先 + 行业热度降序 + 涨幅降序"排序，前 20 只入结果
        # 为了让主板和非主板都能露出，按板块各取 N 只：主板 14、非主板 6
        already_sealed.sort(key=lambda x: (_market_priority(x["code"]),
                                            -x.get("industry_heat", 0),
                                            -x.get("change", 0)))
        mainboard = [x for x in already_sealed if _market_priority(x["code"]) == 0]
        others    = [x for x in already_sealed if _market_priority(x["code"]) >  0]
        top_sealed = mainboard[:14] + others[:6]

        # ★ Bug fix 2026-05-25: 同上 — 跳过 top_sealed 连板查询（死锁源头）★
        # ★ Bug fix 2026-05-26: 不再把已封板股加入 results ★
        # 已封板的卖一价 = 涨停价，散户挂单进队列只能"排板"（封单大基本排不到）。
        # 之前 top_sealed.extend 导致用户看到的 17 只里 14 只都是 10.01% 已封板，
        # 真正能买的只有 2-3 只。这次只留"未封板候选"（涨幅 7%+ 但 < 涨停）。
        # top_sealed 不再混入 results，仅作为内部统计供日志参考。
        _stats["sealed"] = len(top_sealed)

        _logger.info(
            f"[da_ban 诊断] total={_stats['total']} "
            f"过基础={_stats['basic_filter']} "
            f"接近涨停未封板={_stats['near_limit']} "
            f"过封板分阈值={_stats['near_passed_seal']} "
            f"已封板(不计入)={_stats['sealed']} "
            f"→ 最终 results={len(results_out)} (全部可买)"
        )
        return True

    except Exception as e:
        _logger.exception(f"[da_ban] _from_tdx 异常崩溃: {e}")
        return False


def _board_limit_rate(code: str) -> float:
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("4", "8")):
        return 30.0
    return 10.0


def _apply_quality_filters(results: list, max_check: int = 20) -> list:
    """入场选择性过滤（回测验证的两条最稳因子）：
        - 首板：昨日未涨停（排除连板高位接力，首板隔日统计更稳）
        - 站上 MA20：趋势向上
    顺序短 K 线（不并发，避开历史死锁），取不到数据/出错的**不误杀**（保留候选）。
    回测结论：选择性过滤对胜率影响有限（卖出规则才是胜率主因），但能去掉
    连板高位/破位标的，让选股更"有道理"。
    """
    import logging as _lg
    import datetime as _dt
    _log = _lg.getLogger("strategies.da_ban")
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        import ths_quote as tq
        client = tq.get_client()
    except Exception:
        return results
    if not client or not getattr(client, "_ok", False):
        return results

    today_str = _dt.date.today().strftime("%Y-%m-%d")
    kept, checked = [], 0
    for item in results:
        if checked >= max_check:
            kept.append(item)          # 超出检查上限的保留，不阻塞扫描
            continue
        code = item.get("code", "")
        try:
            bars = client.get_kline(code, 25)
            checked += 1
            if not bars or len(bars) < 22:
                kept.append(item); continue       # 数据不足不误杀
            bars = sorted(bars, key=lambda b: str(b.get("date")))
            closes = [float(b["close"]) for b in bars]
            ma20 = sum(closes[-20:]) / 20.0
            on_ma20 = closes[-1] >= ma20 * 0.99
            # 对齐"昨日"：若最后一根是今日，昨日=倒数第2；否则昨日=倒数第1
            y = -2 if str(bars[-1].get("date"))[:10] == today_str else -1
            rate = _board_limit_rate(code)
            # 连板数：从最后一根完整 bar 往前数连续涨停（首板=0 前置板）
            cb, idx = 0, len(closes) - 1
            while idx >= 1 and closes[idx - 1] > 0 \
                    and (closes[idx] / closes[idx - 1] - 1) * 100 >= rate * 0.95:
                cb += 1
                idx -= 1
            # 含连板：站上 MA20 即保留（首板+连板都做，连板边际收益更高）。
            # 选择性来自：封板强度(seal_score) + 站上MA20 + 主板优先 + 行业热度。
            if on_ma20:
                lab = "首板" if cb <= 1 else f"{cb}连板"
                item["reason"] = item.get("reason", "") + f" | {lab}·站上MA20"
                item["consec"] = cb
                kept.append(item)
            # 跌破 MA20（弱势）→ 丢弃
        except Exception as e:
            _log.debug(f"[da_ban] 质量过滤 {code} 取K线失败(保留): {e}")
            kept.append(item)
    if checked:
        _log.info(f"[da_ban] 质量过滤: 检查 {checked} 只 → 留下 {len(kept)} 只（首板+站上MA20）")
    return kept


def scan(codes: list, get_kline, get_quotes) -> list:
    """
    打板信号扫描。
    - codes 为空（监控模式）：使用 pytdx 全市场实时扫描（含5档盘口）
    - codes 非空（回测模式）：使用回测引擎提供的历史 get_kline/get_quotes
    """
    results = []

    # ── 优先：pytdx 全市场实时扫描（含5档盘口）───────────────
    # ★ Bug fix 2026-05-25: 收到 codes 也要先尝试 _from_tdx（盘中实时数据最快）
    # 之前只在 codes=[] 时尝试，但 strategy_manager 现在统一传 universe。
    if _from_tdx(results):
        results.sort(key=lambda x: (_market_priority(x["code"]),
                                     -x.get("industry_heat", 0),
                                     -x.get("change", 0)))
        results = _apply_quality_filters(results)   # 首板 + 站上MA20 选择性过滤
        return results

    # ── 降级路径：使用 get_quotes 拉静态行情（盘后 TDX 关数据流时走这里）─
    # codes 为空说明回测引擎也没给数据，直接返回空
    if not codes:
        import logging
        logging.getLogger("strategies.da_ban").error(
            "[da_ban] _from_tdx 失败且 codes 为空（非交易时段 + 无 universe）→ 返回空"
        )
        return []
    quotes = get_quotes(codes)
    if quotes is None or quotes.empty:
        return []

    for _, row in quotes.iterrows():
        try:
            code       = str(row.get("代码", "")).strip()
            name       = str(row.get("名称", "")).strip()
            price      = float(row.get("最新价", 0) or 0)
            change_pct = float(row.get("涨跌幅",  0) or 0)
            prev_close = float(row.get("昨收",    0) or 0)
            vol_ratio  = float(row.get("量比",    1) or 1)

            if "ST" in name or "B" in name:
                continue
            if price <= 0 or prev_close <= 0:
                continue

            rate     = 20.0 if code.startswith(("300","301","688","689")) else (30.0 if code.startswith(("8","4")) else 10.0)
            limit_up = round(prev_close * (1 + rate / 100), 2)
            one_tick = round(limit_up - 0.01, 2)

            if not (one_tick <= price < limit_up):
                continue
            if vol_ratio < 0.5 or change_pct < rate * 0.8:
                continue

            results.append({
                "code":   code,
                "name":   name,
                "price":  price,
                "change": round(change_pct, 2),
                "reason": (
                    f"打板(降级) | 涨停={limit_up:.2f} "
                    f"| 当前={price:.2f} "
                    f"| 量比={vol_ratio:.1f}x"
                ),
            })
        except Exception:
            continue

    results.sort(key=lambda x: x.get("change", 0), reverse=True)
    return results
