from __future__ import annotations

import os
import threading
import time
import akshare as ak
import pandas as pd
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

try:
    from analytics import datasource_config
    from analytics import tushare_provider
except Exception:  # pragma: no cover - optional selectable datasource
    datasource_config = None  # type: ignore
    tushare_provider = None  # type: ignore

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAILY_CACHE_DIR  = os.path.join(BASE_DIR, "data", "daily")
MINUTE_CACHE_DIR = os.path.join(BASE_DIR, "data", "minute")
INTRADAY_CACHE_TTL_SECONDS = int(os.getenv("KLINE_INTRADAY_CACHE_TTL", "45"))
ORDER_BOOK_CACHE_TTL_SECONDS = int(os.getenv("KLINE_ORDER_BOOK_CACHE_TTL", "5"))
_INTRADAY_CACHE: dict[tuple, tuple[float, dict]] = {}
_INTRADAY_INFLIGHT: set[tuple] = set()
_INTRADAY_INFLIGHT_LOCK = threading.Lock()


def _plain_stock_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    if "." in raw:
        raw = raw.split(".", 1)[0]
    if raw[:2] in {"SZ", "SH", "BJ"}:
        raw = raw[2:]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return (digits[-6:] if digits else raw[-6:]).zfill(6)


def _cache_get(key: tuple, ttl: int) -> dict | None:
    item = _INTRADAY_CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if time.monotonic() - ts > ttl:
        _INTRADAY_CACHE.pop(key, None)
        return None
    return {k: (v.copy() if isinstance(v, list) else v) for k, v in value.items()}


def _cache_set(key: tuple, value: dict) -> dict:
    snap = {k: (v.copy() if isinstance(v, list) else v) for k, v in value.items()}
    _INTRADAY_CACHE[key] = (time.monotonic(), snap)
    return {k: (v.copy() if isinstance(v, list) else v) for k, v in snap.items()}


def clear_intraday_kline_cache() -> None:
    _INTRADAY_CACHE.clear()
    with _INTRADAY_INFLIGHT_LOCK:
        _INTRADAY_INFLIGHT.clear()


def _try_begin_fetch(key: tuple) -> bool:
    with _INTRADAY_INFLIGHT_LOCK:
        if key in _INTRADAY_INFLIGHT:
            return False
        _INTRADAY_INFLIGHT.add(key)
        return True


def _end_fetch(key: tuple) -> None:
    with _INTRADAY_INFLIGHT_LOCK:
        _INTRADAY_INFLIGHT.discard(key)


def _empty_second_ticks() -> dict:
    return {"datetime": [], "price": [], "volume": [], "amount": [], "prev_price": [], "kind": []}


def _empty_minute_kline() -> dict:
    return {"datetime": [], "open": [], "high": [], "low": [], "close": [], "volume": [], "amount": [], "is_complete": []}


try:
    from zoneinfo import ZoneInfo
    _CN_TZ = ZoneInfo("Asia/Shanghai")
except Exception:                       # pragma: no cover - 缺 tzdata 时退回本机时区
    _CN_TZ = None


def _now() -> datetime:
    """当前的**上海挂钟时间**（naive，便于与既有裸 datetime 比较）。

    原实现是裸 datetime.now()：一旦服务器时区不是 Asia/Shanghai（云端 ECS 默认
    可能是 UTC），交易窗口判断、15:00 收盘终止、隔日信号过期会整体平移数小时——
    实盘信号可能在错误时段生成或漏清。这里显式换算到上海时间再去掉 tzinfo，
    下游所有 naive 比较逻辑不用改。
    """
    if _CN_TZ is None:
        return datetime.now()
    return datetime.now(_CN_TZ).replace(tzinfo=None)


def _target_is_live_intraday(date: str | None = None) -> bool:
    expected_date = str(date or "").replace("-", "")[:8]
    today = _now().strftime("%Y%m%d")
    return (not expected_date or expected_date == today) and is_trading_time()


def _tushare_cfg() -> dict | None:
    if not datasource_config or not tushare_provider:
        return None
    try:
        cfg = datasource_config.get_internal_config()
    except Exception as e:
        logger.debug(f"[market] 数据源配置读取失败，用默认源(akshare): {e}")
        return None
    if cfg.get("provider") == "tushare" and cfg.get("tushare_enabled"):
        return cfg
    return None


