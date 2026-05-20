# -*- coding: utf-8 -*-
"""
本地历史数据管理
数据源：akshare stock_zh_a_hist_tx（腾讯财经）
股票名称：ak.stock_info_a_code_name() 全量缓存，不走东方财富
"""

import json, logging, threading, os
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


# ── 市场前缀 ──────────────────────────────────────
def _tx_symbol(code: str) -> str:
    if code.startswith("6"):
        return "sh" + code
    elif code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


# ── 元数据 ────────────────────────────────────────
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
    返回 {"industry": "银行", "concepts": ["大金融", "沪深300", ...]}
    尝试多个接口，失败则返回空。
    """
    result = {"industry": "", "concepts": []}
    # 1. 从本地 meta 读取已缓存的行业信息
    meta = _load_meta()
    cached = meta.get(code, {})
    if cached.get("industry"):
        result["industry"] = cached["industry"]
    if cached.get("concepts"):
        result["concepts"] = cached["concepts"]
        return result

    # 2. 尝试获取行业/概念（可能被代理拦截，吃掉异常）
    try:
        info = ak.stock_individual_info_em(symbol=code)
        if info is not None and not info.empty:
            ind_row = info[info["item"] == "行业"]
            if not ind_row.empty:
                result["industry"] = str(ind_row["value"].iloc[0])
    except Exception:
        pass

    # 3. 尝试获取概念板块
    try:
        df = ak.stock_board_concept_name_em()
        if df is not None and not df.empty:
            concepts = []
            for _, row in df.iterrows():
                board = str(row.get("板块名称", ""))
                try:
                    cons = ak.stock_board_concept_cons_em(symbol=board)
                    if cons is not None and not cons.empty:
                        if code in cons["代码"].astype(str).values:
                            concepts.append(board)
                        if len(concepts) >= 5:
                            break
                except Exception:
                    continue
            result["concepts"] = concepts[:5]
    except Exception:
        pass

    # 4. 写回 meta 缓存
    if result["industry"] or result["concepts"]:
        meta[code] = meta.get(code, {})
        meta[code].update(result)
        _save_meta(meta)

    return result


# ── 单只下载 ──────────────────────────────────────
def _no_proxy_get(url: str, **kwargs):
    """用不走代理的 session 发 GET，供 akshare monkey-patch 后备用"""
    return _ak_session.get(url, **kwargs)


def _download_one(code: str, start_date: str) -> dict:
    try:
        symbol = _tx_symbol(code)
        end    = datetime.today().strftime("%Y%m%d")
        start  = start_date.replace("-", "")

        # 临时清除代理环境变量（akshare 走 urllib3，直接受 env 影响）
        _tmp = {k: os.environ.pop(k) for k in list(_PROXY_KEYS) if k in os.environ}
        logger.info(f"下载 {code}({symbol}) {start}~{end} ...")
        try:
            df = ak.stock_zh_a_hist_tx(symbol=symbol, start_date=start, end_date=end)
        finally:
            os.environ.update(_tmp)   # 还原，避免影响其他请求（如 AI 接口）

        if df is None or df.empty:
            return {"ok": False, "code": code, "msg": "akshare 返回空数据"}

        df = df.rename(columns={"amount": "volume"})
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        required = ["date", "open", "close", "high", "low", "volume"]
        missing  = [c for c in required if c not in df.columns]
        if missing:
            return {"ok": False, "code": code,
                    "msg": f"缺少列: {missing}，实际: {list(df.columns)}"}

        csv_path = DATA_DIR / f"{code}.csv"
        df[required].to_csv(csv_path, index=False, encoding="utf-8")
        logger.info(f"已保存 {csv_path} ({len(df)} 行)")

        # 优先用缓存名称，无则写入 code
        name = get_stock_name(code) or code

        meta = _load_meta()
        meta[code] = {
            "name":    name,
            "start":   str(df["date"].iloc[0].date()),
            "end":     str(df["date"].iloc[-1].date()),
            "rows":    len(df),
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
    return df.sort_values("date").reset_index(drop=True)

def delete_stock(code: str) -> dict:
    p = DATA_DIR / f"{code}.csv"
    if p.exists():
        p.unlink()
    meta = _load_meta()
    meta.pop(code, None)
    _save_meta(meta)
    return {"ok": True}
