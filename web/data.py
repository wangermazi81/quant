"""数据管理 + K 线 API 蓝图。"""
import threading
import uuid
import logging
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, session

import data_manager as dm
import market_data as md
from app_state import require_auth, _add_log

logger = logging.getLogger(__name__)

bp = Blueprint("data", __name__)


def _most_recent_trading_day() -> str:
    """最近一个可能的交易日（粗略：仅跳过周末，不含节假日）。

    用于判断本地 CSV 是否「足够新」。宁可保守：节假日会被当成交易日，
    导致多走一次 TDX 实时拉取——只是多一点延迟，不会拿到错误数据。
    """
    d  = datetime.now()
    wd = d.weekday()          # 0=周一 .. 6=周日
    if wd == 5:               # 周六 → 周五
        d -= timedelta(days=1)
    elif wd == 6:             # 周日 → 周五
        d -= timedelta(days=2)
    return d.strftime("%Y-%m-%d")


def _request_trade_date(code: str) -> str:
    explicit = (request.args.get("date") or "").strip().replace("-", "")[:8]
    if explicit:
        return explicit
    try:
        book = md.get_order_book(code)
        trade_time = str(book.get("trade_time") or "")
        if len(trade_time) >= 10:
            return trade_time[:10].replace("-", "")
    except Exception as exc:
        logger.debug(f"latest trade date lookup failed {code}: {exc}")
    return datetime.now().strftime("%Y%m%d")


@bp.route("/api/data/stocks")
def api_data_list():
    return jsonify(dm.list_stocks())


@bp.route("/api/data/download", methods=["POST"])
@require_auth
def api_data_download():
    uid        = session["user_id"]
    d          = request.json
    raw_codes  = d.get("codes", "")
    start_date = d.get("start_date", "2020-01-01")
    if isinstance(raw_codes, str):
        codes = [c.strip() for c in raw_codes.replace("，", ",").split(",") if c.strip()]
    else:
        codes = raw_codes
    if not codes:
        return jsonify({"ok": False, "msg": "请输入股票代码"})
    task_id = str(uuid.uuid4())[:8]
    _add_log(uid, f"开始下载 {len(codes)} 只股票历史数据...")
    dm.start_download(codes, start_date, task_id)
    return jsonify({"ok": True, "task_id": task_id, "total": len(codes)})


@bp.route("/api/data/progress/<task_id>")
def api_data_progress(task_id):
    return jsonify(dm.get_progress(task_id))


@bp.route("/api/data/delete", methods=["POST"])
@require_auth
def api_data_delete():
    uid  = session["user_id"]
    code = request.json.get("code", "")
    r    = dm.delete_stock(code)
    if r["ok"]:
        _add_log(uid, f"已删除本地数据: {code}", "WARN")
    return jsonify(r)


@bp.route("/api/data/refresh_names", methods=["POST"])
@require_auth
def api_refresh_names():
    uid = session["user_id"]
    threading.Thread(target=dm.refresh_name_cache, daemon=True).start()
    _add_log(uid, "开始刷新股票名称缓存...")
    return jsonify({"ok": True, "msg": "后台刷新中"})


@bp.route("/api/data/sector/<code>")
def api_stock_sector(code):
    r         = dm.get_stock_sector(code)
    r["name"] = dm.get_stock_name(code)
    return jsonify(r)


