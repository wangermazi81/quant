# -*- coding: utf-8 -*-
from __future__ import annotations

"""
本地历史数据管理
数据源：akshare stock_zh_a_hist_tx（腾讯财经）
股票名称：ak.stock_info_a_code_name() 全量缓存，不走东方财富
"""

import json, logging, threading, os, time, re
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import akshare as ak

try:
    from analytics import agudata_provider
    from analytics import datasource_config
    from analytics import factor_history_quarantine
    from analytics import tushare_provider
except Exception:  # pragma: no cover - package import fallback for tests/tools
    from . import (  # type: ignore
        agudata_provider,
        datasource_config,
        factor_history_quarantine,
        tushare_provider,
    )

# ── 强制 akshare 不走代理（直连国内行情接口）───────────────
# requests 底层：trust_env=False 完全忽略系统代理
import requests as _requests
_ak_session = _requests.Session()
_ak_session.trust_env = False      # 不从环境变量读代理
_ak_session.proxies   = {}         # 显式置空
# 替换 akshare 内部使用的 session
try:
    import akshare.utils.func as _af
    _af.requests = _requests
except Exception:
    pass
# 同时通过 urllib 层面清除代理（兜底）
_PROXY_KEYS = (
    "HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy",
    "ALL_PROXY","all_proxy","SOCKS_PROXY","socks_proxy",
)
_saved_proxies = {k: os.environ.pop(k) for k in _PROXY_KEYS if k in os.environ}

logger = logging.getLogger(__name__)


def _allow_current_sources() -> bool:
    try:
        from analytics import data_gateway as dg

        return "current" in dg.get_source_order()
    except Exception as e:
        logger.debug(f"data_gateway source order unavailable, allowing current fallback: {e}")
        return True

DATA_DIR       = Path(__file__).parent.parent / "data" / "daily"
META_FILE      = Path(__file__).parent.parent / "data" / "metadata.json"
NAME_CACHE_FILE= Path(__file__).parent.parent / "data" / "stock_names.json"
SECURITY_MASTER_FILE = Path(__file__).parent.parent / "data" / "security_master.csv"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_progress: dict = {}
_lock = threading.Lock()
_name_cache: dict = {}          # code -> name
_name_cache_lock = threading.Lock()
_sector_cache_lock = threading.Lock()
_SECTOR_CACHE_TTL = 86400
_SECTOR_FAILURE_TTL = 900
_MAX_CONCEPT_SCAN = 12
_SECTOR_TIMEOUT_GUARD = 8.0


def _eastmoney_secid(code: str) -> str:
    return ("1." if str(code).startswith(("5", "6", "9")) else "0.") + str(code).zfill(6)


def _parse_sector_concepts(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,，、]", str(text or "")) if x.strip()]


def _infer_stock_sector_by_code(code: str) -> dict:
    c = str(code or "").zfill(6)
    if c.startswith(("11", "12")):
        return {"industry": "可转债", "concepts": []}
    if c.startswith(("4", "8", "920")):
        return {"industry": "北交所", "concepts": []}
    return {"industry": "", "concepts": []}


def get_stock_sectors_bulk(codes: list[str]) -> dict[str, dict]:
    """Fetch industry/concept labels through the global data-source gateway."""
    try:
        from analytics import data_gateway as dg
        return dg.get_stock_sectors_bulk(codes)
    except Exception as e:
        logger.debug(f"data_gateway sector route failed, using current source: {e}")
        if not _allow_current_sources():
            return {}
        return get_stock_sectors_bulk_current(codes)


def get_stock_sectors_bulk_current(codes: list[str]) -> dict[str, dict]:
    """Fetch industry/concept labels for many stocks from Eastmoney quote list API."""
    clean_codes = []
    for code in codes or []:
        c = str(code or "").zfill(6)
        if c.isdigit() and c not in clean_codes:
            clean_codes.append(c)
    if not clean_codes:
        return {}

    result: dict[str, dict] = {}
    # A-7：请求头与镜像主机列表统一由 market/eastmoney_client 维护，
    # 东财换域名/加校验头时只改那一处。本函数的分批与部分成功语义保持原样。
    from market.eastmoney_client import EM_HEADERS as headers, UUST_URLS as urls
    fields = "f12,f100,f103"
    for i in range(0, len(clean_codes), 80):
        batch = clean_codes[i:i + 80]
        secids = ",".join(_eastmoney_secid(c) for c in batch)
        last_error = None
        for url in urls:
            try:
                r = _requests.get(
                    url,
                    params={"secids": secids, "fields": fields, "fltt": "2", "np": "1"},
                    headers=headers,
                    timeout=8,
                )
                payload = r.json() or {}
                rows = ((payload.get("data") or {}).get("diff") or [])
                for row in rows:
                    code = str(row.get("f12") or "").zfill(6)
                    if not code.isdigit():
                        continue
                    industry = str(row.get("f100") or "").strip()
                    concepts = _parse_sector_concepts(row.get("f103"))[:5]
                    if industry or concepts:
                        result[code] = {"industry": industry, "concepts": concepts}
                if all(c in result for c in batch):
                    break
            except Exception as e:
                last_error = e
        if last_error and not any(c in result for c in batch):
            logger.warning(f"get_stock_sectors_bulk failed for batch {batch[:3]}...: {last_error}")
    missing = [code for code in clean_codes if code not in result]
    if missing:
        result.update(_fetch_stock_sectors_baostock(missing))
    for code in clean_codes:
        if code not in result:
            inferred = _infer_stock_sector_by_code(code)
            if inferred.get("industry") or inferred.get("concepts"):
                result[code] = inferred
    return result


def _baostock_symbol(code: str) -> str:
    c = str(code).zfill(6)
    return ("sh." if c.startswith(("5", "6", "9")) else "sz.") + c


def _fetch_stock_sectors_baostock(codes: list[str]) -> dict[str, dict]:
    """Last-resort local-friendly industry fallback. Baostock has industry only."""
    result: dict[str, dict] = {}
    try:
        import contextlib
        import io
        import baostock as bs
    except Exception as e:
        logger.debug(f"baostock unavailable for sector fallback: {e}")
        return result
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            login = bs.login()
        if getattr(login, "error_code", "") != "0":
            return result
        for code in codes:
            c = str(code or "").zfill(6)
            if not c.isdigit():
                continue
            try:
                rs = bs.query_stock_industry(code=_baostock_symbol(c))
                while rs.next():
                    row = dict(zip(rs.fields, rs.get_row_data()))
                    industry = str(row.get("industry") or "").strip()
                    if industry:
                        result[c] = {"industry": industry, "concepts": []}
                        break
            except Exception as e:
                logger.debug(f"baostock sector failed for {c}: {e}")
                continue
    except Exception as e:
        logger.warning(f"baostock sector fallback failed: {e}")
    finally:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                bs.logout()
        except Exception:
            pass
    return result


def _fetch_stock_sector_eastmoney(code: str) -> dict:
    """Fast fallback for industry/concept labels from Eastmoney quote API."""
    bulk = get_stock_sectors_bulk([code])
    if bulk.get(str(code).zfill(6)):
        return bulk[str(code).zfill(6)]
    result = {"industry": "", "concepts": []}
    params = {"secid": _eastmoney_secid(code), "fields": "f127,f129"}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    last_error = None
    for getter in (_ak_session.get, _requests.get):
        try:
            r = getter(
                "https://push2.eastmoney.com/api/qt/stock/get",
                params=params,
                headers=headers,
                timeout=5,
            )
            payload = r.json() or {}
            data = payload.get("data") or {}
            industry = str(data.get("f127") or "").strip()
            concepts = _parse_sector_concepts(data.get("f129"))
            if industry:
                result["industry"] = industry
            if concepts:
                result["concepts"] = concepts[:5]
            if result["industry"] or result["concepts"]:
                return result
        except Exception as e:
            last_error = e
    try:
        r = _requests.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params=params,
            headers=headers,
            timeout=5,
            proxies={"http": None, "https": None},
        )
        payload = r.json() or {}
        data = payload.get("data") or {}
        industry = str(data.get("f127") or "").strip()
        concepts = _parse_sector_concepts(data.get("f129"))
        if industry:
            result["industry"] = industry
        if concepts:
            result["concepts"] = concepts[:5]
    except Exception as e:
        last_error = e
    if last_error:
        logger.warning(f"get_stock_sector eastmoney fallback failed for {code}: {last_error}")
    return result


# ── 市场前缀 ──────────────────────────────────────
def _tx_symbol(code: str) -> str:
    if code.startswith("6"):
        return "sh" + code
    elif code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


# ── K线数据清洗（下载后 / 读取时统一过一遍）─────────────────
def _clean_kline(df: pd.DataFrame) -> pd.DataFrame:
    """清洗日线数据，保证回测输入干净：

    1. OHLCV 全部转数值，无法解析的置 NaN
    2. 丢弃 close/open 缺失或 ≤0 的行（停牌占位、脏数据）
    3. 日期去重（保留最后一条）+ 按日期升序
    4. high/low 自洽修正：high=max(O,H,L,C)，low=min(O,H,L,C)
    5. volume 负值归零
    """
    if df is None or df.empty:
        return df
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "close", "open"])
    df = df[(df["close"] > 0) & (df["open"] > 0)]
    df = df.drop_duplicates(subset=["date"], keep="last")
    df = df.sort_values("date").reset_index(drop=True)
    if not df.empty:
        ohlc = df[["open", "high", "low", "close"]]
        df["high"] = ohlc.max(axis=1)
        df["low"]  = ohlc.min(axis=1)
        if "volume" in df.columns:
            df["volume"] = df["volume"].clip(lower=0).fillna(0)
    return df


