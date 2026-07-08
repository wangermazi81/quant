"""市场环境 / 大盘复盘 API 蓝图。"""
from __future__ import annotations

from flask import Blueprint, request, jsonify
from copy import deepcopy
from datetime import datetime
from time import time

import market_context as mctx
from app_state import require_auth

bp = Blueprint("market", __name__)

_MARKET_REVIEW_CACHE_TTL = 60
_MARKET_REVIEW_CACHE: dict[str, tuple[float, dict]] = {}
_EASTMONEY_INDUSTRY_URL = "https://17.push2.eastmoney.com/api/qt/clist/get"
_TRUSTED_TWO_MARKET_TURNOVER_SOURCES = {
    "eastmoney_three_market_verified",
    "eastmoney_index",
    "eastmoney_index_daily",
    "tdx_index",
    "official_exchange",
}


def _is_trusted_turnover(turnover: dict | None) -> bool:
    if not isinstance(turnover, dict):
        return False
    return bool(
        not turnover.get("degraded")
        and turnover.get("today") is not None
        and turnover.get("source") in _TRUSTED_TWO_MARKET_TURNOVER_SOURCES
    )


def _float_or_none(value) -> float | None:
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except Exception:
        return None


def _fetch_industry_sector_rotation(timeout: float = 1.5) -> dict:
    result = {"top_gainers": [], "top_losers": []}
    try:
        import requests

        params = {
            "pn": "1",
            "pz": "100",
            "po": "1",
            "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": "m:90 t:2 f:!50",
            "fields": "f3,f12,f14",
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
        response = requests.get(_EASTMONEY_INDUSTRY_URL, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        rows = ((response.json().get("data") or {}).get("diff") or [])
    except Exception:
        return result

    boards = []
    for row in rows:
        name = str(row.get("f14") or "").strip()
        change = _float_or_none(row.get("f3"))
        if name and change is not None:
            boards.append((change, name))
    if not boards:
        return result
    result["top_gainers"] = [name for _, name in sorted(boards, reverse=True)[:3]]
    result["top_losers"] = [name for _, name in sorted(boards)[:3]]
    return result


def _normalize_market_review(review: dict | None) -> dict | None:
    if not isinstance(review, dict):
        return None
    result = deepcopy(review)
    try:
        import market_stats as _mstats

        result["breadth"] = _mstats.normalize_breadth_quality(result.get("breadth"))
    except Exception:
        pass
    return result


def _review_from_archive(archive: dict | None) -> dict | None:
    if not isinstance(archive, dict):
        return None
    data = archive.get("data") if isinstance(archive.get("data"), dict) else {}
    market = data.get("market") if isinstance(data.get("market"), dict) else None
    if not market:
        return None
    review = {
        "date": market.get("date") or data.get("date") or archive.get("date"),
        "time": market.get("time") or "15:00",
        "indices": market.get("indices") or {},
        "turnover": data.get("turnover") or {},
        "breadth": market.get("breadth") or {},
        "sectors": market.get("sectors") or {"top_gainers": [], "top_losers": []},
    }
    normalized = _normalize_market_review(review)
    if normalized:
        normalized["archived"] = True
        normalized["archive_phase"] = archive.get("phase")
    return normalized


def _archive_review_needs_refresh(review: dict | None) -> bool:
    if not isinstance(review, dict):
        return False
    breadth = review.get("breadth") or {}
    turnover = review.get("turnover") or {}
    return bool(
        breadth.get("degraded")
        or breadth.get("source") in {"tdx_quotes", "none"}
        or not _is_trusted_turnover(turnover)
    )


def _refresh_archive_market_review(date_text: str | None, archived: dict | None) -> dict | None:
    if not isinstance(archived, dict):
        return None
    try:
        import market_stats as _mstats

        breadth = _mstats.get_market_breadth(date_text=date_text)
        turnover = _mstats.get_market_turnover(date_text=date_text)
    except Exception:
        return None
    if not breadth or breadth.get("degraded") or breadth.get("source") in {"tdx_quotes", "none"}:
        return None
    result = deepcopy(archived)
    result["breadth"] = breadth
    if _is_trusted_turnover(turnover):
        result["turnover"] = turnover
    elif not _is_trusted_turnover(result.get("turnover")):
        return None
    result["archived"] = False
    result["refreshed_from_archive"] = True
    result.pop("archive_phase", None)
    return result


def _load_archive_review(date_text: str | None = None) -> dict | None:
    try:
        import review_engine as re_
    except Exception:
        return None

    digits = "".join(ch for ch in str(date_text or "") if ch.isdigit())
    if len(digits) >= 8:
        date_str = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        try:
            return _review_from_archive(re_.load(date_str))
        except Exception:
            return None

    try:
        dates = re_.list_dates(limit=10)
    except TypeError:
        try:
            dates = re_.list_dates()[:10]
        except Exception:
            return None
    except Exception:
        return None
    for date_str in dates:
        try:
            review = _review_from_archive(re_.load(date_str))
            if review:
                return review
        except Exception:
            continue
    return None


def _should_use_archive_for_live(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return True
    clock = now.hour * 100 + now.minute
    return clock < 930


@bp.route("/api/market/context")
def api_market_context():
    try:
        force = request.args.get("refresh", "0") == "1"
        ctx   = mctx.get_market_regime(force_refresh=force)
        try:
            import ths_quote as tq
            client = tq.get_client()
            if client and client._ok:
                index_pairs = [(1, "000001"), (0, "399001"), (0, "399006")]
                name_map    = {"000001": "上证指数", "399001": "深证成指", "399006": "创业板指"}
                qs = client.get_index_quotes(index_pairs)
                for q in qs:
                    nm = name_map.get(q.get("code", ""), "")
                    if nm:
                        ctx.setdefault("indices", {}).setdefault(nm, {}).update({
                            "price":  round(float(q.get("price") or 0), 2),
                            "change": round(float(q.get("change_pct") or 0), 2),
                            "code":   q.get("code", ""),
                        })
        except Exception:
            pass
        return jsonify({"ok": True, **ctx})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/api/market/regime", methods=["POST"])
@require_auth
def api_market_regime():
    """
    市场环境检测（牛市/熊市/震荡/急跌）+ 建议仓位系数。
    ---
    responses:
      200:
        description: regime, score, position_multiplier
    """
    try:
        from market_regime import MarketRegimeDetector
        detector = MarketRegimeDetector()
        result   = detector.detect()
        return jsonify({"ok": True, **result})
    except ImportError:
        # 降级：使用 market_context
        ctx = mctx.get_market_regime(force_refresh=True)
        return jsonify({"ok": True, **ctx})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


def _build_market_review(date_text: str | None = None, allow_archive: bool = True) -> dict:
    """大盘复盘：指数行情 + 市场宽度 + 板块轮动（含缓存）"""
    from datetime import datetime as _dt
    digits = "".join(ch for ch in str(date_text or "") if ch.isdigit())
    stat_date = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}" if len(digits) >= 8 else _dt.now().strftime("%Y-%m-%d")
    if len(digits) >= 8:
        try:
            import market_stats as _mstats
            snapshot = _mstats.get_verified_market_snapshot(date_text)
            if snapshot:
                return _normalize_market_review({
                    "date": stat_date,
                    "time": "15:00",
                    "indices": {},
                    "turnover": snapshot["turnover"],
                    "breadth": snapshot["breadth"],
                    "sectors": {"top_gainers": [], "top_losers": []},
                })
        except Exception:
            pass
        if allow_archive:
            archived = _load_archive_review(date_text)
            if archived:
                if not _archive_review_needs_refresh(archived):
                    return archived
                refreshed = _refresh_archive_market_review(date_text, archived)
                if refreshed:
                    return refreshed
                if _is_trusted_turnover(archived.get("turnover")):
                    return archived
    elif allow_archive and _should_use_archive_for_live():
        archived = _load_archive_review()
        if archived:
            return archived
    review = {
        "date":    stat_date,
        "time":    _dt.now().strftime("%H:%M"),
        "indices": {},
        "turnover": {},
        "breadth": {},
        "sectors": {"top_gainers": [], "top_losers": []},
    }

    # 1. 指数实时价格 —— 优先 TDX，降级 akshare
    index_meta = [(1, "000001", "上证指数"), (0, "399001", "深证成指"), (0, "399006", "创业板指")]
    try:
        import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            pairs    = [(mkt, code) for mkt, code, _ in index_meta]
            qs       = client.get_index_quotes(pairs)
            name_map = {code: name for _, code, name in index_meta}
            for q in qs:
                nm = name_map.get(q.get("code", ""), "")
                if nm:
                    review["indices"][nm] = {
                        "code":   q.get("code", ""),
                        "price":  round(float(q.get("price") or 0), 2),
                        "change": round(float(q.get("change_pct") or 0), 2),
                    }
    except Exception:
        pass

    if not review["indices"]:
        try:
            import akshare as ak
            code_map = {"000001": "上证指数", "399001": "深证成指", "399006": "创业板指"}
            df_all = ak.stock_zh_index_spot_em()
            if df_all is not None and not df_all.empty:
                for _, row in df_all.iterrows():
                    code = str(row.get("代码", ""))
                    nm   = code_map.get(code, "")
                    if nm:
                        review["indices"][nm] = {
                            "code":   code,
                            "price":  round(float(row.get("最新价", 0) or 0), 2),
                            "change": round(float(row.get("涨跌幅", 0) or 0), 2),
                        }
        except Exception:
            pass

    # 2. Market breadth. Use dedicated market stats sources; strategy quote
    # batches can be incomplete under TDX fallback and must not define breadth.
    try:
        import market_stats as _mstats
        try:
            metrics = _mstats.get_market_snapshot_metrics(date_text=date_text)
            review["breadth"] = metrics.get("breadth") or {}
            review["turnover"] = metrics.get("turnover") or {}
        except Exception:
            review["breadth"] = _mstats.get_market_breadth(date_text=date_text)
            review["turnover"] = _mstats.get_market_turnover(date_text=date_text)
    except Exception:
        pass

    # 3. Industry sector rotation. Use direct Eastmoney requests with a short
    # timeout; the akshare wrapper can block for several seconds before failing.
    review["sectors"] = _fetch_industry_sector_rotation()

    return _normalize_market_review(review) or review


def _get_market_review(date_text: str | None = None, force_refresh: bool = False) -> dict:
    cache_key = _market_review_cache_key(date_text)
    now = time()
    if not force_refresh:
        cached = _MARKET_REVIEW_CACHE.get(cache_key)
        if cached and now - cached[0] <= _MARKET_REVIEW_CACHE_TTL:
            result = deepcopy(cached[1])
            result["cached"] = True
            return result

    review = _build_market_review(date_text, allow_archive=not force_refresh)
    _MARKET_REVIEW_CACHE[cache_key] = (now, deepcopy(review))
    return review


def _market_review_cache_key(date_text: str | None) -> str:
    # Historical explicit dates are immutable enough to share one cache bucket;
    # live calls are governed by TTL so boundary clicks do not refetch.
    digits = "".join(ch for ch in str(date_text or "") if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return "live"


def _dt_cache_bucket(date_text: str | None) -> str:
    return _market_review_cache_key(date_text)


@bp.route("/api/market/review")
def api_market_review():
    """大盘复盘（公开接口）"""
    try:
        date_text = (request.args.get("date") or "").strip() or None
        force = request.args.get("refresh", "0") == "1"
        return jsonify({"ok": True, **_get_market_review(date_text, force_refresh=force)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
