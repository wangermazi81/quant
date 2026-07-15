"""数据管理 + K 线 API 蓝图。"""
import threading
import uuid
import logging
import time
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, session

from analytics import data_manager as dm
from market import market_data as md
from app_state import require_auth, _add_log

logger = logging.getLogger(__name__)

bp = Blueprint("data", __name__)
_TRADE_DATE_CACHE: dict[str, tuple[float, str]] = {}
_TRADE_DATE_CACHE_TTL = 60


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
    key = str(code or "").strip().upper()
    cached = _TRADE_DATE_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < _TRADE_DATE_CACHE_TTL:
        return cached[1]
    try:
        daily = md.get_kline(code, period=5)
        if daily is not None and not daily.empty and "date" in daily.columns:
            latest = daily["date"].iloc[-1]
            date_text = latest.strftime("%Y%m%d") if hasattr(latest, "strftime") else str(latest).replace("-", "")[:8]
            if len(date_text) == 8:
                _TRADE_DATE_CACHE[key] = (time.monotonic(), date_text)
                return date_text
    except Exception as exc:
        logger.debug(f"latest daily date lookup failed {code}: {exc}")
    try:
        book = md.get_order_book(code)
        trade_time = str(book.get("trade_time") or "")
        if len(trade_time) >= 10:
            date_text = trade_time[:10].replace("-", "")
            _TRADE_DATE_CACHE[key] = (time.monotonic(), date_text)
            return date_text
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
    """K 线数据：统一数据源，第一源 Tushare，第二源按后台配置降级。"""
    try:
        from analytics import data_gateway as dg
        picked = dg.get_daily_kline_result(code, days=120, min_rows=10)
        df = picked.get("df") if isinstance(picked, dict) else None
        if df is not None and not df.empty and len(df) >= 10:
            cols = [c for c in ["date", "open", "close", "high", "low", "volume"] if c in df.columns]
            sub  = df[cols].tail(120).copy()
            sub["date"] = sub["date"].apply(
                lambda d: d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
            )
            records = sub.to_dict("records")
            last = records[-1]["date"] if records else ""
            name = dm.get_stock_name(code) or code
            return jsonify({
                "ok": True,
                "code": code,
                "name": name,
                "source": picked.get("source"),
                "primary_provider": picked.get("primary_provider"),
                "fallback_used": picked.get("fallback_used"),
                "latest_date": picked.get("latest_date") or last,
                "stale": last < _most_recent_trading_day(),
                "data": records,
            })
    except Exception as e:
        logger.warning(f"K 线统一数据源失败: {e}")

    return jsonify({"ok": False, "data": [], "msg": "暂无数据，请检查后台数据源配置"})


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
