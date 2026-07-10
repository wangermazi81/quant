# -*- coding: utf-8 -*-
"""因子库 + Vibe Alpha 因子动物园 API 蓝图（从 main.py 迁出，工单4）。

/api/factors/*（自定义因子 8 条）+ /api/factors_zoo/*（因子动物园 6 条）。
路由路径与行为与 main.py 原实现逐行等价。
"""
import logging
import threading
import uuid as _uuid

from flask import Blueprint, jsonify, request, session

from app_state import (
    require_auth, _add_log,
)
from core import user_manager as um
from analytics import factor_manager as fm
from analytics import ai_analyst as ai
from strategy_engine import strategy_manager as sm
from web._params import (
    _int_value,
)
from web.strategies import _can_view_protected_source  # 内置因子源码保护（与策略同一套判定）

logger = logging.getLogger(__name__)
bp = Blueprint("factors", __name__)


@bp.route("/api/factors")
def api_factor_list():
    return jsonify(fm.list_factors())


@bp.route("/api/factors/template")
def api_factor_template():
    return jsonify({"code": fm.new_factor_template()})


@bp.route("/api/factors/run", methods=["POST"])
@require_auth
def api_factor_run():
    uid = session["user_id"]
    d   = request.json
    fn  = d.get("filename", "")
    cs  = d.get("codes") or []
    try:
        mc = int(d.get("max_codes", 0))
    except (TypeError, ValueError):
        mc = 0
    scope_label = "全市场" if not cs else f"{len(cs)}只"
    if not cs and mc > 0:
        scope_label = f"活跃前{mc}只"
    _add_log(uid, f"执行因子选股: [{fn}] {scope_label}...")
    r       = fm.run(fn, cs, max_codes=mc)
    scanned = r.get("scanned", r.get("count", 0))
    _add_log(uid, f"因子选股完成，扫描 {scanned} 只，命中 {r.get('count', 0)} 只")
    return jsonify(r)


@bp.route("/api/factors/save", methods=["POST"])
@require_auth
def api_factor_save():
    uid  = session["user_id"]
    d    = request.json
    fn   = d.get("filename", "").strip()
    code = d.get("code", "")
    if not fn:
        return jsonify({"ok": False, "msg": "文件名不能为空"})
    r = fm.save(fn, code)
    if r.get("ok"):
        _add_log(uid, f"因子已保存: [{fn}]")
    return jsonify(r)


@bp.route("/api/factors/delete", methods=["POST"])
@require_auth
def api_factor_delete():
    uid = session["user_id"]
    fn  = (request.json or {}).get("filename", "").strip()
    r   = fm.delete(fn)
    if r.get("ok"):
        _add_log(uid, f"因子已删除: [{fn}]")
    return jsonify(r)


@bp.route("/api/factors/ai_generate", methods=["POST"])
@require_auth
def api_factor_ai_generate():
    uid  = session["user_id"]
    desc = (request.json or {}).get("description", "").strip()
    if not desc:
        return jsonify({"ok": False, "error": "描述不能为空"})
    _add_log(uid, f"AI生成因子策略: {desc[:30]}...")
    # 全局配置为权威，per-user 仅兜底（与 ai_analyze 一致，避免旧 key 顶掉新配置导致 401）
    cfg = ai.resolve_config(um.get_user_ai_config(uid) or {})
    r = ai.generate_factor(desc, config_override=cfg)
    if r.get("ok"):
        _add_log(uid, f"AI因子生成完成，共{r.get('length', len(r.get('code','')))}字符")
    return jsonify(r)


@bp.route("/api/factors/<filename>")
@require_auth
def api_factor_get(filename):
    meta = fm.get_meta(filename) if hasattr(fm, "get_meta") else {"name": filename, "builtin": True}
    if meta.get("builtin", True) and not _can_view_protected_source():
        name = meta.get("name") or filename
        placeholder = (
            '# FACTOR: {"name": "%s", "builtin": true}\n'
            '"""\n'
            'Protected built-in factor. Source code and core logic are not exposed through Web/API.\n'
            'Create a custom factor if you need editable source.\n'
            '"""\n'
        ) % name
        return jsonify({"code": placeholder, "builtin": True, "locked": True})
    return jsonify({"code": fm.get_code(filename), "builtin": bool(meta.get("builtin")), "locked": False})


