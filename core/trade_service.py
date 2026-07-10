# -*- coding: utf-8 -*-
"""核心交易逻辑（per-user，从 main.py 下沉，工单4）。

统一买卖入口（模拟盘/实盘信号队列）、加仓追板、打板 Ride / 账户默认风控自动卖出。
函数体与 main.py 原实现逐行等价（仅修复 _check_add_position 里引用了
未定义名 `_logger` 的 NameError 隐患 → 改用本模块 logger）。

测试打桩注意：本模块在 import 时绑定 config.DRY_RUN，
需要改变交易模式的测试请 monkeypatch.setattr(trade_service, "DRY_RUN", ...)。
"""
import logging
from datetime import datetime

from app_state import get_user_state, _add_log
from config import DRY_RUN
from core import user_manager as um
from core import sell_rules
from core import daban_strategy_runtime as daban_runtime
from core.daban_state import (
    _DABAN_PEAKS, _LIVE_SELL_PENDING, _daban_ride_decision, _save_daban_peaks,
)
from broker import paper_broker as pb

logger = logging.getLogger(__name__)


def _reset_daily_if_needed(uid: int):
    """每个交易日开始时清零今日计数 + 过期清理隔日遗留信号。"""
    today = datetime.now().strftime("%Y-%m-%d")
    s = get_user_state(uid)
    if s["today_date"] != today:
        s["today_date"]   = today
        s["today_trades"] = 0
        _add_log(uid, f"📅 新交易日 {today}，今日开仓次数已清零")
        # 隔日未成交的 pending/sent/submitted 信号批量置 expired，
        # 否则会累进资金检查（曾出现"已用仓位9848%"封死开仓）且永久阻塞同名票去重
        try:
            n = um.expire_stale_signals(uid)
            if n:
                _add_log(uid, f"🧹 清理隔日遗留信号 {n} 条（已置为过期）")
        except Exception as _ee:
            logger.warning(f"[uid={uid}] 清理隔日信号失败: {_ee}")


def execute_buy(uid: int, code: str, name: str, price: float, shares: int,
                reason: str = "", strategy: str = "") -> dict:
    """统一买入入口（模拟盘 / 实盘）。返回 {"ok": bool, "msg": str, ...}"""
    s = get_user_state(uid)

    # 获取用户当前的券商配置
    broker_cfg = um.get_user_broker_config(uid)
    broker_type = broker_cfg.get("broker_type", "paper")

    if DRY_RUN or broker_type == "paper":
        # ── 模拟盘逻辑 ──
        broker = pb.get_broker(uid)
        buy_strategy = strategy or s.get("active_strategy") or ""
        result = broker.submit_buy(code, name, price, shares, reason=reason,
                                   strategy=buy_strategy)
        if result.get("ok"):
            s["executed"]    += 1
            s["today_trades"] = s.get("today_trades", 0) + 1
            s["positions"][code] = {
                "name": name, "shares": shares, "cost": result["exec_price"],
                "date": datetime.now().strftime("%Y-%m-%d"),
                "strategy": buy_strategy,
            }
            _add_log(uid,
                f"✅ [模拟] 买入 {code} {name} {shares}股 @{result['exec_price']:.2f} "
                f"花费¥{result['total_cost']:,.0f} 佣金¥{result['commission']:.1f}", "buy")
            return {"ok": True, "exec_price": result["exec_price"],
                    "total_cost": result["total_cost"]}
        else:
            msg = result.get("msg", "")
            _add_log(uid, f"❌ [模拟] 买入拒绝 {code}: {msg}", "error")
            return {"ok": False, "msg": msg}
    else:
        # ── 实盘逻辑：写入信号队列 ──
        signal_id = um.add_trade_signal(uid, code, name, "BUY", price, shares)
        _add_log(uid, f"⏳ 实盘买入信号已发送队列: {code} {name} {shares}股 @{price:.2f}", "info")
        return {"ok": True, "msg": "已生成实盘买入信号，等待本地执行...", "signal_id": signal_id}


