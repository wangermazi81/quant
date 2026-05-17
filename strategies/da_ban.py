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
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        import ths_quote as tq

        client = tq.get_client()
        if not client or not client._ok:
            return False

        quotes = client.get_full_market_quotes()
        if not quotes:
            return False

        for q in quotes:
            try:
                code       = q["code"]
                price      = q["price"]
                last_close = q["last_close"]
                limit_up   = q["limit_up"]
                change_pct = q["change_pct"]
                vol        = q["vol"]          # 手
                asks       = q["asks"]         # 卖一～卖五
                bids       = q["bids"]         # 买一～买五

                if price <= 0 or last_close <= 0 or limit_up <= 0:
                    continue

                one_tick = round(limit_up - 0.01, 2)

                # ── 核心条件：差一档（未封板）────────────────
                if not (one_tick <= price < limit_up):
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

                # 封板强度 >= 50 才考虑介入
                if seal_score < 50:
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
                price     = q["price"]
                limit_up  = q["limit_up"]
                change_pct = q["change_pct"]
                limit_rate = q["limit_rate"]
                if price <= 0 or limit_up <= 0:
                    continue
                # 已封板：price >= limit_up
                if price >= limit_up - 0.001 and change_pct >= limit_rate * 0.98:
                    name = q.get("name") or _names.get(code, "")
                    # 已封板仅显示参考，不做额外kline请求（避免拖慢全市场扫描）
                    already_sealed.append({
                        "code":   code,
                        "name":   name,
                        "price":  price,
                        "change": change_pct,
                        "signal": False,
                        "consec_limit": 0,
                        "consec_label": "",
                        "reason": f"已封板 | 涨停={limit_up:.2f} | 涨幅={change_pct:.2f}%",
                    })
            except Exception:
                continue
        # 只追加前20只已封板（避免刷屏）
        already_sealed.sort(key=lambda x: x.get("change", 0), reverse=True)
        results_out.extend(already_sealed[:20])

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
            results.sort(key=lambda x: x.get("change", 0), reverse=True)
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
