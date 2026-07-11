"""静态页面 + 截图文件服务蓝图。"""
from flask import Blueprint, jsonify, send_from_directory

from app_state import BASE_DIR, require_root_admin
from core import changelog_manager as clm
from web._params import _bounded_int_arg

bp = Blueprint("pages", __name__)


def _nocache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]  = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@bp.route("/")
def index():
    # 主前端：index.html 骨架（内联块已拆分到 static/app/，工单7）+ 糖果主题。
    # HTML 从磁盘实时读取（no-store），静态资源走 ETag 协商 → 改前端刷新即生效。
    # /v2 Vite 双轨与 /legacy 兜底入口已于工单7c 移除（frontend/ 归档 backups/）。
    return _nocache(send_from_directory(str(BASE_DIR), "index.html"))


@bp.route("/admin")
def admin_page():
    resp = send_from_directory(str(BASE_DIR), "admin.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/logs/screenshots/<path:filename>")
@require_root_admin
def serve_screenshot(filename):
    """暴露桥接器截图给前端浏览器（仅登录用户可看）

    用于：当日委托查询、xiadan 下单过程诊断截图等。
    路径：logs/screenshots/today_orders_xxx.png / 买入_000001_x.png 等
    """
    # 防穿越：只允许 .png 文件 + 不能含 .. /
    if "/" in filename or "\\" in filename or ".." in filename or not filename.endswith(".png"):
        return jsonify({"ok": False, "error": "非法文件名"}), 403
    scr_dir = BASE_DIR / "logs" / "screenshots"
    full = scr_dir / filename
    if not full.exists():
        return jsonify({"ok": False, "error": "截图不存在"}), 404
    return send_from_directory(str(scr_dir), filename)


# ── 更新日志（公开接口，所有用户可见）──────────────────────────
@bp.route("/api/changelog", methods=["GET"])
def public_changelog():
    """获取已发布的更新日志（登录/未登录均可看）。"""
    limit = _bounded_int_arg("limit", 50, 1, 200)
    items = clm.list_changelog(limit=limit, published_only=True)
    return jsonify({"ok": True, "items": items, "total": clm.count()})
