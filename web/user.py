"""用户自助 API 蓝图：积分 / 设置 / AI 与券商配置 / 公告（用户侧）。"""
from flask import Blueprint, request, jsonify, session

import user_manager as um
from app_state import require_auth, get_user_state, _persist_state, _add_log

bp = Blueprint("user", __name__)


@bp.route("/api/user/points")
@require_auth
def user_points():
    uid = session["user_id"]
    return jsonify({"ok": True, "points": um.get_points(uid)})


@bp.route("/api/user/points/history")
@require_auth
def user_points_history():
    uid = session["user_id"]
    return jsonify({"ok": True, "history": um.get_point_history(uid, 50)})


@bp.route("/api/user/change_password", methods=["POST"])
@require_auth
def user_change_password():
    d = request.json or {}
    return jsonify(um.change_password(
        session["user_id"],
        d.get("old_password", ""),
        d.get("new_password", ""),
    ))


@bp.route("/api/user/settings", methods=["GET"])
@require_auth
def user_get_settings():
    uid = session["user_id"]
    s   = get_user_state(uid)
    return jsonify({
        "ok":             True,
        "running":        s["running"],
        "auto_execute":   s["auto_execute"],
        "capital":        s["capital"],
        "position_ratio": s["position_ratio"],
        "active_strategy": s["active_strategy"],
    })


@bp.route("/api/user/settings", methods=["POST"])
@require_auth
def user_save_settings():
    uid = session["user_id"]
    d   = request.json or {}
    s   = get_user_state(uid)
    if "capital" in d:
        s["capital"]        = float(d["capital"])
    if "position_ratio" in d:
        s["position_ratio"] = float(d["position_ratio"])
    if "auto_execute" in d:
        s["auto_execute"]   = bool(d["auto_execute"])
    _persist_state(uid,
        capital        = s["capital"],
        position_ratio = s["position_ratio"],
        auto_execute   = int(s["auto_execute"]),
    )
    _add_log(uid, f"账户设置更新: 资金¥{s['capital']:,.0f} "
             f"仓位{s['position_ratio']*100:.0f}%")
    return jsonify({"ok": True})


@bp.route("/api/user/logs")
@require_auth
def user_get_logs():
    uid   = session["user_id"]
    limit = int(request.args.get("limit", 200))
    logs  = um.get_user_logs(uid, limit=limit)
    return jsonify({"ok": True, "logs": logs})


@bp.route("/api/user/ai_config", methods=["GET"])
@require_auth
def user_get_ai_config():
    uid = session["user_id"]
    cfg = um.get_user_ai_config(uid) or {}
    key = cfg.get("api_key", "")
    if len(key) > 8:
        cfg["api_key_masked"] = key[:4] + "****" + key[-4:]
    else:
        cfg["api_key_masked"] = "****" if key else ""
    return jsonify({"ok": True, **cfg})


@bp.route("/api/user/ai_config", methods=["POST"])
@require_auth
def user_save_ai_config():
    uid = session["user_id"]
    d   = request.json or {}
    um.save_user_ai_config(uid, **{k: v for k, v in d.items()
                                   if k in ("api_key", "base_url", "model", "provider", "enabled")})
    _add_log(uid, f"AI配置已更新: provider={d.get('provider','')}")
    return jsonify({"ok": True})


@bp.route("/api/user/broker_config", methods=["GET"])
@require_auth
def user_get_broker_config():
    uid = session["user_id"]
    cfg = um.get_user_broker_config(uid)
    # 隐藏密码/token
    inner = cfg.get("config", {})
    if "password" in inner:
        inner = {**inner, "password": "****"}
    if "access_token" in inner:
        inner = {**inner, "access_token": inner["access_token"][:8] + "****" if inner["access_token"] else ""}
    return jsonify({"ok": True, "broker_type": cfg.get("broker_type", "paper"), "config": inner})


@bp.route("/api/user/broker_config", methods=["POST"])
@require_auth
def user_save_broker_config():
    uid = session["user_id"]
    d   = request.json or {}
    um.save_user_broker_config(uid, d.get("broker_type", "paper"), d.get("config", {}))
    _add_log(uid, f"券商配置已更新: {d.get('broker_type', 'paper')}")
    return jsonify({"ok": True})


# ── 公告（用户侧）──────────────────────────────────────────────
@bp.route("/api/announcements")
def get_announcements():
    uid  = session.get("user_id")
    items = um.get_active_announcements()
    if uid:
        for item in items:
            item["read"] = um.is_announcement_read(uid, item["id"])
    return jsonify({"ok": True, "announcements": items})


@bp.route("/api/announcements/<int:ann_id>/read", methods=["POST"])
@require_auth
def mark_announcement_read(ann_id):
    uid = session["user_id"]
    um.mark_announcement_read(uid, ann_id)
    return jsonify({"ok": True})
