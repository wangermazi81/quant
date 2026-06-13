"""认证 API 蓝图：注册 / 登录 / 登出 / 当前用户。"""
from flask import Blueprint, request, jsonify, session

import user_manager as um
from app_security import rate_limit, client_ip, login_locked, login_fail, login_ok

bp = Blueprint("auth", __name__)


@bp.route("/api/auth/register", methods=["POST"])
@rate_limit(5, 3600, scope="auth", message="注册过于频繁，请稍后再试")  # 同一 IP 每小时最多 5 次，挡批量注册薅羊毛
def auth_register():
    d = request.json or {}
    r = um.register(d.get("username", ""), d.get("email", ""), d.get("password", ""),
                    d.get("invite_code", ""), ip=client_ip())
    return jsonify(r)


@bp.route("/api/auth/login", methods=["POST"])
@rate_limit(20, 300, scope="auth", message="登录尝试过于频繁，请稍后再试")  # 同一 IP 5 分钟最多 20 次
def auth_login():
    d = request.json or {}
    username = (d.get("username", "") or "").strip()
    # 防爆破/撞库：按 用户名+IP 计失败次数，达阈值临时锁定（默认 15 分钟内 5 次 → 锁 15 分钟）。
    # 同时挡住「无限尝试烧 PBKDF2 CPU」的 DoS 入口。
    ident = f"{username.lower()}|{client_ip()}"
    locked = login_locked(ident)
    if locked:
        return jsonify({"ok": False,
                        "error": f"尝试次数过多，请约 {max(1, locked // 60)} 分钟后再试",
                        "code": 429}), 429
    r = um.login(username, d.get("password", ""))
    if r["ok"]:
        login_ok(ident)
        session["user_id"]  = r["user_id"]
        session["username"] = r["username"]
        session["is_admin"] = r["is_admin"]
        session.permanent   = True
    else:
        login_fail(ident)
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
