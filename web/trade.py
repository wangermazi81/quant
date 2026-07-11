# -*- coding: utf-8 -*-
"""监控/交易/账户/持仓 API 蓝图（从 main.py 迁出，工单4）。

/api/status|start|stop|auto_execute、/api/execute、/api/account*、
/api/queue/*、/api/positions*、/api/seal_tracker 共 16 条路由。
路由路径与行为与 main.py 原实现逐行等价。

测试打桩注意：路由引用的 execute_buy/execute_sell/DRY_RUN/pb/um 绑定在
本模块命名空间，相关测试请 monkeypatch web.trade 的属性。
"""
import logging
import platform

from flask import Blueprint, jsonify, request, session

from app_state import (
    require_auth, get_user_state, _persist_state, _add_log, get_operator,
)
from config import DRY_RUN
from core import user_manager as um
from core.trade_service import execute_buy, execute_sell, _auto_buy, _paper_live_prices
from broker import paper_broker as pb
from analytics import data_manager as dm
from strategy_engine import strategy_manager as sm
from web._params import _positive_float_value, _int_value

logger = logging.getLogger(__name__)
bp = Blueprint("trade", __name__)


# ── 监控 API（per-user）──────────────────────────────────────────
@bp.route("/api/status")
def get_status():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"running": False, "logged_in": False})
    s   = get_user_state(uid)
    try:
        from analytics.factors_zoo import activator as _vibe_act
        _vibe_cfg = _vibe_act.get(uid) or {}
        s["vibe_active"] = bool(_vibe_cfg.get("enabled"))
        s["vibe_factor"] = _vibe_cfg.get("factor_id") or ""
    except Exception:
        s["vibe_active"] = False
        s["vibe_factor"] = ""
    return jsonify(s)


@bp.route("/api/start", methods=["POST"])
@require_auth
def start():
    uid = session["user_id"]
    s   = get_user_state(uid)
    s["running"] = True
    _persist_state(uid, running=1)
    _add_log(uid, "监控启动")
    return jsonify({"ok": True})


@bp.route("/api/stop", methods=["POST"])
@require_auth
def stop():
    uid = session["user_id"]
    s   = get_user_state(uid)
    s["running"] = False
    _persist_state(uid, running=0)
    _add_log(uid, "监控停止", "WARN")
    return jsonify({"ok": True})


@bp.route("/api/auto_execute", methods=["POST"])
@require_auth
def set_auto_execute():
    uid     = session["user_id"]
    s       = get_user_state(uid)
    enabled = request.json.get("enabled", False)
    s["auto_execute"] = bool(enabled)
    _persist_state(uid, auto_execute=int(enabled))
    mode = "开启" if enabled else "关闭"
    _add_log(uid,
        f"🤖 自动执行已{mode} — {'信号触发即自动下单' if enabled else '手动确认模式'}", "WARN")
    return jsonify({"ok": True, "auto_execute": s["auto_execute"]})


@bp.route("/api/execute", methods=["POST"])
@require_auth
def execute():
    uid    = session["user_id"]
    s      = get_user_state(uid)
    data   = request.json or {}
    code   = data.get("code")
    action = data.get("action")
    item   = next((q for q in s["queue"] if q["code"] == code), None)

    if not item:
        if action == "buy":
            price  = _positive_float_value(data.get("price"))
            if price is None:
                return jsonify({"ok": False, "msg": "invalid price"})
            name   = data.get("name", code)
            shares = data.get("shares")
            if not shares:
                budget = s["capital"] * s["position_ratio"]
                shares = int(budget / price / 100) * 100 if price > 0 else 100
                shares = max(shares, 100)
            shares = _int_value(shares)
            if shares is None:
                return jsonify({"ok": False, "msg": "invalid shares"})
            res = execute_buy(uid, code, name, price, shares, reason="手动买入")
            return jsonify(res)
        return jsonify({"ok": False, "msg": "未找到"})

    if action == "skip":
        item["status"] = "skip"
    elif action == "buy":
        price  = item["price"]
        name   = item.get("name", code)
        shares = item.get("shares", 100)
        res = execute_buy(uid, code, name, price, shares, reason="队列买入",
                          strategy=item.get("strategy") or "")
        if not res.get("ok"):
            item["status"] = "error"
            item["error_msg"] = res.get("msg", "")
        elif res.get("signal_id"):
            item["status"] = "submitted"          # 实盘：已报，等交易机回传成交确认
            item["signal_id"] = res["signal_id"]
        else:
            item["status"] = "done"               # 模拟：即时成交

    return jsonify({"ok": True})


