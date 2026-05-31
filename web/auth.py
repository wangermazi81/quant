"""认证 API 蓝图：注册 / 登录 / 登出 / 当前用户。"""
from flask import Blueprint, request, jsonify, session

import user_manager as um

bp = Blueprint("auth", __name__)


@bp.route("/api/auth/register", methods=["POST"])
def auth_register():
    d = request.json or {}
    r = um.register(d.get("username", ""), d.get("email", ""), d.get("password", ""))
    return jsonify(r)


@bp.route("/api/auth/login", methods=["POST"])
def auth_login():
    d = request.json or {}
    r = um.login(d.get("username", ""), d.get("password", ""))
    if r["ok"]:
        session["user_id"]  = r["user_id"]
        session["username"] = r["username"]
        session["is_admin"] = r["is_admin"]
        session.permanent   = True
    return jsonify(r)


@bp.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@bp.route("/api/auth/me")
def auth_me():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"ok": False, "logged_in": False})
    user = um.get_user(uid)
    if not user:
        session.clear()
        return jsonify({"ok": False, "logged_in": False})
    return jsonify({
        "ok":        True,
        "logged_in": True,
        "user_id":   user["id"],
        "username":  user["username"],
        "points":    user["points"],
        "is_admin":  bool(user["is_admin"]),
    })