@bp.route("/api/factors/evaluate", methods=["POST"])
@require_auth
def api_factor_evaluate():
    """
    因子评价：计算 IC / ICIR / RankIC / t-stat。
    ---
    parameters:
      - in: body
        schema:
          properties:
            filename: {type: string}
            codes:    {type: array, items: {type: string}}
            period:   {type: integer, default: 20}
    responses:
      200:
        description: 因子评价指标
    """
    uid = session["user_id"]
    d   = request.get_json(silent=True) or {}
    fn  = d.get("filename", "")
    cs  = d.get("codes") or []
    period = _int_value(d.get("period"), 20)
    if period is None or period <= 0:
        return jsonify({"ok": False, "error": "invalid period"}), 400
    period = max(1, min(250, period))
    _add_log(uid, f"因子评价: [{fn}] period={period}")
    try:
        r = fm.evaluate(fn, cs, period=period)
        return jsonify(r)
    except AttributeError:
        return jsonify({"ok": False, "error": "因子管理器暂不支持 evaluate 方法"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})



# ──────────────────────────────────────────────────────────────
#  Vibe Alpha 因子库 (HKUDS/Vibe-Trading 移植)
#  - /api/factors_zoo/list:           列出所有可用 alpha（带元数据）
#  - /api/factors_zoo/compute:        同步算分（< 1000 只，前端等结果）
#  - /api/factors_zoo/compute_async:  异步算分（启动后台任务，返回 task_id）
#  - /api/factors_zoo/progress/<id>:  查询任务进度
#  - /api/factors_zoo/result/<id>:    取任务结果
#  完全独立于 factor_manager，不参与自动监控扫描/下单链路
# ──────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────
# 异步任务表（进程内，重启丢失；阶段 1 够用）
_fz_tasks: dict = {}            # task_id -> {status, progress, total, ...}
_fz_tasks_lock = threading.Lock()
@bp.route("/api/factors_zoo/list")
@require_auth
def api_factors_zoo_list():
    """列出 Vibe Alpha 因子库的所有可用因子"""
    try:
        from analytics.factors_zoo import registry as _fz_registry
        reg = _fz_registry()
        items = []
        for aid in reg.list():
            try:
                h = reg.get(aid)
                m = h.meta or {}
                items.append({
                    "id":              h.id,
                    "zoo":             h.zoo,
                    "nickname":        m.get("nickname") or h.id,
                    "description_cn":  m.get("description_cn", ""),
                    "theme":           m.get("theme", []),
                    "formula":         m.get("formula_latex", ""),
                    "universe":        m.get("universe", []),
                    "frequency":       m.get("frequency", []),
                    "decay_horizon":   m.get("decay_horizon", 0),
                    "min_warmup_bars": m.get("min_warmup_bars", 0),
                    "columns_required": m.get("columns_required", []),
                    "notes":           m.get("notes", ""),
                })
            except Exception as e:
                logger.warning(f"[factors_zoo] meta 读取失败 {aid}: {e}")
        return jsonify({
            "ok": True,
            "factors": sorted(items, key=lambda x: x["id"]),
            "health": reg.health(),
        })
    except Exception as e:
        logger.exception("[factors_zoo] list 失败")
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/api/factors_zoo/compute", methods=["POST"])
@require_auth
def api_factors_zoo_compute():
    """对一组股票算指定 alpha 的分数

    入参:
      factor_id:  str           必填，例 "gtja191_001"
      mode:       str           "manual" (默认) | "market_top"
      codes:      list[str]     mode=manual 时必填，例 ["600519","000001"]
      max_codes:  int           mode=market_top 时取活跃前 N 只（默认 500，上限 1000）
      days:       int           回看天数（默认 60，最少 30，最多 250）
    返回:
      ok:           bool
      factor_meta:  dict
      scores:       {code: float|None}  按分数降序，None 列在末尾
      ranked:       [{code,name,score,rank}]  含名称的排序结果
      scanned:      int       实际算到 panel 里的股票数
      took_ms:      int       服务端总耗时
    """
    import time as _time
    uid = session["user_id"]
    d   = request.json or {}
    factor_id = (d.get("factor_id") or "").strip()
    mode      = (d.get("mode") or "manual").strip()
    try:
        days = max(30, min(250, int(d.get("days", 60))))
    except (TypeError, ValueError):
        days = 60
    try:
        # 0 = 全市场无上限（~5000+ 只）；>0 = 取活跃前 N 只
        max_codes = int(d.get("max_codes", 500))
        if max_codes != 0:
            max_codes = max(10, min(10000, max_codes))
    except (TypeError, ValueError):
        max_codes = 500

    if not factor_id:
        return jsonify({"ok": False, "error": "factor_id 必填"})

    t_start = _time.time()
    try:
        from analytics.factors_zoo import registry as _fz_registry, build_panel, latest_score
        from market.market_data import get_kline as _get_kline
        reg = _fz_registry()
        if factor_id not in reg.list():
            return jsonify({"ok": False, "error": f"因子 {factor_id} 不存在，已注册: {reg.list()[:10]}"})

        # ── 确定股票池 ──
        if mode == "market_top":
            try:
                universe, _qcache = sm._full_market_universe_with_quotes(max_codes=max_codes)
            except Exception as e:
                return jsonify({"ok": False, "error": f"全市场拉取失败: {e}"})
            if not universe:
                return jsonify({"ok": False, "error": "无法获取全市场行情（TDX 未连接），请稍后重试或手动输入代码"})
            codes = list(universe)
            scope_label = f"活跃前{len(codes)}只"
        else:
            raw_codes = d.get("codes") or []
            if isinstance(raw_codes, str):
                codes = [c.strip() for c in raw_codes.replace("，", ",").split(",") if c.strip()]
            else:
                codes = [str(c).strip() for c in raw_codes if str(c).strip()]
            if not codes:
                return jsonify({"ok": False, "error": "manual 模式 codes 必填"})
            if len(codes) > 1000:
                return jsonify({"ok": False, "error": f"代码数 {len(codes)} 超过 1000 上限"})
            scope_label = f"{len(codes)}只"
            _qcache = {}

        _add_log(uid, f"Vibe因子计算: [{factor_id}] {scope_label} days={days}")

        # ── 预热检查（友好错误，比"NaN 占比过高"更好理解）──
        try:
            _meta_check = reg.get(factor_id).meta or {}
            _warmup = int(_meta_check.get("min_warmup_bars", 0) or 0)
            if _warmup > 0 and days < _warmup + 5:
                return jsonify({
                    "ok": False,
                    "error": f"因子 {factor_id} 需要至少 {_warmup} 天历史数据进行预热（当前回看 {days} 天）。"
                             f"请把「回看天数」调到 {_warmup + 20} 天以上再算。",
                    "min_warmup_bars": _warmup,
                    "suggested_days": _warmup + 20,
                })
        except Exception:
            pass

        # ── 拼面板 + 计算 ──
        # 全市场用 12 线程并行拉 K 线（手动模式 ≤ 50 只用 8 线程足够）
        _workers = 12 if mode == "market_top" else 8
        panel = build_panel(codes, _get_kline, days=days, workers=_workers)
        if not panel or "close" not in panel:
            return jsonify({"ok": False, "error": "没有任何股票能拿到 K 线数据，请检查网络/缓存"})

        # 检查实际拿到的 K 线行数是否够预热
        _actual_rows = panel["close"].shape[0]
        if _warmup > 0 and _actual_rows < _warmup:
            return jsonify({
                "ok": False,
                "error": f"实际拉到的 K 线只有 {_actual_rows} 天，不足因子 {factor_id} 所需的 {_warmup} 天预热。"
                         f"原因：股票上市不足 / 缓存数据不全 / 数据源限流。建议换一只历史悠久的股票或减小回看要求。",
                "actual_rows": _actual_rows,
                "min_warmup_bars": _warmup,
            })

        df = reg.compute(factor_id, panel)
        scores = latest_score(df, mode="per_column")
        scanned = panel["close"].shape[1]

        # ── 拼名称 + 价格（用于直接灌入选股池）+ 排序 ──
        # market_top 模式 _qcache 已有 quotes；manual 模式现拉一次
        quote_map: dict = {}
        if mode == "market_top" and _qcache:
            for q in _qcache.values():
                if isinstance(q, dict) and q.get("code"):
                    quote_map[q["code"]] = q
        else:
            try:
                import market.ths_quote as _tq
                _client = _tq.get_client()
                if _client and _client._ok:
                    for q in (_client.get_quotes(codes) or []):
                        if q and q.get("code"):
                            quote_map[q["code"]] = q
            except Exception as e:
                logger.debug(f"[factors_zoo] manual 模式 quote 拉取失败: {e}")

        # 现价 fallback：quote 拿不到时退到 panel.close 最新值（非交易时段必备）
        _close_panel = panel.get("close")
        ranked = []
        for code, val in scores.items():
            q = quote_map.get(code, {})
            price = float(q.get("price") or 0)
            if price <= 0 and _close_panel is not None and code in _close_panel.columns:
                try:
                    _last_close = _close_panel[code].dropna()
                    if len(_last_close) > 0:
                        price = float(_last_close.iloc[-1])
                except Exception:
                    pass
            ranked.append({
                "code":   code,
                "name":   q.get("name", ""),
                "price":  price,
                "change": float(q.get("change_pct") or 0),
                "score":  val,
            })
        # None 排在最后，其余按降序
        ranked.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
        for i, r in enumerate(ranked):
            r["rank"] = i + 1

        meta = reg.get(factor_id).meta or {}
        took_ms = int((_time.time() - t_start) * 1000)
        _add_log(uid, f"Vibe因子完成: [{factor_id}] scanned={scanned} took={took_ms}ms")
        return jsonify({
            "ok": True,
            "factor_id": factor_id,
            "factor_meta": {
                "nickname":        meta.get("nickname") or factor_id,
                "description_cn":  meta.get("description_cn", ""),
                "theme":           meta.get("theme", []),
                "formula":         meta.get("formula_latex", ""),
                "universe":        meta.get("universe", []),
                "min_warmup_bars": meta.get("min_warmup_bars", 0),
                "notes":           meta.get("notes", ""),
            },
            "scores":  scores,
            "ranked":  ranked,
            "scanned": scanned,
            "requested": len(codes),
            "took_ms": took_ms,
        })
    except Exception as e:
        logger.exception(f"[factors_zoo] compute 失败 factor_id={factor_id}")
        return jsonify({"ok": False, "error": str(e), "took_ms": int((_time.time() - t_start) * 1000)})