# ── 账户 API ──────────────────────────────────────────────────
@bp.route("/api/account", methods=["GET"])
@require_auth
def get_account():
    uid = session["user_id"]
    s   = get_user_state(uid)
    return jsonify({
        "capital":        s["capital"],
        "position_ratio": s["position_ratio"],
        "max_positions":             s.get("max_positions", 5),
        "single_position_max_ratio": s.get("single_position_max_ratio", 0.20),
        "today_trades":   s["today_trades"],
        "positions":      s["positions"],
    })


@bp.route("/api/account", methods=["POST"])
@require_auth
def set_account():
    uid = session["user_id"]
    s   = get_user_state(uid)
    d   = request.json
    if "capital" in d:
        s["capital"]        = float(d["capital"])
    if "position_ratio" in d:
        s["position_ratio"] = float(d["position_ratio"])
    # ── 仓位护栏（前端可调）：单笔上限 + 最多持仓数 ──────────────
    if "max_positions" in d:
        try:
            mp = int(d["max_positions"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "max_positions 必须是整数"}), 400
        if mp < 1 or mp > 50:
            return jsonify({"ok": False, "error": "最多持仓数须在 1~50 之间"}), 400
        s["max_positions"] = mp
    if "single_position_max_ratio" in d:
        try:
            sr = float(d["single_position_max_ratio"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "single_position_max_ratio 必须是数字"}), 400
        if sr <= 0 or sr > 1:
            return jsonify({"ok": False, "error": "单笔上限须在 (0, 1] 之间"}), 400
        s["single_position_max_ratio"] = sr
    _persist_state(uid,
        capital=s["capital"], position_ratio=s["position_ratio"],
        max_positions=s["max_positions"],
        single_position_max_ratio=s["single_position_max_ratio"])
    _add_log(uid,
        f"账户设置更新: 资金¥{s['capital']:,.0f} 仓位{s['position_ratio']*100:.0f}% "
        f"| 护栏 单笔≤{s['single_position_max_ratio']*100:.0f}% 最多持{s['max_positions']}只")
    return jsonify({"ok": True})


@bp.route("/api/queue/clear", methods=["POST"])
@require_auth
def clear_queue():
    uid = session["user_id"]
    s   = get_user_state(uid)
    s["queue"] = []
    _add_log(uid, "🗑 队列已清空", "WARN")
    return jsonify({"ok": True})


@bp.route("/api/queue/add", methods=["POST"])
@require_auth
def queue_add():
    uid   = session["user_id"]
    s     = get_user_state(uid)
    d     = request.json or {}
    code  = d.get("code", "")
    price = _positive_float_value(d.get("price"))
    name  = d.get("name", code)
    if not code:
        return jsonify({"ok": False, "msg": "code 缺失"})
    if price is None:
        return jsonify({"ok": False, "msg": "invalid price"})
    if any(q["code"] == code for q in s["queue"]):
        return jsonify({"ok": True, "msg": "已在队列"})
    budget = s["capital"] * s["position_ratio"]
    shares = max(int(budget / price / 100) * 100 if price > 0 else 100, 100)
    item   = {
        "code": code, "name": name, "price": price,
        "shares": shares, "budget": round(budget, 2),
        "pass_count": 5, "reason": d.get("reason", "手动加入"),
        "action": "buy", "status": "pending",
    }
    s["queue"].append(item)
    _add_log(uid, f"📌 手动加入队列: {code} {name} 计划{shares}股 预算¥{budget:,.0f}", "buy")
    if s.get("auto_execute"):
        _auto_buy(uid, item)
    return jsonify({"ok": True, "msg": f"{code} {name} 已入队，计划{shares}股"})


