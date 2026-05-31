"""管理员 API 蓝图：公告管理 / 日志 / 统计 / 用户与积分管理。"""
from flask import Blueprint, request, jsonify, session

import user_manager as um
from app_state import require_admin

bp = Blueprint("admin", __name__)


# ── 公告管理 ──────────────────────────────────────────────────
@bp.route("/api/admin/announcements", methods=["GET"])
@require_admin
def admin_list_announcements():
    items = um.list_all_announcements()
    return jsonify({"ok": True, "announcements": items})


@bp.route("/api/admin/announcements", methods=["POST"])
@require_admin
def admin_create_announcement():
    d = request.json or {}
    r = um.create_announcement(
        title      = d.get("title", ""),
        content    = d.get("content", ""),
        ann_type   = d.get("type", "info"),
        created_by = session.get("user_id", 0),
        expires_at = d.get("expires_at"),
    )
    return jsonify(r)


@bp.route("/api/admin/announcements/<int:ann_id>", methods=["PUT"])
@require_admin
def admin_update_announcement(ann_id):
    d = request.json or {}
    r = um.update_announcement(ann_id, **{k: v for k, v in d.items()
                                          if k in ("title", "content", "type", "active", "expires_at")})
    return jsonify(r)


@bp.route("/api/admin/announcements/<int:ann_id>", methods=["DELETE"])
@require_admin
def admin_delete_announcement(ann_id):
    return jsonify(um.delete_announcement(ann_id))


@bp.route("/api/admin/logs")
@require_admin
def admin_get_logs():
    uid_filter = request.args.get("uid")
    limit      = int(request.args.get("limit", 500))
    logs = um.get_all_logs(uid=int(uid_filter) if uid_filter else None, limit=limit)
    return jsonify({"ok": True, "logs": logs})


# ── 用户与积分管理 ────────────────────────────────────────────
@bp.route("/api/admin/stats")
@require_admin
def admin_stats():
    return jsonify({"ok": True, **um.stats()})


@bp.route("/api/admin/users")
@require_admin
def admin_users():
    page   = int(request.args.get("page", 1))
    search = request.args.get("search", "")
    return jsonify({"ok": True, **um.list_users(page, 20, search)})


@bp.route("/api/admin/users/<int:uid>/points", methods=["POST"])
@require_admin
def admin_add_points(uid):
    d      = request.json or {}
    amount = int(d.get("amount", 0))
    desc   = d.get("description", "管理员充值")
    r = um.add_points(uid, amount, desc)
    return jsonify(r)


@bp.route("/api/admin/users/<int:uid>/active", methods=["POST"])
@require_admin
def admin_set_active(uid):
    active = request.json.get("active", True)
    if uid == session.get("user_id") and not active:
        return jsonify({"ok": False, "error": "不能禁用自己的账号"})
    return jsonify(um.set_active(uid, active))


@bp.route("/api/admin/users/<int:uid>/admin", methods=["POST"])
@require_admin
def admin_set_admin(uid):
    is_admin = request.json.get("is_admin", False)
    return jsonify(um.set_admin(uid, is_admin))


@bp.route("/api/admin/transactions")
@require_admin
def admin_transactions():
    page = int(request.args.get("page", 1))
    return jsonify({"ok": True, **um.all_point_transactions(page, 30)})


@bp.route("/api/admin/users/<int:uid>/reset_password", methods=["POST"])
@require_admin
def admin_reset_password(uid):
    new_pw = (request.json or {}).get("new_password", "")
    return jsonify(um.admin_reset_password(uid, new_pw))
