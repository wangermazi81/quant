"""投研中心 API 蓝图：产业链概念 / 研报（挂在智能搜索页）。

- GET  /api/chain/concepts?q=      概念板块列表/搜索（公开，1小时缓存）
- GET  /api/chain/stocks?name=     概念成分股行情（公开，10分钟缓存）
- GET  /api/chain/graph?name=      读取已生成的产业链图谱（公开）
- POST /api/chain/graph            AI 生成产业链图谱（登录+积分 chain_graph）
- GET  /api/reports?q=             个股研报（代码或名称，公开，6小时缓存）
"""
import logging
import re

from flask import Blueprint, request, jsonify

from app_security import rate_limit

from app_state import require_auth, _deduct

logger = logging.getLogger(__name__)
bp = Blueprint("research", __name__)


@bp.route("/api/chain/concepts")
def api_chain_concepts():
    import research_center as rc
    q = (request.args.get("q") or "").strip()
    return jsonify({"ok": True, "concepts": rc.get_concepts(q)})


@bp.route("/api/chain/stocks")
@rate_limit(20, 60, scope="public_research")
def api_chain_stocks():
    import research_center as rc
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "缺少概念名称"}), 400
    if len(name) > 40 or not re.fullmatch(r"[\w\u4e00-\u9fff·&（）()+＋\- ]+", name):
        return jsonify({"ok": False, "error": "概念名称格式错误"}), 400
    result = rc.get_concept_stocks(name)
    result["graph_exists"] = rc.load_chain(name) is not None
    if not result["graph_exists"]:
        # 无图谱 → 后台自动排队生成（AI key 已配置时），前端提示"生成中"并稍后自动刷新
        result["graph_generating"] = rc.enqueue_chain_autogen(name)
    return jsonify(result)


@bp.route("/api/chain/graph")
def api_chain_graph_get():
    import research_center as rc
    name = (request.args.get("name") or "").strip()
    g = rc.load_chain(name) if name else None
    if g:
        return jsonify(g)
    return jsonify({"ok": False, "error": "该概念尚未生成产业链图谱", "exists": False})


@bp.route("/api/chain/graph", methods=["POST"])
@require_auth
def api_chain_graph_gen():
    import research_center as rc
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "invalid name"}), 400
    err = _deduct("chain_graph", f"产业链图谱 {name}")
    if err:
        return jsonify(err)
    return jsonify(rc.generate_chain(name))


@bp.route("/api/reports/latest")
def api_reports_latest():
    """研报中心：最新研报流。qtype 0=个股 1=行业 3=策略；q=代码/名称/行业/关键词。"""
    import research_center as rc
    try:
        qtype = int(request.args.get("qtype", 0))
    except Exception:
        qtype = 0
    q    = (request.args.get("q") or "").strip()
    code = rc.resolve_code(q) if (q and qtype == 0) else ""
    items = rc.get_latest_reports(qtype=qtype, code=code, q="" if code else q)
    return jsonify({"ok": True, "reports": items, "code": code, "qtype": qtype})


@bp.route("/api/reports")
def api_reports():
    import research_center as rc
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "请输入股票代码或名称"})
    code = rc.resolve_code(q)
    if not code:
        return jsonify({"ok": False, "error": f"未找到股票「{q}」，请输入6位代码或准确名称"})
    result = rc.get_reports(code)
    try:
        from analytics import data_manager as dm
        result["name"] = dm.get_stock_name(code) or code
    except Exception:
        result["name"] = code
    return jsonify(result)
