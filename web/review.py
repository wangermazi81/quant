"""复盘 + 财经新闻 API 蓝图。

- GET  /api/review/daily?date=   某日复盘（归档优先；当天未归档则实时采集，带5分钟缓存）
- POST /api/review/generate      手动触发生成（含 AI 总结，同步执行，可能 1-2 分钟）
- GET  /api/review/dates         可回看的历史日期列表
- GET  /api/news/feed            新闻时间线（source/q/important/limit/offset）
- GET  /api/news/sources         新闻来源列表
"""
from __future__ import annotations

import logging
import threading
import time
from copy import deepcopy
from datetime import datetime

from flask import Blueprint, request, jsonify

from app_security import rate_limit

from app_state import require_auth, _deduct
from web._params import _bounded_int_arg

logger = logging.getLogger(__name__)
bp = Blueprint("review", __name__)


_TRUSTED_TWO_MARKET_TURNOVER_SOURCES = {
    "eastmoney_three_market_verified",
    "eastmoney_index",
    "eastmoney_index_daily",
    "tdx_index",
    "official_exchange",
    "tushare_replay_daily",
    "tushare_replay_daily_info",
}


def _is_trusted_turnover(turnover: dict | None) -> bool:
    if not isinstance(turnover, dict):
        return False
    return bool(
        not turnover.get("degraded")
        and turnover.get("today") is not None
        and turnover.get("source") in _TRUSTED_TWO_MARKET_TURNOVER_SOURCES
    )


def _mark_turnover_unavailable(data: dict, date_str: str) -> None:
    data["turnover"] = {
        "source": "unavailable",
        "today": None,
        "amount": None,
        "stat_date": date_str,
        "degraded": True,
        "reason": "trusted_two_market_turnover_unavailable",
    }

# 当天实时采集缓存（盘中反复刷新不用每次打满 akshare）
_live_cache: dict = {"ts": 0.0, "data": None}
_live_lock = threading.Lock()
_LIVE_TTL = 120   # 盘中天梯/情绪要跟得上盘面，2分钟缓存


def _should_use_latest_archive(now: datetime, explicit_date: bool, force: bool, archived: dict | None) -> bool:
    if explicit_date or force or archived:
        return False
    if now.weekday() >= 5:
        return True
    clock = now.hour * 100 + now.minute
    return clock < 930


def _load_latest_archive(re_) -> dict | None:
    try:
        dates = re_.list_dates(limit=10)
    except TypeError:
        dates = re_.list_dates()[:10]
    except Exception:
        return None
    for date_str in dates:
        try:
            archived = re_.load(date_str)
            if archived:
                return archived
        except Exception:
            continue
    return None


def _verified_snapshot_archive(date_str: str) -> dict | None:
    try:
        from market import market_stats

        snapshot = market_stats.get_verified_market_snapshot(date_str)
    except Exception:
        snapshot = None
    if not snapshot:
        return None

    breadth = snapshot["breadth"]
    turnover = dict(snapshot["turnover"])
    turnover["trend"] = []
    zt = {
        "zt_count": breadth.get("limit_up", 0),
        "dt_count": breadth.get("limit_dn", 0),
        "zb_count": 0,
        "zb_rate": None,
        "max_height": 0,
        "ladder": [],
        "prev_zt_avg": None,
        "prev_zt_red_rate": None,
    }
    score = 50.0
    score += min(zt["zt_count"], 120) * 0.25
    score -= min(zt["dt_count"], 60) * 0.5
    if breadth.get("total"):
        score += (breadth.get("up", 0) - breadth.get("down", 0)) / breadth["total"] * 30

    return {
        "ok": True,
        "date": snapshot["stat_date"],
        "phase": "verified_snapshot",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": {
            "date": snapshot["stat_date"],
            "market": {
                "date": snapshot["stat_date"],
                "time": "15:00",
                "indices": {},
                "breadth": breadth,
                "sectors": {"top_gainers": [], "top_losers": []},
            },
            "zt": zt,
            "turnover": turnover,
            "sectors": {"industry_top": [], "industry_bottom": [], "concept_top": []},
            "news": [],
            "trading_day": True,
            "sentiment_score": int(max(0, min(100, round(score)))),
        },
        "ai_summary": (
            f"{snapshot['stat_date']} 复盘归档已按东方财富三市 A 股市场概况截图校准："
            f"三市成交额 {turnover.get('today')} 亿；上涨 {breadth.get('up')} 家、"
            f"平盘 {breadth.get('flat')} 家、下跌 {breadth.get('down')} 家；"
            f"涨停 {zt['zt_count']} 家、跌停 {zt['dt_count']} 家。"
        ),
    }


