# -*- coding: utf-8 -*-
"""健康检查 / Vibe激活 / 回测 / 组合优化 API 蓝图（从 main.py 迁出，工单4）。

/api/health、/api/vibe_activations/*(3)、/api/backtest*(3)、
/api/portfolio/optimize 共 8 条路由。路由路径与行为与 main.py 原实现逐行等价。
"""
import logging
from datetime import datetime

from flask import Blueprint, jsonify, request, session

from app_state import (
    require_auth, require_root_admin, get_user_state, _add_log, _deduct,
)
from strategy_engine import strategy_manager as sm
from web._params import (
    _positive_float_value, _float_value, _int_value,
)

logger = logging.getLogger(__name__)
bp = Blueprint("misc", __name__)


@bp.route("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "status": "healthy",
        "service": "jwalpha",
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@bp.route("/api/vibe_activations/me", methods=["GET"])
@require_root_admin
def api_vibe_activation_me():
    uid = session["user_id"]
    try:
        from analytics.factors_zoo import activator as _act
        cfg = _act.get(uid)
        return jsonify({"ok": True, "activation": cfg})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/api/vibe_activations/activate", methods=["POST"])
@require_root_admin
def api_vibe_activation_activate():
    uid = session["user_id"]
    d   = request.json or {}
    factor_id = (d.get("factor_id") or "").strip()
    if not factor_id:
        return jsonify({"ok": False, "error": "factor_id 必填"})
    # 二次保护：检查因子是否存在 + 是否做过回测且胜率 ≥ 55%（强烈建议但不强制）
    try:
        from analytics.factors_zoo import registry as _r, activator as _act
        if factor_id not in _r().list():
            return jsonify({"ok": False, "error": f"因子 {factor_id} 不存在"})
        params = {
            "mode":             d.get("mode", "market_top"),
            "codes":            d.get("codes", []),
            "max_codes":        d.get("max_codes", 500),
            "daily_limit":      d.get("daily_limit", 3),
            "position_pct":     d.get("position_pct", 0.2),
            "score_percentile": d.get("score_percentile", 0.8),
            "stop_loss_pct":    d.get("stop_loss_pct", -0.03),
            "interval_sec":     d.get("interval_sec", 120),
        }
        cfg = _act.activate(uid, factor_id, params)
        _add_log(uid, f"📚 Alpha因子激活: [{factor_id}] 仓位={params['position_pct']*100:.0f}% 日限={params['daily_limit']} 阈值=top{(1-params['score_percentile'])*100:.0f}% 止损={params['stop_loss_pct']*100:.1f}% 每{params['interval_sec']}秒")
        return jsonify({"ok": True, "activation": cfg})
    except Exception as e:
        logger.exception("[vibe.activate] 失败")
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/api/vibe_activations/deactivate", methods=["POST"])
@require_root_admin
def api_vibe_activation_deactivate():
    uid = session["user_id"]
    try:
        from analytics.factors_zoo import activator as _act
        cfg = _act.deactivate(uid)
        _add_log(uid, "📚 Alpha 因子已停用")
        return jsonify({"ok": True, "activation": cfg})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# Vibe Alpha 因子激活调度已下沉 core/schedulers.py（工单4）




@bp.route("/api/backtest", methods=["POST"])
@require_auth
def api_backtest():
    uid      = session["user_id"]
    d        = request.get_json(silent=True) or {}
    fn       = d.get("filename", "")
    code_str = sm.get_code(fn, user_id=uid) if fn else ""
    if not code_str:
        return jsonify({"ok": False, "error": "策略代码为空"})

    raw_codes = d.get("codes")
    if raw_codes and isinstance(raw_codes, str):
        codes = [c.strip() for c in raw_codes.replace("，", ",").split(",") if c.strip()]
    elif raw_codes and isinstance(raw_codes, list):
        codes = raw_codes
    else:
        codes = []

    full_market = not codes
    mode_label  = "全市场" if full_market else f"{len(codes)}只"
    initial_capital = _positive_float_value(d.get("capital"), 100000)
    max_positions = _int_value(d.get("max_positions"), 5)
    stop_loss = _float_value(d.get("stop_loss"), -0.05)
    take_profit = _float_value(d.get("take_profit"), 0.10)
    if initial_capital is None:
        return jsonify({"ok": False, "error": "invalid capital"}), 400
    if max_positions is None or max_positions <= 0:
        return jsonify({"ok": False, "error": "invalid max_positions"}), 400
    if stop_loss is None:
        return jsonify({"ok": False, "error": "invalid stop_loss"}), 400
    if take_profit is None:
        return jsonify({"ok": False, "error": "invalid take_profit"}), 400

    bt_kwargs   = dict(
        strategy_code   = code_str,
        codes           = codes,
        start_date      = d.get("start_date", "2024-01-01"),
        end_date        = d.get("end_date",   "2024-12-31"),
        initial_capital = initial_capital,
        max_positions   = max_positions,
        stop_loss       = stop_loss,
        take_profit     = take_profit,
    )
    err = _deduct("backtest", "策略回测")
    if err:
        return jsonify(err), 402

    if full_market:
        from strategy_engine.backtest import start_backtest_async
        task_id = start_backtest_async(**bt_kwargs, uid=uid)
        _add_log(uid, f"全市场回测已启动 [{fn}] {d.get('start_date')}~{d.get('end_date')} task={task_id}")
        return jsonify({"ok": True, "async": True, "task_id": task_id})
    else:
        from strategy_engine.backtest import run_backtest
        _add_log(uid, f"回测: [{fn}] {d.get('start_date')}~{d.get('end_date')} {mode_label}")
        result = run_backtest(**bt_kwargs)
        if result.get("ok"):
            m = result["metrics"]
            _add_log(uid, f"回测完成 收益={m['total_return']}% 回撤={m['max_drawdown']}% "
                     f"胜率={m['win_rate']}% Sharpe={m['sharpe']}")
        else:
            _add_log(uid, f"回测失败: {result.get('error','')}", "error")
        return jsonify(result)


@bp.route("/api/backtest/task/<task_id>")
@require_auth
def api_backtest_task(task_id):
    from strategy_engine import backtest
    task = backtest._bt_tasks.get(task_id)
    if not task or task.get("uid") != session["user_id"]:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify(backtest.get_backtest_task(task_id))


@bp.route("/api/backtest/cancel/<task_id>", methods=["POST"])
@require_auth
def api_backtest_cancel(task_id):
    uid = session["user_id"]
    from strategy_engine import backtest
    task = backtest._bt_tasks.get(task_id)
    if not task or task.get("uid") != uid:
        return jsonify({"ok": False, "error": "not found"}), 404
    result = backtest.cancel_backtest_task(task_id)
    if result.get("ok"):
        _add_log(uid, f"⏹ 回测已终止: task={task_id}", "WARN")
    return jsonify(result)


# ── 投资组合优化 API ──────────────────────────────────────────
@bp.route("/api/portfolio/optimize", methods=["POST"])
@require_auth
def api_portfolio_optimize():
    """
    投资组合优化。
    ---
    parameters:
      - in: body
        schema:
          properties:
            codes:  {type: array, items: {type: string}}
            mode:   {type: string, enum: [equal_weight, max_sharpe, min_variance, risk_parity]}
            capital:{type: number}
    responses:
      200:
        description: 各股权重及建议仓位
    """
    uid = session["user_id"]
    d   = request.get_json(silent=True) or {}
    codes   = d.get("codes", [])
    mode    = d.get("mode", "equal_weight")
    capital = _positive_float_value(d.get("capital"), get_user_state(uid)["capital"])
    if capital is None:
        return jsonify({"ok": False, "error": "invalid capital"}), 400
    if not codes:
        return jsonify({"ok": False, "error": "codes 不能为空"})
    try:
        from analytics.portfolio_optimizer import optimize_portfolio
        result = optimize_portfolio(codes, mode=mode, capital=capital)
        _add_log(uid, f"组合优化完成: mode={mode} codes={len(codes)}")
        return jsonify(result)
    except ImportError:
        return jsonify({"ok": False, "error": "portfolio_optimizer 模块未找到"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