def execute_sell(uid: int, code: str, price: float, sell_reason: str = "手动卖出") -> dict:
    """统一卖出入口。"""
    s   = get_user_state(uid)
    pos = s["positions"].get(code)

    # 获取用户当前的券商配置
    broker_cfg = um.get_user_broker_config(uid)
    broker_type = broker_cfg.get("broker_type", "paper")

    if DRY_RUN or broker_type == "paper":
        # ── 模拟盘逻辑 ──
        broker = pb.get_broker(uid)
        result = broker.submit_sell(code, price, sell_reason=sell_reason)
        if result.get("ok"):
            s["executed"] += 1
            s["positions"].pop(code, None)
            pnl  = result.get("pnl", 0)
            sign = "✅" if pnl >= 0 else "❌"
            _add_log(uid,
                f"{sign} [模拟] 卖出 {code} @{result['exec_price']:.2f} "
                f"盈亏¥{pnl:+,.2f}({result.get('pnl_pct',0):+.2f}%) 原因:{sell_reason}",
                "buy" if pnl >= 0 else "error")
        return result
    else:
        # ── 实盘逻辑：写入信号队列 ──
        shares = pos["shares"] if pos else 0
        name = pos["name"] if pos else ""
        if shares <= 0:
            return {"ok": False, "msg": "无持仓"}
        signal_id = um.add_trade_signal(uid, code, name, "SELL", price, shares)
        _add_log(uid, f"⏳ 实盘卖出信号已发送队列: {code} {name} {shares}股 @{price:.2f}", "info")
        return {"ok": True, "msg": "已生成实盘卖出信号，等待本地执行...", "signal_id": signal_id}


# ── 加仓逻辑（涨幅到9%时以涨停价追板）──────────────────────
_ADD_POSITION_LAYERS: dict = {}  # (uid, code) -> 已加仓层数

def _check_add_position(uid: int, s: dict, broker_type: str, base_cash: float):
    """检查持仓票是否涨到9%，触发加仓。每次扫描时调用。"""
    positions = s.get("positions", {})
    if not positions:
        return
    try:
        from market import ths_quote as tq
        client = tq.get_client()
        if not client or not client._ok:
            return
        codes = list(positions.keys())
        quotes = client.get_quotes(codes) or []
        for q in quotes:
            try:
                code = q.get("code", "")
                if not code:
                    continue
                price = float(q.get("price", 0) or 0)
                last_close = float(q.get("last_close", 0) or 0)
                limit_up = float(q.get("limit_up", 0) or 0)
                if price <= 0 or last_close <= 0 or limit_up <= 0:
                    continue
                change_pct = (price - last_close) / last_close * 100
                # 涨幅到9%触发加仓
                if change_pct < 9.0:
                    continue
                # 检查已加仓层数（最多2.5层）
                key = (uid, code)
                current_layers = _ADD_POSITION_LAYERS.get(key, 1.0)  # 初始1层
                if current_layers >= 3.0:
                    continue
                # 计算加仓金额（从当前层加到3层=25%）
                target_ratio = 0.25
                current_ratio = current_layers * 0.08
                add_ratio = target_ratio - current_ratio
                add_amount = base_cash * add_ratio
                add_shares = max(int(add_amount / limit_up / 100) * 100, 100)
                if add_shares <= 0:
                    continue
                # 以涨停价挂单
                _add_log(uid, f"?? 加仓触发: {code} 涨幅{change_pct:.1f}% ≥ 9%，以涨停价{limit_up:.2f}追板，加仓至25%", "buy")
                name = positions[code].get("name", code)
                res = execute_buy(uid, code, name, limit_up, add_shares, reason="加仓追板")
                if res.get("ok"):
                    _ADD_POSITION_LAYERS[key] = 3.0  # 加到3层=25%
                    _add_log(uid, f"?? 加仓成功: {code} 已加至3层(25%)", "info")
                else:
                    _add_log(uid, f"? 加仓失败: {code} {res.get('msg', '')}", "error")
            except Exception as _e:
                logger.debug(f"加仓检查异常 {q.get('code')}: {_e}")
                continue
    except Exception as e:
        logger.debug(f"加仓检查整体异常: {e}")


def _auto_buy(uid: int, item: dict):
    """自动买入（信号触发后立即执行）。

    队列状态必须反映真实情况：
      模拟盘成交即 done（已买-模拟）；
      实盘只是生成了信号 → submitted（已报实盘，等交易机回传），
      成交确认后由 bridge_report 把队列项推进到 executed。
    """
    code   = item["code"]
    name   = item.get("name", "")
    price  = item["price"]
    shares = item.get("shares", 100)
    try:
        _add_log(uid, f"🤖 自动买入触发: {code} {name} {shares}股 @{price}", "buy")
        res = execute_buy(uid, code, name, price, shares, reason="自动买入",
                          strategy=item.get("strategy") or "")
        if not res.get("ok"):
            item["status"] = "error"
            item["error_msg"] = res.get("msg", "")
        elif res.get("signal_id"):
            item["status"] = "submitted"          # 实盘：已报，未确认成交
            item["signal_id"] = res["signal_id"]
        else:
            item["status"] = "done"               # 模拟：即时成交
    except Exception as e:
        item["status"]    = "error"
        item["error_msg"] = str(e)
        logger.exception(f"[uid={uid}] 自动买入异常: {code}")
        _add_log(uid, f"❌ 自动买入异常: {code} — {e}", "error")


