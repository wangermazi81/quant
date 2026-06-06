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


@bp.route("/api/admin/users/<int:uid>/set_points", methods=["POST"])
@require_admin
def admin_set_points(uid):
    """把用户积分设为指定值（可增可减），区别于 /points 的增量充值。"""
    d     = request.json or {}
    value = d.get("value")
    desc  = d.get("description", "管理员修改积分")
    return jsonify(um.set_points(uid, value, desc))


@bp.route("/api/admin/users/<int:uid>", methods=["DELETE"])
@require_admin
def admin_delete_user(uid):
    """删除用户（不可删自己）。"""
    if uid == session.get("user_id"):
        return jsonify({"ok": False, "error": "不能删除自己的账号"})
    return jsonify(um.delete_user(uid))


# ── 邀请码管理（邀请制注册）──────────────────────────────────
@bp.route("/api/admin/invite_codes", methods=["GET"])
@require_admin
def admin_list_invites():
    """列出全部邀请码（含生成者、名额、已用、奖励、状态）。"""
    return jsonify({"ok": True, "codes": um.list_invite_codes()})


@bp.route("/api/admin/invite_codes", methods=["POST"])
@require_admin
def admin_create_invite():
    """生成邀请码：max_uses 名额 / reward 每邀请奖励积分 / note 备注 / expires_at 可选过期。"""
    d = request.json or {}
    r = um.create_invite_code(
        creator_uid   = session.get("user_id", 0),
        max_uses      = d.get("max_uses", 1),
        reward        = d.get("reward"),
        note          = d.get("note", ""),
        is_admin_code = True,
        expires_at    = d.get("expires_at"),
    )
    return jsonify(r)


@bp.route("/api/admin/invite_codes/<int:cid>", methods=["DELETE"])
@require_admin
def admin_revoke_invite(cid):
    """作废邀请码（停止可用，不删历史）。"""
    return jsonify(um.revoke_invite_code(cid))
