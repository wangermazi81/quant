"""静态页面 + 截图文件服务蓝图。"""
from flask import Blueprint, jsonify, send_from_directory

from app_state import BASE_DIR, require_auth

bp = Blueprint("pages", __name__)


def _nocache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]  = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@bp.route("/")
def index():
    # 主前端 index.html 已套用糖果主题（theme-candy.css），从磁盘实时读取 → 改样式免重启。
    # 模块化 Vite 构建版本在 /v2 并行可选。
    return _nocache(send_from_directory(str(BASE_DIR), "index.html"))


@bp.route("/v2")
def index_v2():
    """新版前端（frontend/ 经 Vite 构建到 static/dist/）。登录后逐页验收功能对等。"""
    dist = BASE_DIR / "static" / "dist"
    if not (dist / "index.html").exists():
        return ("新版前端尚未构建：请在 frontend/ 下执行 npm install && npm run build", 503)
    return _nocache(send_from_directory(str(dist), "index.html"))


@bp.route("/legacy")
def index_legacy():
    """旧版单文件 UI 兜底入口（切换期保留）。"""
    return _nocache(send_from_directory(str(BASE_DIR), "index.html"))


@bp.route("/admin")
def admin_page():
    resp = send_from_directory(str(BASE_DIR), "admin.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/logs/screenshots/<path:filename>")
@require_auth
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
