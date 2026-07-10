"""问题反馈 API 蓝图。

用户在前端提交反馈 → 落库 → 管理员后台查看/处理。
数据结构稳定（见 feedback_manager），便于日后 OpenClaw 巡检消费。
"""
import logging

from flask import Blueprint, request, jsonify, session

from core import feedback_manager as fbm
from app_state import require_auth, require_admin_perm, _add_log

logger = logging.getLogger(__name__)

bp = Blueprint("feedback", __name__)


@bp.route("/api/feedback", methods=["POST"])
@require_auth
def submit_feedback():
    """提交反馈（登录用户）。"""
    uid = session["user_id"]
    d   = request.json or {}
    content = (d.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "error": "请填写反馈内容"}), 400
    content  = content[:4000]
    category = d.get("category", "other")
    page     = (d.get("page") or "")[:80]
    contact  = (d.get("contact") or "")[:120]
    ua       = (request.headers.get("User-Agent") or "")[:300]
    version  = (d.get("app_version") or "")[:40]
    fid = fbm.add_feedback(uid, session.get("username", ""), category,
                           page, content, contact, ua, version)
    _add_log(uid, f"提交问题反馈 #{fid}（{category}）")
    return jsonify({"ok": True, "id": fid})


@bp.route("/api/feedback/mine", methods=["GET"])
@require_auth
def my_feedback():
    """查看自己的反馈与处理状态。"""
    uid = session["user_id"]
    return jsonify({"ok": True, "items": fbm.get_user_feedback(uid)})


@bp.route("/api/feedback", methods=["GET"])
@require_admin_perm("content_manage")
def admin_list_feedback():
    """管理员列表（可 ?status=open 过滤）。"""
    status = request.args.get("status") or None
    items  = fbm.list_feedback(status=status)
    return jsonify({"ok": True, "items": items, "counts": fbm.count_by_status()})


@bp.route("/api/feedback/<int:fid>/status", methods=["POST"])
@require_admin_perm("content_manage")
def admin_update_feedback(fid):
    """管理员更新状态 / 备注。"""
    d      = request.json or {}
    status = d.get("status")
    note   = d.get("admin_note")
    if status is not None and status not in fbm.STATUSES:
        return jsonify({"ok": False, "error": "无效状态"}), 400
    ok = fbm.update_feedback(fid, status=status, admin_note=note)
    if ok:
        uid = session.get("user_id")
        if uid:
            _add_log(uid, f"处理反馈 #{fid} → status={status} note={'有' if note else '无'}")
    return jsonify({"ok": ok})