def _check_live_daban_ride(uid: int):
    """实盘自动卖出（覆盖台账全部持仓，不再只管 da_ban）：
      - strategy=da_ban 的持仓 → 打板 Ride 卖法（吃连板+紧止损+回落卖）；
      - 其他持仓（含无策略标签的历史买入）→ 账户默认止损/止盈/超期。
        旧版只过滤 da_ban，无标签持仓从来不会被自动卖出（-17%还躺着就是这个原因）。

    触发则生成 SELL 信号（桥接器在 xiadan 执行）。遵守 T+1。已发卖出信号未成交的
    持仓不重复发（_LIVE_SELL_PENDING 去重，卖出成交/失败时在 report 处清除）。
    """
    try:
        positions = um.get_live_positions(uid)
    except Exception as e:
        logger.warning(f"[实盘Ride uid={uid}] 读台账失败: {e}")
        return
    if not positions:
        return
    # 账户默认风控参数（非打板持仓用；与模拟盘 check_risk_rules 同一套设置）
    try:
        _bcfg = (um.get_user_broker_config(uid) or {}).get("config") or {}
    except Exception:
        _bcfg = {}
    _acct_stop = float(_bcfg.get("stop_loss") or -0.05)
    _acct_take = float(_bcfg.get("take_profit") or 0.10)
    _acct_hold = int(_bcfg.get("max_hold_days") or 10)

    codes = [p["code"] for p in positions]
    qmap = {}
    try:
        from market import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            for q in client.get_quotes(codes):
                if q.get("code"):
                    qmap[q["code"]] = q
    except Exception as e:
        logger.debug(f"[实盘Ride uid={uid}] 行情获取失败: {e}")
        return
    import datetime as _dt
    today = _dt.date.today()
    for p in positions:
        code = p["code"]
        if (uid, code) in _LIVE_SELL_PENDING:        # 已发卖出信号、等成交
            continue
        q = qmap.get(code)
        if not q:
            continue
        cur = float(q.get("price") or 0)
        last_close = float(q.get("last_close") or 0)
        cost = float(p.get("buy_price", 0) or 0)
        if cur <= 0 or cost <= 0:
            continue
        try:
            bd = _dt.datetime.strptime(str(p.get("buy_date"))[:10], "%Y-%m-%d").date()
            hold = (today - bd).days
        except Exception:
            hold = 1
        if hold < 1:                                  # T+1：当天买入不卖
            continue

        strategy = p.get("strategy") or ""
        is_daban = daban_runtime.is_daban_ride_strategy(strategy)
        if is_daban:
            sell, reason, new_peak = _daban_ride_decision(
                code, cost, cur, last_close, hold, float(p.get("peak") or cost), strategy)
            if new_peak > float(p.get("peak") or 0):
                try:
                    um.update_live_peak(uid, code, new_peak)
                except Exception:
                    pass
        else:
            # 账户默认止损/止盈/超期（决策见 core/sell_rules.account_exit；与 paper_broker 同源）
            pnl = (cur - cost) / cost
            sell, reason = sell_rules.account_exit(
                pnl, hold, _acct_stop, _acct_take, _acct_hold, thr_decimals=0)
        if not sell:
            continue
        shares = int(p.get("shares", 0) or 0)
        if shares <= 0:
            continue
        try:
            tag = "实盘Ride" if is_daban else "实盘风控"
            sid = um.add_trade_signal(uid, code, p.get("name", ""), "SELL", cur, shares)
            _LIVE_SELL_PENDING.add((uid, code))
            _add_log(uid, f"📤 [{tag}] {code} {p.get('name','')} {reason} → 生成卖出信号"
                          f"(#{sid}, {shares}股 @{cur:.2f})，等桥接器执行", "info")
        except Exception as e:
            logger.warning(f"[实盘风控 uid={uid}] 生成卖出信号失败 {code}: {e}")