@bp.route("/api/kline/<code>")
def api_kline(code):
    """K 线数据：本地已是最新交易日才走本地快路径，否则优先 TDX 实时日K，
    TDX 不可用再退回本地并标记 stale（避免一直显示陈旧 K 线）。"""
    # 1) 读本地缓存（仅作为「已最新」快路径或最终兜底）
    local_records = None
    local_last    = ""
    try:
        df = dm.get_local(code)
        if df is not None and not df.empty and len(df) >= 10:
            cols = [c for c in ["date", "open", "close", "high", "low", "volume"] if c in df.columns]
            sub  = df[cols].tail(120).copy()
            sub["date"] = sub["date"].apply(
                lambda d: d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
            )
            local_records = sub.to_dict("records")
            local_last    = local_records[-1]["date"] if local_records else ""
    except Exception:
        pass

    # 2) 本地已覆盖最近交易日 → 直接用（快、且不算陈旧）
    if local_records and local_last >= _most_recent_trading_day():
        name = dm.get_stock_name(code) or code
        return jsonify({"ok": True, "code": code, "name": name,
                        "source": "local", "stale": False, "data": local_records})

    # 3) 本地缺失或过期 → 优先 TDX 实时日K（含当日）
    try:
        import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            bars = client.get_kline(code, 120)
            if bars and len(bars) >= 10:
                name = tq.get_stock_name(code) or dm.get_stock_name(code) or code
                return jsonify({"ok": True, "code": code, "name": name,
                                "source": "tdx", "stale": False, "data": bars})
    except Exception as e:
        logger.warning(f"K 线 TDX 失败: {e}")

    # 4) TDX 不可用 → 退回本地（可能过期，标记 stale 让前端提示）
    if local_records:
        name = dm.get_stock_name(code) or code
        return jsonify({"ok": True, "code": code, "name": name,
                        "source": "local", "stale": True,
                        "msg": f"实时源暂不可用，显示本地缓存（最新至 {local_last}），"
                               f"可在「数据管理」重新下载更新",
                        "data": local_records})

    return jsonify({"ok": False, "data": [], "msg": "暂无数据，请先在「数据管理」下载"})


@bp.route("/api/kline/<code>/seconds")
def api_second_kline(code):
    """Second-level tick data for the stock chart modal."""
    try:
        date_text = _request_trade_date(code)
        raw = md.get_second_ticks(code, date_text)
        times = raw.get("datetime") or []
        if not times:
            return jsonify({"ok": False, "code": code, "data": [], "source": "tushare_ticks", "msg": "no second data"})
        data = []
        fallback = [None] * len(times)
        zeroes = [0] * len(times)
        blanks = [""] * len(times)
        for idx, dt in enumerate(times):
            dt_text = str(dt)
            data.append({
                "date": dt_text[11:19] if len(dt_text) >= 19 else dt_text,
                "datetime": dt_text,
                "price": (raw.get("price") or fallback)[idx],
                "volume": (raw.get("volume") or zeroes)[idx],
                "amount": (raw.get("amount") or zeroes)[idx],
                "prev_price": (raw.get("prev_price") or fallback)[idx],
                "kind": (raw.get("kind") or blanks)[idx],
            })
        return jsonify({"ok": True, "code": code, "source": "tushare_ticks", "freq": "tick", "data": data})
    except Exception as exc:
        logger.warning(f"second kline api failed {code}: {exc}")
        return jsonify({"ok": False, "code": code, "data": [], "error": str(exc)})


@bp.route("/api/quote/<code>/order_book")
def api_order_book(code):
    """Realtime five-level bid/ask book for the stock chart modal."""
    try:
        book = md.get_order_book(code)
        if not book:
            return jsonify({"ok": False, "code": code, "data": {}, "msg": "no order book"})
        return jsonify({"ok": True, "code": code, "source": book.get("source", "realtime"), "data": book})
    except Exception as exc:
        logger.warning(f"order book api failed {code}: {exc}")
        return jsonify({"ok": False, "code": code, "data": {}, "error": str(exc)})


@bp.route("/api/kline/<code>/minute")
def api_minute_kline(code):
    """Minute K-line data for the stock chart modal."""
    try:
        date_text = _request_trade_date(code)
        raw = md.get_minute_kline(code, date_text)
        times = raw.get("datetime") or []
        if not times:
            return jsonify({"ok": False, "code": code, "data": [], "source": "minute", "msg": "no minute data"})
        data = []
        for idx, dt in enumerate(times):
            dt_text = str(dt)
            fallback = [None] * len(times)
            zeroes = [0] * len(times)
            data.append({
                "date": dt_text[11:16] if len(dt_text) >= 16 else dt_text,
                "datetime": dt_text,
                "open": (raw.get("open") or fallback)[idx],
                "close": (raw.get("close") or fallback)[idx],
                "high": (raw.get("high") or fallback)[idx],
                "low": (raw.get("low") or fallback)[idx],
                "volume": (raw.get("volume") or zeroes)[idx],
                "amount": (raw.get("amount") or zeroes)[idx],
            })
        return jsonify({"ok": True, "code": code, "source": "tushare_minute", "freq": "1MIN", "data": data})
    except Exception as exc:
        logger.warning(f"minute kline api failed {code}: {exc}")
        return jsonify({"ok": False, "code": code, "data": [], "error": str(exc)})
