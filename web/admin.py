"""管理员 API 蓝图：公告管理 / 日志 / 统计 / 用户与积分管理。"""
from flask import Blueprint, request, jsonify, session

import user_manager as um
import app_security as sec
from app_state import require_admin, require_admin_perm, require_root_admin

bp = Blueprint("admin", __name__)


# ── 公告管理 ──────────────────────────────────────────────────
@bp.route("/api/admin/announcements", methods=["GET"])
@require_admin_perm("content_manage")
def admin_list_announcements():
    items = um.list_all_announcements()
    return jsonify({"ok": True, "announcements": items})


@bp.route("/api/admin/announcements", methods=["POST"])
@require_admin_perm("content_manage")
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
@require_admin_perm("content_manage")
def admin_update_announcement(ann_id):
    d = request.json or {}
    r = um.update_announcement(ann_id, **{k: v for k, v in d.items()
                                          if k in ("title", "content", "type", "active", "expires_at")})
    return jsonify(r)


@bp.route("/api/admin/announcements/<int:ann_id>", methods=["DELETE"])
@require_admin_perm("content_manage")
def admin_delete_announcement(ann_id):
    return jsonify(um.delete_announcement(ann_id))


@bp.route("/api/admin/logs")
@require_admin_perm("security_audit")
def admin_get_logs():
    uid_filter = request.args.get("uid")
    limit      = int(request.args.get("limit", 500))
    logs = um.get_all_logs(uid=int(uid_filter) if uid_filter else None, limit=limit)
    return jsonify({"ok": True, "logs": logs})


@bp.route("/api/admin/security/events")
@require_admin_perm("security_audit")
def admin_security_events():
    limit = int(request.args.get("limit", 200))
    ip = (request.args.get("ip") or "").strip()
    event_type = (request.args.get("event_type") or "").strip()
    return jsonify({
        "ok": True,
        "events": sec.list_security_events(limit=limit, ip=ip, event_type=event_type),
    })


@bp.route("/api/admin/security/ip_blocks")
@require_admin_perm("security_audit")
def admin_security_ip_blocks():
    return jsonify({"ok": True, "blocks": sec.list_ip_blocks()})


@bp.route("/api/admin/security/ip_blocks", methods=["POST"])
@require_admin_perm("security_audit")
def admin_security_block_ip():
    d = request.json or {}
    ip = (d.get("ip") or "").strip()
    if not ip:
        return jsonify({"ok": False, "error": "ip required"}), 400
    seconds = int(d.get("seconds") or 900)
    reason = (d.get("reason") or "manual admin block").strip()
    sec.block_ip(ip, seconds=seconds, reason=reason)
    sec.record_security_event(
        "manual_ip_block",
        severity="medium",
        ip=ip,
        user_id=session.get("user_id"),
        detail=reason,
    )
    return jsonify({"ok": True, "blocks": sec.list_ip_blocks()})


@bp.route("/api/admin/security/ip_blocks/<path:ip>", methods=["DELETE"])
@require_admin_perm("security_audit")
def admin_security_release_ip(ip):
    ip = (ip or "").strip()
    sec.release_ip_block(ip)
    sec.record_security_event(
        "manual_ip_release",
        severity="medium",
        ip=ip,
        user_id=session.get("user_id"),
    )
    return jsonify({"ok": True, "blocks": sec.list_ip_blocks()})


# ── 用户与积分管理 ────────────────────────────────────────────
@bp.route("/api/admin/stats")
@require_admin_perm("dashboard_view")
def admin_stats():
    return jsonify({"ok": True, **um.stats()})


@bp.route("/api/admin/permissions/catalog")
@require_admin
def admin_permissions_catalog():
    return jsonify({"ok": True, "permissions": um.admin_permission_catalog()})


@bp.route("/api/admin/users")
@require_admin_perm("user_manage")
def admin_users():
    page   = int(request.args.get("page", 1))
    search = request.args.get("search", "")
    return jsonify({"ok": True, **um.list_users(page, 20, search)})


@bp.route("/api/admin/users/<int:uid>/points", methods=["POST"])
@require_admin_perm("points_manage")
def admin_add_points(uid):
    d      = request.json or {}
    amount = int(d.get("amount", 0))
    desc   = d.get("description", "管理员充值")
    r = um.add_points(uid, amount, desc)
    return jsonify(r)


@bp.route("/api/admin/users/<int:uid>/active", methods=["POST"])
@require_admin_perm("user_manage")
def admin_set_active(uid):
    active = request.json.get("active", True)
    if uid == session.get("user_id") and not active:
        return jsonify({"ok": False, "error": "不能禁用自己的账号"})
    return jsonify(um.set_active(uid, active))


@bp.route("/api/admin/users/<int:uid>/admin", methods=["POST"])
@require_root_admin
def admin_set_admin(uid):
    d = request.json or {}
    is_admin = d.get("is_admin", False)
    r = um.set_admin(uid, is_admin)
    if r.get("ok") and is_admin:
        perms = d.get("permissions")
        if perms is not None:
            pr = um.set_admin_permissions(uid, perms, granted_by=session.get("user_id"))
            if not pr.get("ok"):
                return jsonify(pr), 400
            r["permissions"] = pr.get("permissions", [])
    return jsonify(r)


@bp.route("/api/admin/users/<int:uid>/permissions", methods=["GET"])
@require_root_admin
def admin_get_user_permissions(uid):
    user = um.get_user(uid)
    if not user:
        return jsonify({"ok": False, "error": "user not found"}), 404
    return jsonify({
        "ok": True,
        "user_id": uid,
        "is_root_admin": um.is_root_admin_user(user),
        "permissions": um.get_admin_permissions(uid),
    })


