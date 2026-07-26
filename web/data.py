"""数据管理 + K 线 API 蓝图。"""
from __future__ import annotations
import threading
import uuid
import logging
import re
import time
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, session

from analytics import data_manager as dm
from market import market_data as md
from app_state import require_auth, require_admin, _add_log
from app_security import rate_limit
from web._concurrency import limit_heavy

logger = logging.getLogger(__name__)

bp = Blueprint("data", __name__)
_TRADE_DATE_CACHE: dict[str, tuple[float, str]] = {}
_TRADE_DATE_CACHE_TTL = 60
MAX_DOWNLOAD_CODES = 200
_DOWNLOAD_OWNERS: dict[str, int] = {}


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
        plain_code = md._plain_stock_code(code) if hasattr(md, "_plain_stock_code") else key[-6:]
        book = md._cache_get(("order_book", plain_code), md.INTRADAY_CACHE_TTL_SECONDS) if hasattr(md, "_cache_get") else None
        if not isinstance(book, dict) and hasattr(md, "get_order_book"):
            book = md.get_order_book(code)
        trade_time = str(book.get("trade_time") or "") if isinstance(book, dict) else ""
        if len(trade_time) >= 10:
            date_text = trade_time[:10].replace("-", "")
            _TRADE_DATE_CACHE[key] = (time.monotonic(), date_text)
            return date_text
    except Exception as exc:
        logger.debug(f"cached trade date lookup failed {code}: {exc}")
    date_text = datetime.now().strftime("%Y%m%d")
    _TRADE_DATE_CACHE[key] = (time.monotonic(), date_text)
    return date_text


def _minute_bars_from_ticks(ticks: dict) -> dict:
    """把秒级成交聚合成 1 分钟 OHLC，作为分时源兜底。"""
    times = ticks.get("datetime") or [] if isinstance(ticks, dict) else []
    prices = ticks.get("price") or [] if isinstance(ticks, dict) else []
    volumes = ticks.get("volume") or [] if isinstance(ticks, dict) else []
    amounts = ticks.get("amount") or [] if isinstance(ticks, dict) else []
    bars: dict[str, dict] = {}
    order: list[str] = []
    for idx, dt in enumerate(times):
        dt_text = str(dt)
        price = prices[idx] if idx < len(prices) else None
        try:
            price = float(price)
        except Exception:
            continue
        if price <= 0:
            continue
        minute_key = dt_text[:16] if len(dt_text) >= 16 else dt_text[:5]
        if not minute_key:
            continue
        hhmm = dt_text[11:16] if len(dt_text) >= 16 else dt_text[:5]
        if not ("09:30" <= hhmm <= "11:30" or "13:00" <= hhmm <= "15:00"):
            continue
        vol = volumes[idx] if idx < len(volumes) else 0
        amt = amounts[idx] if idx < len(amounts) else 0
        try:
            vol = float(vol or 0)
        except Exception:
            vol = 0
        try:
            amt = float(amt or 0)
        except Exception:
            amt = 0
        bar = bars.get(minute_key)
        if bar is None:
            bar = {
                "datetime": minute_key + ":00" if len(minute_key) == 16 else minute_key,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0.0,
                "amount": 0.0,
                "is_complete": False,
            }
            bars[minute_key] = bar
            order.append(minute_key)
        bar["high"] = max(float(bar["high"]), price)
        bar["low"] = min(float(bar["low"]), price)
        bar["close"] = price
        bar["volume"] = float(bar.get("volume") or 0) + vol
        bar["amount"] = float(bar.get("amount") or 0) + amt
    rows = [bars[k] for k in sorted(order)]
    result = {"datetime": [], "open": [], "high": [], "low": [], "close": [], "volume": [], "amount": [], "is_complete": []}
    for row in rows:
        for col in result:
            result[col].append(row.get(col))
    return result

@bp.route("/api/data/stocks")
def api_data_list():
    return jsonify(dm.list_stocks())