def _allow_current_sources() -> bool:
    try:
        from analytics import data_gateway as dg

        return "current" in dg.get_source_order()
    except Exception as e:
        logger.debug(f"[market] datasource order unavailable, allowing current fallback: {e}")
        return True


def _tushare_quotes_frame(rows: list[dict]) -> pd.DataFrame:
    mapped = []
    for row in rows or []:
        mapped.append({
            "\u4ee3\u7801": row.get("code", ""),
            "\u540d\u79f0": row.get("name", ""),
            "\u6700\u65b0\u4ef7": row.get("price", 0),
            "\u6da8\u8dcc\u5e45": row.get("change_pct", 0),
            "\u6628\u6536": row.get("last_close", 0),
            "\u4eca\u5f00": row.get("open", 0),
            "\u6700\u9ad8": row.get("high", 0),
            "\u6700\u4f4e": row.get("low", 0),
            "\u6210\u4ea4\u91cf": row.get("vol", 0),
            "\u6210\u4ea4\u989d": row.get("amount", 0),
        })
    return pd.DataFrame(mapped)

def get_realtime_quotes(codes: list) -> pd.DataFrame:
    """Fetch realtime A-share quotes through the unified datasource gateway."""
    try:
        from analytics import data_gateway as dg
        rows = dg.get_realtime_quotes(codes)
        if rows:
            return _tushare_quotes_frame(rows).reset_index(drop=True)
    except Exception as e:
        logger.error(f"gateway realtime quotes failed: {e}")
    return pd.DataFrame()


def get_kline(code: str, period: int = 60) -> pd.DataFrame:
    """
    获取日K线数据。
    优先按后台数据源配置读取（默认 Tushare），失败后保留本地/akshare 兜底。
    返回 DataFrame，列: date/open/close/high/low/volume
    """
    try:
        from analytics import data_gateway as dg
        df = dg.get_daily_kline(code, days=period, min_rows=10)
        if df is not None and not df.empty and len(df) >= 10:
            return df.tail(period).reset_index(drop=True)
    except Exception as e:
        logger.debug(f"配置数据源K线读取失败 {code}: {e}")

    # 1. 本地 CSV 缓存
    if not _allow_current_sources():
        logger.debug(f"current daily kline fallback disabled by datasource config: {code}")
        return pd.DataFrame()

    cache_path = os.path.join(DAILY_CACHE_DIR, f"{code}.csv")
    if os.path.exists(cache_path):
        try:
            df = pd.read_csv(cache_path)
            if len(df) >= 10:
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").tail(period).reset_index(drop=True)
                return df
        except Exception as e:
            logger.debug(f"本地K线读取失败 {code}: {e}")

    # 2. akshare 远程拉取
    return _fetch_kline_remote(code, period)


def _fetch_kline_remote(code: str, period: int = 60) -> pd.DataFrame:
    code = _plain_stock_code(code)
    try:
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=(pd.Timestamp.now() - pd.Timedelta(days=period * 2)).strftime("%Y%m%d"),
            end_date=pd.Timestamp.now().strftime("%Y%m%d"),
            adjust="qfq",
        )
        rename_map = {
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
        }
        df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
        # 兼容英文列名（老版本 akshare）
        if "date" not in df.columns and len(df.columns) >= 6:
            df.columns = ["date", "open", "close", "high", "low", "volume",
                          "turnover", "change_pct", "change_amt", "amplitude", "turnover_rate"][:len(df.columns)]
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").tail(period).reset_index(drop=True)
        return df
    except Exception as e:
        logger.error(f"akshare K线失败 {code}: {e}")
        return pd.DataFrame()


