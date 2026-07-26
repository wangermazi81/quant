# -*- coding: utf-8 -*-
"""模拟盘 API 蓝图（从 main.py 迁出，工单4）。

/api/paper/account|positions|trades|orders|stats|equity|reset|config(GET/POST)|
buy|sell|stream 共 12 条路由。路由路径与行为与 main.py 原实现逐行等价。

测试打桩注意：路由引用的 execute_buy/execute_sell/_deduct/DRY_RUN/pb/um
绑定在本模块命名空间，相关测试请 monkeypatch web.paper 的属性。
"""
import json
import logging
import queue as _queue
import time

from flask import Blueprint, Response, jsonify, request, session

from app_state import (
    require_auth, get_user_state, _add_log, _deduct, _sse_subscribers, _sse_lock,
)
from config import DRY_RUN, INITIAL_CAPITAL
from core import user_manager as um
from core.trade_service import execute_buy, execute_sell, _paper_live_prices
from broker import paper_broker as pb
from web._params import _bounded_int_arg, _positive_float_value, _int_value
from web._concurrency import sse_slot, sse_deadline

logger = logging.getLogger(__name__)
bp = Blueprint("paper", __name__)


@bp.route("/api/paper/account")
@require_auth
def paper_account():
    uid    = session["user_id"]
    broker = pb.get_broker(uid)
    return jsonify({**broker.get_account(cur_prices=_paper_live_prices(broker)), "ok": True})


@bp.route("/api/paper/positions")
@require_auth
def paper_positions():
    uid        = session["user_id"]
    broker     = pb.get_broker(uid)
    return jsonify(broker.get_positions(cur_prices=_paper_live_prices(broker)))


@bp.route("/api/paper/trades")
@require_auth
def paper_trades():
    uid    = session["user_id"]
    limit  = _bounded_int_arg("limit", 100, 1, 500)
    broker = pb.get_broker(uid)
    return jsonify(broker.get_trades(limit=limit))


@bp.route("/api/paper/orders")
@require_auth
def paper_orders():
    uid    = session["user_id"]
    limit  = _bounded_int_arg("limit", 100, 1, 500)
    broker = pb.get_broker(uid)
    return jsonify(broker.get_orders(limit=limit))


@bp.route("/api/paper/stats")
@require_auth
def paper_stats():
    uid    = session["user_id"]
    broker = pb.get_broker(uid)
    stats  = broker.get_stats()
    acct   = broker.get_account(cur_prices=_paper_live_prices(broker))
    return jsonify({**stats, **acct, "ok": True})


@bp.route("/api/paper/equity")
@require_auth
def paper_equity():
    uid    = session["user_id"]
    days   = _bounded_int_arg("days", 90, 1, 3650)
    broker = pb.get_broker(uid)
    return jsonify(broker.get_equity_curve(days=days))


@bp.route("/api/paper/reset", methods=["POST"])
@require_auth
def paper_reset():
    uid    = session["user_id"]
    s      = get_user_state(uid)
    cap    = _positive_float_value((request.json or {}).get("initial_capital"), INITIAL_CAPITAL)
    if cap is None:
        return jsonify({"ok": False, "error": "invalid initial_capital"}), 400
    broker = pb.get_broker(uid)
    result = broker.reset(initial_capital=cap)
    s["positions"]    = {}
    s["today_trades"] = 0
    s["executed"]     = 0
    _add_log(uid, f"🔄 模拟盘已重置，初始资金¥{cap:,.0f}", "WARN")
    return jsonify(result)


@bp.route("/api/paper/config", methods=["GET"])
@require_auth
def paper_get_config():
    uid    = session["user_id"]
    broker = pb.get_broker(uid)
    return jsonify({**broker.get_config(), "ok": True})


@bp.route("/api/paper/config", methods=["POST"])
@require_auth
def paper_set_config():
    uid    = session["user_id"]
    broker = pb.get_broker(uid)
    result = broker.set_config(request.json)
    _add_log(uid, f"模拟盘参数已更新: {list(request.json.keys())}")
    return jsonify(result)