# ── 元数据 ────────────────────────────────────────
def _standardize_kline_for_cache(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize common kline shapes before writing the daily CSV cache."""
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    if "date" not in out.columns:
        if isinstance(out.index, pd.DatetimeIndex):
            out = out.reset_index().rename(columns={"index": "date"})
        elif "datetime" in out.columns:
            out = out.rename(columns={"datetime": "date"})
        elif "Date" in out.columns:
            out = out.rename(columns={"Date": "date"})
        elif "trade_date" in out.columns:
            out = out.rename(columns={"trade_date": "date"})

    if "volume" not in out.columns:
        for col in ("vol", "amount", "成交量"):
            if col in out.columns:
                out["volume"] = out[col]
                break

    required = ["date", "open", "high", "low", "close", "volume"]
    if any(c not in out.columns for c in required):
        return pd.DataFrame()
    optional = [column for column in ("amount", "name", "is_st") if column in out.columns]
    return _clean_kline(out[required + optional])


def merge_kline_cache(code: str, df: pd.DataFrame, update_metadata: bool = True) -> pd.DataFrame:
    """Merge a refreshed kline slice into data/daily/<code>.csv.

    Short refreshes from TDX must not overwrite long historical CSV files.
    New rows win on duplicate dates, then the merged file is cleaned and sorted.
    """
    new_df = _standardize_kline_for_cache(df)
    if new_df is None or new_df.empty:
        return pd.DataFrame()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = DATA_DIR / f"{code}.csv"

    if csv_path.exists():
        try:
            old_df = pd.read_csv(csv_path, parse_dates=["date"])
            old_df = _standardize_kline_for_cache(old_df)
        except Exception:
            old_df = pd.DataFrame()
        if old_df is not None and not old_df.empty:
            old_by_date = old_df.drop_duplicates("date", keep="last").set_index("date")
            for column in ("amount", "name", "is_st"):
                if column not in old_by_date.columns:
                    continue
                previous = new_df["date"].map(old_by_date[column])
                if column in new_df.columns:
                    missing = new_df[column].isna()
                    if column == "name":
                        missing |= new_df[column].astype("string").str.strip().eq("").fillna(True)
                    new_df.loc[missing, column] = previous.loc[missing]
                else:
                    new_df[column] = previous
            merged = _clean_kline(pd.concat([old_df, new_df], ignore_index=True))
        else:
            merged = new_df
    else:
        merged = new_df

    required = ["date", "open", "close", "high", "low", "volume"]
    optional = [column for column in ("amount", "name", "is_st") if column in merged.columns]
    merged[required + optional].to_csv(csv_path, index=False, encoding="utf-8")

    if update_metadata:
        try:
            meta = _load_meta()
            item = dict(meta.get(code, {}))
            item.update({
                "name": item.get("name") or code,
                "start": str(merged["date"].iloc[0].date()),
                "end": str(merged["date"].iloc[-1].date()),
                "rows": len(merged),
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            meta[code] = item
            # 逐只股票的热路径：只标脏，按间隔合并落盘（见 _save_meta 上方注释）
            _save_meta(meta, immediate=False)
        except Exception as e:
            logger.debug(f"[data] K线元数据写入失败（不影响本次返回）{code}: {e}")

    return merged


def _latest_date_text(df: pd.DataFrame) -> str:
    try:
        if df is None or df.empty or "date" not in df.columns:
            return ""
        latest = pd.to_datetime(df["date"], errors="coerce").max()
        if pd.isna(latest):
            return ""
        return latest.strftime("%Y-%m-%d")
    except Exception:
        return ""


def expected_completed_trade_date(now: datetime | None = None) -> str:
    current = now or datetime.now()
    market_close_guard = current.replace(hour=15, minute=30, second=0, microsecond=0)
    candidate = current.date() if current >= market_close_guard else (current - timedelta(days=1)).date()
    try:
        from market import trading_calendar

        is_trading_day = trading_calendar.is_trading_day
    except Exception:
        is_trading_day = lambda value: value.weekday() < 5
    for _ in range(31):
        if is_trading_day(candidate):
            return candidate.isoformat()
        candidate -= timedelta(days=1)
    raise RuntimeError("completed trading day unavailable")


def _stale_factor_source_result(
    source_trade_date: str,
    expected_trade_date: str,
    *,
    source: str,
    min_required: int,
) -> dict:
    return {
        "ok": False,
        "error": (
            f"factor daily source is stale: expected {expected_trade_date}, "
            f"got {source_trade_date}"
        ),
        "source": source,
        "primary_source": source,
        "source_trade_date": source_trade_date,
        "expected_trade_date": expected_trade_date,
        "latest_count": 0,
        "min_required": max(1, int(min_required)),
    }


def _kline_is_stale_for_live_use(df: pd.DataFrame, max_lag_days: int = 3) -> bool:
    try:
        if df is None or df.empty or "date" not in df.columns:
            return True
        latest = pd.to_datetime(df["date"], errors="coerce").max()
        if pd.isna(latest):
            return True
        return (datetime.now().date() - latest.date()).days > max_lag_days
    except Exception:
        return False


def _empty_kline_result(code: str, days: int, primary: str, error: str = "") -> dict:
    return {
        "ok": False,
        "code": str(code or "").zfill(6),
        "days": int(days or 0),
        "df": pd.DataFrame(),
        "source": "",
        "primary_provider": primary,
        "fallback_used": False,
        "latest_date": "",
        "error": error,
    }


def _normalize_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    if "." in raw:
        raw = raw.split(".", 1)[0]
    if raw[:2] in {"SZ", "SH", "BJ"}:
        raw = raw[2:]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return (digits[-6:] if digits else raw[-6:]).zfill(6)


def _fetch_tushare_kline_by_config(code: str, days: int, cfg: dict) -> pd.DataFrame:
    token = cfg.get("tushare_token") or datasource_config.resolve_tushare_token(cfg, mode=cfg.get("tushare_mode"))
    if not (cfg.get("tushare_enabled") and token):
        return pd.DataFrame()
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=max(int(days or 60) * 3, int(days or 60) + 90))).strftime("%Y%m%d")
    df = tushare_provider.fetch_kline_qfq(
        code,
        start,
        end,
        token=token,
        mode=cfg.get("tushare_mode"),
    )
    return _standardize_kline_for_cache(df)


def _fetch_agudata_kline_by_config(
    code: str,
    days: int,
    cfg: dict,
) -> pd.DataFrame:
    api_key = (
        cfg.get("agudata_api_key")
        or datasource_config.resolve_agudata_api_key(cfg)
    )
    if not (cfg.get("agudata_enabled") and api_key):
        return pd.DataFrame()
    end = datetime.now().strftime("%Y%m%d")
    start = (
        datetime.now()
        - timedelta(days=max(int(days or 60) * 3, int(days or 60) + 90))
    ).strftime("%Y%m%d")
    frame = agudata_provider.fetch_kline_qfq(
        code,
        start,
        end,
        api_key=api_key,
        base_url=cfg.get("agudata_base_url"),
    )
    return _standardize_kline_for_cache(frame)


# 标记「这份数据原封不动来自本地 CSV 缓存」。用于让 get_kline_by_config 跳过
# merge_kline_cache —— 把刚从 CSV 读出来的东西再原样写回去，等于每只股票白做
# 一次「读 CSV + concat + 重写 CSV + 更新元数据」。一轮 5000 只的全市场扫描
# 就是 5000 次无谓的读写放大，而且全在 GIL 内串行抢占唯一 worker 的 CPU。
_FROM_LOCAL_CACHE = "_jw_from_local_cache"


def _mark_from_local(df: pd.DataFrame) -> pd.DataFrame:
    try:
        df.attrs[_FROM_LOCAL_CACHE] = True
    except Exception:
        pass
    return df


def _fetch_current_kline(code: str, days: int, min_rows: int, prefer_local: bool = True) -> pd.DataFrame:
    if prefer_local:
        local = get_local(code)
        if local is not None and not local.empty and len(local) >= min_rows and not _kline_is_stale_for_live_use(local):
            return _mark_from_local(local.tail(days).reset_index(drop=True))

    try:
        from market import ths_quote as _tq
        client = _tq.get_client()
        if client and getattr(client, "_ok", False):
            bars = client.get_kline(code, max(int(days or 60), int(min_rows or 10)))
            if bars:
                return _standardize_kline_for_cache(pd.DataFrame(bars))
    except Exception as exc:
        logger.debug(f"[data] current kline source failed for {code}: {exc}")

    local = get_local(code)
    if local is not None and not local.empty and len(local) >= min_rows:
        return _mark_from_local(local.tail(days).reset_index(drop=True))
    return pd.DataFrame()


def get_kline_by_config(code: str, days: int = 60, min_rows: int = 10, update_cache: bool = True) -> dict:
    """Fetch daily K-line data using the admin-selected primary/secondary source order."""
    clean_code = _normalize_code(code)
    days = max(1, int(days or 60))
    min_rows = max(1, int(min_rows or 10))
    try:
        cfg = datasource_config.get_internal_config()
    except Exception as exc:
        logger.warning(f"[data] datasource config unavailable, falling back to current source: {exc}")
        cfg = datasource_config.DEFAULT_CONFIG.copy()
        cfg["provider"] = "current"
    primary = str(cfg.get("provider") or "current").strip().lower()
    fallback_enabled = bool(cfg.get("fallback_enabled", True))
    result = _empty_kline_result(clean_code, days, primary)

    if primary not in {"agudata", "current", "tushare"}:
        primary = "current"
    attempts = [primary]
    secondary = "tushare" if primary == "current" else "current"
    if fallback_enabled and secondary not in attempts:
        attempts.append(secondary)

    errors: list[str] = []
    for idx, source in enumerate(attempts):
        df = pd.DataFrame()
        try:
            if source == "agudata":
                df = _fetch_agudata_kline_by_config(
                    clean_code,
                    days,
                    cfg,
                )
            elif source == "tushare":
                df = _fetch_tushare_kline_by_config(clean_code, days, cfg)
            elif source == "current":
                df = _fetch_current_kline(clean_code, days, min_rows, prefer_local=True)
        except Exception as exc:
            errors.append(f"{source}: {exc}")
            logger.info(f"[data] {source} kline failed for {clean_code}: {exc}")
            df = pd.DataFrame()

        if df is None or df.empty or len(df) < min_rows:
            errors.append(f"{source}: no data")
            continue

        # 数据原样来自本地 CSV 时无需回写：没有任何新增行，merge 只会白读白写一遍
        _cached_hit = False
        try:
            _cached_hit = bool(df.attrs.get(_FROM_LOCAL_CACHE))
        except Exception:
            _cached_hit = False

        if update_cache and not _cached_hit:
            try:
                merged = merge_kline_cache(clean_code, df)
                if merged is not None and not merged.empty and len(merged) >= min_rows:
                    df = merged
            except Exception as exc:
                logger.debug(f"[data] merge kline cache failed for {clean_code}: {exc}")

        out = df.tail(days).reset_index(drop=True)
        return {
            "ok": True,
            "code": clean_code,
            "days": days,
            "df": out,
            "source": source,
            "primary_provider": primary,
            "fallback_used": idx > 0,
            "latest_date": _latest_date_text(out),
            "error": "",
        }

    result["error"] = "; ".join(errors[-4:]) if errors else "no data"
    return result


def _refresh_factor_daily_cache_tdx(days: int = 180, min_required: int = 800, now: datetime | None = None) -> dict:
    """Incrementally refresh the factor-mining daily cache from the TDX fallback."""
    from market import ths_quote

    client = ths_quote.get_client()
    if client is None or not getattr(client, "_ok", True):
        return {"ok": False, "error": "TDX daily source unavailable", "latest_count": 0, "min_required": min_required}

    index_bars = client.get_index_kline("000001", 1, 5) or []
    source_dates = pd.to_datetime([row.get("date") for row in index_bars], errors="coerce")
    source_dates = source_dates[~pd.isna(source_dates)]
    if len(source_dates) == 0:
        return {"ok": False, "error": "TDX index daily date unavailable", "latest_count": 0, "min_required": min_required}

    current = now or datetime.now()
    source_date = pd.Timestamp(source_dates.max()).normalize()
    market_close_guard = current.replace(hour=15, minute=30, second=0, microsecond=0)
    if source_date.date() == current.date() and current < market_close_guard:
        completed_dates = source_dates[pd.DatetimeIndex(source_dates).normalize() < source_date]
        if len(completed_dates) == 0:
            return {
                "ok": False,
                "error": "TDX completed trade date unavailable before market close",
                "latest_count": 0,
                "min_required": min_required,
            }
        source_date = pd.Timestamp(completed_dates.max()).normalize()
    expected_trade_date = expected_completed_trade_date(current)
    source_trade_date = str(source_date.date())
    if source_trade_date != expected_trade_date:
        return _stale_factor_source_result(
            source_trade_date,
            expected_trade_date,
            source="tdx_daily",
            min_required=min_required,
        )
    csv_paths = sorted(DATA_DIR.glob("*.csv"))
    if not csv_paths:
        return {"ok": False, "error": "daily cache is empty", "latest_count": 0, "min_required": min_required}

    min_history = min(max(1, int(days)), 60)
    metadata = _load_meta()
    refreshed = 0
    already_current = 0
    failed = 0
    errors = []

    for idx, csv_path in enumerate(csv_paths, start=1):
        code = csv_path.stem
        try:
            old_dates = pd.read_csv(csv_path, usecols=["date"])
            parsed = pd.to_datetime(old_dates["date"], errors="coerce").dropna()
            if len(parsed) >= min_history and parsed.max().normalize() == source_date:
                already_current += 1
                continue

            bars = client.get_kline(code, max(int(days), min_history)) or []
            if not bars:
                failed += 1
                continue
            refresh_frame = _standardize_kline_for_cache(pd.DataFrame(bars))
            refresh_frame = refresh_frame[refresh_frame["date"] <= source_date]
            if refresh_frame.empty:
                failed += 1
                continue
            merged = merge_kline_cache(code, refresh_frame, update_metadata=False)
            if merged.empty or len(merged) < min_history or pd.to_datetime(merged["date"]).max().normalize() != source_date:
                failed += 1
                continue

            refreshed += 1
            item = dict(metadata.get(code, {}))
            item.update({
                "name": item.get("name") or code,
                "start": str(pd.to_datetime(merged["date"]).iloc[0].date()),
                "end": str(source_date.date()),
                "rows": len(merged),
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            metadata[code] = item
        except Exception as exc:
            failed += 1
            if len(errors) < 20:
                errors.append(f"{code}: {str(exc)[:120]}")
        if idx % 250 == 0:
            logger.info(
                "[factor_cache] progress %s/%s current=%s refreshed=%s failed=%s",
                idx,
                len(csv_paths),
                already_current,
                refreshed,
                failed,
            )

    if refreshed:
        _save_meta(metadata)
    latest_count = already_current + refreshed
    result = {
        "ok": latest_count >= max(1, int(min_required)),
        "source": "tdx_daily",
        "source_trade_date": str(source_date.date()),
        "expected_trade_date": expected_trade_date,
        "total": len(csv_paths),
        "latest_count": latest_count,
        "min_required": max(1, int(min_required)),
        "already_current": already_current,
        "refreshed": refreshed,
        "failed": failed,
    }
    if errors:
        result["errors"] = errors
    if not result["ok"]:
        result["error"] = f"latest daily coverage {latest_count} is below required {result['min_required']}"
    return result


def _factor_tushare_enabled(config: dict) -> bool:
    return bool(
        config.get("provider") == "tushare"
        and config.get("tushare_enabled")
        and config.get("tushare_token")
    )


def _factor_remote_source(config: dict) -> tuple[str, object | None, dict]:
    if (
        config.get("provider") == "agudata"
        and config.get("agudata_enabled")
        and config.get("agudata_api_key")
    ):
        return (
            "agudata",
            agudata_provider,
            {
                "api_key": config.get("agudata_api_key"),
                "base_url": config.get("agudata_base_url"),
            },
        )
    if _factor_tushare_enabled(config):
        return (
            "tushare",
            tushare_provider,
            {
                "token": config.get("tushare_token"),
                "mode": config.get("tushare_mode"),
            },
        )
    return "", None, {}


def _completed_date_candidates(current: datetime, lookback_days: int = 12) -> list[str]:
    market_close_guard = current.replace(hour=15, minute=30, second=0, microsecond=0)
    start = current.date() if current >= market_close_guard else current.date() - timedelta(days=1)
    return [(start - timedelta(days=offset)).strftime("%Y%m%d") for offset in range(max(1, lookback_days))]


def _same_daily_row(old_frame: pd.DataFrame, new_frame: pd.DataFrame, source_date: pd.Timestamp) -> bool:
    if old_frame is None or old_frame.empty or new_frame is None or new_frame.empty:
        return False
    old_rows = old_frame[pd.to_datetime(old_frame["date"], errors="coerce").dt.normalize() == source_date]
    if old_rows.empty:
        return False
    old_row = old_rows.iloc[-1]
    new_row = new_frame.iloc[-1]
    for column in ("open", "high", "low", "close", "volume"):
        try:
            left = float(old_row[column])
            right = float(new_row[column])
        except Exception:
            return False
        tolerance = max(1e-6, abs(right) * 1e-8)
        if abs(left - right) > tolerance:
            return False
    if "amount" in new_row.index and pd.notna(new_row.get("amount")):
        if "amount" not in old_row.index or pd.isna(old_row.get("amount")):
            return False
        try:
            old_amount = float(old_row["amount"])
            new_amount = float(new_row["amount"])
        except (TypeError, ValueError):
            return False
        tolerance = max(1e-4, abs(new_amount) * 1e-8)
        if abs(old_amount - new_amount) > tolerance:
            return False
    return True


def _factor_history_cache_complete(
    cache_path: Path,
    *,
    security_start: pd.Timestamp,
    security_end: pd.Timestamp,
    minimum_rows: int,
    require_end_coverage: bool,
    end_tolerance_days: int = 0,
) -> bool:
    if not cache_path.is_file():
        return False
    try:
        cached = pd.read_csv(cache_path)
    except Exception:
        return False
    required = {"date", "close", "amount"}
    if not required.issubset(cached.columns):
        return False
    cached = cached.copy()
    cached["date"] = pd.to_datetime(cached["date"], errors="coerce")
    cached["close"] = pd.to_numeric(cached["close"], errors="coerce")
    cached["amount"] = pd.to_numeric(cached["amount"], errors="coerce")
    cached = (
        cached.dropna(subset=["date", "close"])
        .drop_duplicates("date", keep="last")
        .sort_values("date")
    )
    tolerance = pd.Timedelta(days=14)
    relevant = cached[
        (cached["date"] >= security_start - tolerance)
        & (cached["date"] <= security_end)
    ]
    if len(relevant) < max(1, int(minimum_rows)):
        return False
    if pd.Timestamp(relevant["date"].min()) > security_start + tolerance:
        return False
    end_tolerance = pd.Timedelta(days=max(0, int(end_tolerance_days)))
    if (
        require_end_coverage
        and pd.Timestamp(relevant["date"].max()) < security_end - end_tolerance
    ):
        return False
    if bool(relevant["close"].pct_change(fill_method=None).abs().gt(0.35).any()):
        return False
    from analytics.relay_factor_model import MIN_REAL_AMOUNT_COVERAGE

    amount_is_real = relevant["amount"].notna() & (relevant["amount"] > 0)
    if float(amount_is_real.mean()) < MIN_REAL_AMOUNT_COVERAGE:
        return False
    return True


def _security_master_validation_error(
    master: pd.DataFrame,
) -> str | None:
    required = {
        "code",
        "ts_code",
        "list_date",
        "delist_date",
        "list_status",
        "lifecycle_source",
        "lifecycle_snapshot_complete",
        "universe_scope",
        "snapshot_at",
        "security_master_pagination_complete",
        "security_master_page_requests",
        "security_master_row_count",
        "security_master_snapshot_complete",
        "security_master_snapshot_trade_date",
    }
    if master.empty or not required.issubset(master.columns):
        return "security master lifecycle metadata is incomplete"

    codes = (
        master["code"]
        .astype("string")
        .str.replace(r"\.0$", "", regex=True)
        .str.strip()
    )
    ts_codes = master["ts_code"].astype("string").str.strip().str.upper()
    if (
        not codes.str.fullmatch(r"\d{6}", na=False).all()
        or codes.duplicated().any()
        or ts_codes.duplicated().any()
        or not (
            ts_codes
            == codes
            + "."
            + ts_codes.str.rsplit(".", n=1).str[-1]
        ).all()
    ):
        return "security master contains invalid or repeated codes"

    exchanges = ts_codes.str.rsplit(".", n=1).str[-1]
    if not exchanges.isin(["SH", "SZ"]).all():
        return "security master contains unsupported exchange"
    statuses = master["list_status"].astype("string").str.strip().str.upper()
    if not statuses.isin(["L", "D"]).all():
        return "security master contains invalid lifecycle status"
    coverage = set(zip(exchanges.tolist(), statuses.tolist()))
    if not {
        ("SH", "L"),
        ("SH", "D"),
        ("SZ", "L"),
        ("SZ", "D"),
    }.issubset(coverage):
        return "security master lifecycle coverage is incomplete"

    list_text = (
        master["list_date"]
        .astype("string")
        .fillna("")
        .str.replace(r"\.0$", "", regex=True)
        .str.strip()
    )
    delist_text = (
        master["delist_date"]
        .astype("string")
        .fillna("")
        .str.replace(r"\.0$", "", regex=True)
        .str.strip()
    )
    list_dates = pd.to_datetime(
        list_text,
        format="%Y%m%d",
        errors="coerce",
    )
    delist_dates = pd.to_datetime(
        delist_text.where(delist_text.ne("")),
        format="%Y%m%d",
        errors="coerce",
    )
    delisted = statuses.eq("D")
    if (
        list_dates.isna().any()
        or delist_text.loc[~delisted].ne("").any()
        or delist_text.loc[delisted].eq("").any()
        or delist_dates.loc[delisted].isna().any()
        or (
            delist_dates.loc[delisted]
            < list_dates.loc[delisted]
        ).any()
    ):
        return "security master contains invalid lifecycle dates"

    trusted_marker = lambda value: (
        str(value).strip().lower() in {"1", "true", "yes"}
    )
    if not (
        master["lifecycle_snapshot_complete"].map(trusted_marker).all()
        and master["security_master_pagination_complete"]
        .map(trusted_marker)
        .all()
        and master["security_master_snapshot_complete"]
        .map(trusted_marker)
        .all()
    ):
        return "security master lifecycle snapshot is incomplete"
    if (
        not master["lifecycle_source"]
        .astype("string")
        .str.strip()
        .eq("baostock_stock_basic")
        .all()
        or not master["universe_scope"]
        .astype("string")
        .str.strip()
        .eq("sh_sz_a_share")
        .all()
    ):
        return "security master lifecycle source is untrusted"

    page_requests = pd.to_numeric(
        master["security_master_page_requests"],
        errors="coerce",
    )
    row_counts = pd.to_numeric(
        master["security_master_row_count"],
        errors="coerce",
    )
    if (
        page_requests.isna().any()
        or page_requests.le(0).any()
        or row_counts.isna().any()
        or row_counts.nunique() != 1
        or int(row_counts.iloc[0]) != len(master)
    ):
        return "security master completeness counters are invalid"
    if pd.to_datetime(
        master["snapshot_at"],
        errors="coerce",
    ).isna().any():
        return "security master snapshot timestamp is invalid"
    if pd.to_datetime(
        master["security_master_snapshot_trade_date"]
        .astype("string")
        .str.replace(r"\.0$", "", regex=True),
        format="%Y%m%d",
        errors="coerce",
    ).isna().any():
        return "security master snapshot trade date is invalid"
    return None



def factor_cache_freshness(
    *,
    now: datetime | None = None,
    min_required: int = 800,
) -> dict:
    """Validate point-in-time master and latest daily cross-section coverage."""
    current = now or datetime.now()
    expected_trade_date = expected_completed_trade_date(current)
    minimum = max(1, int(min_required))
    quarantined = factor_history_quarantine.active_codes()

    master_trade_date = None
    master_error = None
    if not SECURITY_MASTER_FILE.is_file():
        master_error = "security master is missing"
    else:
        try:
            master = pd.read_csv(
                SECURITY_MASTER_FILE,
                dtype={"code": "string", "security_master_snapshot_trade_date": "string"},
            )
            if master.empty or "security_master_snapshot_trade_date" not in master.columns:
                master_error = "security master snapshot trade date is missing"
            else:
                snapshots = pd.to_datetime(
                    master["security_master_snapshot_trade_date"]
                    .astype("string")
                    .str.replace(r"\.0$", "", regex=True),
                    format="%Y%m%d",
                    errors="coerce",
                ).dropna()
                if snapshots.empty:
                    master_error = "security master snapshot trade date is invalid"
                else:
                    master_trade_date = snapshots.min().strftime("%Y-%m-%d")
        except Exception as exc:
            master_error = f"security master unreadable: {str(exc)[:160]}"

    latest_by_code: dict[str, str] = {}
    for path in sorted(DATA_DIR.glob("*.csv")):
        code = path.stem.split(".", 1)[0].zfill(6)
        if code in quarantined:
            continue
        try:
            dates = pd.read_csv(path, usecols=["date"])["date"]
            parsed = pd.to_datetime(dates, errors="coerce").dropna()
        except Exception:
            continue
        if not parsed.empty:
            latest_by_code[code] = parsed.max().strftime("%Y-%m-%d")
    daily_cache_trade_date = max(latest_by_code.values()) if latest_by_code else None
    daily_latest_count = sum(
        trade_date == expected_trade_date
        for trade_date in latest_by_code.values()
    )
    security_master_fresh = bool(
        master_trade_date == expected_trade_date
        and master_error is None
    )
    daily_cache_fresh = daily_latest_count >= minimum
    status = "fresh" if security_master_fresh and daily_cache_fresh else "stale"
    return {
        "status": status,
        "expected_trade_date": expected_trade_date,
        "security_master_trade_date": master_trade_date,
        "daily_cache_trade_date": daily_cache_trade_date,
        "daily_latest_count": int(daily_latest_count),
        "min_required": minimum,
        "security_master_fresh": security_master_fresh,
        "daily_cache_fresh": daily_cache_fresh,
        "quarantined_count": len(quarantined),
        "security_master_error": master_error,
    }

def refresh_factor_security_master_history(
    days: int = 180,
    n_stocks: int | None = None,
    now: datetime | None = None,
    token: str | None = None,
    mode: str | None = None,
    history_provider: str | None = None,
    agudata_api_key: str | None = None,
    agudata_base_url: str | None = None,
) -> dict:
    current = now or datetime.now()
    cfg = datasource_config.get_internal_config()
    selected_history_provider = str(
        history_provider or cfg.get("provider") or "tushare"
    ).strip().lower()
    resolved_token = token or cfg.get("tushare_token") or datasource_config.resolve_tushare_token(
        cfg,
        mode=mode or cfg.get("tushare_mode"),
    )
    resolved_mode = mode or cfg.get("tushare_mode")
    rows: list[dict] = []
    provider_errors: list[str] = []
    master_source = f"{selected_history_provider}_security_master"
    fallback_used = False
    try:
        if selected_history_provider == "agudata":
            rows = agudata_provider.fetch_security_master(
                api_key=agudata_api_key or cfg.get("agudata_api_key"),
                base_url=agudata_base_url or cfg.get("agudata_base_url"),
            )
        elif selected_history_provider == "baostock":
            rows = tushare_provider.fetch_baostock_security_master(
                snapshot_now=current,
            )
            master_source = "baostock_security_master"
        else:
            rows = tushare_provider.fetch_security_master(
                token=resolved_token,
                mode=resolved_mode,
            )
    except Exception as exc:
        provider_errors.append(
            f"{selected_history_provider or 'remote'}: {str(exc)[:200]}"
        )

    if not rows and selected_history_provider != "baostock":
        fallback_used = True
        try:
            rows = tushare_provider.fetch_baostock_security_master(
                snapshot_now=current,
            )
            master_source = "baostock_security_master"
        except Exception as exc:
            provider_errors.append(f"baostock: {str(exc)[:200]}")

    if not rows:
        return {
            "ok": False,
            "error": "security master provider failed",
            "source": master_source,
            "primary_source": f"{selected_history_provider}_security_master",
            "fallback_used": fallback_used,
            "provider_errors": provider_errors,
            "master_count": 0,
            "history_required": 0,
            "history_backfilled": 0,
        }

    master = pd.DataFrame(rows).copy()
    validation_error = _security_master_validation_error(master)
    if validation_error:
        return {
            "ok": False,
            "error": validation_error,
            "master_count": int(len(master)),
            "history_required": 0,
            "history_backfilled": 0,
        }
    master["code"] = (
        master["code"]
        .astype("string")
        .str.replace(r"\.0$", "", regex=True)
        .str.strip()
    )
    master = master.sort_values("code")
    SECURITY_MASTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = SECURITY_MASTER_FILE.with_suffix(SECURITY_MASTER_FILE.suffix + ".tmp")
    master.to_csv(temp_path, index=False, encoding="utf-8-sig")
    temp_path.replace(SECURITY_MASTER_FILE)

    end_date = pd.Timestamp(current.date())
    history_calendar_days = max(365, int(days) * 2 + 120)
    window_start = end_date - pd.Timedelta(days=history_calendar_days)
    list_dates = pd.to_datetime(
        master.get("list_date", pd.Series(index=master.index, dtype="string"))
        .astype("string")
        .str.replace(r"\.0$", "", regex=True),
        format="%Y%m%d",
        errors="coerce",
    )
    delist_dates = pd.to_datetime(
        master.get("delist_date", pd.Series(index=master.index, dtype="string"))
        .astype("string")
        .str.replace(r"\.0$", "", regex=True),
        format="%Y%m%d",
        errors="coerce",
    )
    lifecycle_relevant = (list_dates.isna() | (list_dates <= end_date)) & (
        delist_dates.isna() | (delist_dates >= window_start)
    )
    sample_master: pd.DataFrame | None = None
    sample_dates = pd.DatetimeIndex([])
    pool_size: int | None = None
    if n_stocks is not None:
        from analytics.factors_zoo.scorer import (
            _master_sample_mask,
            _point_in_time_pool_size,
        )

        minimum_history = min(max(1, int(days)), 60)
        sample_start = end_date - pd.offsets.BDay(
            max(int(days), minimum_history) + 5
        )
        sample_master = master.copy()
        sample_master["list_date"] = list_dates
        sample_master["delist_date"] = delist_dates
        sample_master = sample_master.set_index("code", drop=False)
        sample_dates = pd.bdate_range(sample_start, end_date)
        pool_size = _point_in_time_pool_size(n_stocks)

    def sampled_required_codes(excluded_codes: set[str] | None = None) -> set[str]:
        if n_stocks is None:
            return set(master.loc[lifecycle_relevant, "code"].astype(str))
        assert sample_master is not None
        eligible_master = sample_master
        if excluded_codes:
            eligible_master = eligible_master.loc[
                ~eligible_master.index.astype(str).isin(excluded_codes)
            ]
        sample_mask = _master_sample_mask(
            eligible_master,
            sample_dates,
            pool_size,
            minimum_history=minimum_history,
        )
        return {
            str(code)
            for code in sample_mask.columns
            if bool(sample_mask[code].any())
        }

    minimum_usable = (
        max(0, int(n_stocks))
        if n_stocks is not None
        else int(lifecycle_relevant.sum())
    )
    active_quarantine_before = factor_history_quarantine.active_codes()
    attempted_codes: set[str] = set()
    backfilled = 0
    failed = 0
    usable = 0
    errors: list[str] = []
    failure_reasons: dict[str, str] = {}
    recovered_codes: list[str] = []

    def build_entries(codes: set[str]) -> list[tuple]:
        entries: list[tuple] = []
        selected_rows = master.loc[master["code"].isin(codes)]
        for index, row in selected_rows.iterrows():
            code = str(row["code"])
            cache_path = DATA_DIR / f"{code}.csv"
            is_delisted = pd.notna(delist_dates.loc[index])
            security_end = min(
                end_date,
                pd.Timestamp(delist_dates.loc[index])
                if is_delisted
                else end_date,
            )
            security_start = max(
                window_start,
                pd.Timestamp(list_dates.loc[index])
                if pd.notna(list_dates.loc[index])
                else window_start,
            )
            end_tolerance_days = 14 if is_delisted else 0
            complete = _factor_history_cache_complete(
                cache_path,
                security_start=security_start,
                security_end=security_end,
                minimum_rows=min(max(1, int(days)), 60),
                require_end_coverage=True,
                end_tolerance_days=end_tolerance_days,
            )
            entries.append(
                (
                    code,
                    cache_path,
                    security_start,
                    security_end,
                    end_tolerance_days,
                    complete,
                )
            )
        return entries

    def process_entry(entry: tuple) -> None:
        nonlocal backfilled, failed, usable
        (
            code,
            cache_path,
            security_start,
            security_end,
            end_tolerance_days,
            complete,
        ) = entry
        attempted_codes.add(code)
        if complete:
            usable += 1
            recovered_codes.append(code)
            return
        try:
            try:
                if selected_history_provider == "agudata":
                    history = agudata_provider.fetch_kline_qfq(
                        code,
                        security_start.strftime("%Y%m%d"),
                        security_end.strftime("%Y%m%d"),
                        api_key=(
                            agudata_api_key
                            or cfg.get("agudata_api_key")
                        ),
                        base_url=(
                            agudata_base_url
                            or cfg.get("agudata_base_url")
                        ),
                    )
                else:
                    history = tushare_provider.fetch_kline_qfq(
                        code,
                        security_start.strftime("%Y%m%d"),
                        security_end.strftime("%Y%m%d"),
                        token=resolved_token,
                        mode=resolved_mode,
                    )
            except Exception:
                history = None
            history = _standardize_kline_for_cache(history)
            if history.empty:
                history = _standardize_kline_for_cache(
                    _fetch_kline_baostock(
                        code,
                        security_start.strftime("%Y-%m-%d"),
                        security_end.strftime("%Y-%m-%d"),
                    )
                )
            if history is None or history.empty:
                raise ValueError("empty qfq history")
            merged = merge_kline_cache(code, history)
            if merged.empty:
                raise ValueError("cache merge produced no rows")
            if not _factor_history_cache_complete(
                cache_path,
                security_start=security_start,
                security_end=security_end,
                minimum_rows=min(max(1, int(days)), 60),
                require_end_coverage=True,
                end_tolerance_days=end_tolerance_days,
            ):
                raise ValueError("history remains incomplete after backfill")
            backfilled += 1
            usable += 1
            recovered_codes.append(code)
        except Exception as exc:
            failed += 1
            reason = str(exc)[:160]
            failure_reasons[code] = reason
            if len(errors) < 20:
                errors.append(f"{code}: {reason}")

    pending_codes = sampled_required_codes()
    while pending_codes:
        new_codes = pending_codes - attempted_codes
        if not new_codes:
            break
        for entry in build_entries(new_codes):
            process_entry(entry)
        if n_stocks is None:
            break
        excluded_codes = (
            active_quarantine_before | set(failure_reasons)
        ) - set(recovered_codes)
        pending_codes = sampled_required_codes(excluded_codes)

    required_count = len(attempted_codes)
    if failure_reasons:
        factor_history_quarantine.record_failures(failure_reasons)
    if recovered_codes:
        factor_history_quarantine.clear_codes(recovered_codes)
    active_quarantine = factor_history_quarantine.active_codes()


    return {
        "ok": usable >= minimum_usable,
        "source": master_source,
        "primary_source": f"{selected_history_provider}_security_master",
        "fallback_used": fallback_used,
        "provider_errors": provider_errors,
        "master_count": int(len(master)),
        "history_required": required_count,
        "history_backfilled": backfilled,
        "history_failed": failed,
        "history_usable": usable,
        "history_incomplete": required_count - usable,
        "minimum_usable": minimum_usable,
        "history_quarantined": len(failure_reasons),
        "quarantined_codes": sorted(failure_reasons),
        "quarantine_reasons": failure_reasons,
        "active_quarantine_count": len(active_quarantine),
        "coverage": (usable / minimum_usable) if minimum_usable else 1.0,
        "errors": errors,
        "path": str(SECURITY_MASTER_FILE),
    }


def refresh_factor_daily_cache(
    days: int = 180,
    min_required: int = 800,
    now: datetime | None = None,
    refresh_security_master: bool = False,
) -> dict:
    """Refresh factor daily bars with Tushare primary and TDX fallback."""
    current = now or datetime.now()
    config = datasource_config.get_internal_config()
    remote_name, remote_provider, remote_kwargs = (
        _factor_remote_source(config)
    )
    security_master_result: dict | None = None
    if refresh_security_master:
        try:
            security_master_result = refresh_factor_security_master_history(
                days=days,
                n_stocks=min_required,
                now=current,
                token=config.get("tushare_token"),
                mode=config.get("tushare_mode"),
                history_provider=remote_name or "baostock",
                agudata_api_key=config.get("agudata_api_key"),
                agudata_base_url=config.get("agudata_base_url"),
            )
        except Exception as exc:
            security_master_result = {"ok": False, "error": str(exc)[:200]}
        if not security_master_result.get("ok"):
            return {
                "ok": False,
                "error": "point-in-time security master refresh failed",
                "security_master": security_master_result,
            }
    if remote_provider is None:
        result = _refresh_factor_daily_cache_tdx(days=days, min_required=min_required, now=current)
        result.setdefault("primary_source", "tdx_daily")
        result.setdefault("tushare_count", 0)
        result.setdefault("tushare_qfq_count", 0)
        result.setdefault("tdx_fallback_count", int(result.get("refreshed") or 0))
        return result

    token = config.get("tushare_token")
    mode = config.get("tushare_mode")
    fallback_enabled = bool(config.get("fallback_enabled", True))
    configured_source = (
        "agudata_daily"
        if remote_name == "agudata"
        else (
            "tushare_replay_daily"
            if str(mode).lower() == "replay"
            else "tushare_sdk_daily"
        )
    )
    source_name = configured_source
    source_frame = pd.DataFrame()
    source_date = None
    tushare_errors: list[str] = []
    csv_paths = sorted(DATA_DIR.glob("*.csv"))
    if not csv_paths:
        return {
            "ok": False,
            "error": "daily cache is empty",
            "source": configured_source,
            "primary_source": configured_source,
            "latest_count": 0,
            "min_required": max(1, int(min_required)),
        }
    minimum_market_rows = min(
        len(csv_paths),
        max(max(1, int(min_required)), (len(csv_paths) + 1) // 2),
    )
    market_close_guard = current.replace(hour=15, minute=30, second=0, microsecond=0)
    today_compact = current.strftime("%Y%m%d")

    for compact_date in _completed_date_candidates(current):
        candidate = pd.DataFrame()
        try:
            candidate = remote_provider.fetch_full_market_daily(
                compact_date,
                **remote_kwargs,
            )
        except Exception as exc:
            tushare_errors.append(f"daily {compact_date}: {str(exc)[:200]}")

        if candidate is not None and not candidate.empty and "date" in candidate.columns:
            dates = pd.to_datetime(candidate["date"], errors="coerce").dropna()
            if not dates.empty:
                candidate_source_date = pd.Timestamp(dates.max()).normalize()
                candidate = candidate[
                    pd.to_datetime(candidate["date"], errors="coerce").dt.normalize() == candidate_source_date
                ]
                if len(candidate) >= minimum_market_rows:
                    source_frame = candidate.copy()
                    source_date = candidate_source_date
                    source_name = configured_source
                    break
                tushare_errors.append(
                    f"daily {compact_date}: market rows {len(candidate)} below required {minimum_market_rows}"
                )

        should_try_realtime = (
            (
                remote_name == "agudata"
                or str(mode).lower() == "replay"
            )
            and current >= market_close_guard
            and compact_date == today_compact
        )
        if should_try_realtime:
            try:
                realtime = remote_provider.fetch_full_market_realtime_daily(
                    **remote_kwargs
                )
            except Exception as exc:
                tushare_errors.append(f"rt_k: {str(exc)[:200]}")
                realtime = pd.DataFrame()
            if realtime is not None and not realtime.empty and "date" in realtime.columns:
                realtime_dates = pd.to_datetime(realtime["date"], errors="coerce").dropna()
                if not realtime_dates.empty:
                    realtime_source_date = pd.Timestamp(realtime_dates.max()).normalize()
                    realtime = realtime[
                        pd.to_datetime(realtime["date"], errors="coerce").dt.normalize() == realtime_source_date
                    ]
                    if len(realtime) >= minimum_market_rows:
                        source_frame = realtime.copy()
                        source_date = realtime_source_date
                        source_name = (
                            "agudata_rt_k"
                            if remote_name == "agudata"
                            else "tushare_replay_rt_k"
                        )
                        break
                    tushare_errors.append(
                        f"rt_k: market rows {len(realtime)} below required {minimum_market_rows}"
                    )

    if source_date is None or source_frame.empty:
        cache_freshness = factor_cache_freshness(
            now=current,
            min_required=min_required,
        )
        cache_is_usable = bool(
            cache_freshness.get("daily_cache_fresh")
            and (
                not refresh_security_master
                or cache_freshness.get("security_master_fresh")
            )
        )
        if cache_is_usable:
            result = {
                "ok": True,
                "source": "existing_daily_cache",
                "primary_source": configured_source,
                "fallback_used": True,
                "source_trade_date": cache_freshness.get(
                    "daily_cache_trade_date"
                ),
                "expected_trade_date": cache_freshness.get(
                    "expected_trade_date"
                ),
                "latest_count": int(
                    cache_freshness.get("daily_latest_count") or 0
                ),
                "min_required": max(1, int(min_required)),
                "already_current": int(
                    cache_freshness.get("daily_latest_count") or 0
                ),
                "refreshed": 0,
                "failed": 0,
                "tushare_count": 0,
                "agudata_count": 0,
                "tushare_qfq_count": 0,
                "baostock_qfq_count": 0,
                "tdx_fallback_count": 0,
                "security_master": security_master_result,
                "cache_freshness": cache_freshness,
            }
            if tushare_errors:
                result["tushare_errors"] = tushare_errors
            return result
        if not fallback_enabled:
            return {
                "ok": False,
                "error": "Tushare full-market daily source unavailable and fallback is disabled",
                "source": configured_source,
                "primary_source": configured_source,
                "latest_count": 0,
                "min_required": max(1, int(min_required)),
                "tushare_count": 0,
                "tushare_qfq_count": 0,
                "tdx_fallback_count": 0,
                "tushare_errors": tushare_errors,
                "cache_freshness": cache_freshness,
            }
        result = _refresh_factor_daily_cache_tdx(days=days, min_required=min_required, now=current)
        result["primary_source"] = "tdx_daily"
        result["tushare_count"] = 0
        result["tushare_qfq_count"] = 0
        result["tdx_fallback_count"] = int(result.get("refreshed") or 0)
        if tushare_errors:
            result["tushare_errors"] = tushare_errors
        return result

    expected_trade_date = expected_completed_trade_date(current)
    source_trade_date = str(source_date.date())
    if source_trade_date != expected_trade_date:
        result = _stale_factor_source_result(
            source_trade_date,
            expected_trade_date,
            source=source_name,
            min_required=min_required,
        )
        result["tushare_count"] = 0
        result["tushare_qfq_count"] = 0
        result["tdx_fallback_count"] = 0
        if tushare_errors:
            result["tushare_errors"] = tushare_errors
        return result

    if refresh_security_master:
        from analytics.factors_zoo.scorer import (
            _load_security_master,
            _master_sample_mask,
            _point_in_time_pool_size,
            _security_master_lifecycle_trusted,
        )

        security_master = _load_security_master(SECURITY_MASTER_FILE)
        if not _security_master_lifecycle_trusted(security_master):
            return {
                "ok": False,
                "error": "point-in-time security master is unavailable or untrusted",
                "security_master": security_master_result,
            }
        min_history = min(max(1, int(days)), 60)
        sample_start = source_date - pd.offsets.BDay(
            max(int(days), min_history) + 5
        )
        sample_mask = _master_sample_mask(
            security_master,
            pd.bdate_range(sample_start, source_date),
            _point_in_time_pool_size(min_required),
            minimum_history=min_history,
        )
        required_codes = {
            str(code)
            for code in sample_mask.columns
            if bool(sample_mask[code].any())
        }
        if not required_codes:
            return {
                "ok": False,
                "error": "point-in-time security master produced an empty factor sample",
                "security_master": security_master_result,
            }
        csv_paths = [
            DATA_DIR / f"{code}.csv"
            for code in sorted(required_codes)
        ]

    source_frame = source_frame.copy()
    if "code" not in source_frame.columns:
        return {
            "ok": False,
            "error": "Tushare full-market daily payload has no stock code",
            "source": source_name,
            "primary_source": source_name,
            "source_trade_date": str(source_date.date()),
            "latest_count": 0,
            "min_required": max(1, int(min_required)),
        }
    source_frame["code"] = source_frame["code"].astype(str).str.extract(r"(\d{6})", expand=False)
    source_frame = source_frame.dropna(subset=["code"])
    source_by_code = {
        code: _standardize_kline_for_cache(group)
        for code, group in source_frame.groupby("code")
    }

    current_factors: dict[str, float] = {}
    previous_factors: dict[str, float] = {}
    previous_trade_date = None
    previous_expected_date = expected_completed_trade_date(
        datetime.combine((source_date - timedelta(days=1)).date(), datetime.min.time()).replace(hour=23)
    )
    try:
        factor_frame = remote_provider.fetch_full_market_adj_factors(
            source_date.strftime("%Y%m%d"),
            **remote_kwargs,
        )
        if factor_frame is not None and not factor_frame.empty:
            factor_frame = factor_frame.copy()
            factor_frame["date"] = pd.to_datetime(
                factor_frame.get("date"),
                errors="coerce",
            ).dt.normalize()
            factor_frame["adj_factor"] = pd.to_numeric(
                factor_frame.get("adj_factor"),
                errors="coerce",
            )
            factor_frame = factor_frame[
                (factor_frame["date"] == source_date)
                & (factor_frame["adj_factor"] > 0)
            ]
            current_factors = {
                str(row.code): float(row.adj_factor)
                for row in factor_frame.itertuples()
            }
            for compact_date in _completed_date_candidates(
                datetime.combine((source_date - timedelta(days=1)).date(), datetime.min.time()).replace(hour=23),
                lookback_days=10,
            ):
                previous = remote_provider.fetch_full_market_adj_factors(
                    compact_date,
                    **remote_kwargs,
                )
                if previous is None or previous.empty:
                    continue
                previous_dates = pd.to_datetime(previous.get("date"), errors="coerce").dropna()
                if previous_dates.empty:
                    continue
                candidate_previous_date = pd.Timestamp(previous_dates.max()).normalize()
                if str(candidate_previous_date.date()) != previous_expected_date:
                    continue
                previous = previous.copy()
                previous["date"] = pd.to_datetime(
                    previous.get("date"),
                    errors="coerce",
                ).dt.normalize()
                previous["adj_factor"] = pd.to_numeric(
                    previous.get("adj_factor"),
                    errors="coerce",
                )
                previous = previous[
                    (previous["date"] == candidate_previous_date)
                    & (previous["adj_factor"] > 0)
                ]
                if previous.empty:
                    continue
                previous_trade_date = candidate_previous_date
                previous_factors = {
                    str(row.code): float(row.adj_factor)
                    for row in previous.itertuples()
                }
                break
    except Exception as exc:
        tushare_errors.append(f"adj_factor: {str(exc)[:200]}")

    min_history = min(max(1, int(days)), 60)
    from analytics.factors_zoo.scorer import _has_discontinuous_price_history

    metadata = _load_meta()
    refreshed = 0
    already_current = 0
    failed = 0
    tushare_count = 0
    tushare_qfq_count = 0
    baostock_qfq_count = 0
    tdx_fallback_count = 0
    adjustment_fallback_count = 0
    adjustment_repair_failed_count = 0
    gap_repair_failed_count = 0
    continuity_repair_count = 0
    continuity_repair_failed_count = 0
    factor_coverage_repair_count = 0
    factor_coverage_repair_failed_count = 0
    errors: list[str] = []
    tdx_client = None

    for idx, csv_path in enumerate(csv_paths, start=1):
        code = csv_path.stem
        merged = pd.DataFrame()
        old_frame = pd.DataFrame()
        final_source = ""
        updated = False
        current_without_write = False
        factor_changed = False
        try:
            try:
                old_frame = _standardize_kline_for_cache(pd.read_csv(csv_path, parse_dates=["date"]))
            except Exception:
                old_frame = pd.DataFrame()
            old_dates = (
                pd.to_datetime(old_frame["date"], errors="coerce").dropna()
                if not old_frame.empty and "date" in old_frame.columns
                else pd.Series(dtype="datetime64[ns]")
            )
            old_latest = pd.Timestamp(old_dates.max()).normalize() if not old_dates.empty else None
            basis_dates = old_dates[old_dates.dt.normalize() < source_date]
            basis_date = pd.Timestamp(basis_dates.max()).normalize() if not basis_dates.empty else old_latest
            current_factor = current_factors.get(code)
            previous_factor = previous_factors.get(code)
            factor_coverage_missing = bool(
                current_factor is None
                or current_factor <= 0
                or previous_factor is None
                or previous_factor <= 0
            )
            factor_changed = bool(
                current_factor
                and previous_factor
                and abs(current_factor - previous_factor) > max(1e-8, abs(previous_factor) * 1e-10)
            )
            has_gap = bool(previous_trade_date is not None and basis_date is not None and basis_date < previous_trade_date)
            has_discontinuity = bool(
                len(old_frame) >= min_history
                and _has_discontinuous_price_history(
                    old_frame.tail(max(int(days), min_history))
                )
            )
            primary_slice = source_by_code.get(code, pd.DataFrame())
            requires_full_qfq_repair = bool(
                factor_changed
                or factor_coverage_missing
                or has_discontinuity
            )

            if (
                requires_full_qfq_repair
                or has_gap
                or primary_slice.empty
            ):
                start_date = (
                    pd.Timestamp(old_dates.min()).strftime("%Y%m%d")
                    if requires_full_qfq_repair and not old_dates.empty
                    else (
                        (old_latest + timedelta(days=1)).strftime("%Y%m%d")
                        if old_latest is not None
                        else (source_date - timedelta(days=max(365, int(days) * 2))).strftime("%Y%m%d")
                    )
                )
                replay_mode = (
                    remote_name == "tushare"
                    and str(mode).lower() == "replay"
                )
                qfq_frame = pd.DataFrame()
                qfq_source = ""
                if replay_mode and fallback_enabled:
                    qfq_frame = _standardize_kline_for_cache(
                        _fetch_kline_baostock(
                            code,
                            datetime.strptime(start_date, "%Y%m%d").strftime("%Y-%m-%d"),
                            source_date.strftime("%Y-%m-%d"),
                        )
                    )
                    if not qfq_frame.empty:
                        qfq_source = "baostock_qfq"
                if qfq_frame.empty:
                    try:
                        qfq_frame = remote_provider.fetch_kline_qfq(
                            code,
                            start_date,
                            source_date.strftime("%Y%m%d"),
                            **remote_kwargs,
                        )
                    except Exception as exc:
                        qfq_frame = None
                        if len(errors) < 20:
                            errors.append(f"{code} tushare qfq: {str(exc)[:120]}")
                    qfq_frame = _standardize_kline_for_cache(qfq_frame)
                    if not qfq_frame.empty:
                        qfq_source = (
                            "agudata_qfq"
                            if remote_name == "agudata"
                            else (
                                "tushare_replay_qfq"
                                if replay_mode
                                else "tushare_sdk_qfq"
                            )
                        )
                if qfq_frame.empty and fallback_enabled and not replay_mode:
                    qfq_frame = _standardize_kline_for_cache(
                        _fetch_kline_baostock(
                            code,
                            datetime.strptime(start_date, "%Y%m%d").strftime("%Y-%m-%d"),
                            source_date.strftime("%Y-%m-%d"),
                        )
                    )
                    if not qfq_frame.empty:
                        qfq_source = "baostock_qfq"
                if qfq_frame is not None and not qfq_frame.empty:
                    qfq_frame = qfq_frame[qfq_frame["date"] <= source_date]
                    if requires_full_qfq_repair:
                        qfq_dates = pd.to_datetime(
                            qfq_frame["date"],
                            errors="coerce",
                        ).dropna()
                        covers_history = bool(
                            not qfq_dates.empty
                            and (
                                old_dates.empty
                                or pd.Timestamp(qfq_dates.min()).normalize()
                                <= pd.Timestamp(old_dates.min()).normalize()
                            )
                        )
                        qfq_is_continuous = bool(
                            len(qfq_frame) >= min_history
                            and not _has_discontinuous_price_history(
                                qfq_frame.tail(max(int(days), min_history))
                            )
                        )
                        if not covers_history or not qfq_is_continuous:
                            qfq_frame = pd.DataFrame()
                    if not qfq_frame.empty and pd.to_datetime(qfq_frame["date"]).max().normalize() == source_date:
                        merged = merge_kline_cache(code, qfq_frame, update_metadata=False)
                        final_source = qfq_source
                        updated = not merged.empty

            if (
                requires_full_qfq_repair
                or has_gap
            ) and merged.empty:
                if factor_changed:
                    adjustment_repair_failed_count += 1
                if factor_coverage_missing:
                    factor_coverage_repair_failed_count += 1
                if has_gap:
                    gap_repair_failed_count += 1
                if has_discontinuity:
                    continuity_repair_failed_count += 1
                failed += 1
                if len(errors) < 20:
                    reason = (
                        "adjustment change"
                        if factor_changed
                        else (
                            "adjustment factor coverage"
                            if factor_coverage_missing
                            else (
                                "history gap"
                                if has_gap
                                else "price discontinuity"
                            )
                        )
                    )
                    errors.append(f"{code}: QFQ repair unavailable for {reason}")
                continue

            if merged.empty and not primary_slice.empty:
                if _same_daily_row(old_frame, primary_slice, source_date) and len(old_frame) >= min_history:
                    merged = old_frame
                    final_source = source_name
                    current_without_write = True
                else:
                    merged = merge_kline_cache(code, primary_slice, update_metadata=False)
                    if not merged.empty:
                        final_source = source_name
                        updated = True
                        if factor_changed:
                            adjustment_fallback_count += 1

            latest = (
                pd.to_datetime(merged["date"], errors="coerce").dropna()
                if not merged.empty and "date" in merged.columns
                else pd.Series(dtype="datetime64[ns]")
            )
            valid = bool(
                not merged.empty
                and len(merged) >= min_history
                and not latest.empty
                and pd.Timestamp(latest.max()).normalize() == source_date
            )
            if not valid and fallback_enabled:
                if tdx_client is None:
                    from market import ths_quote
                    tdx_client = ths_quote.get_client()
                bars = tdx_client.get_kline(code, max(int(days), min_history)) if tdx_client is not None else []
                fallback_frame = _standardize_kline_for_cache(pd.DataFrame(bars or []))
                if not fallback_frame.empty:
                    fallback_frame = fallback_frame[fallback_frame["date"] <= source_date]
                    merged = merge_kline_cache(code, fallback_frame, update_metadata=False)
                    latest = (
                        pd.to_datetime(merged["date"], errors="coerce").dropna()
                        if not merged.empty and "date" in merged.columns
                        else pd.Series(dtype="datetime64[ns]")
                    )
                    valid = bool(
                        not merged.empty
                        and len(merged) >= min_history
                        and not latest.empty
                        and pd.Timestamp(latest.max()).normalize() == source_date
                    )
                    if valid:
                        final_source = "tdx_daily_fallback"
                        updated = True
                        current_without_write = False

            if not valid:
                failed += 1
                continue

            if final_source == "tdx_daily_fallback":
                tdx_fallback_count += 1
            elif final_source == "baostock_qfq":
                baostock_qfq_count += 1
                if factor_coverage_missing:
                    factor_coverage_repair_count += 1
                if has_discontinuity:
                    continuity_repair_count += 1
            else:
                tushare_count += 1
                if final_source.endswith("_qfq"):
                    tushare_qfq_count += 1
                    if factor_coverage_missing:
                        factor_coverage_repair_count += 1
                    if has_discontinuity:
                        continuity_repair_count += 1
            if current_without_write:
                already_current += 1
            elif updated:
                refreshed += 1
                item = dict(metadata.get(code, {}))
                item.update({
                    "name": item.get("name") or code,
                    "start": str(pd.to_datetime(merged["date"]).iloc[0].date()),
                    "end": str(source_date.date()),
                    "rows": len(merged),
                    "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "daily_source": final_source,
                })
                metadata[code] = item
        except Exception as exc:
            failed += 1
            if len(errors) < 20:
                errors.append(f"{code}: {str(exc)[:120]}")
        if idx % 250 == 0:
            logger.info(
                "[factor_cache] progress %s/%s tushare=%s tdx_fallback=%s failed=%s",
                idx,
                len(csv_paths),
                tushare_count,
                tdx_fallback_count,
                failed,
            )

    if refreshed:
        _save_meta(metadata)
    latest_count = len(csv_paths) - failed
    result = {
        "ok": latest_count >= max(1, int(min_required)),
        "source": source_name,
        "primary_source": source_name,
        "source_trade_date": str(source_date.date()),
        "expected_trade_date": expected_trade_date,
        "total": len(csv_paths),
        "latest_count": latest_count,
        "min_required": max(1, int(min_required)),
        "already_current": already_current,
        "refreshed": refreshed,
        "failed": failed,
        "tushare_count": (
            tushare_count if remote_name == "tushare" else 0
        ),
        "tushare_qfq_count": (
            tushare_qfq_count if remote_name == "tushare" else 0
        ),
        "agudata_count": (
            tushare_count if remote_name == "agudata" else 0
        ),
        "agudata_qfq_count": (
            tushare_qfq_count if remote_name == "agudata" else 0
        ),
        "baostock_qfq_count": baostock_qfq_count,
        "tdx_fallback_count": tdx_fallback_count,
        "adjustment_fallback_count": adjustment_fallback_count,
        "adjustment_repair_failed_count": adjustment_repair_failed_count,
        "gap_repair_failed_count": gap_repair_failed_count,
        "continuity_repair_count": continuity_repair_count,
        "continuity_repair_failed_count": continuity_repair_failed_count,
        "factor_coverage_repair_count": factor_coverage_repair_count,
        "factor_coverage_repair_failed_count": factor_coverage_repair_failed_count,
        "current_factor_coverage": (
            len(set(current_factors).intersection(path.stem for path in csv_paths))
            / len(csv_paths)
            if csv_paths
            else 0.0
        ),
        "previous_factor_coverage": (
            len(set(previous_factors).intersection(path.stem for path in csv_paths))
            / len(csv_paths)
            if csv_paths
            else 0.0
        ),
        "tushare_market_rows": (
            len(source_frame) if remote_name == "tushare" else 0
        ),
        "agudata_market_rows": (
            len(source_frame) if remote_name == "agudata" else 0
        ),
    }
    if security_master_result is not None:
        result["security_master"] = security_master_result
    if tushare_errors:
        result["tushare_errors"] = tushare_errors
    if errors:
        result["errors"] = errors
    if not result["ok"]:
        result["error"] = f"latest daily coverage {latest_count} is below required {result['min_required']}"
    return result


# ── K线元数据：进程内缓存 + 延迟原子落盘 ──────────────────────────
# 背景：metadata.json 实测 858KB / 4000+ 条目，而 merge_kline_cache() 对**每只股票**
# 都要「整份读 + 整份重写」。一轮 5000 只的全市场扫描 ≈ 数 GB 无谓磁盘 I/O，
# 外加 4000+ 次 json.loads/dumps 在 GIL 内串行，直接抢占唯一 gunicorn worker 的 CPU。
# 且 write_text 是非原子截断写、该路径无锁，并发扫描会把文件写残
#（"元数据文件损坏，回退空"的自愈日志就是这么来的）。
#
# 现在：读走 mtime+size 校验的进程内缓存；写只标脏，按 _META_FLUSH_INTERVAL 合并落盘，
# 落盘走 tempfile + os.replace 原子替换。批量任务结束/进程退出时强制 flush。
# 元数据是缓存不是真相（真相是 data/daily/*.csv），极端情况下丢几条只会触发重新下载。
_META_LOCK = threading.RLock()
_META_CACHE: dict | None = None
_META_STAMP = None          # 上次读/写时的 (mtime_ns, size)
_META_DIRTY = False
_META_LAST_FLUSH = 0.0
_META_FLUSH_INTERVAL = 30.0


def _meta_stamp():
    try:
        st = META_FILE.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _load_meta() -> dict:
    global _META_CACHE, _META_STAMP
    with _META_LOCK:
        # 有未落盘的改动时以内存版为准（此时文件 stamp 必然对不上）
        if _META_CACHE is not None and (_META_DIRTY or _meta_stamp() == _META_STAMP):
            return _META_CACHE
        data = {}
        if META_FILE.exists():
            try:
                data = json.loads(META_FILE.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[data] K线元数据文件损坏，回退空（将触发重新下载）: {e}")
                data = {}
        _META_CACHE = data
        _META_STAMP = _meta_stamp()
        return _META_CACHE


def _flush_meta_locked() -> None:
    """把内存态原子写盘。调用方必须已持有 _META_LOCK。"""
    global _META_STAMP, _META_DIRTY, _META_LAST_FLUSH
    if _META_CACHE is None:
        return
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = META_FILE.with_suffix(f".tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(_META_CACHE, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, META_FILE)          # 原子替换，不会留下写残的半份文件
        _META_STAMP = _meta_stamp()
        _META_DIRTY = False
    except Exception as e:
        logger.warning(f"[data] K线元数据落盘失败（内存态保留，下次再试）: {e}")
        try:
            tmp.unlink()
        except OSError:
            pass
    finally:
        _META_LAST_FLUSH = time.time()


def flush_meta() -> None:
    """强制把元数据落盘。批量下载/扫描结束时调用；进程退出时自动调用。"""
    with _META_LOCK:
        if _META_DIRTY:
            _flush_meta_locked()


def _save_meta(meta: dict, immediate: bool = True):
    """更新元数据。immediate=False 只标脏并按间隔合并落盘（供逐只股票的热路径用）。"""
    global _META_CACHE, _META_DIRTY
    with _META_LOCK:
        _META_CACHE = meta
        _META_DIRTY = True
        if immediate or (time.time() - _META_LAST_FLUSH) >= _META_FLUSH_INTERVAL:
            _flush_meta_locked()


import atexit as _atexit
_atexit.register(flush_meta)


# ── 股票名称缓存（全量，避免东方财富接口被代理拦截）────────
def _load_name_cache() -> dict:
    """从本地文件加载缓存"""
    if NAME_CACHE_FILE.exists():
        try:
            return json.loads(NAME_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[data] 股票名称缓存损坏，回退空（名称将显示为代码）: {e}")
    return {}

def refresh_name_cache() -> dict:
    """
    调用 ak.stock_info_a_code_name() 下载全市场股票名称并缓存。
    此接口走上交所/深交所，不走东方财富，企业代理一般不拦截。
    """
    global _name_cache
    try:
        _tmp = {k: os.environ.pop(k) for k in list(_PROXY_KEYS) if k in os.environ}
        try:
            df = ak.stock_info_a_code_name()
        finally:
            os.environ.update(_tmp)
        if df is None or df.empty:
            logger.warning("stock_info_a_code_name 返回空")
            return _name_cache
        cache = {}
        for _, row in df.iterrows():
            cache[str(row.get("code", row.iloc[0])).strip()] = str(row.get("name", row.iloc[1])).strip()
        NAME_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        NAME_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        with _name_cache_lock:
            _name_cache = cache
        logger.info(f"股票名称缓存更新完成，共 {len(cache)} 只")
        return cache
    except Exception as e:
        logger.warning(f"更新股票名称缓存失败: {e}")
        return _name_cache

def _ensure_name_cache():
    global _name_cache
    with _name_cache_lock:
        if _name_cache:
            return
        loaded = _load_name_cache()
        if loaded:
            _name_cache = loaded
            return
    # 缓存文件不存在，后台静默刷新
    threading.Thread(target=refresh_name_cache, daemon=True).start()

def get_stock_name(code: str) -> str:
    _ensure_name_cache()
    name = _name_cache.get(code, "")
    if name:
        return name
    # 最后从 meta 查
    meta = _load_meta()
    return meta.get(code, {}).get("name", code)


# ── 概念板块查询 ──────────────────────────────────
def get_stock_sector(code: str) -> dict:
    """
    返回 {"industry": "银行", "concepts": ["大金融", "沪深300", ...]}。
    优先使用本地缓存；远程概念查询做限流、超时保护和失败冷却，避免单接口拖死整个站点。
    """
    now = time.time()
    result = {"industry": "", "concepts": []}
    clean_code = str(code or "").zfill(6)

    try:
        sector_map = get_stock_sectors_bulk([clean_code]) or {}
        routed = sector_map.get(clean_code) or sector_map.get(str(code)) or {}
        if routed.get("industry") or routed.get("concepts"):
            return {
                "industry": str(routed.get("industry") or ""),
                "concepts": list(routed.get("concepts") or [])[:5],
            }
    except Exception as e:
        logger.debug(f"data_gateway single sector route failed for {code}: {e}")

    meta = _load_meta()
    cached = meta.get(code, {})
    sector_cached_at = float(cached.get("sector_cached_at") or 0)
    sector_error_at = float(cached.get("sector_error_at") or 0)

    if cached.get("industry"):
        result["industry"] = cached["industry"]
    if cached.get("concepts"):
        result["concepts"] = cached["concepts"]

    if result["industry"] and result["concepts"] and now - sector_cached_at < _SECTOR_CACHE_TTL:
        return result
    if sector_error_at and now - sector_error_at < _SECTOR_FAILURE_TTL and (result["industry"] or result["concepts"]):
        return result
    if not _allow_current_sources():
        return result

    try:
        info = ak.stock_individual_info_em(symbol=code)
        if info is not None and not info.empty:
            ind_row = info[info["item"] == "行业"]
            if not ind_row.empty:
                result["industry"] = str(ind_row["value"].iloc[0])
    except Exception as e:
        logger.warning(f"get_stock_sector industry failed for {code}: {e}")

    concepts = list(result["concepts"] or [])
    started = time.monotonic()
    remote_scan_ok = False
    try:
        df = ak.stock_board_concept_name_em()
        if df is not None and not df.empty:
            remote_scan_ok = True
            for _, row in df.head(_MAX_CONCEPT_SCAN).iterrows():
                if time.monotonic() - started > _SECTOR_TIMEOUT_GUARD:
                    logger.warning(f"get_stock_sector concept scan timed out for {code}")
                    remote_scan_ok = False
                    break
                board = str(row.get("板块名称", "")).strip()
                if not board:
                    continue
                try:
                    cons = ak.stock_board_concept_cons_em(symbol=board)
                    if cons is None or cons.empty or "代码" not in cons.columns:
                        continue
                    codes = cons["代码"].astype(str)
                    if code in codes.values and board not in concepts:
                        concepts.append(board)
                    if len(concepts) >= 5:
                        break
                except Exception as e:
                    logger.debug(f"get_stock_sector concept board failed for {code}/{board}: {e}")
                    continue
    except Exception as e:
        logger.warning(f"get_stock_sector concept list failed for {code}: {e}")

    if not result["industry"] or not concepts:
        fallback = _fetch_stock_sector_eastmoney(code)
        if not result["industry"] and fallback.get("industry"):
            result["industry"] = fallback["industry"]
        for concept in fallback.get("concepts") or []:
            if concept not in concepts:
                concepts.append(concept)
            if len(concepts) >= 5:
                break

    result["concepts"] = concepts[:5]

    cache_patch = {
        "industry": result["industry"],
        "concepts": result["concepts"],
    }
    if result["industry"] or result["concepts"]:
        cache_patch["sector_cached_at"] = now
        cache_patch["sector_error_at"] = 0
    elif not remote_scan_ok:
        cache_patch["sector_error_at"] = now

    if cache_patch:
        with _sector_cache_lock:
            meta = _load_meta()
            meta[code] = meta.get(code, {})
            meta[code].update(cache_patch)
            _save_meta(meta)

    return result


# ── 单只下载 ──────────────────────────────────────
def _no_proxy_get(url: str, **kwargs):
    """用不走代理的 session 发 GET，供 akshare monkey-patch 后备用"""
    return _ak_session.get(url, **kwargs)


# ── 多数据源 K 线降级链：akshare(腾讯) → baostock → 优矿(uqer) ──────
def _fetch_kline_baostock(code: str, start: str, end: str):
    """baostock 日线（免费，无需 token）。start/end: YYYY-MM-DD。失败返回 None。"""
    try:
        import io
        import contextlib
        import baostock as bs
        prefix = "sh." if code.startswith(("6", "9", "5")) else "sz."
        with contextlib.redirect_stdout(io.StringIO()):   # 压掉 login/logout 的打印
            lg = bs.login()
            try:
                if lg.error_code != "0":
                    return None
                rs = bs.query_history_k_data_plus(
                    prefix + code, "date,open,high,low,close,volume,amount",
                    start_date=start, end_date=end,
                    frequency="d", adjustflag="2")   # 2=前复权，与腾讯源口径一致
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
            finally:
                bs.logout()
        if not rows:
            return None
        df = pd.DataFrame(
            rows,
            columns=["date", "open", "high", "low", "close", "volume", "amount"],
        )
        for c in ("open", "high", "low", "close", "volume", "amount"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        # Baostock uses shares/yuan; the cache follows Tushare lots/thousand-yuan units.
        df["volume"] = df["volume"] / 100.0
        df["amount"] = df["amount"] / 1000.0
        df = df.dropna(subset=["close"])
        return df if not df.empty else None
    except Exception as e:
        logger.debug(f"[baostock] {code} 失败: {e}")
        return None


def _uqer_token() -> str:
    """优矿 token 从 data/datasource_config.json 读取（{"uqer_token": "..."}）。"""
    try:
        import json as _json
        p = DATA_DIR.parent / "datasource_config.json"
        if p.exists():
            return (_json.loads(p.read_text(encoding="utf-8")) or {}).get("uqer_token", "")
    except Exception as e:
        logger.debug(f"[data] 优矿 token 读取失败，优矿数据源跳过: {e}")
    return ""


def _fetch_kline_uqer(code: str, start: str, end: str):
    """优矿(通联) 日线。需 pip install uqer + 配置 token，未配置时静默跳过。"""
    token = _uqer_token()
    if not token:
        return None
    try:
        import uqer
        from uqer import DataAPI
        uqer.Client(token=token)
        df = DataAPI.MktEqudAdjGet(
            ticker=code,
            beginDate=start.replace("-", ""), endDate=end.replace("-", ""),
            field="tradeDate,openPrice,highestPrice,lowestPrice,closePrice,turnoverVol",
            pandas="1")
        if df is None or df.empty:
            return None
        df = df.rename(columns={
            "tradeDate": "date", "openPrice": "open", "highestPrice": "high",
            "lowestPrice": "low", "closePrice": "close", "turnoverVol": "volume"})
        return df[["date", "open", "high", "low", "close", "volume"]]
    except Exception as e:
        logger.debug(f"[uqer] {code} 失败: {e}")
        return None


def _download_one(code: str, start_date: str) -> dict:
    try:
        symbol = _tx_symbol(code)
        end    = datetime.today().strftime("%Y%m%d")
        start  = start_date.replace("-", "")
        csv_path = DATA_DIR / f"{code}.csv"
        existing_latest = None
        if csv_path.exists():
            try:
                old_dates = pd.read_csv(csv_path, usecols=["date"])
                existing_latest = pd.to_datetime(old_dates["date"], errors="coerce").max()
            except Exception:
                existing_latest = None

        ds_cfg = datasource_config.get_internal_config()
        use_tushare = (
            ds_cfg.get("provider") == "tushare"
            and bool(ds_cfg.get("tushare_enabled"))
            and bool(ds_cfg.get("tushare_token"))
        )

        # 临时清除代理环境变量（akshare 走 urllib3，直接受 env 影响）
        _tmp = {k: os.environ.pop(k) for k in list(_PROXY_KEYS) if k in os.environ}
        logger.info(f"下载 {code}({symbol}) {start}~{end} ...")
        src = "腾讯"
        df = None
        try:
            if use_tushare:
                src = "tushare"
                try:
                    df = tushare_provider.fetch_kline_qfq(
                        code,
                        start,
                        end,
                        token=ds_cfg.get("tushare_token"),
                        mode=ds_cfg.get("tushare_mode"),
                    )
                except Exception as _tse:
                    logger.info(f"[{code}] Tushare源失败({_tse})")
                    df = None
                if df is not None and not df.empty and existing_latest is not None and not pd.isna(existing_latest):
                    fetched_latest = pd.to_datetime(df.get("date"), errors="coerce").max()
                    if not pd.isna(fetched_latest) and fetched_latest < existing_latest:
                        msg = f"Tushare最新日期 {fetched_latest.date()} 早于现有缓存 {existing_latest.date()}"
                        logger.info(f"[{code}] {msg}")
                        if ds_cfg.get("fallback_enabled", True):
                            df = None
                        else:
                            return {"ok": False, "code": code, "msg": msg}
                if (df is None or df.empty) and not ds_cfg.get("fallback_enabled", True):
                    return {"ok": False, "code": code, "msg": "Tushare 无数据，且未启用回退数据源"}

            if df is None or df.empty:
                src = "腾讯"
                try:
                    # adjust="qfq" 前复权 —— 必须与 baostock(adjustflag=2)/优矿(AdjGet) 口径一致；
                    # 不复权数据在除权日有价格断崖，回测会误触发止损、收益完全失真
                    df = ak.stock_zh_a_hist_tx(symbol=symbol, start_date=start, end_date=end,
                                               adjust="qfq")
                except Exception as _te:
                    logger.info(f"[{code}] 腾讯源失败({_te})，降级 baostock")
                    df = None
            # 降级链：Tushare(可选) → 腾讯 → baostock → 优矿
            if df is None or df.empty:
                src = "baostock"
                df  = _fetch_kline_baostock(code, start_date, datetime.today().strftime("%Y-%m-%d"))
            if df is None or df.empty:
                src = "uqer"
                df  = _fetch_kline_uqer(code, start_date, datetime.today().strftime("%Y-%m-%d"))
        finally:
            os.environ.update(_tmp)   # 还原，避免影响其他请求（如 AI 接口）

        if df is None or df.empty:
            return {"ok": False, "code": code, "msg": "Tushare/腾讯/baostock/优矿 均无数据"}
        if src != "腾讯":
            logger.info(f"[{code}] 已用降级数据源: {src}")

        if "volume" not in df.columns and "amount" in df.columns:
            df = df.rename(columns={"amount": "volume"})

        required = ["date", "open", "close", "high", "low", "volume"]
        missing  = [c for c in required if c not in df.columns]
        if missing:
            return {"ok": False, "code": code,
                    "msg": f"缺少列: {missing}，实际: {list(df.columns)}"}

        df = _clean_kline(df)
        if df is None or df.empty:
            return {"ok": False, "code": code, "msg": "清洗后无有效数据"}
        new_latest = pd.to_datetime(df["date"], errors="coerce").max()
        if existing_latest is not None and not pd.isna(existing_latest) and not pd.isna(new_latest) and new_latest < existing_latest:
            return {
                "ok": False,
                "code": code,
                "msg": f"下载结果最新日期 {new_latest.date()} 早于现有缓存 {existing_latest.date()}，已拒绝覆盖",
            }

        optional = [column for column in ("amount", "name", "is_st") if column in df.columns]
        df[required + optional].to_csv(csv_path, index=False, encoding="utf-8")
        logger.info(f"已保存 {csv_path} ({len(df)} 行, 前复权+已清洗)")

        # 优先用缓存名称，无则写入 code
        name = get_stock_name(code) or code

        meta = _load_meta()
        meta[code] = {
            "name":    name,
            "start":   str(df["date"].iloc[0].date()),
            "end":     str(df["date"].iloc[-1].date()),
            "rows":    len(df),
            "adjust":  "qfq",   # 标记前复权，区分旧的不复权数据
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        _save_meta(meta)

        return {"ok": True, "code": code, "name": name, "rows": len(df)}

    except Exception as e:
        logger.error(f"下载 {code} 失败: {e}")
        return {"ok": False, "code": code, "msg": str(e)}


# ── 批量后台下载 ──────────────────────────────────
_PROGRESS_MAX     = 20        # 最多保留多少个已结束任务
_PROGRESS_TTL_SEC = 3600.0    # 已结束任务保留多久


def _prune_progress():
    """淘汰已结束的下载任务记录（调用方必须已持有 _lock）。

    _progress 此前只增不删：每个条目带 results 全量列表（200 只股票的
    逐只结果），云端 worker 常驻数周 → 稳定泄漏。运行中的任务永不淘汰。
    """
    finished = [(v.get("finished_at", 0.0), k) for k, v in _progress.items()
                if v.get("status") != "running"]
    if not finished:
        return
    now = time.time()
    stale = {k for ts, k in finished if ts and now - ts > _PROGRESS_TTL_SEC}
    keep = sorted((t for t in finished if t[1] not in stale), reverse=True)
    for _ts, k in keep[_PROGRESS_MAX:]:
        stale.add(k)
    for k in stale:
        _progress.pop(k, None)


def start_download(codes: list, start_date: str, task_id: str):
    _ensure_name_cache()          # 顺便触发名称缓存加载
    with _lock:
        _prune_progress()         # 新任务入表前先清理已结束的旧任务
        _progress[task_id] = {
            "status": "running", "total": len(codes),
            "done": 0, "current": "", "errors": [], "results": [],
        }

    def _worker():
        for code in codes:
            with _lock:
                _progress[task_id]["current"] = code
            result = _download_one(code, start_date)
            with _lock:
                p = _progress[task_id]
                p["done"]   += 1
                p["results"].append(result)
                if not result["ok"]:
                    p["errors"].append(f"{code}: {result.get('msg','')}")
        with _lock:
            _progress[task_id]["status"]  = "done"
            _progress[task_id]["current"] = ""
            _progress[task_id]["finished_at"] = time.time()
        ok_cnt = sum(1 for r in _progress[task_id]["results"] if r["ok"])
        logger.info(f"批量下载完成 task={task_id} 成功={ok_cnt} 失败={len(_progress[task_id]['errors'])}")

    threading.Thread(target=_worker, daemon=True).start()


def get_progress(task_id: str) -> dict:
    with _lock:
        return dict(_progress.get(task_id, {"status": "not_found"}))


# ── 读取 / 列表 / 删除 ───────────────────────────
def list_stocks() -> list:
    meta = _load_meta()
    return sorted([{"code": k, **v} for k, v in meta.items()], key=lambda x: x["code"])

def get_local(code: str) -> pd.DataFrame:
    p = DATA_DIR / f"{code}.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, parse_dates=["date"])
    # 读取时防御性清洗（兼容旧版下载的脏数据）
    return _clean_kline(df)

def delete_stock(code: str) -> dict:
    p = DATA_DIR / f"{code}.csv"
    if p.exists():
        p.unlink()
    meta = _load_meta()
    meta.pop(code, None)
    _save_meta(meta)
    return {"ok": True}