def get_minute_kline(code: str, date: str) -> dict:
    """Fetch 1-minute bars through the unified datasource gateway."""
    code = _plain_stock_code(code)
    cache_key = ("minute", code, str(date or ""))
    cached = _cache_get(cache_key, INTRADAY_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    live_intraday = _target_is_live_intraday(date)
    if live_intraday:
        snap = _minute_snapshot(code, cache_key, date)
        if snap is not None:
            return snap

    if not _try_begin_fetch(cache_key):
        snap = _minute_snapshot(code, cache_key, date) if live_intraday else None
        return snap if snap is not None else _empty_minute_kline()

    try:
        from analytics import data_gateway as dg
        df = dg.get_minute_kline(code, freq="1MIN", limit=None, trade_date=date)
        result = _empty_minute_kline()
        if df is not None and not df.empty and "datetime" in df.columns:
            df = df.copy()
            df["datetime"] = df["datetime"].apply(lambda v: v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else str(v))
        for col in ("datetime", "open", "high", "low", "close", "volume", "amount", "is_complete"):
            result[col] = df[col].tolist() if col in df.columns else []
        if len(result.get("datetime") or []) > 1:
            return _cache_set(cache_key, result)
        if str(date or "") == datetime.now().strftime("%Y%m%d"):
            rows = dg.get_tick_series(code, limit=None, trade_date=date)
            tick_result = _minute_kline_from_tick_rows(rows)
            if len(tick_result.get("datetime") or []) > len(result.get("datetime") or []):
                return _cache_set(cache_key, tick_result)
        if result.get("datetime"):
            return _cache_set(cache_key, result)
        snap = _minute_snapshot(code, cache_key, date) if live_intraday else None
        if snap is not None:
            return snap
        return result
    finally:
        _end_fetch(cache_key)


def _minute_kline_from_tick_rows(rows: list[dict]) -> dict:
    bars: dict[str, dict] = {}
    order: list[str] = []
    for row in rows or []:
        dt_text = str(row.get("datetime") or "")
        minute_key = dt_text[:16] if len(dt_text) >= 16 else dt_text[:5]
        if not minute_key:
            continue
        hhmm = dt_text[11:16] if len(dt_text) >= 16 else dt_text[:5]
        if not ("09:30" <= hhmm <= "11:30" or "13:00" <= hhmm <= "15:00"):
            continue
        try:
            price = float(row.get("price") or 0)
        except Exception:
            continue
        if price <= 0:
            continue
        try:
            volume = float(row.get("volume") or 0)
        except Exception:
            volume = 0.0
        try:
            amount = float(row.get("amount") or 0)
        except Exception:
            amount = 0.0
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
        bar["volume"] = float(bar.get("volume") or 0) + volume
        bar["amount"] = float(bar.get("amount") or 0) + amount
    result = {"datetime": [], "open": [], "high": [], "low": [], "close": [], "volume": [], "amount": [], "is_complete": []}
    for key in sorted(order):
        bar = bars[key]
        for col in result:
            result[col].append(bar.get(col))
    result["source"] = "tick_agg_minute"
    return result


def get_minute_kline_current(code: str, date: str | None = None) -> dict:
    """Fetch 1-minute bars from the legacy/current datasource only."""
    code = _plain_stock_code(code)
    date = str(date or datetime.now().strftime("%Y%m%d"))
    os.makedirs(MINUTE_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(MINUTE_CACHE_DIR, f"{code}_{date}.csv")
    today = datetime.now().strftime("%Y%m%d")
    use_disk_cache = date != today

    if use_disk_cache and os.path.exists(cache_path):
        try:
            df = pd.read_csv(cache_path)
        except Exception:
            df = pd.DataFrame()
    else:
        df = pd.DataFrame()
        try:
            start_text = f"{date[:4]}-{date[4:6]}-{date[6:8]} 09:30:00"
            end_text = f"{date[:4]}-{date[4:6]}-{date[6:8]} 15:00:00"
            raw = ak.stock_zh_a_hist_min_em(
                symbol=code,
                start_date=start_text,
                end_date=end_text,
                period="1",
                adjust="",
            )
            col_map = {
                "\u65f6\u95f4": "datetime",
                "\u5f00\u76d8": "open",
                "\u6700\u9ad8": "high",
                "\u6700\u4f4e": "low",
                "\u6536\u76d8": "close",
                "\u6210\u4ea4\u91cf": "volume",
                "\u6210\u4ea4\u989d": "amount",
            }
            raw = raw.rename(columns={k: v for k, v in col_map.items() if k in raw.columns})
            if raw is not None and not raw.empty:
                raw.to_csv(cache_path, index=False)
            df = raw
        except Exception as e:
            logger.warning(f"current minute kline failed {code} {date}: {e}")

    result = {}
    for col in ("datetime", "open", "high", "low", "close", "volume", "amount"):
        result[col] = df[col].tolist() if col in df.columns else []
    return result


def _minute_snapshot(code: str, cache_key: tuple, date: str | None = None, fetch_order_book: bool = True) -> dict | None:
    if not _target_is_live_intraday(date):
        return None
    book = _cache_get(("order_book", code), INTRADAY_CACHE_TTL_SECONDS)
    if book is None and fetch_order_book:
        book = get_order_book(code)
    price = book.get("price") if isinstance(book, dict) else None
    trade_time = str(book.get("trade_time") or "") if isinstance(book, dict) else ""
    if not price or not trade_time:
        return None
    price = float(price)
    result = {
        "datetime": [trade_time],
        "open": [price],
        "high": [price],
        "low": [price],
        "close": [price],
        "volume": [book.get("volume", 0) or 0],
        "amount": [book.get("amount", 0) or 0],
        "is_complete": [False],
    }
    return _cache_set(cache_key, result)


def _regular_trade_datetime(dt_text: str, expected_date: str | None = None) -> bool:
    text = str(dt_text or "")
    if len(text) < 16:
        return False
    day = text[:10].replace("-", "")
    if expected_date and day != str(expected_date).replace("-", "")[:8]:
        return False
    hm = text[11:16]
    return ("09:30" <= hm <= "11:30") or ("13:00" <= hm <= "15:00")

def get_second_ticks(code: str, date: str | None = None) -> dict:
    """Fetch second-level tick series through the unified datasource gateway."""
    code = _plain_stock_code(code)
    cache_key = ("second", code, str(date or ""))
    cached = _cache_get(cache_key, INTRADAY_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    if not _try_begin_fetch(cache_key):
        snap = _second_tick_snapshot(code, cache_key, date)
        return snap if snap is not None else _empty_second_ticks()

    try:
        from analytics import data_gateway as dg
        try:
            rows = dg.get_tick_series(code, limit=None, trade_date=date)
        except Exception as exc:
            logger.warning(f"second ticks source failed {code} {date}: {exc}")
            rows = []
        if rows:
            df = pd.DataFrame(rows)
            if "datetime" in df.columns:
                df["datetime"] = df["datetime"].apply(lambda v: v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else str(v))
            result = {}
            for col in ("datetime", "price", "volume", "amount", "prev_price", "kind"):
                result[col] = df[col].tolist() if col in df.columns else []
            return _cache_set(cache_key, result)
        snap = _second_tick_snapshot(code, cache_key, date)
        if snap is not None:
            return snap
        return _cache_set(cache_key, _empty_second_ticks())
    finally:
        _end_fetch(cache_key)


def _second_tick_snapshot(code: str, cache_key: tuple, date: str | None = None) -> dict | None:
    book = _cache_get(("order_book", code), INTRADAY_CACHE_TTL_SECONDS) or get_order_book(code)
    price = book.get("price") if isinstance(book, dict) else None
    trade_time = str(book.get("trade_time") or "") if isinstance(book, dict) else ""
    if not price or not trade_time:
        return None
    expected_date = str(date or "").replace("-", "")[:8]
    if not _target_is_live_intraday(date):
        return None
    if not _regular_trade_datetime(trade_time, expected_date or None):
        return None
    result = {
        "datetime": [trade_time],
        "price": [float(price)],
        "volume": [book.get("volume", 0) or 0],
        "amount": [book.get("amount", 0) or 0],
        "prev_price": [book.get("last_close", 0) or 0],
        "kind": ["SNAPSHOT"],
    }
    return _cache_set(cache_key, result)


def get_order_book(code: str) -> dict:
    """Fetch current five-level bid/ask book through the unified datasource gateway."""
    code = _plain_stock_code(code)
    cache_key = ("order_book", code)
    cached = _cache_get(cache_key, ORDER_BOOK_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    if not _try_begin_fetch(cache_key):
        return {}

    try:
        from analytics import data_gateway as dg
        book = dg.get_order_book(code)
        return _cache_set(cache_key, book or {})
    finally:
        _end_fetch(cache_key)


def is_trading_time() -> bool:
    """判断是否在 A 股交易时间（含法定休市日判断）。

    此前只看「周中 + 时间段」，落在工作日的法定休市日照常被当成交易日：
    调度器继续派发扫描 → 策略基于最后一个交易日的陈旧行情产生信号 →
    模拟盘按陈旧价「成交」，today_trades / hold_days 全按错误口径累计。
    """
    now = _now()
    if now.weekday() >= 5:
        return False
    try:
        from market import trading_calendar
        if not trading_calendar.is_trading_day(now):
            return False
    except Exception as e:                  # 日历模块异常绝不能把交易全停掉
        logger.debug(f"[market] 交易日历判断失败，按周中处理: {e}")
    t = now.hour * 100 + now.minute
    return (930 <= t <= 1130) or (1300 <= t <= 1500)
