# -*- coding: utf-8 -*-
"""
本地历史数据管理
数据源：akshare stock_zh_a_hist_tx（腾讯财经）
股票名称：ak.stock_info_a_code_name() 全量缓存，不走东方财富
"""

import json, logging, threading, os, time, re
from pathlib import Path
from datetime import datetime

import pandas as pd
import akshare as ak

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

DATA_DIR       = Path(__file__).parent.parent / "data" / "daily"
META_FILE      = Path(__file__).parent.parent / "data" / "metadata.json"
NAME_CACHE_FILE= Path(__file__).parent.parent / "data" / "stock_names.json"
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
    """Fetch industry/concept labels for many stocks from Eastmoney quote list API."""
    clean_codes = []
    for code in codes or []:
        c = str(code or "").zfill(6)
        if c.isdigit() and c not in clean_codes:
            clean_codes.append(c)
    if not clean_codes:
        return {}

    result: dict[str, dict] = {}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    fields = "f12,f100,f103"
    urls = (
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        "https://48.push2.eastmoney.com/api/qt/ulist.np/get",
    )
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
    for c in ("open", "high", "low", "close", "volume"):
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
    return _clean_kline(out[required])


def merge_kline_cache(code: str, df: pd.DataFrame) -> pd.DataFrame:
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
            merged = _clean_kline(pd.concat([old_df, new_df], ignore_index=True))
        else:
            merged = new_df
    else:
        merged = new_df

    required = ["date", "open", "close", "high", "low", "volume"]
    merged[required].to_csv(csv_path, index=False, encoding="utf-8")

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
        _save_meta(meta)
    except Exception:
        pass

    return merged


def _load_meta() -> dict:
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_meta(meta: dict):
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 股票名称缓存（全量，避免东方财富接口被代理拦截）────────
def _load_name_cache() -> dict:
    """从本地文件加载缓存"""
    if NAME_CACHE_FILE.exists():
        try:
            return json.loads(NAME_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
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
                    prefix + code, "date,open,high,low,close,volume",
                    start_date=start, end_date=end,
                    frequency="d", adjustflag="2")   # 2=前复权，与腾讯源口径一致
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
            finally:
                bs.logout()
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
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
    except Exception:
        pass
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

        # 临时清除代理环境变量（akshare 走 urllib3，直接受 env 影响）
        _tmp = {k: os.environ.pop(k) for k in list(_PROXY_KEYS) if k in os.environ}
        logger.info(f"下载 {code}({symbol}) {start}~{end} ...")
        src = "腾讯"
        try:
            try:
                # adjust="qfq" 前复权 —— 必须与 baostock(adjustflag=2)/优矿(AdjGet) 口径一致；
                # 不复权数据在除权日有价格断崖，回测会误触发止损、收益完全失真
                df = ak.stock_zh_a_hist_tx(symbol=symbol, start_date=start, end_date=end,
                                           adjust="qfq")
            except Exception as _te:
                logger.info(f"[{code}] 腾讯源失败({_te})，降级 baostock")
                df = None
            # 降级链：腾讯 → baostock → 优矿
            if df is None or df.empty:
                src = "baostock"
                df  = _fetch_kline_baostock(code, start_date, datetime.today().strftime("%Y-%m-%d"))
            if df is None or df.empty:
                src = "uqer"
                df  = _fetch_kline_uqer(code, start_date, datetime.today().strftime("%Y-%m-%d"))
        finally:
            os.environ.update(_tmp)   # 还原，避免影响其他请求（如 AI 接口）

        if df is None or df.empty:
            return {"ok": False, "code": code, "msg": "腾讯/baostock/优矿 三源均无数据"}
        if src != "腾讯":
            logger.info(f"[{code}] 已用降级数据源: {src}")

        df = df.rename(columns={"amount": "volume"})

        required = ["date", "open", "close", "high", "low", "volume"]
        missing  = [c for c in required if c not in df.columns]
        if missing:
            return {"ok": False, "code": code,
                    "msg": f"缺少列: {missing}，实际: {list(df.columns)}"}

        df = _clean_kline(df)
        if df is None or df.empty:
            return {"ok": False, "code": code, "msg": "清洗后无有效数据"}

        csv_path = DATA_DIR / f"{code}.csv"
        df[required].to_csv(csv_path, index=False, encoding="utf-8")
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
def start_download(codes: list, start_date: str, task_id: str):
    _ensure_name_cache()          # 顺便触发名称缓存加载
    with _lock:
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