@bp.route("/api/admin/users/<int:uid>/permissions", methods=["POST"])
@require_root_admin
def admin_save_user_permissions(uid):
    d = request.json or {}
    r = um.set_admin_permissions(uid, d.get("permissions", []), granted_by=session.get("user_id"))
    return (jsonify(r), 200 if r.get("ok") else 400)


@bp.route("/api/admin/transactions")
@require_admin_perm("points_manage")
def admin_transactions():
    page = int(request.args.get("page", 1))
    return jsonify({"ok": True, **um.all_point_transactions(page, 30)})


@bp.route("/api/admin/users/<int:uid>/reset_password", methods=["POST"])
@require_admin_perm("user_manage")
def admin_reset_password(uid):
    new_pw = (request.json or {}).get("new_password", "")
    return jsonify(um.admin_reset_password(uid, new_pw))


# ── 找回密码申请（后台审批重置）────────────────────────────────
@bp.route("/api/admin/reset_requests", methods=["GET"])
@require_admin_perm("user_manage")
def admin_list_reset_requests():
    """列出找回密码申请（默认仅 pending）。"""
    status = request.args.get("status", "pending")
    return jsonify({"ok": True, "requests": um.list_reset_requests(status),
                    "pending": um.count_pending_reset_requests()})


@bp.route("/api/admin/reset_requests/<int:req_id>/resolve", methods=["POST"])
@require_admin_perm("user_manage")
def admin_resolve_reset_request(req_id):
    """审批通过：生成临时密码并重置，返回临时密码供管理员转告用户。"""
    return jsonify(um.resolve_reset_request(req_id, session.get("user_id", 0)))


@bp.route("/api/admin/reset_requests/<int:req_id>/reject", methods=["POST"])
@require_admin_perm("user_manage")
def admin_reject_reset_request(req_id):
    """驳回申请（不重置密码）。"""
    return jsonify(um.reject_reset_request(req_id, session.get("user_id", 0)))


@bp.route("/api/admin/users/<int:uid>/set_points", methods=["POST"])
@require_admin_perm("points_manage")
def admin_set_points(uid):
    """把用户积分设为指定值（可增可减），区别于 /points 的增量充值。"""
    d     = request.json or {}
    value = d.get("value")
    desc  = d.get("description", "管理员修改积分")
    return jsonify(um.set_points(uid, value, desc))


@bp.route("/api/admin/users/<int:uid>", methods=["DELETE"])
@require_admin_perm("user_manage")
def admin_delete_user(uid):
    """删除用户（不可删自己）。"""
    if uid == session.get("user_id"):
        return jsonify({"ok": False, "error": "不能删除自己的账号"})
    return jsonify(um.delete_user(uid))


# ── AI/功能调用积分设置 ───────────────────────────────────────
@bp.route("/api/admin/point_costs", methods=["GET"])
@require_admin_perm("points_manage")
def admin_get_point_costs():
    """各功能调用积分消耗（0=免费）。"""
    return jsonify({"ok": True, "costs": um.get_point_costs(),
                    "labels": um.POINT_COST_LABELS})


@bp.route("/api/admin/point_costs", methods=["POST"])
@require_admin_perm("points_manage")
def admin_set_point_costs():
    """保存积分消耗配置：内存即时生效 + data/point_costs.json 持久化。"""
    d = request.json or {}
    return jsonify(um.set_point_costs(d.get("costs", d)))


# ── 邀请码管理（邀请制注册）──────────────────────────────────
@bp.route("/api/admin/invite_codes", methods=["GET"])
@require_admin_perm("invite_manage")
def admin_list_invites():
    """列出全部邀请码（含生成者、名额、已用、奖励、状态）。"""
    return jsonify({"ok": True, "codes": um.list_invite_codes()})


@bp.route("/api/admin/invite_codes", methods=["POST"])
@require_admin_perm("invite_manage")
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
@require_admin_perm("invite_manage")
def admin_revoke_invite(cid):
    """作废邀请码（停止可用，不删历史）。"""
    return jsonify(um.revoke_invite_code(cid))


# ── 更新日志管理 ──────────────────────────────────────────────
import changelog_manager as clm

@bp.route("/api/admin/changelog", methods=["GET"])
@require_admin_perm("content_manage")
def admin_list_changelog():
    """管理员列出全部更新日志（含未发布）。"""
    items = clm.list_changelog(published_only=False)
    return jsonify({"ok": True, "items": items})


@bp.route("/api/admin/changelog", methods=["POST"])
@require_admin_perm("content_manage")
def admin_create_changelog():
    """管理员创建更新日志。"""
    d = request.json or {}
    title = (d.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "标题不能为空"}), 400
    cid = clm.add_changelog(
        title=title,
        content=d.get("content", ""),
        version=d.get("version", ""),
        change_type=d.get("change_type", "notice"),
        created_by=session.get("user_id", 0),
        is_published=d.get("is_published", 1),
    )
    return jsonify({"ok": True, "id": cid})


@bp.route("/api/admin/changelog/<int:cid>", methods=["PUT"])
@require_admin_perm("content_manage")
def admin_update_changelog(cid):
    """管理员更新日志。"""
    d = request.json or {}
    ok = clm.update_changelog(cid, **{k: v for k, v in d.items()
                                       if k in ("version", "title", "content", "change_type", "is_published")})
    return jsonify({"ok": ok})


@bp.route("/api/admin/changelog/<int:cid>", methods=["DELETE"])
@require_admin_perm("content_manage")
def admin_delete_changelog(cid):
    """管理员删除更新日志。"""
    return jsonify({"ok": clm.delete_changelog(cid)})
