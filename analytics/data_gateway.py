# -*- coding: utf-8 -*-
"""Unified market-data gateway.

Business modules should call this module instead of deciding whether to use
Tushare, TDX, AkShare, or local CSV directly. The source order is controlled by
analytics.datasource_config: primary provider first, optional fallback second.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

import pandas as pd

from analytics import datasource_config
from analytics import tushare_provider

logger = logging.getLogger(__name__)


def normalize_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    if "." in raw:
        raw = raw.split(".", 1)[0]
    if raw[:2] in {"SZ", "SH", "BJ"}:
        raw = raw[2:]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return (digits[-6:] if digits else raw[-6:]).zfill(6)


def _config() -> dict:
    try:
        return datasource_config.get_internal_config()
    except Exception as exc:
        logger.warning(f"[gateway] datasource config unavailable: {exc}")
        cfg = datasource_config.DEFAULT_CONFIG.copy()
        cfg["provider"] = "current"
        return cfg


def _source_order(cfg: dict | None = None) -> list[str]:
    cfg = cfg or _config()
    primary = str(cfg.get("provider") or "tushare").strip().lower()
    if primary not in {"tushare", "current"}:
        primary = "tushare"
    order = [primary]
    secondary = "current" if primary == "tushare" else "tushare"
    if cfg.get("fallback_enabled", True) and secondary not in order:
        order.append(secondary)
    return order


def _has_tushare(cfg: dict) -> bool:
    return bool(cfg.get("tushare_enabled") and cfg.get("tushare_token"))


def _empty_result(kind: str, cfg: dict, error: str = "") -> dict:
    return {
        "ok": False,
        "kind": kind,
        "source": "",
        "primary_provider": str(cfg.get("provider") or ""),
        "fallback_used": False,
        "error": error,
    }


def get_stock_universe() -> list[str]:
    codes: list[str] = []
    try:
        from analytics import data_manager as dm
        loaded = getattr(dm, "_load_name_cache", lambda: {})()
        codes.extend(str(c).zfill(6) for c in loaded.keys())
    except Exception:
        pass
    if not codes:
        try:
            from market import ths_quote as tq
            client = tq.get_client()
            if client and getattr(client, "_ok", False):
                codes.extend(client.get_all_stock_codes() or [])
        except Exception as exc:
            logger.debug(f"[gateway] current universe failed: {exc}")

    out, seen = [], set()
    for code in codes:
        c = normalize_code(code)
        if c.isdigit() and c[0] in {"0", "3", "6", "4", "8", "9"} and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def get_daily_kline_result(code: str, days: int = 60, min_rows: int = 10, update_cache: bool = True) -> dict:
    from analytics import data_manager as dm
    return dm.get_kline_by_config(code, days=days, min_rows=min_rows, update_cache=update_cache)


def get_daily_kline(code: str, days: int = 60, min_rows: int = 10, update_cache: bool = True) -> pd.DataFrame:
    result = get_daily_kline_result(code, days=days, min_rows=min_rows, update_cache=update_cache)
    df = result.get("df") if isinstance(result, dict) else None
    return df if df is not None else pd.DataFrame()


def _current_quotes(codes: Iterable[str]) -> list[dict]:
    try:
        from market import ths_quote as tq
        client = tq.get_client()
        if client and getattr(client, "_ok", False):
            return client.get_quotes([normalize_code(c) for c in codes]) or []
    except Exception as exc:
        logger.debug(f"[gateway] current quotes failed: {exc}")
    return []


def _current_quote(code: str) -> dict:
    rows = _current_quotes([code])
    return rows[0] if rows else {}


def _current_full_market_quotes() -> list[dict]:
    try:
        from market import ths_quote as tq
        client = tq.get_client()
        if client and getattr(client, "_ok", False):
            rows = client.get_full_market_quotes() or []
            if rows:
                return rows
    except Exception as exc:
        logger.debug(f"[gateway] current full-market quotes failed: {exc}")
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return []
        rows = []
        code_col = "\u4ee3\u7801"
        name_col = "\u540d\u79f0"
        price_col = "\u6700\u65b0\u4ef7"
        prev_close_col = "\u6628\u6536"
        open_col = "\u4eca\u5f00"
        high_col = "\u6700\u9ad8"
        low_col = "\u6700\u4f4e"
        volume_col = "\u6210\u4ea4\u91cf"
        amount_col = "\u6210\u4ea4\u989d"
        pct_col = "\u6da8\u8dcc\u5e45"
        for _, row in df.iterrows():
            rows.append({
                "code": normalize_code(row.get(code_col, "")),
                "name": str(row.get(name_col, "")),
                "price": _num(row.get(price_col)),
                "last_close": _num(row.get(prev_close_col)),
                "open": _num(row.get(open_col)),
                "high": _num(row.get(high_col)),
                "low": _num(row.get(low_col)),
                "vol": _num(row.get(volume_col)),
                "amount": _num(row.get(amount_col)),
                "change_pct": _num(row.get(pct_col)),
                "source": "current_akshare",
            })
        return rows
    except Exception as exc:
        logger.debug(f"[gateway] akshare full-market quotes failed: {exc}")
    return []

def _tushare_quotes(codes: Iterable[str], cfg: dict) -> list[dict]:
    if not _has_tushare(cfg):
        return []
    clean = [normalize_code(c) for c in codes if str(c or "").strip()]
    rows: list[dict] = []
    chunk_size = 600
    for i in range(0, len(clean), chunk_size):
        chunk = clean[i:i + chunk_size]
        try:
            rows.extend(tushare_provider.fetch_realtime_quotes(
                chunk,
                token=cfg.get("tushare_token"),
                mode=cfg.get("tushare_mode"),
            ) or [])
        except Exception as exc:
            logger.debug(f"[gateway] tushare quotes failed {i}-{i + len(chunk)}: {exc}")
    return rows


def get_realtime_quotes_result(codes: Iterable[str] | None = None) -> dict:
    cfg = _config()
    code_list = [normalize_code(c) for c in (codes or []) if str(c or "").strip()]
    if not code_list:
        code_list = get_stock_universe()
    result = _empty_result("realtime_quotes", cfg)
    errors: list[str] = []
    for idx, source in enumerate(_source_order(cfg)):
        rows: list[dict] = []
        try:
            if source == "tushare":
                rows = _tushare_quotes(code_list, cfg)
            else:
                rows = _current_quotes(code_list) if codes else _current_full_market_quotes()
        except Exception as exc:
            errors.append(f"{source}: {exc}")
        if rows:
            result.update({
                "ok": True,
                "source": source,
                "fallback_used": idx > 0,
                "quotes": rows,
                "count": len(rows),
                "error": "",
            })
            return result
        errors.append(f"{source}: no data")
    result["quotes"] = []
    result["count"] = 0
    result["error"] = "; ".join(errors[-4:])
    return result


def get_realtime_quotes(codes: Iterable[str] | None = None) -> list[dict]:
    return get_realtime_quotes_result(codes).get("quotes", [])


def get_realtime_quote(code: str) -> dict:
    rows = get_realtime_quotes([code])
    return rows[0] if rows else {}


def get_order_book_result(code: str) -> dict:
    cfg = _config()
    result = _empty_result("order_book", cfg)
    errors: list[str] = []
    clean = normalize_code(code)
    for idx, source in enumerate(_source_order(cfg)):
        book: dict[str, Any] = {}
        try:
            if source == "tushare" and _has_tushare(cfg):
                book = tushare_provider.fetch_order_book(
                    clean,
                    token=cfg.get("tushare_token"),
                    mode=cfg.get("tushare_mode"),
                ) or {}
            elif source == "current":
                quote = _current_quote(clean)
                bids = quote.get("bids") or []
                asks = quote.get("asks") or []
                if not bids:
                    bids = [{"price": quote.get(f"bid{i}"), "volume": quote.get(f"bid_vol{i}")} for i in range(1, 6)]
                if not asks:
                    asks = [{"price": quote.get(f"ask{i}"), "volume": quote.get(f"ask_vol{i}")} for i in range(1, 6)]
                book = {"code": clean, "bids": bids, "asks": asks, "quote": quote}
        except Exception as exc:
            errors.append(f"{source}: {exc}")
        if book and (book.get("bids") or book.get("asks") or book.get("quote")):
            result.update({"ok": True, "source": source, "fallback_used": idx > 0, "order_book": book, "error": ""})
            return result
        errors.append(f"{source}: no data")
    result["order_book"] = {}
    result["error"] = "; ".join(errors[-4:])
    return result


def get_order_book(code: str) -> dict:
    return get_order_book_result(code).get("order_book", {})


def get_tick_series_result(code: str, limit: int = 120) -> dict:
    cfg = _config()
    result = _empty_result("tick_series", cfg)
    clean = normalize_code(code)
    errors: list[str] = []
    for idx, source in enumerate(_source_order(cfg)):
        rows: list[dict] = []
        try:
            if source == "tushare" and _has_tushare(cfg):
                df = tushare_provider.fetch_tick_series(
                    clean,
                    limit=limit,
                    token=cfg.get("tushare_token"),
                    mode=cfg.get("tushare_mode"),
                )
                if df is not None and not df.empty:
                    rows = df.to_dict("records")
            elif source == "current":
                quote = _current_quote(clean)
                if quote:
                    rows = [{
                        "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "price": quote.get("price") or quote.get("now"),
                        "volume": quote.get("vol") or quote.get("volume"),
                        "amount": quote.get("amount"),
                        "source": "current_quote_snapshot",
                    }]
        except Exception as exc:
            errors.append(f"{source}: {exc}")
        if rows:
            result.update({"ok": True, "source": source, "fallback_used": idx > 0, "ticks": rows, "count": len(rows), "error": ""})
            return result
        errors.append(f"{source}: no data")
    result["ticks"] = []
    result["count"] = 0
    result["error"] = "; ".join(errors[-4:])
    return result


def get_tick_series(code: str, limit: int = 120) -> list[dict]:
    return get_tick_series_result(code, limit=limit).get("ticks", [])


def get_minute_kline_result(code: str, freq: str = "1MIN", limit: int = 240) -> dict:
    cfg = _config()
    result = _empty_result("minute_kline", cfg)
    clean = normalize_code(code)
    errors: list[str] = []
    for idx, source in enumerate(_source_order(cfg)):
        df = pd.DataFrame()
        try:
            if source == "tushare" and _has_tushare(cfg):
                df = tushare_provider.fetch_minute_kline(
                    clean,
                    freq=freq,
                    limit=limit,
                    token=cfg.get("tushare_token"),
                    mode=cfg.get("tushare_mode"),
                )
            elif source == "current":
                from market import market_data
                payload = market_data.get_minute_kline_current(clean)
                df = pd.DataFrame(payload)
        except Exception as exc:
            errors.append(f"{source}: {exc}")
        if df is not None and not df.empty:
            result.update({"ok": True, "source": source, "fallback_used": idx > 0, "df": df, "count": len(df), "error": ""})
            return result
        errors.append(f"{source}: no data")
    result["df"] = pd.DataFrame()
    result["count"] = 0
    result["error"] = "; ".join(errors[-4:])
    return result


def get_minute_kline(code: str, freq: str = "1MIN", limit: int = 240) -> pd.DataFrame:
    return get_minute_kline_result(code, freq=freq, limit=limit).get("df", pd.DataFrame())


def get_stock_sectors_bulk(codes: list[str]) -> dict[str, dict]:
    cfg = _config()
    clean_codes = []
    for code in codes or []:
        c = normalize_code(code)
        if c and c not in clean_codes:
            clean_codes.append(c)
    if not clean_codes:
        return {}
    errors: list[str] = []
    for source in _source_order(cfg):
        try:
            if source == "tushare" and _has_tushare(cfg):
                rows = tushare_provider.fetch_stock_sectors_bulk(
                    clean_codes,
                    token=cfg.get("tushare_token"),
                    mode=cfg.get("tushare_mode"),
                )
            elif source == "current":
                from analytics import data_manager as dm
                rows = dm.get_stock_sectors_bulk_current(clean_codes)
            else:
                rows = {}
        except Exception as exc:
            errors.append(f"{source}: {exc}")
            rows = {}
        if rows:
            if errors:
                logger.debug(f"[gateway] sector fallback after: {'; '.join(errors[-2:])}")
            return rows
        errors.append(f"{source}: no data")
    return {}


def get_lhb_stocks_result(trade_date: str) -> dict:
    cfg = _config()
    result = _empty_result("lhb_stocks", cfg)
    errors: list[str] = []
    for idx, source in enumerate(_source_order(cfg)):
        rows: list[dict] = []
        try:
            if source == "tushare" and _has_tushare(cfg):
                rows = tushare_provider.fetch_lhb_stocks(trade_date, token=cfg.get("tushare_token"), mode=cfg.get("tushare_mode"))
            elif source == "current":
                from analytics import lhb_engine
                rows = lhb_engine.fetch_lhb_stocks_current(trade_date)
        except Exception as exc:
            errors.append(f"{source}: {exc}")
        if rows:
            result.update({"ok": True, "source": source, "fallback_used": idx > 0, "stocks": rows, "count": len(rows), "error": ""})
            return result
        errors.append(f"{source}: no data")
    result["stocks"] = []
    result["count"] = 0
    result["error"] = "; ".join(errors[-4:])
    return result


def get_lhb_stocks(trade_date: str) -> list[dict]:
    return get_lhb_stocks_result(trade_date).get("stocks", [])


def get_lhb_seats_result(code: str, trade_date: str) -> dict:
    cfg = _config()
    result = _empty_result("lhb_seats", cfg)
    errors: list[str] = []
    clean = normalize_code(code)
    for idx, source in enumerate(_source_order(cfg)):
        rows: list[dict] = []
        try:
            if source == "tushare" and _has_tushare(cfg):
                rows = tushare_provider.fetch_lhb_seats(clean, trade_date, token=cfg.get("tushare_token"), mode=cfg.get("tushare_mode"))
            elif source == "current":
                from analytics import lhb_engine
                rows = lhb_engine.fetch_lhb_seats_current(clean, trade_date)
        except Exception as exc:
            errors.append(f"{source}: {exc}")
        if rows:
            result.update({"ok": True, "source": source, "fallback_used": idx > 0, "seats": rows, "count": len(rows), "error": ""})
            return result
        errors.append(f"{source}: no data")
    result["seats"] = []
    result["count"] = 0
    result["error"] = "; ".join(errors[-4:])
    return result


def get_lhb_seats(code: str, trade_date: str) -> list[dict]:
    return get_lhb_seats_result(code, trade_date).get("seats", [])


def _current_index_kline(code: str, days: int = 60, market: int | None = None) -> pd.DataFrame:
    try:
        from market import ths_quote as tq
        client = tq.get_client()
        if not client or not getattr(client, "_ok", False):
            return pd.DataFrame()
        clean = normalize_code(code)
        if hasattr(client, "get_index_kline"):
            bars = client.get_index_kline(clean, 0 if market is None else market, days)
        else:
            bars = client.get_kline(clean, days)
        if bars:
            df = pd.DataFrame(bars)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").reset_index(drop=True)
            return df
    except Exception as exc:
        logger.debug(f"[gateway] current index kline failed {code}: {exc}")
    return pd.DataFrame()


def get_index_kline_result(code: str, days: int = 60, market: int | None = None) -> dict:
    cfg = _config()
    result = _empty_result("index_kline", cfg)
    errors: list[str] = []
    for idx, source in enumerate(_source_order(cfg)):
        df = pd.DataFrame()
        try:
            if source == "current":
                df = _current_index_kline(code, days=days, market=market)
            elif source == "tushare":
                if _has_tushare(cfg):
                    df = tushare_provider.fetch_index_kline(
                        code,
                        days=days,
                        market=market,
                        token=cfg.get("tushare_token"),
                        mode=cfg.get("tushare_mode"),
                    )
        except Exception as exc:
            errors.append(f"{source}: {exc}")
        if df is not None and not df.empty:
            result.update({"ok": True, "source": source, "fallback_used": idx > 0, "df": df, "count": len(df), "error": ""})
            return result
        errors.append(f"{source}: no data")
    result["df"] = pd.DataFrame()
    result["count"] = 0
    result["error"] = "; ".join(errors[-4:])
    return result


def get_index_kline(code: str, days: int = 60, market: int | None = None) -> pd.DataFrame:
    return get_index_kline_result(code, days=days, market=market).get("df", pd.DataFrame())


def _current_index_quotes(pairs: Iterable[tuple[int, str]]) -> list[dict]:
    pair_list = [(int(m), normalize_code(c)) for m, c in pairs]
    try:
        from market import ths_quote as tq
        client = tq.get_client()
        if client and getattr(client, "_ok", False) and hasattr(client, "get_index_quotes"):
            rows = client.get_index_quotes(pair_list) or []
            if rows:
                return rows
    except Exception as exc:
        logger.debug(f"[gateway] current index quotes failed: {exc}")
    try:
        import akshare as ak
        wanted = {code for _, code in pair_list}
        df = ak.stock_zh_index_spot_em()
        if df is None or df.empty:
            return []
        rows: list[dict] = []
        code_col = "\u4ee3\u7801"
        name_col = "\u540d\u79f0"
        price_col = "\u6700\u65b0\u4ef7"
        pct_col = "\u6da8\u8dcc\u5e45"
        for _, row in df.iterrows():
            code = normalize_code(row.get(code_col, ""))
            if code not in wanted:
                continue
            rows.append({
                "code": code,
                "name": str(row.get(name_col, "")),
                "price": _num(row.get(price_col)),
                "change_pct": _num(row.get(pct_col)),
            })
        return rows
    except Exception as exc:
        logger.debug(f"[gateway] current akshare index quotes failed: {exc}")
    return []


def get_index_quotes_result(pairs: Iterable[tuple[int, str]]) -> dict:
    cfg = _config()
    result = _empty_result("index_quotes", cfg)
    pair_list = [(int(m), normalize_code(c)) for m, c in pairs]
    errors: list[str] = []
    for idx, source in enumerate(_source_order(cfg)):
        rows: list[dict] = []
        try:
            if source == "current":
                rows = _current_index_quotes(pair_list)
            elif source == "tushare":
                if _has_tushare(cfg):
                    rows = tushare_provider.fetch_index_quotes(
                        pair_list,
                        token=cfg.get("tushare_token"),
                        mode=cfg.get("tushare_mode"),
                    )
        except Exception as exc:
            errors.append(f"{source}: {exc}")
        if rows:
            result.update({"ok": True, "source": source, "fallback_used": idx > 0, "quotes": rows, "count": len(rows), "error": ""})
            return result
        errors.append(f"{source}: no data")
    result["quotes"] = []
    result["count"] = 0
    result["error"] = "; ".join(errors[-4:])
    return result


def get_index_quotes(pairs: Iterable[tuple[int, str]]) -> list[dict]:
    return get_index_quotes_result(pairs).get("quotes", [])


def get_finance_info_result(code: str) -> dict:
    cfg = _config()
    result = _empty_result("finance_info", cfg)
    clean = normalize_code(code)
    errors: list[str] = []
    for idx, source in enumerate(_source_order(cfg)):
        info: dict[str, Any] = {}
        try:
            if source == "current":
                from market import ths_quote as tq
                client = tq.get_client()
                info = client.get_finance_info(clean) if client else {}
            elif source == "tushare":
                if _has_tushare(cfg):
                    info = tushare_provider.fetch_finance_info(
                        clean,
                        token=cfg.get("tushare_token"),
                        mode=cfg.get("tushare_mode"),
                    )
        except Exception as exc:
            errors.append(f"{source}: {exc}")
        if info:
            result.update({"ok": True, "source": source, "fallback_used": idx > 0, "finance_info": info, "error": ""})
            return result
        errors.append(f"{source}: no data")
    result["finance_info"] = {}
    result["error"] = "; ".join(errors[-4:])
    return result


def get_finance_info(code: str) -> dict:
    return get_finance_info_result(code).get("finance_info", {})


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default
