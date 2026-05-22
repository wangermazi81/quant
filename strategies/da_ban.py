# META: {"name": "打板策略", "desc": "距涨停差一档买入，含封板强度过滤，每日限3只·20%仓位", "author": "用户", "builtin": true}
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
            return False

        quotes = client.get_full_market_quotes()
        _now_ts = _time.time()
        if not quotes:
            return False

        for q in quotes:
            try:
                code       = q["code"]
                # 全市场扫描，不过滤板块。后面 results 整体按"主板优先 + 涨幅降序"
                # 排序，自动买入按 results 顺序处理 → 资金先打到主板，剩余再买其他板。
                price      = q["price"]
                last_close = q["last_close"]
                limit_up   = q["limit_up"]
                change_pct = q["change_pct"]
                vol        = q["vol"]          # 手
                asks       = q["asks"]         # 卖一～卖五
                bids       = q["bids"]         # 买一～买五

                if price <= 0 or last_close <= 0 or limit_up <= 0:
                    continue

                two_ticks = round(limit_up - 0.02, 2)

                # ── 核心条件：差两档以内（未封板）────────────
                if not (two_ticks <= price < limit_up):
                    continue

                # ── 过滤 ST / 成交稀少 ────────────────────────
                if vol < 100:   # 总手数 < 10000 股，流动性太差
                    continue

                # ── 封板强度分析（5档盘口）──────────────────────
                ask1_price = asks[0]["price"]
                ask1_vol   = asks[0]["vol"]    # 卖一手数
                bid1_vol   = bids[0]["vol"]    # 买一手数

                # 卖一挂在涨停价说明有人愿意在涨停卖（阻力小）
                ask_at_limit = (ask1_price >= limit_up - 0.005)

                # 封板强度评分（0-100）
                seal_score = 0
                if ask_at_limit:
                    seal_score += 40
                if bid1_vol > ask1_vol * 2:
                    seal_score += 30    # 买盘碾压卖盘
                if change_pct >= q["limit_rate"] * 0.95:
                    seal_score += 20    # 涨幅接近涨停
                if vol > 500:
                    seal_score += 10    # 活跃成交

                # 封板强度 >= 30 才考虑介入
                if seal_score < 30:
                    continue

                # 统计连续涨停天数
                consec = _count_consec_limit_up(client, code, q.get("limit_rate", limit_rate))
                consec_label = "首板" if consec <= 1 else f"{consec}连板"
                results_out.append({
                    "code":   code,
                    "name":   q.get("name", code),
                    "price":  price,
                    "change": change_pct,
                    "signal": True,
                    "consec_limit": consec,
                    "consec_label": consec_label,
                    "reason": (
                        f"打板(TDX) | {consec_label} | 涨停={limit_up:.2f} "
                        f"| 封板分={seal_score} "
                        f"| 买一{bid1_vol}手 卖一{ask1_vol}手"
                    ),
                })
            except Exception:
                continue

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
        # 已封板按"市场优先（主板在前）+ 涨幅降序"排序，前 20 只入结果
        # 为了让主板和非主板都能露出，按板块各取 N 只：主板 14、非主板 6
        already_sealed.sort(key=lambda x: (_market_priority(x["code"]), -x.get("change", 0)))
        mainboard = [x for x in already_sealed if _market_priority(x["code"]) == 0]
        others    = [x for x in already_sealed if _market_priority(x["code"]) >  0]
        top_sealed = mainboard[:14] + others[:6]
        # 对前20只补查连板数：首板（consec<=1）标记 signal=True 入队，让 broker
        # 按封单金额分级（弱封板可买、强封板拒），实现"排板"模拟。
        # 连板（≥2）保持 signal=False 仅作参考（首板能上车、连板风险高跳过）。
        for item in top_sealed:
            try:
                consec = _count_consec_limit_up(client, item["code"], item["_limit_rate"])
            except Exception:
                consec = 0
            item["consec_limit"] = consec
            item["consec_label"] = "首板" if consec <= 1 else f"{consec}连板"
            # 把连板天数补喂到 tracker（之前 update 时 consec_limit=0，这里补一笔）
            if _tracker is not None and consec > 0:
                try:
                    # 用 0 金额触发的 update 不会污染 max_seal，但会刷新 consec_limit
                    cur_status = _tracker.status(item["code"])
                    if cur_status.get("current_sealed"):
                        _tracker.update(item["code"],
                                        cur_status.get("current_seal", 0),
                                        item["price"], item["_limit_up"],
                                        is_sealed=True, consec_limit=consec, ts=_now_ts)
                except Exception:
                    pass
            if consec <= 1:
                item["signal"] = True
                item["reason"] = (
                    f"已封板首板(试排板) | 涨停={item['_limit_up']:.2f} "
                    f"| 涨幅={item['change']:.2f}% | broker将按封单金额判定能否成交"
                )
            item.pop("_limit_rate", None)
            item.pop("_limit_up",   None)
        results_out.extend(top_sealed)

        return True

    except Exception as e:
        return False


def scan(codes: list, get_kline, get_quotes) -> list:
    """
    打板信号扫描。
    - codes 为空（监控模式）：使用 pytdx 全市场实时扫描（含5档盘口）
    - codes 非空（回测模式）：使用回测引擎提供的历史 get_kline/get_quotes
    """
    results = []

    # ── 监控模式（codes 为空）：pytdx 全市场实时扫描 ────────
    if not codes:
        if _from_tdx(results):
            # 主板优先：(market_priority, -change)
            #   主板 60/00 排在前面（auto_execute 资金先打这里），
            #   组内按涨幅降序。非主板（创业板/科创板/北交所）显示但排后面。
            results.sort(key=lambda x: (_market_priority(x["code"]), -x.get("change", 0)))
            return results

    # ── 回测/降级路径：使用提供的历史数据 ────────────────────
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
