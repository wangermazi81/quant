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
from web._concurrency import heavy_slot, HeavyBusy

logger = logging.getLogger(__name__)
bp = Blueprint("misc", __name__)

# 同步回测允许的最大股票数；超过则自动转异步（避免在 HTTP 请求里跑分钟级任务）
MAX_SYNC_BACKTEST_CODES = 50


@bp.route("/api/health")
def api_health():
    # 带上 SSE 占用：线程池被长连接吃干是历史事故的直接原因，
    # 出问题时这一眼就能看出「是不是又被 SSE 占满了」，不必去猜。
    # 注意只暴露计数与上限，不含任何用户数据（test_health_api 有防泄漏断言）。
    try:
        from web._concurrency import sse_stats
        _sse = sse_stats()
        sse_info = {"active": _sse["total"], "limit": _sse["global_limit"]}
    except Exception:
        sse_info = None
    return jsonify({
        "ok": True,
        "status": "healthy",
        "service": "jwalpha",
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sse": sse_info,
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

    # 内置策略靠 sys.path 做兼容导入，豁免 AST 检查（与 strategy_manager.run 同口径）
    is_builtin = bool((sm.get_meta(fn, user_id=uid) or {}).get("builtin"))
    bt_kwargs   = dict(
        strategy_code   = code_str,
        is_builtin      = is_builtin,
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

    # 同步分支原本对 codes 长度没有任何上限，传 3000 个代码进来就是在 HTTP 请求里
    # 跑分钟级回测，直接占死一个 gthread（threads 有限，几个这种请求就能拖死全站）。
    # 超过阈值就改走已有的异步分支——返回体与全市场分支同形，前端本来就会处理。
    if full_market or len(codes) > MAX_SYNC_BACKTEST_CODES:
        from strategy_engine.backtest import start_backtest_async
        task_id = start_backtest_async(**bt_kwargs, uid=uid)
        _scope = "全市场" if full_market else f"{len(codes)}只(超过{MAX_SYNC_BACKTEST_CODES}转异步)"
        _add_log(uid, f"{_scope}回测已启动 [{fn}] {d.get('start_date')}~{d.get('end_date')} task={task_id}")
        return jsonify({"ok": True, "async": True, "task_id": task_id})

    from strategy_engine.backtest import run_backtest
    try:
        with heavy_slot("backtest_sync"):
            _add_log(uid, f"回测: [{fn}] {d.get('start_date')}~{d.get('end_date')} {mode_label}")
            result = run_backtest(**bt_kwargs)
    except HeavyBusy:
        # 占不到名额时不排队（排队就是占着线程等），直接转异步
        from strategy_engine.backtest import start_backtest_async
        task_id = start_backtest_async(**bt_kwargs, uid=uid)
        _add_log(uid, f"同步回测名额已满，转异步 [{fn}] task={task_id}")
        return jsonify({"ok": True, "async": True, "task_id": task_id})

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


