"""龙虎榜 API 蓝图。

- GET /api/lhb/data?date=YYYY-MM-DD   当日龙虎榜全量（股票+席位+游资图谱）
- GET /api/lhb/dates                  本地已缓存的历史日期列表
"""
import logging
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify

from app_security import rate_limit

logger = logging.getLogger(__name__)
bp = Blueprint("lhb", __name__)
_lhb_refresh_lock = threading.Lock()
_lhb_refreshing: set[str] = set()


def _lhb_refresh_key(date: str | None) -> str:
    digits = "".join(ch for ch in str(date or "") if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else "today"


def _lhb_refresh_active(date: str | None) -> bool:
    with _lhb_refresh_lock:
        return _lhb_refresh_key(date) in _lhb_refreshing


def _clear_lhb_refresh_state() -> None:
    with _lhb_refresh_lock:
        _lhb_refreshing.clear()


def _queue_lhb_refresh(engine, date: str | None) -> tuple[bool, bool]:
    """Return (active, started); keep at most one upstream crawl in the web worker."""
    key = _lhb_refresh_key(date)
    with _lhb_refresh_lock:
        if key in _lhb_refreshing:
            return True, False
        if _lhb_refreshing:
            return False, False
        _lhb_refreshing.add(key)

    def _worker():
        try:
            engine.get_lhb_data(date or None, force_refresh=True)
        except Exception:
            logger.exception("[lhb] background refresh failed date=%s", date or "today")
        finally:
            with _lhb_refresh_lock:
                _lhb_refreshing.discard(key)

    try:
        threading.Thread(
            target=_worker,
            daemon=True,
            name=f"lhb-refresh-{key}",
        ).start()
    except Exception:
        with _lhb_refresh_lock:
            _lhb_refreshing.discard(key)
        logger.exception("[lhb] failed to start background refresh date=%s", date or "today")
        return False, False
    return True, True


@bp.route("/api/lhb/data")
@rate_limit(6, 60, scope="public_refresh")
def api_lhb_data():
    from analytics import lhb_engine as le
    date = (request.args.get("date") or "").strip()
    force = request.args.get("refresh", "0") == "1"
    cached = le.get_lhb_cached_data(date or None)
    ready = le.lhb_cache_ready(cached)
    active = False
    started = False
    if force or not ready:
        active, started = _queue_lhb_refresh(le, date)

    if cached:
        data = dict(cached)
        data["refreshing"] = active
        data["refresh_started"] = started
        if active and not ready:
            data["status"] = "refreshing"
        return jsonify(data)

    status = "refreshing" if active else "busy"
    error = (
        "龙虎榜数据正在后台整理，请稍后重试"
        if active
        else "已有其他龙虎榜日期正在后台整理，请稍后重试"
    )
    return jsonify({
        "ok": False,
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "stocks": [],
        "youzai_stat": [],
        "total": 0,
        "status": status,
        "refreshing": active,
        "refresh_started": started,
        "error": error,
    })


@bp.route("/api/lhb/dates")
def api_lhb_dates():
    from analytics import lhb_engine as le
    return jsonify({"dates": le.get_lhb_dates()})