@bp.route("/api/paper/buy", methods=["POST"])
@require_auth
def paper_manual_buy():
    payload = request.json or {}
    raw_code = payload.get("code", "")
    code_for_validation = raw_code.strip() if isinstance(raw_code, str) else ""
    if not code_for_validation:
        return jsonify({"ok": False, "msg": "missing code"})
    if _positive_float_value(payload.get("price")) is None:
        return jsonify({"ok": False, "msg": "invalid price"})
    if _int_value(payload.get("shares"), 0) is None:
        return jsonify({"ok": False, "msg": "invalid shares"})
    uid    = session["user_id"]
    s      = get_user_state(uid)
    d      = request.json or {}
    code   = d.get("code", "").strip()
    name   = d.get("name", code)
    price  = _positive_float_value(d.get("price"))
    shares = _int_value(d.get("shares"), 0)
    # 防御：name 含 "?" / 乱码 / 为空，从本地 stock_names.json 查
    if not name or "?" in name or "??" in name or name == code or len(name.strip()) < 2:
        try:
            from pathlib import Path
            import json as _json
            f = Path(__file__).parent.parent / "data" / "stock_names.json"
            if f.exists():
                _names = _json.loads(f.read_text(encoding="utf-8"))
                name = _names.get(code, name) or code
        except Exception:
            name = name or code
    if not code:
        return jsonify({"ok": False, "msg": "请输入股票代码"})
    if price is None:
        return jsonify({"ok": False, "msg": "价格必须大于0"})
    if shares is None:
        return jsonify({"ok": False, "msg": "invalid shares"})

    # ── 成交价以服务端行情为准（与 /api/paper/sell 同口径）────────────
    # 此前完全信任前端传入的 price：用户可用 API 以 ¥0.01「买入」再按市价卖出，
    # 凭空刷出模拟盘利润，污染收益统计、排行与积分体系。
    _server_price = 0.0
    try:
        from analytics import data_gateway as dg
        q = dg.get_realtime_quote(code)
        if q:
            _server_price = float(q.get("price") or q.get("now") or 0)
    except Exception as _e:
        logger.warning("[paper.buy] 取 %s 实时价失败，回退前端价: %s", code, _e)
    if _server_price > 0:
        # 允许偏离 ±20%（覆盖涨跌停与盘中波动），超出即视为伪造价格拒单
        if abs(price - _server_price) / _server_price > 0.20:
            return jsonify({"ok": False, "msg":
                            f"价格 {price:.2f} 偏离实时价 {_server_price:.2f} 超过 20%，已拒单"})
        price = _server_price
    # 非交易时段提示但仍允许（方便用户测试 + 复盘）
    try:
        from market.market_data import is_trading_time
        if not is_trading_time():
            _add_log(uid, f"⚠️ 非交易时段手动下单: {code} ({name}) — 已按上次收盘价模拟成交", "warning")
    except Exception:
        pass
    if shares <= 0:
        # 仓位基数：实盘走 live_cash（xiadan 真账户余额），模拟走 paper_broker.cash
        broker_cfg = um.get_user_broker_config(uid)
        broker_type = broker_cfg.get("broker_type", "paper")
        if broker_type == "paper" or DRY_RUN:
            broker = pb.get_broker(uid)
            acct   = broker.get_account()
            base_cash = float(acct.get("cash") or 0)
        else:
            # 实盘：从 broker_config.config.live_cash 取 xiadan 真账户余额
            live_cash = float((broker_cfg.get("config") or {}).get("live_cash") or 0)
            if live_cash <= 0:
                return jsonify({
                    "ok": False,
                    "msg": "实盘模式下未配置真账户余额（live_cash），请在顶部切换实盘开关时输入 xiadan 当前可用资金",
                    "need_live_cash": True,
                }), 400
            base_cash = live_cash
        # 单笔上限护栏：把仓位比夹到 single_position_max_ratio 内（防一次大面腰斩）
        _ratio = float(s.get("position_ratio", 0.20))
        _cap   = float(s.get("single_position_max_ratio", 0.20))
        _eff   = min(_ratio, _cap)
        budget = base_cash * _eff
        shares = max(int(budget / price / 100) * 100, 100)
        if _eff < _ratio:
            _add_log(uid, f"🛡 单笔护栏：仓位比 {_ratio*100:.0f}% > 单笔上限 {_cap*100:.0f}%，按 {_eff*100:.0f}% 建仓")
        _add_log(uid, f"💰 仓位计算: {broker_type} base_cash=¥{base_cash:,.0f} × {_eff*100:.0f}% = ¥{budget:,.0f} → {shares}股 @{price:.2f}")
    err = _deduct("paper_trade", "模拟盘下单")
    if err:
        return jsonify(err), 402
    res = execute_buy(uid, code, name, price, shares, reason="模拟盘手动买入")
    if res.get("ok"):
        # 透传 execute_buy 所有字段（含实盘的 signal_id，前端轮询用）
        return jsonify({
            "ok":         True,
            "shares":     res.get("shares", shares),
            "msg":        res.get("msg") or f"已买入 {code} {shares}股 @{price}",
            "signal_id":  res.get("signal_id"),     # 实盘模式才有
            "exec_price": res.get("exec_price"),    # 模拟模式才有
            "total_cost": res.get("total_cost"),
        })
    return jsonify({"ok": False, "msg": res.get("msg") or "买入失败，请检查资金或持仓（详见日志）"})


