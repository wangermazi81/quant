# -*- coding: utf-8 -*-
"""策略中心 API 蓝图（从 main.py 迁出，工单4）。

/api/strategies 列表/读取/保存/删除/运行(同步+异步任务)/激活/重命名 共 10 条路由。
路由路径与行为与 main.py 原实现逐行等价。
"""
import json
import logging
import threading
import uuid as _uuid

from flask import Blueprint, jsonify, request, session

from app_state import (
    require_auth, get_user_state, _persist_state,
    _add_log, _deduct, is_root_admin_session, current_admin_permissions,
)
from strategy_engine import strategy_manager as sm

logger = logging.getLogger(__name__)
bp = Blueprint("strategies", __name__)


# 策略选股异步任务表（进程内，重启丢失）
_strat_tasks: dict = {}            # task_id -> {status, result, error, ...}
_strat_tasks_lock = threading.Lock()

def _get_owned_strat_task(task_id: str, uid: int):
    with _strat_tasks_lock:
        task = _strat_tasks.get(task_id)
        if not task or task.get("uid") != uid:
            return None
        return dict(task)


@bp.route("/api/strategies")
def api_list():
    items = sm.list_strategies()
    # ★ 策略隐私：非 admin 看不到内置(自研)策略的简介(desc)，只保留名称供选择/激活
    if not session.get("is_admin"):
        for it in items:
            if it.get("builtin"):
                it["desc"] = "🔒 内置策略，逻辑保密"
    return jsonify(items)


@bp.route("/api/strategies/<filename>")
@require_auth
def api_get(filename):
    meta = sm.get_meta(filename)
    # ★ 策略隐私：内置策略（自研）的源码仅 admin 或被授权管理员可见；其他云端用户拿到"受保护"占位，
    #   防止被查看/复制复用。策略仍可正常激活运行（运行/回测走 sm 内部真实代码，不经此路由）。
    if meta.get("builtin") and not _can_view_protected_source():
        nm = meta.get("name", filename)
        placeholder = (
            '# META: {"name": "%s", "builtin": true}\n'
            '"""\n'
            '🔒 内置策略「%s」— 源码与逻辑均受保护\n\n'
            '这是平台内置策略，选股/交易逻辑及说明均不对外展示。\n'
            '你可以直接「激活」运行它，但无法查看其源码或策略细节。\n'
            '如需自定义策略，请点「+ 新建」编写你自己的（你自建的可正常查看/编辑）。\n'
            '"""\n'
        ) % (nm, nm)
        return jsonify({"code": placeholder, "builtin": True, "locked": True})
    return jsonify({"code": sm.get_code(filename),
                    "builtin": bool(meta.get("builtin")), "locked": False})


def _can_view_protected_source() -> bool:
    return is_root_admin_session() or "strategy_source_view" in current_admin_permissions()


def _inject_builtin_flag(code: str, is_admin: bool) -> str:
    """在策略 META 行注入 builtin 标记，自动落实"admin 创建的策略一律受保护、
    普通用户自建的可见"：admin 存→builtin:true，普通用户存→builtin:false。
    保留 META 其它字段；无 META 行则补一行。"""
    protect = bool(is_admin)
    lines = code.split("\n", 1)
    first = lines[0].strip() if lines else ""
    rest  = lines[1] if len(lines) > 1 else ""
    if first.startswith("# META:"):
        try:
            meta = json.loads(first[len("# META:"):].strip())
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
        meta["builtin"] = protect
        new_first = "# META: " + json.dumps(meta, ensure_ascii=False)
        return new_first + ("\n" + rest if rest != "" or len(lines) > 1 else "\n")
    # 没有 META 行：补一行
    meta = {"name": "策略", "desc": "", "author": ("admin" if protect else "用户"), "builtin": protect}
    return "# META: " + json.dumps(meta, ensure_ascii=False) + "\n" + code


@bp.route("/api/strategies/save", methods=["POST"])
@require_auth
def api_save():
    uid = session["user_id"]
    d   = request.json
    fn  = d.get("filename", "")
    is_admin = bool(session.get("is_admin"))
    # ★ 内置(admin)策略受保护：非 admin 不可保存/覆盖
    if sm.get_meta(fn).get("builtin") and not is_admin:
        return jsonify({"ok": False, "msg": "内置策略受保护，不可修改或覆盖。请点『+ 新建』编写你自己的策略"})
    # ★ 自动打标记：admin 创建/保存的策略一律受保护(builtin:true)，普通用户自建的可见(false)
    code = _inject_builtin_flag(d.get("code", ""), is_admin)
    r   = sm.save(fn, code)
    if r["ok"]:
        _add_log(uid, f"策略已保存: {r['file']}")
    return jsonify(r)


@bp.route("/api/strategies/delete", methods=["POST"])
@require_auth
def api_delete():
    uid = session["user_id"]
    s   = get_user_state(uid)
    fn  = request.json.get("filename", "")
    r   = sm.delete(fn)
    if r["ok"]:
        _add_log(uid, f"策略已删除: {fn}", "WARN")
        if s.get("active_strategy") == fn:
            s["active_strategy"] = None
    return jsonify(r)


