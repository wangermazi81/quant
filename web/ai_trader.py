# -*- coding: utf-8 -*-
"""AI 自主交易模块 —— REST + SSE 蓝图（本地，不同步云端）。

把 analytics/ai_trader.py 引擎暴露给前端「🤖 AI交易」面板。
所有路由 require_auth；按 session 的 user_id 取对应引擎实例。
"""
import json

from flask import Blueprint, Response, jsonify, request, session

from app_state import require_auth
import ai_trader as engine

bp = Blueprint("ai_trader", __name__)


def _uid():
    return session["user_id"]


def _mask_key(k: str) -> str:
    if not k:
        return ""
    return (k[:4] + "•" * 6 + k[-4:]) if len(k) > 10 else "••••••"


# ── 元信息：角色 + 服务商预设（页面初始化用）──────────────
@bp.route("/api/ai_trader/meta")
@require_auth
def meta():
    return jsonify({"ok": True, "roles": engine.roles_meta(),
                    "providers": engine.list_providers()})


# ── 模型配置（交易模块单独配）────────────────────────────
@bp.route("/api/ai_trader/config", methods=["GET"])
@require_auth
def get_config():
    cfg = engine.load_trader_config()
    return jsonify({"ok": True, "config": {
        "provider": cfg.get("provider", ""),
        "base_url": cfg.get("base_url", ""),
        "model":    cfg.get("model", ""),
        "enabled":  cfg.get("enabled", False),
        "api_key_mask": _mask_key(cfg.get("api_key", "")),
        "has_key":  bool(cfg.get("api_key")),
    }})


@bp.route("/api/ai_trader/config", methods=["POST"])
@require_auth
def set_config():
    cfg = engine.save_trader_config(request.json or {})
    return jsonify({"ok": True, "config": {
        "provider": cfg.get("provider"), "base_url": cfg.get("base_url"),
        "model": cfg.get("model"), "enabled": cfg.get("enabled"),
        "has_key": bool(cfg.get("api_key")),
    }})


@bp.route("/api/ai_trader/config/test", methods=["POST"])
@require_auth
def test_config():
    return jsonify(engine.test_trader_config(request.json or {}))


@bp.route("/api/ai_trader/detect_models", methods=["POST"])
@require_auth
def detect_models():
    """填 Key 后探测可用模型列表（与后台「AI接口设置」同款体验）。"""
    d = request.json or {}
    return jsonify(engine.detect_models(d.get("api_key", ""), d.get("base_url", "")))


# ── 设置（候选池 / 节奏 / 风控）──────────────────────────
@bp.route("/api/ai_trader/settings", methods=["GET"])
@require_auth
def get_settings():
    return jsonify({"ok": True, "settings": engine.load_settings(_uid())})


@bp.route("/api/ai_trader/settings", methods=["POST"])
@require_auth
def set_settings():
    return jsonify({"ok": True, "settings": engine.save_settings(_uid(), request.json or {})})


@bp.route("/api/ai_trader/watchlist", methods=["POST"])
@require_auth
def watchlist():
    uid = _uid()
    d = request.json or {}
    op = d.get("op")
    code = str(d.get("code", "")).strip()
    st = engine.load_settings(uid)
    wl = list(st.get("watchlist", []))
    if op == "add" and code and code not in wl:
        wl.append(code)
    elif op == "remove" and code in wl:
        wl.remove(code)
    st = engine.save_settings(uid, {"watchlist": wl})
    return jsonify({"ok": True, "watchlist": st["watchlist"]})


# ── 引擎控制 ──────────────────────────────────────────────
@bp.route("/api/ai_trader/start", methods=["POST"])
@require_auth
def start():
    return jsonify(engine.get_engine(_uid()).start())


@bp.route("/api/ai_trader/stop", methods=["POST"])
@require_auth
def stop():
    return jsonify(engine.get_engine(_uid()).stop())


@bp.route("/api/ai_trader/emergency_stop", methods=["POST"])
@require_auth
def emergency_stop():
    return jsonify(engine.get_engine(_uid()).emergency_stop())


@bp.route("/api/ai_trader/run_once", methods=["POST"])
@require_auth
def run_once():
    return jsonify(engine.get_engine(_uid()).run_once())


@bp.route("/api/ai_trader/confirm", methods=["POST"])
@require_auth
def confirm():
    d = request.json or {}
    return jsonify(engine.get_engine(_uid()).confirm(
        d.get("id", ""), bool(d.get("approve"))))


@bp.route("/api/ai_trader/status")
@require_auth
def status():
    return jsonify({"ok": True, **engine.get_engine(_uid()).status()})


@bp.route("/api/ai_trader/snapshot")
@require_auth
def snapshot():
    return jsonify({"ok": True, **engine.get_engine(_uid()).snapshot()})


@bp.route("/api/ai_trader/stop_once", methods=["POST"])
@require_auth
def stop_once():
    return jsonify(engine.get_engine(_uid()).stop_once())


# ── AI 独立模拟账户（与主模拟盘隔离，单独在本界面展示）──────
@bp.route("/api/ai_trader/account")
@require_auth
def account():
    return jsonify({"ok": True, **engine.get_engine(_uid()).ai_account()})


@bp.route("/api/ai_trader/account/reset", methods=["POST"])
@require_auth
def account_reset():
    d = request.json or {}
    cap = d.get("initial_capital")
    try:
        cap = float(cap) if cap not in (None, "") else None
    except (TypeError, ValueError):
        cap = None
    return jsonify(engine.get_engine(_uid()).reset_ai_account(cap))


# ── SSE 实时流（团队思考 / 决策 / 执行 / 待确认）──────────
@bp.route("/api/ai_trader/stream")
@require_auth
def stream():
    uid = _uid()

    def gen():
        q = engine.subscribe(uid)
        try:
            snap = engine.get_engine(uid).snapshot()
            yield f"event: snapshot\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield (f"event: {msg['event']}\n"
                           f"data: {json.dumps(msg['data'], ensure_ascii=False)}\n\n")
                except Exception:
                    yield ": heartbeat\n\n"
        finally:
            engine.unsubscribe(uid, q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