def _check_daban_ride(uid: int, broker, positions: list, qmap: dict):
    """打板 Ride 卖出：吃连板（当日仍封板则持有，让利润奔跑）+ 初始止损 + 从最高回落卖。

    这才是打板正确的非对称盈亏比卖法（回测盈亏比≈2.5、期望+3%/笔）：
      - 当日仍封板(连板继续) → 持有，让利润奔跑（不卖）；
      - 跌破成本 DABAN_STOP_LOSS(-5%) → 止损（砍得快）；
      - 从持仓期最高价回落 ≥ DABAN_TRAIL(7%) → 卖（吃完连板后保护利润）；
      - 持有满 DABAN_MAX_HOLD 天 → 卖（backstop）。
    遵守 T+1（当天买入不卖，submit_sell 也会兜底拒绝）。
    """
    s = get_user_state(uid)
    for p in positions:
        code = p.get("code", "")
        strategy = p.get("strategy") or s.get("active_strategy") or ""
        if not daban_runtime.is_daban_ride_strategy(strategy):
            continue
        cost = float(p.get("cost_price", 0) or 0)
        q    = qmap.get(code)
        if not q or cost <= 0:
            continue
        cur = float(q.get("price") or 0)
        last_close = float(q.get("last_close") or 0)
        if cur <= 0:
            continue
        hold = int(p.get("hold_days", 0) or 0)
        if hold < 1:                       # T+1：当天买入不卖
            continue
        key  = (uid, code)
        sell, reason, new_peak = _daban_ride_decision(
            code, cost, cur, last_close, hold, _DABAN_PEAKS.get(key, cost), strategy)
        if _DABAN_PEAKS.get(key) != new_peak:
            _DABAN_PEAKS[key] = new_peak
            _save_daban_peaks()          # 峰值变化即持久化（跟实盘一致，重启不丢）
        if not sell:
            continue
        res = broker.submit_sell(code, cur, sell_reason=reason)
        if res.get("ok"):
            pnl_amt = res.get("pnl", 0)
            sign = "✅" if pnl_amt >= 0 else "❌"
            _add_log(uid, f"{sign} [打板Ride] {code} {p.get('name','')} {reason} "
                          f"盈亏¥{pnl_amt:+,.2f}({res.get('pnl_pct',0):+.1f}%)",
                     "buy" if pnl_amt >= 0 else "error")
            s["positions"].pop(code, None)
            _DABAN_PEAKS.pop(key, None)
            _save_daban_peaks()


def _check_paper_risk(uid: int):
    """检查模拟盘持仓风控并自动卖出。打板策略用 Ride 卖法（吃连板+紧止损+回落卖，
    正确的非对称盈亏比），其它策略沿用账户默认（止损/止盈/超期）。
    仅对「模拟盘持仓」生效（实盘持仓在 xiadan，走另一条卖出链）。
    """
    try:
        broker_cfg = um.get_user_broker_config(uid)
        is_paper   = DRY_RUN or broker_cfg.get("broker_type", "paper") == "paper"
    except Exception:
        is_paper = DRY_RUN
    if not is_paper:
        return
    try:
        broker    = pb.get_broker(uid)
        positions = broker.get_positions()
        if not positions:
            return
        codes = [p["code"] for p in positions]
        qmap  = {}
        try:
            from market import ths_quote as tq
            client = tq.get_client()
            if client and client._ok:
                for q in client.get_quotes(codes):
                    if q.get("code"):
                        qmap[q["code"]] = q
        except Exception as _qe:
            logger.debug(f"[风控 uid={uid}] 实时行情获取失败: {_qe}")

        s = get_user_state(uid)
        active_strategy = s.get("active_strategy") or ""
        ride_positions = []
        default_positions = []
        for p in positions:
            pos_strategy = p.get("strategy") or active_strategy
            if daban_runtime.is_daban_ride_strategy(pos_strategy):
                ride_positions.append(p)
            else:
                default_positions.append(p)

        if ride_positions:
            _check_daban_ride(uid, broker, ride_positions, qmap)   # 打板 Ride 卖法
        if not default_positions:
            return
        # 其它策略：账户默认止损/止盈/超期
        default_codes = {p.get("code") for p in default_positions}
        cur_prices = {
            c: float(q.get("price") or 0)
            for c, q in qmap.items()
            if c in default_codes
        }
        triggered = broker.check_risk_rules(cur_prices)
        for t in triggered:
            pnl  = t.get("pnl", 0)
            sign = "✅" if pnl >= 0 else "❌"
            _add_log(uid,
                f"{sign} [风控] {t['code']} {t['name']} 触发{t['reason']} 盈亏¥{pnl:+,.2f}",
                "buy" if pnl >= 0 else "error")
            s["positions"].pop(t["code"], None)
    except Exception as e:
        logger.warning(f"[风控检查 uid={uid}] {e}")


def _paper_live_prices(broker) -> dict:
    """为模拟盘持仓拉取 TDX 实时价 {code: price}，用于实时市值/盈亏。失败返回 {}。"""
    try:
        positions = broker.get_positions()
        if not positions:
            return {}
        from market import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            codes = [p["code"] for p in positions]
            qs    = client.get_quotes(codes)
            return {q["code"]: q.get("price", 0) for q in qs if q.get("price", 0) > 0}
    except Exception:
        pass
    return {}