# ── 持仓 API ──────────────────────────────────────────────────
@bp.route("/api/positions")
@require_auth
def get_positions_api():
    uid       = session["user_id"]
    s         = get_user_state(uid)
    positions = s.get("positions", {})
    if not positions:
        return jsonify([])
    live = {}
    try:
        from market import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            qs   = client.get_quotes(list(positions.keys()))
            live = {q["code"]: q["price"] for q in qs if q.get("price", 0) > 0}
    except Exception:
        pass
    result = []
    for code, pos in positions.items():
        cost      = pos.get("cost", 0)
        cur_price = live.get(code, cost)
        shares    = pos.get("shares", 0)
        pnl_pct   = round((cur_price - cost) / cost * 100, 2) if cost > 0 else 0
        pnl_amt   = round((cur_price - cost) * shares, 2)
        name      = pos.get("name", "") or dm.get_stock_name(code) or code
        result.append({
            "code":         code, "name":         name,
            "shares":       shares, "cost":        round(cost, 2),
            "cur_price":    round(cur_price, 2),   "pnl_pct":     pnl_pct,
            "pnl_amount":   pnl_amt, "market_value": round(cur_price * shares, 2),
            "date":         pos.get("date", ""),
        })
    return jsonify(result)


@bp.route("/api/positions/sell", methods=["POST"])
@require_auth
def sell_position():
    uid  = session["user_id"]
    s    = get_user_state(uid)
    d    = request.json
    code = d.get("code", "")
    if DRY_RUN:
        broker           = pb.get_broker(uid)
        broker_positions = {p["code"]: p for p in broker.get_positions()}
        bpos = broker_positions.get(code)
        if not bpos:
            return jsonify({"ok": False, "msg": "模拟盘持仓不存在"})
        # 优先拉 ths_quote 实时价（避免前端 cur_price fallback 到 cost_price 导致
        # 卖出价≈买入价、盈亏只剩费用的假象）
        price = 0.0
        try:
            from market import ths_quote as tq
            client = tq.get_client()
            if client and client._ok:
                qs = client.get_quotes([code])
                if qs:
                    price = _positive_float_value(qs[0].get("price"), 0.0)
        except Exception:
            pass
        if not price:
            price = (
                _positive_float_value(d.get("price"), 0.0)
                or _positive_float_value(bpos.get("cur_price"), 0.0)
                or _positive_float_value(bpos.get("cost_price"), 0.0)
            )
    else:
        pos = s["positions"].get(code)
        if not pos:
            return jsonify({"ok": False, "msg": "持仓不存在"})
        price = _positive_float_value(d.get("price"), _positive_float_value(pos.get("cost"), 0.0))
    if not price:
        return jsonify({"ok": False, "msg": "无法获取实时价格，请稍后重试"})
    return jsonify(execute_sell(uid, code, price, sell_reason="手动卖出"))


@bp.route("/api/positions/close", methods=["POST"])
@require_auth
def close_position():
    uid  = session["user_id"]
    s    = get_user_state(uid)
    code = request.json.get("code", "")
    if code in s["positions"]:
        del s["positions"][code]
        _add_log(uid, f"🔻 平仓: {code}", "WARN")
    return jsonify({"ok": True})