def _archive_market_metrics_need_refresh(archive: dict | None) -> bool:
    if not isinstance(archive, dict):
        return False
    data = archive.get("data") if isinstance(archive.get("data"), dict) else {}
    market = data.get("market") if isinstance(data.get("market"), dict) else {}
    breadth = market.get("breadth") if isinstance(market.get("breadth"), dict) else {}
    try:
        from market import market_stats

        breadth = market_stats.normalize_breadth_quality(breadth)
    except Exception:
        pass
    turnover = data.get("turnover") if isinstance(data.get("turnover"), dict) else {}
    if breadth.get("degraded") or breadth.get("source") in {"tdx_quotes", "none", None}:
        return True
    return not _is_trusted_turnover(turnover)


def _refresh_archive_market_metrics(archive: dict, date_str: str, re_) -> dict:
    if not _archive_market_metrics_need_refresh(archive):
        return archive
    refreshed = deepcopy(archive)
    data = refreshed.setdefault("data", {})
    market = data.setdefault("market", {})
    changed = False
    try:
        from market import market_stats

        breadth = market_stats.get_market_breadth(date_text=date_str)
        turnover = market_stats.get_market_turnover(date_text=date_str)
        if breadth and not breadth.get("degraded") and breadth.get("source") not in {"tdx_quotes", "none"}:
            market["date"] = breadth.get("stat_date") or date_str
            market["breadth"] = breadth
            changed = True
        if _is_trusted_turnover(turnover):
            data["turnover"] = turnover
            changed = True
        if changed and _is_trusted_turnover(data.get("turnover")):
            return refreshed
    except Exception as exc:
        logger.warning(f"[review] refresh archive market metrics failed: {exc}")

    try:
        live = re_.collect(date_str)
    except Exception as exc:
        logger.warning(f"[review] fallback collect for archive refresh failed: {exc}")
        return archive
    live_market = live.get("market") if isinstance(live.get("market"), dict) else {}
    live_breadth = live_market.get("breadth") if isinstance(live_market.get("breadth"), dict) else {}
    live_turnover = live.get("turnover") if isinstance(live.get("turnover"), dict) else {}
    if live_breadth and not live_breadth.get("degraded"):
        market["date"] = live_market.get("date") or date_str
        market["breadth"] = live_breadth
    if _is_trusted_turnover(live_turnover):
        data["turnover"] = live_turnover
    elif not _is_trusted_turnover(data.get("turnover")):
        _mark_turnover_unavailable(data, date_str)
    return refreshed