def _fz_run_async_compute(task_id: str, uid: int, factor_id: str, codes: list,
                          quote_map: dict, days: int):
    """后台线程执行因子计算，结果存到 _fz_tasks[task_id]"""
    import time as _time
    from analytics.factors_zoo import registry as _fz_registry, build_panel, latest_score
    from market.market_data import get_kline as _get_kline
    t0 = _time.time()
    try:
        # 注意：build_panel 内部用 factor_manager 的 _scan_prog 上报进度
        # 我们这里只把任务从 init → running → done 三态切
        with _fz_tasks_lock:
            _fz_tasks[task_id]["status"] = "running"
            _fz_tasks[task_id]["phase"]  = "拉取K线"
        panel = build_panel(codes, _get_kline, days=days, workers=12)
        if not panel or "close" not in panel:
            with _fz_tasks_lock:
                _fz_tasks[task_id].update({"status": "error", "error": "无任何 K 线数据"})
            return

        with _fz_tasks_lock:
            _fz_tasks[task_id]["phase"] = "计算因子分数"
        reg = _fz_registry()
        df = reg.compute(factor_id, panel)
        scores = latest_score(df, mode="per_column")
        scanned = panel["close"].shape[1]

        with _fz_tasks_lock:
            _fz_tasks[task_id]["phase"] = "整理结果"
        # 现价 fallback：quote 拿不到时退到 panel.close 最新值（非交易时段必备）
        _close_panel = panel.get("close")
        ranked = []
        for code, val in scores.items():
            q = quote_map.get(code, {})
            price = float(q.get("price") or 0)
            if price <= 0 and _close_panel is not None and code in _close_panel.columns:
                try:
                    _last_close = _close_panel[code].dropna()
                    if len(_last_close) > 0:
                        price = float(_last_close.iloc[-1])
                except Exception:
                    pass
            ranked.append({
                "code":   code,
                "name":   q.get("name", ""),
                "price":  price,
                "change": float(q.get("change_pct") or 0),
                "score":  val,
            })
        ranked.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
        for i, r in enumerate(ranked):
            r["rank"] = i + 1

        meta = reg.get(factor_id).meta or {}
        took_ms = int((_time.time() - t0) * 1000)
        result = {
            "ok": True,
            "factor_id": factor_id,
            "factor_meta": {
                "nickname":        meta.get("nickname") or factor_id,
                "description_cn":  meta.get("description_cn", ""),
                "theme":           meta.get("theme", []),
                "formula":         meta.get("formula_latex", ""),
                "universe":        meta.get("universe", []),
                "min_warmup_bars": meta.get("min_warmup_bars", 0),
            },
            "scores":  scores,
            "ranked":  ranked,
            "scanned": scanned,
            "requested": len(codes),
            "took_ms": took_ms,
        }
        with _fz_tasks_lock:
            _fz_tasks[task_id].update({"status": "done", "result": result, "took_ms": took_ms})
        _add_log(uid, f"Vibe异步完成: [{factor_id}] scanned={scanned} took={took_ms}ms")
    except Exception as e:
        logger.exception(f"[factors_zoo] async compute 失败 task={task_id}")
        with _fz_tasks_lock:
            _fz_tasks[task_id].update({"status": "error", "error": str(e)})