@bp.route("/api/paper/sell", methods=["POST"])
@require_auth
def paper_manual_sell():
    uid   = session["user_id"]
    d     = request.json
    code  = d.get("code", "")
    # 优先拉统一数据源实时价（前端传的 price 可能是 cur_price fallback 到 cost_price
    # 的脏值，会导致卖出价≈买入价、盈亏只剩费用 -0.22% 的假象）
    price = 0.0
    try:
        from analytics import data_gateway as dg
        q = dg.get_realtime_quote(code)
        if q:
            price = float(q.get("price") or q.get("now") or 0)
    except Exception:
        pass
    # 实时价拉不到时，再用前端传的 price 作 fallback
    if not price:
        price = _positive_float_value(d.get("price"), 0.0)
    if not price:
        return jsonify({"ok": False, "msg": "无法获取实时价格，请稍后重试"})
    return jsonify(execute_sell(uid, code, price, sell_reason="手动卖出"))


@bp.route("/api/paper/stream")
@require_auth
def paper_stream():
    """SSE 实时推送：日志、持仓更新、扫描完成事件。"""
    uid = session["user_id"]

    def event_stream():
        # gthread 模型下每个 SSE 连接永久占住一个 worker 线程（threads 有限），
        # 必须限额 + 限时，否则几个标签页就能把线程池吃干（见 web/_concurrency.py）。
        with sse_slot("paper", uid) as granted:
            if not granted:
                yield ("event: rejected\ndata: "
                       + json.dumps({"msg": "实时推送连接数已达上限，请关闭多余页面后重试"})
                       + "\n\n")
                return
            deadline = sse_deadline()
            q = _queue.Queue(maxsize=200)
            with _sse_lock:
                _sse_subscribers.setdefault(uid, []).append(q)
            try:
                # 首次连接推送当前状态快照（重连后也靠它重新对齐状态）
                s = get_user_state(uid)
                yield f"event: snapshot\ndata: {json.dumps({'running': s['running'], 'logs': s['logs'][:20]})}\n\n"
                while True:
                    if time.time() >= deadline:
                        # 到期主动收摊，把线程还回池子；浏览器 EventSource 会自动重连
                        yield ": reconnect\n\n"
                        return
                    try:
                        msg = q.get(timeout=30)
                        yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
                    except _queue.Empty:
                        yield ": heartbeat\n\n"
            finally:
                with _sse_lock:
                    subs = _sse_subscribers.get(uid, [])
                    if q in subs:
                        subs.remove(q)

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