@bp.route("/api/strategies/run", methods=["POST"])
@require_auth
def api_run():
    uid = session["user_id"]
    d   = request.get_json(silent=True) or {}
    fn  = (d.get("filename") or "").strip()
    if not fn:
        return jsonify({"ok": False, "error": "invalid filename"}), 400
    cs  = d.get("codes") or []
    # 前端可传 max_codes 控制扫描范围：
    #   0       = 全市场（约 5000 只，2-3 分钟）
    #   600     = 快速扫描（按成交量取前 600 只，约 15 秒）
    #   不传    = 默认全市场
    try:
        mc = int(d.get("max_codes", 0))
    except (TypeError, ValueError):
        mc = 0
    err = _deduct("scan", "全市场选股扫描")
    if err:
        return jsonify(err), 402
    scope_label = "全市场" if not cs else f"{len(cs)}只"
    if not cs and mc > 0:
        scope_label = f"活跃前{mc}只"
    _add_log(uid, f"执行选股: [{fn}] {scope_label}...")
    r       = sm.run(fn, cs, max_codes=mc)
    scanned = r.get("scanned", r.get("count", 0))
    _add_log(uid, f"选股完成，扫描 {scanned} 只，命中 {r.get('count',0)} 只")
    return jsonify(r)


@bp.route("/api/strategies/run_async", methods=["POST"])
@require_auth
def api_run_async():
    """异步选股：立即返回 task_id，前端轮询 /strategies/progress 和 /strategies/result。"""
    uid = session["user_id"]
    d   = request.get_json(silent=True) or {}
    fn  = (d.get("filename") or "").strip()
    if not fn:
        return jsonify({"ok": False, "error": "invalid filename"}), 400
    cs  = d.get("codes") or []
    try:
        mc = int(d.get("max_codes", 0))
    except (TypeError, ValueError):
        mc = 0
    err = _deduct("scan", "全市场选股扫描")
    if err:
        return jsonify(err), 402
    task_id = _uuid.uuid4().hex
    with _strat_tasks_lock:
        _strat_tasks[task_id] = {"status": "pending", "uid": uid, "fn": fn}
    scope_label = "全市场" if not cs else f"{len(cs)}只"
    if not cs and mc > 0:
        scope_label = f"活跃前{mc}只"
    _add_log(uid, f"执行选股(异步): [{fn}] {scope_label}... task={task_id}")
    def _worker():
        try:
            with _strat_tasks_lock:
                _strat_tasks[task_id]["status"] = "running"
            r = sm.run(fn, cs, max_codes=mc)
            with _strat_tasks_lock:
                _strat_tasks[task_id].update({"status": "done", "result": r})
            scanned = r.get("scanned", r.get("count", 0))
            _add_log(uid, f"选股完成(异步)，扫描 {scanned} 只，命中 {r.get('count',0)} 只")
        except Exception as e:
            logger.exception(f"[uid={uid}] async scan 失败 task={task_id}")
            with _strat_tasks_lock:
                _strat_tasks[task_id].update({"status": "error", "error": str(e)})
    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id})


@bp.route("/api/strategies/progress/<task_id>")
@require_auth
def api_strat_progress(task_id):
    """查询异步选股任务状态（前端可 1-2s 轮询）"""
    task = _get_owned_strat_task(task_id, session["user_id"])
    if task is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    snap = {"ok": True, "task_id": task_id, "status": task.get("status")}
    # 借用 strategy_manager 的全局扫描进度
    try:
        from market import ths_quote as tq
        tdx_prog = tq.get_scan_progress()
        strat_prog = sm.get_scan_progress()
        prog = tdx_prog if tdx_prog.get("running") else strat_prog
        if isinstance(prog, dict) and prog.get("running"):
            snap["scan_current"] = int(prog.get("current") or 0)
            snap["scan_total"]   = int(prog.get("total") or 0)
            snap["scan_code"]    = prog.get("code") or ""
            snap["scan_stage"]   = prog.get("stage") or ("行情扫描" if prog is tdx_prog else "策略扫描")
    except Exception:
        pass
    if task.get("status") == "error":
        snap["error"] = task.get("error", "未知错误")
    return jsonify(snap)


@bp.route("/api/strategies/result/<task_id>")
@require_auth
def api_strat_result(task_id):
    """获取异步选股结果（任务完成后调用）"""
    task = _get_owned_strat_task(task_id, session["user_id"])
    if task is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    if task["status"] == "done":
        return jsonify(task.get("result", {"ok": False}))
    if task["status"] == "error":
        return jsonify({"ok": False, "error": task.get("error", "未知错误")})
    return jsonify({"ok": False, "status": task["status"], "msg": "任务未完成"})


@bp.route("/api/strategies/activate", methods=["POST"])
@require_auth
def api_activate():
    uid       = session["user_id"]
    s         = get_user_state(uid)
    fn        = request.json.get("filename")
    was_running = bool(s.get("running"))
    s["active_strategy"] = fn
    if fn:
        s["running"] = True
        _persist_state(uid, active_strategy=fn, running=1)
    else:
        _persist_state(uid, active_strategy="")
    meta_list = sm.list_strategies()
    meta      = next((m for m in meta_list if m["file"] == fn), {})
    label     = meta.get('name', fn) if fn else '默认'
    if fn and not was_running:
        _add_log(uid, f"自动交易策略切换并启动监控: {label}")
    else:
        _add_log(uid, f"自动交易策略切换: {label}")
    return jsonify({"ok": True, "active": fn})

@bp.route("/api/strategies/rename", methods=["POST"])
@require_auth
def api_rename():
    uid    = session["user_id"]
    s      = get_user_state(uid)
    d      = request.json
    old_fn = d.get("old", "")
    new_fn = d.get("new", "")
    r      = sm.rename(old_fn, new_fn)
    if r["ok"]:
        _add_log(uid, f"策略重命名: {old_fn} → {r['file']}")
        if s.get("active_strategy") == old_fn:
            s["active_strategy"] = r["file"]
    return jsonify(r)


# ── 因子库 API ────────────────────────────────────────────────