@bp.route("/api/factors_zoo/compute_async", methods=["POST"])
@require_auth
def api_factors_zoo_compute_async():
    """启动异步因子计算（适合全市场扫描）

    入参同 /compute，多支持 max_codes=0（全市场无上限 ~5000+ 只）
    返回：{ok, task_id} —— 客户端凭 task_id 轮询 /progress 和 /result
    """
    uid = session["user_id"]
    d   = request.json or {}
    factor_id = (d.get("factor_id") or "").strip()
    mode      = (d.get("mode") or "manual").strip()
    try:
        days = max(30, min(250, int(d.get("days", 60))))
    except (TypeError, ValueError):
        days = 60
    try:
        max_codes = int(d.get("max_codes", 0))
        if max_codes != 0:
            max_codes = max(10, min(10000, max_codes))
    except (TypeError, ValueError):
        max_codes = 0

    if not factor_id:
        return jsonify({"ok": False, "error": "factor_id 必填"})

    try:
        from analytics.factors_zoo import registry as _fz_registry
        reg = _fz_registry()
        if factor_id not in reg.list():
            return jsonify({"ok": False, "error": f"因子 {factor_id} 不存在"})

        # 确定股票池 + quote
        quote_map = {}
        if mode == "market_top":
            try:
                universe, _qcache = sm._full_market_universe_with_quotes(max_codes=max_codes)
            except Exception as e:
                return jsonify({"ok": False, "error": f"全市场拉取失败: {e}"})
            if not universe:
                return jsonify({"ok": False, "error": "无法获取全市场行情"})
            codes = list(universe)
            for q in (_qcache or {}).values():
                if isinstance(q, dict) and q.get("code"):
                    quote_map[q["code"]] = q
        else:
            raw_codes = d.get("codes") or []
            if isinstance(raw_codes, str):
                codes = [c.strip() for c in raw_codes.replace("，", ",").split(",") if c.strip()]
            else:
                codes = [str(c).strip() for c in raw_codes if str(c).strip()]
            if not codes:
                return jsonify({"ok": False, "error": "manual 模式 codes 必填"})

        task_id = _uuid.uuid4().hex[:12]
        scope_label = f"活跃{max_codes or '全市场'}只" if mode == "market_top" else f"{len(codes)}只"
        with _fz_tasks_lock:
            _fz_tasks[task_id] = {
                "status":    "pending",
                "phase":     "排队中",
                "factor_id": factor_id,
                "scope":     scope_label,
                "total":     len(codes),
                "created":   _time.time() if (_time := __import__("time")) else 0,
            }
        threading.Thread(
            target=_fz_run_async_compute,
            args=(task_id, uid, factor_id, codes, quote_map, days),
            daemon=True,
            name=f"fz-{task_id}",
        ).start()
        _add_log(uid, f"Vibe异步启动: [{factor_id}] {scope_label} task={task_id}")
        return jsonify({"ok": True, "task_id": task_id, "scope": scope_label, "total": len(codes)})
    except Exception as e:
        logger.exception(f"[factors_zoo] compute_async 启动失败")
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/api/factors_zoo/progress/<task_id>")
@require_auth
def api_factors_zoo_progress(task_id):
    """查询异步任务进度（轻量级，前端可 1s 轮询）"""
    with _fz_tasks_lock:
        task = _fz_tasks.get(task_id)
        if not task:
            return jsonify({"ok": False, "error": "任务不存在"})
        snap = {
            "ok":        True,
            "task_id":   task_id,
            "status":    task.get("status"),
            "phase":     task.get("phase", ""),
            "scope":     task.get("scope", ""),
            "factor_id": task.get("factor_id", ""),
            "total":     task.get("total", 0),
        }
    # 借用 sm._scan_prog（factor_manager / strategy_manager 共享的全局扫描进度）
    try:
        from strategy_engine import strategy_manager as _sm_mod
        prog = getattr(_sm_mod, "_scan_prog", None) or getattr(_sm_mod, "_tdx_prog", {})
        if isinstance(prog, dict) and prog.get("running"):
            snap["scan_current"] = int(prog.get("current") or 0)
            snap["scan_total"]   = int(prog.get("total") or 0)
            snap["scan_code"]    = prog.get("code") or ""
    except Exception:
        pass
    if snap["status"] == "error":
        with _fz_tasks_lock:
            snap["error"] = _fz_tasks[task_id].get("error", "未知错误")
    return jsonify(snap)