# ── 实盘资金/账户/交易设置（xiadan GUI 读取）─────────────────────
@bp.route("/api/account/funds")
@require_auth
def account_funds():
    """统一「可用资金」查询，方便下单前知道还能买多少。

    模拟盘 / DRY_RUN：从模拟盘账本读现金 + 实时权益（可靠、实时）。
    实盘：调用 xiadan 操作器尽力读「可用资金」（GUI 抓取，可能需现场校准）。
    返回统一字段 available（可下单金额），便于前端直接用。
    """
    uid         = session["user_id"]
    broker_cfg  = um.get_user_broker_config(uid)
    broker_type = broker_cfg.get("broker_type", "paper")

    if DRY_RUN or broker_type == "paper":
        broker = pb.get_broker(uid)
        acct   = broker.get_account(cur_prices=_paper_live_prices(broker))
        return jsonify({
            "ok":           True,
            "mode":         "paper",
            "available":    acct["cash"],          # 可用资金 = 现金（可下单金额）
            "cash":         acct["cash"],
            "market_value": acct["market_value"],
            "equity":       acct["equity"],
            "total_return": acct["total_return"],
        })

    # ── 实盘：读 xiadan ──
    if platform.system() != "Windows":
        return jsonify({"ok": False, "mode": "real", "msg": "服务端非 Windows，无法读 xiadan 资金"})
    operator = get_operator()
    if not operator:
        return jsonify({"ok": False, "mode": "real", "msg": "xiadan 操作员未初始化（DRY_RUN 或 Linux）"})
    try:
        import time as _time
        force = (request.args.get("force") or "").strip().lower() in ("1", "true", "yes")
        cache = getattr(operator, "_funds_cache", None)
        # ⚠️ 事件驱动 + 只读缓存：实盘资金缓存由「下单成功后」和「手动查实盘账户」更新。
        # 这个轮询接口默认只返回缓存，绝不激活 xiadan 窗口——彻底告别每 10 秒弹窗。
        if cache and cache.get("data") and not force:
            age = round(_time.time() - cache.get("ts", 0))
            return jsonify({**cache["data"], "cached": True, "age_sec": age})
        if not force:
            # 无缓存且非强制 → 不弹窗，返回占位；交易后会自动有值，或手动刷新一次
            return jsonify({
                "ok": True, "mode": "real", "available": None, "total_asset": None,
                "cached": False,
                "msg": "暂无资金缓存：下单成功后自动更新，或打开「📊 查实盘账户」刷新一次",
            })
        # 仅当 ?force=1（用户主动）才真正去读 xiadan（会激活窗口一次）
        r = operator.query_funds()
        r.setdefault("mode", "real")
        if r.get("ok"):
            prev = (cache or {}).get("data", {}) if cache else {}
            operator._funds_cache = {"ts": _time.time(), "data": {
                "ok": True, "mode": "real",
                "available":   r.get("available"),
                "total_asset": prev.get("total_asset"),  # query_funds 不含总资产，保留旧值
            }}
            return jsonify(operator._funds_cache["data"])
        return jsonify(r)
    except Exception as e:
        logger.exception(f"读 xiadan 资金失败: {e}")
        return jsonify({"ok": False, "mode": "real", "msg": f"调用异常: {e}"})


@bp.route("/api/account/snapshot")
@require_auth
def account_snapshot():
    """实盘资金 + 持仓快照（一次性返回 可用资金 / 总资产 / 持仓列表）。

    模拟盘 / DRY_RUN：从模拟盘账本读现金 + 持仓（可靠、实时）。
    实盘：调用 xiadan 操作器按 F4 截图 + 本地 OCR 解析「资金股票」页：
          - 可用资金 / 总资产：顶部工具栏 OCR，已在真账户验证可靠；
          - 持仓代码：词坐标行聚类，可靠；
          - 名称：OCR 易错，这里用本地名称库按 code 纠正；
          - 数字列（股数/成本/盈亏）：自绘表格 OCR 仅供参考，附截图供复核。
    统一字段：available（可用资金）/ total_asset（总资产）/ positions（持仓列表）。
    """
    uid         = session["user_id"]
    broker_cfg  = um.get_user_broker_config(uid)
    broker_type = broker_cfg.get("broker_type", "paper")

    if DRY_RUN or broker_type == "paper":
        broker    = pb.get_broker(uid)
        prices    = _paper_live_prices(broker)
        acct      = broker.get_account(cur_prices=prices)
        positions = broker.get_positions(cur_prices=prices)
        return jsonify({
            "ok":           True,
            "mode":         "paper",
            "available":    acct["cash"],
            "total_asset":  acct["equity"],
            "market_value": acct["market_value"],
            "positions":    positions,
        })

    # ── 实盘：F4 截图 + 本地 OCR ──
    if platform.system() != "Windows":
        return jsonify({"ok": False, "mode": "real", "msg": "服务端非 Windows，无法读 xiadan 账户"})
    operator = get_operator()
    if not operator:
        return jsonify({"ok": False, "mode": "real", "msg": "xiadan 操作员未初始化（DRY_RUN 或 Linux）"})
    try:
        r = operator.query_account_snapshot()
        r.setdefault("mode", "real")
        # code 可靠、OCR 名称易错 → 用本地名称库按 code 纠正
        for p in r.get("positions", []):
            code = (p.get("code") or "").strip()
            if code:
                fixed = dm.get_stock_name(code)
                if fixed and fixed != code:
                    p["name"] = fixed
        return jsonify(r)
    except Exception as e:
        logger.exception(f"读 xiadan 账户快照失败: {e}")
        return jsonify({"ok": False, "mode": "real", "msg": f"调用异常: {e}"})


