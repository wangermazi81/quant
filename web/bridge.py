# -*- coding: utf-8 -*-
"""实盘桥接 API 蓝图（从 main.py 迁出，工单4）。

桥接器（tools/ths_bridge.py，跑在 Windows 交易机）轮询信号 → xiadan 执行 → 回传；
前端经这些接口开关总闸、查当日委托、跟踪单条信号状态。
路由路径与行为与 main.py 原实现逐行等价。
"""
import logging
import platform
from datetime import datetime

from flask import Blueprint, jsonify, request, session

from app_state import require_auth, require_root_admin, get_user_state, _add_log, get_operator
from core import user_manager as um
from core.bridge_state import _live_enabled, set_live_enabled as _set_live_enabled
from core.daban_state import _DABAN_PEAKS, _LIVE_SELL_PENDING
from analytics import data_manager as dm

logger = logging.getLogger(__name__)
bp = Blueprint("bridge", __name__)


@bp.route("/api/bridge/signals", methods=["GET"])
@require_auth
def bridge_get_signals():
    uid = session["user_id"]
    # ★ 实盘下单总闸：关闭时不给桥接器任何信号（桥接器照常轮询但拿不到单 → 不下单）
    if not _live_enabled(uid):
        return jsonify({"ok": True, "signals": [], "live_disabled": True})
    signals = um.get_pending_signals(uid)
    return jsonify({"ok": True, "signals": signals})


@bp.route("/api/bridge/live_enabled", methods=["GET"])
@require_auth
def get_live_enabled():
    return jsonify({"ok": True, "enabled": _live_enabled(session["user_id"])})


@bp.route("/api/bridge/live_enabled", methods=["POST"])
@require_auth
def set_live_enabled():
    uid = session["user_id"]
    en  = bool((request.json or {}).get("enabled", True))
    _set_live_enabled(uid, en)
    _add_log(uid, ("✅ 已开启实盘下单（桥接器恢复执行信号）" if en
                   else "🛑 已关闭实盘下单（桥接器不再收到任何信号，不会下单到同花顺）"), "WARN")
    return jsonify({"ok": True, "enabled": en})


@bp.route("/api/bridge/today_orders", methods=["GET"])
@require_root_admin
def bridge_today_orders():
    """读 xiadan 当日委托表，返回每笔精确状态（已成/已报/部成/已撤/废单）

    Query 参数:
      code     - 可选, 只查该 code 的委托
      limit    - 可选, 最多读多少行（默认 50）
      navigate - 可选, 1/true 时 F4 后自校准点击左侧「当日委托」再截图（拿真实成交
                 状态）；默认不导航=只截 F4 默认的资金股票页（不向实盘发送鼠标点击）

    返回:
      {ok, screenshot, abs_path, orders:[{code,name,action,status,numbers,raw}],
       funds, positions, tabs:[...], ocr_lines:[...], msg}

    实现：xiadan 委托表是自绘控件读不到，桥接器按 F4 切到查询页 → 截图 →
    本地 Windows OCR(winocr) 逐行识别 → 解析结构化 → 按 F1 切回买入页（约 2-3 秒）。
    未装 winocr 时 orders 为空，仅返回截图。
    """
    if platform.system() != "Windows":
        return jsonify({"ok": False, "msg": "服务端非 Windows，无法读 xiadan"})
    operator = get_operator()
    if not operator:
        return jsonify({"ok": False, "msg": "xiadan 操作员未初始化（DRY_RUN 或 Linux）"})

    code = (request.args.get("code") or "").strip() or None
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    # navigate=1：F4 后自校准点击左侧「当日委托」再截图（拿真实成交状态）。
    # 默认 0=只截 F4 默认的资金股票页（安全，不向实盘客户端发送鼠标点击）。
    navigate = (request.args.get("navigate") or "").strip().lower() in ("1", "true", "yes", "on")

    try:
        r = operator.query_today_orders(code_filter=code, max_rows=limit, navigate=navigate)
        # code 可靠、OCR 名称易错或缺失 → 用本地名称库按 code 纠正/补全（持仓同样处理）
        for lst in (r.get("orders") or [], r.get("positions") or []):
            for it in lst:
                c = (it.get("code") or "").strip()
                if c:
                    nm = dm.get_stock_name(c)
                    if nm and nm != c:
                        it["name"] = nm
        # ★ 缓存 OCR 实际持仓（当日有效）：自动扫描把这些票算"已持有"不再买入。
        #   兜住"实际已买入但回传丢失/误报失败"的票——只要查过一次实盘账户就拦得住。
        try:
            if r.get("ok") and r.get("positions"):
                _s = get_user_state(session["user_id"])
                _s["live_ocr_pos"] = {
                    "date":  datetime.now().strftime("%Y-%m-%d"),
                    "codes": [p.get("code") for p in r["positions"] if p.get("code")],
                }
        except Exception:
            pass
        return jsonify(r)
    except Exception as e:
        logger.exception(f"读当日委托失败: {e}")
        return jsonify({"ok": False, "msg": f"调用异常: {e}"})


