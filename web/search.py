"""新闻 / 联网搜索 / TwelveData API 蓝图。"""
import json

from flask import Blueprint, request, jsonify

from analytics import data_manager as dm
from analytics import search_manager as search_mgr
from app_state import require_auth, require_admin_perm

bp = Blueprint("search", __name__)


def _bounded_int_value(raw, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def _bounded_int_arg(name: str, default: int, min_value: int, max_value: int) -> int:
    return _bounded_int_value(request.args.get(name, default), default, min_value, max_value)


@bp.route("/api/stock/news/<code>")
@require_auth
def api_stock_news(code):
    """获取个股新闻 + 情绪分析（search_manager 五级降级）"""
    try:
        name = dm.get_stock_name(code) or code
        from market.news_fetcher import get_stock_news, _simple_sentiment, news_to_context_text
        news = get_stock_news(code, limit=8, name=name)
        sentiment = _simple_sentiment(news)
        return jsonify({
            "ok": True,
            "news": news,
            "sentiment": sentiment,
            "context_text": news_to_context_text(news, code),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "news": [], "sentiment": {}})


@bp.route("/api/search/news/<code>")
@require_auth
def api_search_news(code):
    """个股新闻搜索（Tavily→SerpAPI→直接抓取）"""
    name  = dm.get_stock_name(code) or request.args.get("name", "")
    limit = _bounded_int_arg("limit", 8, 1, 50)
    force = request.args.get("refresh", "0") == "1"
    try:
        results = search_mgr.search_stock_news(code, name=name, limit=limit,
                                               force_refresh=force)
        return jsonify({"ok": True, "results": results, "count": len(results),
                        "code": code, "name": name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "results": []})


@bp.route("/api/search/web", methods=["POST"])
@require_auth
def api_search_web():
    """通用网页搜索（Tavily→SerpAPI）"""
    d     = request.json or {}
    query = d.get("query", "").strip()
    if not query:
        return jsonify({"ok": False, "error": "query 不能为空"})
    topic = d.get("topic", "general")   # general | news | research
    limit = _bounded_int_value(d.get("limit", 10), 10, 1, 50)
    force = bool(d.get("force_refresh", False))
    try:
        results = search_mgr.search_web(query, topic=topic, limit=limit,
                                        force_refresh=force)
        return jsonify({"ok": True, "results": results, "count": len(results)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "results": []})


@bp.route("/api/search/stock_info/<code>")
@require_auth
def api_search_stock_info(code):
    """综合股票信息（新闻+网页+TwelveData报价）"""
    name = dm.get_stock_name(code) or code
    try:
        result = search_mgr.search_stock_info(code, name=name)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/api/search/twelvedata/quote/<code>")
@require_auth
def api_td_quote(code):
    """TwelveData 实时报价（A股）"""
    try:
        q = search_mgr.td_get_quote(code)
        return jsonify({"ok": True, **q})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/api/search/twelvedata/kline/<code>")
@require_auth
def api_td_kline(code):
    """TwelveData K线数据（A股）"""
    days     = _bounded_int_arg("days", 60, 1, 3650)
    interval = request.args.get("interval", "1day")
    try:
        bars = search_mgr.td_get_kline(code, days=days, interval=interval)
        return jsonify({"ok": True, "data": bars, "count": len(bars)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/api/search/symbol_search")
def api_symbol_search():
    """TwelveData 股票代码/名称搜索"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"ok": False, "error": "q 不能为空"})
    try:
        results = search_mgr.td_symbol_search(q)
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "results": []})


@bp.route("/api/search/stats")
@require_auth
def api_search_stats():
    """搜索服务用量统计"""
    return jsonify({"ok": True, **search_mgr.get_stats()})


@bp.route("/api/search/status")
def api_search_status():
    """搜索服务状态（是否有可用额度）"""
    return jsonify({"ok": True, **search_mgr.get_service_status()})


@bp.route("/api/search/config", methods=["GET"])
@require_admin_perm("data_config")
def api_search_config_get():
    """获取搜索服务配置（管理员）"""
    cfg = search_mgr.get_config()
    # 脱敏 key
    safe = json.loads(json.dumps(cfg))
    for svc in ("tavily", "serpapi", "twelvedata"):
        for k in safe.get(svc, {}).get("keys", []):
            raw = k.get("key", "")
            if len(raw) > 12:
                k["key_masked"] = raw[:6] + "****" + raw[-4:]
    return jsonify({"ok": True, "config": safe})


@bp.route("/api/search/config", methods=["POST"])
@require_admin_perm("data_config")
def api_search_config_save():
    """保存搜索服务配置（管理员）"""
    d = request.json or {}
    existing = search_mgr.get_config()
    # 合并（保留未变更的 key）
    for svc in ("tavily", "serpapi", "twelvedata"):
        if svc in d:
            if svc not in existing:
                existing[svc] = {}
            for k2, v2 in d[svc].items():
                if k2 == "keys":
                    # 按 label 匹配更新（不覆盖 ****）
                    for new_key in v2:
                        for old_key in existing[svc].get("keys", []):
                            if old_key.get("label") == new_key.get("label"):
                                for field, val in new_key.items():
                                    if field != "key" or "****" not in str(val):
                                        old_key[field] = val
                else:
                    existing[svc][k2] = v2
    search_mgr.save_config(existing)
    return jsonify({"ok": True})