@bp.route("/api/review/daily")
@rate_limit(6, 60, scope="public_refresh")
def api_review_daily():
    from analytics import review_engine as re_
    now = datetime.now()
    raw_date = (request.args.get("date") or "").strip()
    explicit_date = bool(raw_date)
    date_str = raw_date or now.strftime("%Y-%m-%d")
    force    = request.args.get("refresh", "0") == "1"

    archived = re_.load(date_str)
    today    = now.strftime("%Y-%m-%d")
    # Verified historical snapshots must override archived files because old
    # archives may have been generated from degraded intraday sources.
    if date_str != today:
        verified = _verified_snapshot_archive(date_str)
        if verified:
            return jsonify(verified)
        if archived:
            return jsonify(_refresh_archive_market_metrics(archived, date_str, re_))
        return jsonify({"ok": False, "error": "该日期无复盘归档", "date": date_str})

    # 当天：已有收盘/晚间归档直接用；否则实时采集（无 AI，5分钟缓存）
    if archived and not force:
        return jsonify(_refresh_archive_market_metrics(archived, date_str, re_))
    if _should_use_latest_archive(now, explicit_date, force, archived):
        latest = _load_latest_archive(re_)
        if latest:
            return jsonify(latest)
    with _live_lock:
        if not force and _live_cache["data"] and time.time() - _live_cache["ts"] < _LIVE_TTL:
            return jsonify(_live_cache["data"])
    try:
        data = re_.collect(date_str)
        out  = {"ok": True, "date": date_str, "phase": "live",
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "data": data,
                "ai_summary": (archived or {}).get("ai_summary", "")}
        with _live_lock:
            _live_cache["ts"], _live_cache["data"] = time.time(), out
        return jsonify(out)
    except Exception as e:
        logger.warning(f"[review] 实时采集失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/api/review/generate", methods=["POST"])
@require_auth
def api_review_generate():
    """异步启动生成（采集+AI约1-2分钟，HTTP立即返回），前端轮询 /api/review/status。"""
    from analytics import review_engine as re_
    d        = request.json or {}
    date_str = (d.get("date") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    with_ai  = bool(d.get("with_ai", True))
    try:
        # 已在跑 → 不重复启动也不扣分，直接返回状态
        if re_.get_gen_status().get("running"):
            return jsonify({"ok": True, "started": True, **re_.get_gen_status()})
        # 积分制：AI 复盘按 review_generate 计费（admin「积分设置」可调，0=免费）
        if with_ai:
            err = _deduct("review_generate", f"AI复盘生成 {date_str}")
            if err:
                return jsonify(err)
        st = re_.generate_async(date_str, phase="manual", with_ai=with_ai)
        with _live_lock:                  # 生成完成后 daily 接口要立刻看到新版本
            _live_cache["data"] = None
        return jsonify({"ok": True, "started": True, **st})
    except Exception as e:
        logger.warning(f"[review] 启动生成失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/api/review/status")
def api_review_status():
    from analytics import review_engine as re_
    return jsonify({"ok": True, **re_.get_gen_status()})


@bp.route("/api/review/dates")
def api_review_dates():
    from analytics import review_engine as re_
    return jsonify({"ok": True, "dates": re_.list_dates()})


# ── 新闻 ──────────────────────────────────────────────────────
_first_fetch_fired = False


@bp.route("/api/news/feed")
def api_news_feed():
    import news_center as nc
    global _first_fetch_fired
    items = nc.query(
        source=(request.args.get("source") or "").strip(),
        q=(request.args.get("q") or "").strip(),
        important=request.args.get("important", "0") == "1",
        since=(request.args.get("since") or "").strip(),
        limit=_bounded_int_arg("limit", 80, 1, 200),
        offset=_bounded_int_arg("offset", 0, 0, 10000),
    )
    # 库还是空的（首次部署）→ 后台补一轮抓取，前端下次刷新即有数据
    if not items and not _first_fetch_fired:
        _first_fetch_fired = True
        threading.Thread(target=nc.fetch_quick, daemon=True, name="news-first-fetch").start()
    # fetch=1：前端打开/轮询时顺手触发增量抓取（服务端限频60秒），保证准实时
    if request.args.get("fetch", "0") == "1":
        try:
            nc.maybe_fetch_async(min_gap=60)
        except Exception:
            pass
    return jsonify({"ok": True, "items": items})


@bp.route("/api/news/sources")
def api_news_sources():
    import news_center as nc
    return jsonify({"ok": True, "sources": nc.list_sources()})
