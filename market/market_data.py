import os
import time
import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, date
import logging

logger = logging.getLogger(__name__)

try:
    import datasource_config
    import tushare_provider
except Exception:  # pragma: no cover - optional selectable datasource
    datasource_config = None  # type: ignore
    tushare_provider = None  # type: ignore

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAILY_CACHE_DIR  = os.path.join(BASE_DIR, "data", "daily")
MINUTE_CACHE_DIR = os.path.join(BASE_DIR, "data", "minute")
INTRADAY_CACHE_TTL_SECONDS = int(os.getenv("KLINE_INTRADAY_CACHE_TTL", "45"))
ORDER_BOOK_CACHE_TTL_SECONDS = int(os.getenv("KLINE_ORDER_BOOK_CACHE_TTL", "5"))
_INTRADAY_CACHE: dict[tuple, tuple[float, dict]] = {}


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


def _tushare_cfg() -> dict | None:
    if not datasource_config or not tushare_provider:
        return None
    try:
        cfg = datasource_config.get_internal_config()
    except Exception:
        return None
    if cfg.get("provider") == "tushare" and cfg.get("tushare_enabled"):
        return cfg
    return None


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
    """Fetch realtime A-share quotes, preferring selected Tushare replay when enabled."""
    cfg = _tushare_cfg()
    if cfg and codes:
        try:
            rows = tushare_provider.fetch_realtime_quotes(
                codes,
                token=cfg.get("tushare_token"),
                mode=cfg.get("tushare_mode"),
            )
            if rows:
                return _tushare_quotes_frame(rows).reset_index(drop=True)
        except Exception as exc:
            logger.warning(f"Tushare realtime quotes failed: {exc}")
    try:
        df = ak.stock_zh_a_spot_em()
        code_col = "\u4ee3\u7801"
        if codes and code_col in df.columns:
            df = df[df[code_col].isin(codes)].copy()
        keep = [
            "\u4ee3\u7801", "\u540d\u79f0", "\u6700\u65b0\u4ef7",
            "\u6da8\u8dcc\u5e45", "\u6da8\u8dcc\u989d", "\u6628\u6536", "\u4eca\u5f00",
            "\u6700\u9ad8", "\u6700\u4f4e", "\u6210\u4ea4\u91cf", "\u6362\u624b\u7387", "\u91cf\u6bd4",
        ]
        keep = [c for c in keep if c in df.columns]
        return df[keep].reset_index(drop=True) if keep else df.reset_index(drop=True)
    except Exception as e:
        logger.error(f"realtime quotes failed: {e}")
        return pd.DataFrame()