@bp.route("/api/account/trade_settings", methods=["GET", "POST"])
@require_auth
def account_trade_settings():
    """读/写 实盘交易自动化开关（运行时切换，免改 config）：
      - auto_confirm_order   下单后自动按 Y 提交(True) / 下单后等人工按 Y(False)
      - auto_cancel_unfilled 未成交自动撤单(True) / 手动撤单(False)
      - auto_cancel_delay    未成交多少秒后撤
    这是驱动单一 xiadan 客户端的全局设置，持久化到 data/trade_settings.json（重启仍生效）。
    """
    operator = get_operator()
    if not operator:
        return jsonify({"ok": False, "msg": "xiadan 操作员未初始化（DRY_RUN 或 Linux），无实盘设置"})
    if request.method == "POST":
        d = request.json or {}
        s = operator.set_trade_settings(
            auto_confirm_order=d.get("auto_confirm_order"),
            auto_cancel_unfilled=d.get("auto_cancel_unfilled"),
            auto_cancel_delay=d.get("auto_cancel_delay"),
        )
        uid = session.get("user_id")
        _add_log(uid,
                 f"⚙️ 实盘自动化设置: 下单{'自动确认' if s['auto_confirm_order'] else '手动确认'} · "
                 f"{'自动撤单' if s['auto_cancel_unfilled'] else '手动撤单'}"
                 f"(未成交{s['auto_cancel_delay']:.0f}秒)", "WARN")
        return jsonify({"ok": True, **s})
    return jsonify({"ok": True, **operator.get_trade_settings()})


@bp.route("/api/seal_tracker")
@require_auth
def seal_tracker_dump():
    """SealTracker 状态查询（调试/前端展示用）。
    /api/seal_tracker          → 列出所有被追踪股的强度评分（按 score 降序）
    /api/seal_tracker?code=XXX → 单只详情（含 samples 时间线）
    """
    try:
        from analytics.seal_tracker import get_tracker
        tr = get_tracker()
    except Exception as e:
        return jsonify({"ok": False, "msg": f"tracker 不可用: {e}"})
    code = request.args.get("code", "").strip()
    if code:
        st = tr.status(code)
        if not st:
            return jsonify({"ok": False, "msg": f"{code} 无 tracker 记录"})
        # 加 samples 时间线
        with tr._lock:
            d = tr._data.get(code, {})
            st["samples"] = [
                {"ts": s["ts"], "seal": s["seal"], "price": s["price"],
                 "lu": s["lu"], "sealed": s["sealed"]}
                for s in list(d.get("samples", []))
            ]
        return jsonify({"ok": True, "code": code, "status": st})
    # 全量列表
    out = []
    for c in tr.all_tracked():
        s = tr.status(c)
        s["code"] = c
        out.append(s)
    out.sort(key=lambda x: x.get("score", 0), reverse=True)
    return jsonify({"ok": True, "count": len(out), "items": out})