@bp.route("/api/bridge/signals/<int:signal_id>/status", methods=["GET"])
@require_auth
def bridge_signal_status(signal_id):
    """前端轮询单条信号当前状态（pending/sent/executed/failed/cancelled）"""
    uid = session["user_id"]
    sig = um.get_trade_signal(signal_id)
    if not sig or sig.get("user_id") != uid:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({
        "ok": True,
        "signal": {
            "id":          sig["id"],
            "code":        sig["code"],
            "name":        sig["name"],
            "action":      sig["action"],
            "price":       sig["price"],
            "shares":      sig["shares"],
            "status":      sig["status"],     # pending/sent/executed/failed/cancelled
            "report_msg":  sig.get("msg") or "",
            "exec_price":  sig.get("exec_price") or sig["price"],
            "created_at":  sig.get("created_at"),
            "updated_at":  sig.get("updated_at"),
        }
    })


@bp.route("/api/bridge/signals/<int:signal_id>/report", methods=["POST"])
@require_auth
def bridge_report_signal(signal_id):
    uid = session["user_id"]
    sig = um.get_trade_signal(signal_id)
    if not sig or sig.get("user_id") != uid:
        return jsonify({"ok": False, "error": "not found"}), 404

    data = request.json or {}
    status = data.get("status")
    msg = data.get("msg", "")

    # 校验并更新状态
    res = um.report_trade_signal(signal_id, status, msg)
    if not res.get("ok"):
        return jsonify(res)

    action_label = "买入" if res["action"] == "BUY" else "卖出"
    # 三态显示：executed=✅ 成功 / submitted=⏳ 已提交未确认 / 其他=❌ 失败
    if status == "executed":
        sign = "✅"; log_level = "buy"
    elif status == "submitted":
        sign = "⏳"; log_level = "info"   # 中间态，不当失败标红
    else:
        sign = "❌"; log_level = "error"
    log_msg = f"{action_label} {res['code']} {res['name'] or ''} {res['shares']}股 @{res['price']:.2f} — 执行完毕: {msg}"
    _add_log(uid, f"{sign} [实盘回传] {log_msg}", log_level)

    # 同步更新云端的仓位视图缓存
    s = get_user_state(uid)
    code = res["code"]
    if status == "executed":
        if res["action"] == "BUY":
            s["executed"] += 1
            s["today_trades"] = s.get("today_trades", 0) + 1
            s["positions"][code] = {
                "name": res["name"],
                "shares": res["shares"],
                "cost": res["price"],
                "date": datetime.now().strftime("%Y-%m-%d"),
            }
            # ── 实盘持仓台账（打板 Ride 自动卖出的数据源）──
            # 买入成交确认 → 记台账（带当前策略，da_ban 才会被 Ride 自动管理卖出）。
            # OCR 读不到成本价，但这里是我们自己下的单，价/量/日精确。
            try:
                stg = s.get("active_strategy") or ""
                um.record_live_position(uid, code, res["name"] or "",
                                        float(res["price"]), int(res["shares"]), strategy=stg)
            except Exception as _e:
                logger.warning(f"[live台账 uid={uid}] 记录买入失败（不影响下单）: {_e}")
            # 同步执行队列状态：找到这条队列项，标记 executed（含旧版误标 done 的）
            for q in s.get("queue", []):
                if q.get("code") == code and q.get("status") in ("pending", "submitted", "sent", "done"):
                    q["status"] = "executed"
                    break
        elif res["action"] == "SELL":
            s["executed"] += 1
            s["positions"].pop(code, None)
            # 卖出成交 → 移除台账 + 清峰值 + 解锁卖出去重
            try:
                um.remove_live_position(uid, code)
            except Exception as _e:
                logger.warning(f"[live台账 uid={uid}] 移除持仓失败: {_e}")
            _DABAN_PEAKS.pop((uid, code), None)
            _LIVE_SELL_PENDING.discard((uid, code))
    elif status == "submitted":
        # 已报未成交：同步队列为 submitted，前端如实显示"已报实盘"而非"已买"
        for q in s.get("queue", []):
            if q.get("code") == code and q.get("status") in ("pending", "sent", "done"):
                q["status"] = "submitted"
                break
    elif status == "failed":
        # 卖出失败：解锁去重，下轮可重发；若失败原因是"无持仓"说明买单其实没成交=幽灵→清台账
        if res["action"] == "SELL":
            _LIVE_SELL_PENDING.discard((uid, code))
            if any(k in (msg or "") for k in ("无持仓", "持仓不足", "可用不足", "可用为0", "数量为0")):
                try:
                    um.remove_live_position(uid, code)
                except Exception as e:
                    # 幽灵持仓没清掉 → 台账残留会持续占名额/触发无效卖出信号
                    logger.warning(f"[实盘回传 uid={uid}] 清理幽灵持仓失败 {code}: {e}")
                _DABAN_PEAKS.pop((uid, code), None)
        # ★ 桥接器执行失败：把执行队列里的"已买"假象清掉 ★
        # 之前 _auto_buy 把队列项标成 sent/submitted，前端显示"已入队/已买"
        # 现在桥接器明确失败 → 重置回 pending 状态，让用户能看清楚没买进去
        for q in s.get("queue", []):
            if q.get("code") == code and q.get("status") in ("pending", "submitted", "sent"):
                q["status"] = "failed"
                q["fail_msg"] = (msg or "桥接器执行失败")[:80]
                break
    return jsonify({"ok": True})