@bp.route("/api/factors_zoo/backtest", methods=["POST"])
@require_auth
def api_factors_zoo_backtest():
    """因子回测（同步，前端等结果；大池子用 codes 限制规模）

    入参:
      factor_id:      str    必填
      codes:          list/str  股票池（manual 模式）
      mode:           "manual" | "market_top"
      max_codes:      int    market_top 模式时取活跃前 N（默认 100，回测建议 ≤ 500）
      days:           int    历史天数（默认 200）
      top_k:          int    每次调仓选前 K 只（默认 10）
      rebalance_days: int    调仓周期（默认 5）
      hold_days:      int    持有天数（默认 5）
      fee_rate:       float  单边费率（默认 0）
    """
    uid = session["user_id"]
    d   = request.json or {}
    factor_id = (d.get("factor_id") or "").strip()
    mode      = (d.get("mode") or "manual").strip()
    try:    days = max(60, min(400, int(d.get("days", 200))))
    except Exception: days = 200
    try:    top_k = max(1, min(50, int(d.get("top_k", 10))))
    except Exception: top_k = 10
    try:    rebalance_days = max(1, min(30, int(d.get("rebalance_days", 5))))
    except Exception: rebalance_days = 5
    try:    hold_days = max(1, min(rebalance_days, int(d.get("hold_days", 5))))
    except Exception: hold_days = 5
    try:    fee_rate = max(0.0, min(0.01, float(d.get("fee_rate", 0))))
    except Exception: fee_rate = 0.0
    try:    max_codes = max(10, min(1000, int(d.get("max_codes", 100))))
    except Exception: max_codes = 100

    if not factor_id:
        return jsonify({"ok": False, "error": "factor_id 必填"})

    try:
        if mode == "market_top":
            universe, _qcache = sm._full_market_universe_with_quotes(max_codes=max_codes)
            codes = list(universe or [])
            if not codes:
                return jsonify({"ok": False, "error": "全市场拉取失败"})
        else:
            raw = d.get("codes") or []
            if isinstance(raw, str):
                codes = [c.strip() for c in raw.replace("，", ",").split(",") if c.strip()]
            else:
                codes = [str(c).strip() for c in raw if str(c).strip()]
            if not codes:
                return jsonify({"ok": False, "error": "codes 必填"})

        _add_log(uid, f"Vibe因子回测: [{factor_id}] {len(codes)}只 days={days} top_k={top_k} hold={hold_days}")
        from analytics.factors_zoo.backtest import backtest_factor
        from market.market_data import get_kline as _get_kline
        result = backtest_factor(
            factor_id=factor_id,
            codes=codes,
            get_kline=_get_kline,
            days=days,
            top_k=top_k,
            rebalance_days=rebalance_days,
            hold_days=hold_days,
            fee_rate=fee_rate,
        )
        if result.get("ok"):
            s = result["stats"]
            _add_log(uid, f"回测完成: 总收益={s['total_return']*100:.2f}% 胜率={s['win_rate']*100:.1f}% IC={s['ic_mean']:.3f}")
        return jsonify(result)
    except Exception as e:
        logger.exception(f"[factors_zoo] backtest 失败")
        return jsonify({"ok": False, "error": str(e)})


# ──────────────────────────────────────────────────────────────
#  Vibe 因子激活 API（激活后调度器每 5 分钟自动算分+开单）
# ──────────────────────────────────────────────────────────────


@bp.route("/api/factors_zoo/result/<task_id>")
@require_auth
def api_factors_zoo_result(task_id):
    """取异步任务结果（仅 status=done 时返回完整 ranked）"""
    with _fz_tasks_lock:
        task = _fz_tasks.get(task_id)
        if not task:
            return jsonify({"ok": False, "error": "任务不存在"})
        if task.get("status") != "done":
            return jsonify({"ok": False, "error": f"任务尚未完成: status={task.get('status')}"})
        return jsonify(task["result"])


# ── 回测 API ──────────────────────────────────────────────────
