"""认证 API 蓝图：注册 / 登录 / 登出 / 当前用户 / 找回密码。"""
import os
import logging

from flask import Blueprint, request, jsonify, session

import user_manager as um
import email_sender as mailer
from app_security import rate_limit, client_ip, login_locked, login_fail, root_login_fail, login_ok

logger = logging.getLogger(__name__)

# 重置链接的站点根地址（生产 .env 里设 APP_BASE_URL=https://jwalpha.top）
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://jwalpha.top").rstrip("/")

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
    username_key = username.lower()
    ident = f"{username_key}|{client_ip()}"
    root_idents = []
    if username_key == um.ROOT_ADMIN_USERNAME.lower():
        root_idents = [f"root:{username_key}|{client_ip()}", f"root:{username_key}"]
    locked = max([login_locked(ident)] + [login_locked(root_ident) for root_ident in root_idents])
    if locked:
        return jsonify({"ok": False,
                        "error": f"尝试次数过多，请约 {max(1, locked // 60)} 分钟后再试",
                        "code": 429}), 429
    r = um.login(username, d.get("password", ""))
    if r["ok"]:
        login_ok(ident)
        for root_ident in root_idents:
            login_ok(root_ident)
        session["user_id"]  = r["user_id"]
        session["username"] = r["username"]
        session["is_admin"] = r["is_admin"]
        session.permanent   = True
    else:
        login_fail(ident)
        for root_ident in root_idents:
            root_login_fail(root_ident)
        if root_idents:
            logger.warning("[security] root admin login failed username=%s ip=%s", username_key, client_ip())
    return jsonify(r)


@bp.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


# ── 找回密码（邮箱自助重置）────────────────────────────────────
@bp.route("/api/auth/forgot", methods=["POST"])
@rate_limit(5, 3600, scope="auth", message="操作过于频繁，请稍后再试")  # 同 IP 每小时最多 5 次，防刷别人邮箱
def auth_forgot():
    """申请重置：按邮箱/用户名找到用户→生成 token→发邮件。

    无论邮箱是否存在都统一返回成功，防止用此接口枚举注册邮箱。
    """
    d = request.json or {}
    ident = (d.get("email") or d.get("username") or "").strip()
    # 统一文案：无论邮箱/用户名是否存在都一样返回，防止借此枚举注册账号。
    generic = {"ok": True, "msg": "已提交找回申请。管理员审核后会重置你的密码，并通过原渠道（QQ/微信等）告知临时密码。"}
    if not ident:
        return jsonify({"ok": False, "error": "请输入注册邮箱或用户名"})
    try:
        user, token = um.create_password_reset(ident)
        if user:
            # 主路径：登记到后台审批队列（无邮件依赖）
            um.create_reset_request(user["id"], user["username"], user["email"])
            # 可选增强：若将来在 .env 配了 SMTP，则同时发一封自助重置邮件
            if token and mailer.smtp_configured():
                link = f"{APP_BASE_URL}/?pwreset={token}"
                html = mailer.render_reset_email(user["username"], link, um.PW_RESET_TTL_MIN)
                r = mailer.send_email(user["email"], "见微 Alpha 密码重置", html)
                if not r.get("ok"):
                    logger.warning(f"[forgot] 邮件发送失败 user={user['username']}: {r.get('error')}")
    except Exception as e:
        logger.warning(f"[forgot] 异常: {e}")
    return jsonify(generic)


@bp.route("/api/auth/reset/validate")
@rate_limit(30, 600, scope="auth")
def auth_reset_validate():
    """前端打开重置页时先校验 token 是否有效（决定显示表单还是失效提示）。"""
    info = um.verify_reset_token(request.args.get("token", ""))
    return jsonify({"ok": bool(info), "username": info["username"] if info else ""})


@bp.route("/api/auth/reset", methods=["POST"])
@rate_limit(10, 3600, scope="auth", message="操作过于频繁，请稍后再试")
def auth_reset():
    """凭 token 设置新密码。"""
    d = request.json or {}
    return jsonify(um.reset_password_with_token(d.get("token", ""), d.get("new_password", "")))


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
        "is_root_admin": um.is_root_admin_user(user),
        "admin_permissions": um.get_admin_permissions(user["id"]),
    })