@bp.route("/api/data/download", methods=["POST"])
@rate_limit(3, 3600, scope="data_download")
@require_auth
def api_data_download():
    uid        = session["user_id"]
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify({"ok": False, "msg": "请求字段格式错误"}), 400
    raw_codes  = d.get("codes", "")
    start_date = d.get("start_date", "2020-01-01")
    if isinstance(raw_codes, str):
        codes = [c.strip() for c in raw_codes.replace("，", ",").split(",") if c.strip()]
    elif isinstance(raw_codes, (list, tuple)):
        codes = [str(c).strip() for c in raw_codes if str(c).strip()]
    else:
        return jsonify({"ok": False, "msg": "股票代码格式错误"}), 400
    codes = list(dict.fromkeys(codes))
    if not codes:
        return jsonify({"ok": False, "msg": "请输入股票代码"}), 400
    if len(codes) > MAX_DOWNLOAD_CODES:
        return jsonify({"ok": False, "msg": f"单次最多下载 {MAX_DOWNLOAD_CODES} 只股票"}), 400
    if any(not re.fullmatch(r"[A-Za-z0-9._-]{1,16}", code) for code in codes):
        return jsonify({"ok": False, "msg": "股票代码格式错误"}), 400
    task_id = uuid.uuid4().hex
    # 与 data_manager._progress 的淘汰保持同步：那边已经清掉的任务，
    # 这里的属主映射也要跟着清，否则本表只增不删（每次下载 +1，永不回收）。
    for _stale in [t for t in _DOWNLOAD_OWNERS
                   if dm.get_progress(t).get("status") == "not_found"]:
        _DOWNLOAD_OWNERS.pop(_stale, None)
    _DOWNLOAD_OWNERS[task_id] = uid
    _add_log(uid, f"开始下载 {len(codes)} 只股票历史数据...")
    dm.start_download(codes, start_date, task_id)
    return jsonify({"ok": True, "task_id": task_id, "total": len(codes)})


@bp.route("/api/data/progress/<task_id>")
@require_auth
def api_data_progress(task_id):
    if _DOWNLOAD_OWNERS.get(task_id) != session["user_id"]:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify(dm.get_progress(task_id))


@bp.route("/api/data/delete", methods=["POST"])
@require_admin
def api_data_delete():
    uid  = session["user_id"]
    code = request.json.get("code", "")
    r    = dm.delete_stock(code)
    if r["ok"]:
        _add_log(uid, f"已删除本地数据: {code}", "WARN")
    return jsonify(r)


@bp.route("/api/data/refresh_names", methods=["POST"])
@require_admin
def api_refresh_names():
    uid = session["user_id"]
    threading.Thread(target=dm.refresh_name_cache, daemon=True).start()
    _add_log(uid, "开始刷新股票名称缓存...")
    return jsonify({"ok": True, "msg": "后台刷新中"})


@bp.route("/api/data/sector/<code>")
@limit_heavy("data_sector", limit=4,
             busy_msg="板块信息查询繁忙，请稍后再试")
def api_stock_sector(code):
    # 冷缓存时这里会在请求内串行抓最多 12 个概念板块成分表 + baostock 登录兜底，
    # 单次可阻塞 8-15 秒。并发打进来就是线程池耗尽的又一触发源，故限并发。
    r         = dm.get_stock_sector(code)
    r["name"] = dm.get_stock_name(code)
    return jsonify(r)


@bp.route("/api/kline/<code>")
def api_kline(code):
    """K 线数据：统一数据源，第一源 Tushare，第二源按后台配置降级。"""
    try:
        local = dm.get_local(code)
        if local is not None and not local.empty and len(local) >= 10:
            cols = [c for c in ["date", "open", "close", "high", "low", "volume"] if c in local.columns]
            sub = local[cols].tail(120).copy()
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
                "source": "local_daily_cache",
                "primary_provider": "cache",
                "fallback_used": False,
                "latest_date": last,
                "stale": last < _most_recent_trading_day(),
                "data": records,
            })

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
        source = raw.get("source") or "tushare_minute"
        times = raw.get("datetime") or []
        if len(times) <= 1:
            tick_raw = md.get_second_ticks(code, date_text)
            tick_minute = _minute_bars_from_ticks(tick_raw)
            tick_times = tick_minute.get("datetime") or []
            if len(tick_times) <= len(times):
                from analytics import data_gateway as dg
                rows = dg.get_tick_series(code, limit=None, trade_date=date_text)
                tick_raw = {
                    col: [row.get(col) for row in rows]
                    for col in ("datetime", "price", "volume", "amount", "prev_price", "kind")
                }
                tick_minute = _minute_bars_from_ticks(tick_raw)
                tick_times = tick_minute.get("datetime") or []
            if len(tick_times) > len(times):
                raw = tick_minute
                times = tick_times
                source = "tick_agg_minute"
        if not times:
            return jsonify({"ok": False, "code": code, "data": [], "source": "minute", "msg": "no minute data"})
        data = []
        fallback = [None] * len(times)
        zeroes = [0] * len(times)
        for idx, dt in enumerate(times):
            dt_text = str(dt)
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
        return jsonify({"ok": True, "code": code, "source": source, "freq": "1MIN", "data": data})
    except Exception as exc:
        logger.warning(f"minute kline api failed {code}: {exc}")
        return jsonify({"ok": False, "code": code, "data": [], "error": str(exc)})