def get_kline(code: str, period: int = 60) -> pd.DataFrame:
    """
    获取日K线数据。
    优先读取本地缓存 data/daily/<code>.csv，若无则从 akshare 拉取。
    返回 DataFrame，列: date/open/close/high/low/volume
    """
    # 1. 本地 CSV 缓存
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
    """Fetch 1-minute bars, preferring selected Tushare replay when enabled."""
    code = _plain_stock_code(code)
    cache_key = ("minute", code, str(date or ""))
    cached = _cache_get(cache_key, INTRADAY_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    cfg = _tushare_cfg()
    if cfg:
        try:
            df = tushare_provider.fetch_minute_kline(
                code,
                freq="1MIN",
                limit=240,
                token=cfg.get("tushare_token"),
                mode=cfg.get("tushare_mode"),
            )
            if df is not None and not df.empty:
                out = df.copy()
                out["datetime"] = out["datetime"].apply(lambda v: v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else str(v))
                result = {}
                for col in ("datetime", "open", "high", "low", "close", "volume", "amount", "is_complete"):
                    result[col] = out[col].tolist() if col in out.columns else []
                return _cache_set(cache_key, result)
        except Exception as exc:
            logger.warning(f"Tushare minute kline failed {code} {date}: {exc}")

    os.makedirs(MINUTE_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(MINUTE_CACHE_DIR, f"{code}_{date}.csv")

    if os.path.exists(cache_path):
        try:
            df = pd.read_csv(cache_path)
        except Exception:
            df = pd.DataFrame()
    else:
        df = pd.DataFrame()
        try:
            raw = ak.stock_zh_a_hist_min_em(
                symbol=code,
                start_date=date,
                end_date=date,
                period="1",
                adjust="qfq",
            )
            col_map = {
                "\u65f6\u95f4": "datetime",
                "\u5f00\u76d8": "open",
                "\u6700\u9ad8": "high",
                "\u6700\u4f4e": "low",
                "\u6536\u76d8": "close",
                "\u6210\u4ea4\u91cf": "volume",
            }
            raw = raw.rename(columns={k: v for k, v in col_map.items() if k in raw.columns})
            raw.to_csv(cache_path, index=False)
            df = raw
        except Exception as e:
            logger.warning(f"minute kline failed {code} {date}: {e}")

    result = {}
    for col in ("datetime", "open", "high", "low", "close", "volume"):
        result[col] = df[col].tolist() if col in df.columns else []
    if result.get("datetime"):
        return _cache_set(cache_key, result)
    snap = _minute_snapshot(code, cache_key)
    if snap is not None:
        return snap
    return result


def _minute_snapshot(code: str, cache_key: tuple) -> dict | None:
    book = _cache_get(("order_book", code), INTRADAY_CACHE_TTL_SECONDS) or get_order_book(code)
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


def get_second_ticks(code: str, date: str | None = None) -> dict:
    """Fetch second-level tick series from selected Tushare replay datasource."""
    code = _plain_stock_code(code)
    cache_key = ("second", code, str(date or ""))
    cached = _cache_get(cache_key, INTRADAY_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    cfg = _tushare_cfg()
    if cfg:
        try:
            df = tushare_provider.fetch_tick_series(
                code,
                trade_date=date,
                limit=240,
                token=cfg.get("tushare_token"),
                mode=cfg.get("tushare_mode"),
            )
            if df is not None and not df.empty:
                out = df.copy()
                out["datetime"] = out["datetime"].apply(lambda v: v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else str(v))
                result = {}
                for col in ("datetime", "price", "volume", "amount", "prev_price", "kind"):
                    result[col] = out[col].tolist() if col in out.columns else []
                return _cache_set(cache_key, result)
        except Exception as exc:
            logger.warning(f"Tushare second ticks failed {code} {date}: {exc}")
        snap = _second_tick_snapshot(code, cache_key)
        if snap is not None:
            return snap
    return _cache_set(cache_key, {"datetime": [], "price": [], "volume": [], "amount": [], "prev_price": [], "kind": []})


def _second_tick_snapshot(code: str, cache_key: tuple) -> dict | None:
    book = _cache_get(("order_book", code), INTRADAY_CACHE_TTL_SECONDS) or get_order_book(code)
    price = book.get("price") if isinstance(book, dict) else None
    trade_time = str(book.get("trade_time") or "") if isinstance(book, dict) else ""
    if not price or not trade_time:
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
    """Fetch current five-level bid/ask book from selected Tushare realtime snapshot."""
    code = _plain_stock_code(code)
    cache_key = ("order_book", code)
    cached = _cache_get(cache_key, ORDER_BOOK_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    cfg = _tushare_cfg()
    if cfg:
        try:
            book = tushare_provider.fetch_order_book(
                code,
                token=cfg.get("tushare_token"),
                mode=cfg.get("tushare_mode"),
            )
            if book:
                return _cache_set(cache_key, book)
        except Exception as exc:
            logger.warning(f"Tushare order book failed {code}: {exc}")
    return _cache_set(cache_key, {})


def is_trading_time() -> bool:
    """判断是否在 A 股交易时间"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return (930 <= t <= 1130) or (1300 <= t <= 1500)
