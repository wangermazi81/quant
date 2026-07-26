# -*- coding: utf-8 -*-
"""自动扫描编排（从 main.py 下沉，工单4）。

do_scan：对单个用户执行一次「风控自动卖出 → 策略选股 → 仓位护栏 → 入队/自动买入」。
_scan_thread_worker：单用户扫描线程入口（防重复并发 + 异常兜底清进度条）。
函数体与 main.py 原实现逐行等价。

测试打桩注意：本模块在 import 时绑定 config.DRY_RUN 与
market_data.is_trading_time，相关测试请 monkeypatch 本模块属性
（scan_service.is_trading_time / scan_service.DRY_RUN）。
"""
from __future__ import annotations
import logging
import threading
from datetime import datetime

from app_state import get_user_state, _add_log, _sse_push
from config import DRY_RUN
from core import user_manager as um
from core import notification_manager as nm
from core import order_sizing
from core import daban_strategy_runtime as daban_runtime
from core.trade_service import (
    _reset_daily_if_needed, _check_paper_risk, _check_live_daban_ride,
    _check_add_position, _auto_buy,
)
from market.market_data import is_trading_time
from strategy_engine import strategy_manager as sm
from strategy_engine.strategy import scan_all

logger = logging.getLogger(__name__)


def do_scan(uid: int):
    """对单个用户执行一次扫描。"""
    s = get_user_state(uid)
    if not s["running"] or not is_trading_time():
        return
    _reset_daily_if_needed(uid)
    _check_paper_risk(uid)        # 模拟盘持仓风控/Ride 自动卖出
    _check_live_daban_ride(uid)   # 实盘打板持仓 Ride 自动卖出（生成卖出信号）

    # ── 仓位规定跟随策略（写在策略 META 的 position 字段）────────────
    # 原硬编码"80%兜底+保留20%现金"已移除：它按金额累加待成交信号，实盘信号
    # 卡在已报状态时每轮扫描刷屏"已用仓位90%"且与策略口径冲突。
    # 现在：策略声明 max_daily（如打板=每日最多5个平均仓）→ 按当日开仓「数量」限额。
    _broker_cfg = um.get_user_broker_config(uid)
    _broker_type = _broker_cfg.get("broker_type", "paper")
    if _broker_type == "live" and not DRY_RUN:
        _live_cash = float((_broker_cfg.get("config") or {}).get("live_cash") or 0)
        _base_cash = _live_cash if _live_cash > 0 else s["capital"]
    else:
        _base_cash = s["capital"]

    _pos_cfg = sm.get_position_config(s.get("active_strategy") or "", user_id=uid)
    _max_daily = int(_pos_cfg.get("max_daily") or 0)
    if _max_daily > 0:
        if _broker_type == "live" and not DRY_RUN:
            _opened_today = um.count_today_open_codes(uid)   # 已报未成交的也占名额
        else:
            _opened_today = int(s.get("today_trades") or 0)
        if _opened_today >= _max_daily:
            _today_str = datetime.now().strftime("%Y-%m-%d")
            if s.get("_daily_limit_logged") != _today_str:    # 只提示一次，不刷屏
                _add_log(uid, f"🛡 策略日内限额：今日已开 {_opened_today}/{_max_daily} 仓，"
                              f"按策略规定不再开新仓（明日自动重置）", "info")
                s["_daily_limit_logged"] = _today_str
            s["scan_progress"] = {"running": False, "stage": "今日仓位已满", "current": 0, "total": 0}
            return

    # ★ 加仓追板：仅当策略 META 显式声明 add_on=true 才启用。
    #   打板策略=每日5个平均仓位，不加仓 —— 原无条件加仓到25%与策略口径冲突，已停用。
    if _pos_cfg.get("add_on"):
        _check_add_position(uid, s, _broker_type, _base_cash)

    active    = s.get("active_strategy")
    is_daban  = daban_runtime.is_daban_ride_strategy(active)
    scan_label = "全市场"
    _add_log(uid, f"扫描 {scan_label} 策略=[{active or '默认'}]...")

    s["scan_progress"] = {
        "running": True,
        "stage":   f"扫描{scan_label}",
        "current": 0,
        "total":   5200 if is_daban else 0,
    }

    if active:
        # max_codes=0 触发真·全市场扫描（不再裁剪 600 只）
        def _on_strategy_progress(progress):
            if not isinstance(progress, dict):
                return
            s["scan_progress"] = {
                "running": bool(progress.get("running")),
                "stage": progress.get("stage") or "",
                "current": int(progress.get("current") or 0),
                "total": int(progress.get("total") or 0),
                "code": progress.get("code") or "",
            }

        ret = sm.run(active, [], max_codes=0, user_id=uid, progress_callback=_on_strategy_progress)
        results = [{
            "code":        r.get("code", ""),
            "name":        r.get("name", ""),
            "price":       r.get("price", 0),
            "change":      r.get("change", 0),
            "pass_count":  5,
            "signal":      r.get("signal", True),
            "consec_limit": r.get("consec_limit", 0),
            "consec_label": r.get("consec_label", ""),
            "reason":      r.get("reason", ""),
            "position_ratio": r.get("position_ratio", 0),
            "conditions":  {},
        } for r in ret.get("results", [])]
        s["scanned"] += ret.get("scanned", ret.get("count", 0))
    else:
        results = scan_all([])
        s["scanned"] += len(results)

    s["pool"] = results

    added = 0
    buy_candidates = [r for r in results if r.get("signal", True)]
    # 仓位基数：实盘走 live_cash，模拟走 s["capital"]（默认 1000000 虚拟资金）
    _broker_cfg = um.get_user_broker_config(uid)
    _broker_type = _broker_cfg.get("broker_type", "paper")
    if _broker_type == "live" and not DRY_RUN:
        _live_cash = float((_broker_cfg.get("config") or {}).get("live_cash") or 0)
        if _live_cash > 0:
            _base_cash = _live_cash
            _add_log(uid, f"💰 [自动扫描] 实盘资金基数 ¥{_base_cash:,.0f} × {s['position_ratio']*100:.0f}% 仓位", "info")
        else:
            _add_log(uid, "⚠️ 实盘模式但未配置 live_cash，本轮自动入队跳过。请在顶部切换实盘 Modal 输入 xiadan 真账户余额", "error")
            s["scan_progress"] = {"running": False, "stage": "等待配置", "current": 0, "total": 0}
            return
    else:
        _base_cash = s["capital"]

    # ★ 资金检查：可用资金只做"够不够下这一单"的闸门，不再作为仓位比例基数。
    # 旧逻辑 _base_cash = 可用资金 → 每仓 = 可用×比例，越买后面的仓越小
    # （实测5仓变成 45k/45k/10k/10k/9k，完全不是"平均仓位"）。
    # 现在：每仓 = 全仓总资金 × 比例，所有仓位等大；可用不足该仓金额时停止开仓。
    _pending_buy_total = um.get_pending_buy_total(uid)
    _available_cash = _base_cash - _pending_buy_total
    if _available_cash < 1000:  # 最小一手金额估算（不足则本轮不开新仓）
        _add_log(uid, f"⏭ 资金不足：总额¥{_base_cash:,.0f} - 待成交¥{_pending_buy_total:,.0f} = 可用¥{_available_cash:,.0f}，本轮跳过", "info")
        s["scan_progress"] = {"running": False, "stage": "资金不足", "current": 0, "total": 0}
        return

    # ── 仓位规则：策略 META 优先，账户面板设置兜底 ─────────────────────
    # 策略声明了 position 字段（如打板=每日5个平均仓）时以策略为准；
    # 未声明的策略沿用前端面板/config 默认值。
    try:
        from config import MAX_POSITIONS as _CFG_POS, SINGLE_POSITION_MAX_RATIO as _CFG_RATIO
    except Exception:
        _CFG_POS, _CFG_RATIO = 5, 0.20
    _q_pos_cfg = sm.get_position_config(s.get("active_strategy") or "", user_id=uid)
    _q_max_daily = int(_q_pos_cfg.get("max_daily") or 0)
    _MAX_POS  = int(_q_pos_cfg.get("max_hold") or 0) or int(s.get("max_positions") or _CFG_POS)
    _SP_RATIO = float(s.get("single_position_max_ratio") or _CFG_RATIO)
    # 平均仓位：每仓 = 可用资金 / max_daily（策略规定的口径，优先于面板仓位比）
    _strat_ratio_equal = (1.0 / _q_max_daily) if (
        _q_max_daily > 0 and (_q_pos_cfg.get("sizing") or "equal") == "equal") else 0.0
    # 已占用的持仓名额 = 实际持仓 + 队列里待成交/已下单的买单（按 code 去重，避免双算）
    if _broker_type == "live":
        try:
            _occupied = {p["code"] for p in um.get_live_positions(uid)}
        except Exception:
            _occupied = set()
        # OCR 实际持仓（查实盘账户时缓存，当日有效）：实际已买入的票绝不再次买入，
        # 即使买入回传丢失/误报失败、台账里没有
        _ocr = s.get("live_ocr_pos") or {}
        if _ocr.get("date") == datetime.now().strftime("%Y-%m-%d"):
            _occupied |= {c for c in (_ocr.get("codes") or []) if c}
    else:
        _occupied = set((s.get("positions") or {}).keys())
    # 仅计 pending（待执行）买单：done 的买单已落到持仓/台账，由上面持仓侧统计，
    # 再从队列计会在"会话内买入又卖出"后被 done 残留记录永久错占名额。
    _occupied |= {q["code"] for q in s["queue"]
                  if q.get("action") == "buy" and q.get("status") == "pending"}
    # 单笔仓位：策略平均仓位优先（策略明确规定的口径不再被面板/护栏夹），
    # 策略未声明时用面板 position_ratio 并夹到护栏上限内
    if _strat_ratio_equal > 0:
        _eff_ratio = _strat_ratio_equal
    else:
        _eff_ratio = min(float(s["position_ratio"]), _SP_RATIO)
        if _eff_ratio < float(s["position_ratio"]):
            _add_log(uid,
                f"🛡 单笔护栏：仓位比 {s['position_ratio']*100:.0f}% 超过单笔上限 "
                f"{_SP_RATIO*100:.0f}%，本轮按 {_eff_ratio*100:.0f}% 建仓", "info")
    _guard_full_logged = False
    _avail_left = _available_cash    # 剩余可用资金（含扣除在途买单），逐仓递减

    for r in buy_candidates:
        code  = r["code"]
        # 报价缺失绝不能用 ¥1 兜底：那会把 20 万预算算成 20 万股，
        # 模拟盘按 ≈¥1 成交后成本严重失真，实盘则发出 ¥1 限价单白占开仓名额与额度。
        try:
            price = float(r["price"] or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            _add_log(uid, f"⚠️ {code} 无有效报价（price={r.get('price')!r}），本轮跳过不入队", "error")
            continue
        if code in _occupied:
            continue   # 已持有 / 已在队列 → 不重复开仓
        # ★ 数据库去重：
        #   实盘 = 当日一码一买（不论信号最终状态）。GUI 偶发把实际已挂单误报 failed，
        #   只看未完成状态的旧去重会立即放行 → 已买的票反复挂单重复买入。
        #   模拟 = 仍按未完成信号去重（模拟即时成交，持仓本身就能挡重复）。
        if _broker_type == "live" and not DRY_RUN:
            if um.has_today_buy_signal(uid, code):
                continue
        elif um.has_pending_signal(uid, code, "BUY"):
            continue
        # 护栏①：最多持仓数 —— 名额满则停止开新仓（含待成交队列）
        if len(_occupied) >= _MAX_POS:
            if not _guard_full_logged:
                _add_log(uid,
                    f"🛡 持仓护栏：已占用 {len(_occupied)}/{_MAX_POS} 个名额，"
                    f"本轮剩余信号不再开新仓", "info")
                _guard_full_logged = True
            continue
        if not any(q["code"] == code for q in s["queue"]):
            # 单笔金额：信号自带分层仓位 > 策略平均仓位（原样执行）> 面板比例（夹护栏）
            # 决策逻辑见 core/order_sizing.pick_ratio（与原内联 if/elif/else 严格等价，有等价性测试）
            _strat_ratio = float(r.get("position_ratio", 0) or 0)
            _use_ratio = order_sizing.pick_ratio(
                float(s["position_ratio"]), _SP_RATIO,
                strat_ratio=_strat_ratio, strat_equal=_strat_ratio_equal)
            budget = _base_cash * _use_ratio    # 基数=全仓总资金，各仓等大
            shares = order_sizing.lot_shares(budget, price)   # 整手取整、最少1手（等价于原 max(int(budget/price/100)*100,100)）
            # 可用资金闸门：剩余可用不够本仓金额 → 停止开新仓（不缩仓，保持等大）
            if price * shares > _avail_left:
                if not _guard_full_logged:
                    _add_log(uid, f"⏭ 可用资金不足：剩余¥{_avail_left:,.0f} < 本仓¥{price*shares:,.0f}，"
                                  f"本轮不再开新仓", "info")
                    _guard_full_logged = True
                continue
            # ★ 实盘模式：单笔金额护栏（超 MAX_ORDER_AMOUNT 自动缩仓；连 1 手都超则跳过）★
            #   缩仓/拒单决策见 core/order_sizing.clamp_to_max_order —— 修复了原内联实现的 bug
            #   （单价×100 股已超上限时仍会强制 100 股照下单，靠 broker 层硬拒兜底）。
            if _broker_type == "live":
                try:
                    from config import MAX_ORDER_AMOUNT as _MAX_ORD
                except Exception:
                    _MAX_ORD = 50000
                _est = price * shares
                _safe_shares, _clamped = order_sizing.clamp_to_max_order(shares, price, _MAX_ORD)
                if _clamped:
                    if _safe_shares == 0:
                        _add_log(uid, f"⏭ {code} 单股 @¥{price:.2f} × 100股 = ¥{price*100:.0f} 已超 ¥{_MAX_ORD} 护栏，跳过", "error")
                        continue
                    _add_log(uid, f"⚠️ {code} 预估 ¥{_est:,.0f} > 护栏 ¥{_MAX_ORD}，自动缩仓 {shares}→{_safe_shares} 股", "info")
                    shares = _safe_shares
                    budget = price * shares
            item = {
                "code":       code,
                "name":       r["name"],
                "price":      price,
                "shares":     shares,
                "budget":     round(budget, 2),
                "strategy":   active or "",
                "pass_count": r.get("pass_count", 5),
                "reason":     r.get("reason", ""),
                "action":     "buy",
                "status":     "pending",
            }
            s["queue"].append(item)
            _occupied.add(code)   # 占用一个名额，后续信号据此判断是否已满
            _avail_left -= price * shares   # 扣减剩余可用，防止本轮超买
            _add_log(uid,
                f"📌 {code} {r['name']} 进入队列 | 计划买入{shares}股 预算¥{budget:,.0f} "
                f"| 名额 {len(_occupied)}/{_MAX_POS}", "buy")
            added += 1
            if s.get("auto_execute"):
                _auto_buy(uid, item)

    buy_signals = len(buy_candidates)
    _add_log(uid, f"扫描完成，信号 {buy_signals} 只，已入队 {added} 只")
    s["scan_progress"] = {"running": False, "stage": "完成", "current": 0, "total": 0}
    _sse_push(uid, "scan_done", {"pool_size": len(results), "signals": buy_signals})

    # ── 推送通知（有买入信号时推送）──────────────────
    if buy_signals > 0:
        try:
            top = buy_candidates[:5]
            detail = "\n".join(
                f"• {r.get('name', r['code'])}({r['code']}) "
                f"¥{r.get('price', 0):.2f} {r.get('change', 0):+.2f}%"
                for r in top
            )
            notify_content = (
                f"策略：{active or '默认'}\n"
                f"时间：{datetime.now().strftime('%H:%M:%S')}\n"
                f"信号数：{buy_signals} 只\n\n"
                f"前5只：\n{detail}"
            )
            threading.Thread(
                target=nm.send_notification,
                args=(f"📈 扫描到 {buy_signals} 只买入信号",  notify_content),
                daemon=True,
            ).start()
        except Exception as _ne:
            logger.debug(f"[notify] 推送失败: {_ne}")


# ── 扫描线程（防止同一用户重复并发）────────────────────────────
_scan_running: dict[int, bool] = {}


def _scan_thread_worker(uid: int):
    """单用户扫描线程入口，防止重复并发。"""
    if _scan_running.get(uid):
        return
    _scan_running[uid] = True
    try:
        do_scan(uid)
    except Exception as _e:
        logger.exception(f"[uid={uid}] do_scan 异常崩溃: {_e}")
        try:
            _s = get_user_state(uid)
            _add_log(uid, f"❌ 扫描异常崩溃: {_e}", "error")
        except Exception:
            pass
    finally:
        _scan_running[uid] = False
        # ★ 强制清进度条假状态（防止异常崩溃后前端永远显示"扫描中"）★
        try:
            _s = get_user_state(uid)
            _s["scan_progress"] = {"running": False, "stage": "已结束", "current": 0, "total": 0}
        except Exception:
            pass
