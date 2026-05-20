# -*- coding: utf-8 -*-
"""
因子库管理器：AlphaLens 风格内置因子策略

核心优化（全市场，保证覆盖率）：
  ① 共享全市场行情缓存（5分钟 TTL）：因子扫描与策略扫描共用，避免重复拉取
  ② 消除二次报价请求：_full_market_universe_with_quotes 一次返回代码+行情
  ③ 多连接并行 K 线：建立 N 个独立 TDX 连接（无锁），真正并行拉取 K 线
     - 5000 只全量 → 预筛后 ~600 只 → 并行 K 线 ~10-15 秒（vs 串行 ~30 秒）
"""

import json
import ast
import importlib.util
import traceback
import logging
import threading
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)

FACTORS_DIR = Path(__file__).parent.parent / "factors"
FACTORS_DIR.mkdir(exist_ok=True)

# 因子分类 → 颜色
CATEGORY_COLOR = {
    "动量": "#8250df",
    "反转": "#bd561d",
    "波动": "#1f6feb",
    "量价": "#238636",
    "趋势": "#e3b341",
    "质量": "#1a7f64",
    "综合": "#8b949e",
}

FACTOR_TEMPLATE = '''\
# FACTOR: {"name": "新因子", "desc": "在这里填写因子描述", "category": "综合", "period": 20, "builtin": false}
"""
因子名称：新因子
因子逻辑：描述你的选股逻辑

接口规范：
    codes     : 股票代码列表
    get_kline : get_kline(code, days) -> DataFrame  列: date,open,close,high,low,volume
    get_quotes: get_quotes(codes)    -> DataFrame  列: 代码,名称,最新价,涨跌幅,成交量

返回列表，每项：{"code","name","price","change","score","reason"}
"""


def scan(codes: list, get_kline, get_quotes) -> list:
    results = []
    quotes_df = get_quotes(codes)          # 建议整批获取，性能更好

    for code in codes:
        try:
            df = get_kline(code, 60)
            if df.empty or len(df) < 22:
                continue

            close  = df["close"]
            volume = df["volume"]

            # ── 在这里写你的因子逻辑 ──────────────────────
            ma5  = close.rolling(5).mean()
            ma20 = close.rolling(20).mean()

            if ma5.iloc[-1] <= ma20.iloc[-1]:
                continue

            score = round(float(ma5.iloc[-1] / ma20.iloc[-1] - 1) * 100, 2)
            # ─────────────────────────────────────────────

            row   = quotes_df[quotes_df["代码"] == code] if not quotes_df.empty else None
            name  = row.iloc[0]["名称"]  if row is not None and not row.empty else ""
            price = float(row.iloc[0]["最新价"]) if row is not None and not row.empty else float(close.iloc[-1])
            chg   = float(row.iloc[0]["涨跌幅"]) if row is not None and not row.empty else 0

            results.append({
                "code":   code,
                "name":   name,
                "price":  round(price, 2),
                "change": round(chg, 2),
                "score":  score,
                "reason": f"MA5/MA20 偏离 {score:.2f}%",
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x["score"], reverse=True)[:50]
'''


# ══════════════════════════════════════════════════════════
# 因子元数据
# ══════════════════════════════════════════════════════════
def _read_meta(path: Path) -> dict:
    meta = {
        "name":     path.stem,
        "desc":     "",
        "category": "综合",
        "period":   20,
        "builtin":  True,
    }
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline().strip()
            if first.startswith("# FACTOR:"):
                meta.update(json.loads(first[9:].strip()))
    except Exception:
        pass
    return meta


def list_factors() -> list:
    result = []
    for f in sorted(FACTORS_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        meta = _read_meta(f)
        result.append({
            "file":  f.stem,
            "color": CATEGORY_COLOR.get(meta.get("category", "综合"), "#8b949e"),
            **meta,
        })
    return result


def get_code(filename: str) -> str:
    path = FACTORS_DIR / f"{filename}.py"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def save(filename: str, code: str) -> dict:
    safe = "".join(c for c in filename if c.isalnum() or c in "_-")
    if not safe:
        return {"ok": False, "msg": "文件名不合法"}
    # 复用 strategy_manager 的 AST 安全检查
    try:
        import strategy_manager as _sm
        chk = _sm.ast_check(code)
        if not chk["ok"]:
            return {"ok": False, "msg": f"代码安全检查未通过: {chk['msg']}"}
    except Exception:
        pass
    path = FACTORS_DIR / f"{safe}.py"
    path.write_text(code, encoding="utf-8")
    meta = _read_meta(path)
    return {"ok": True, "file": safe, "meta": meta}


def delete(filename: str) -> dict:
    path = FACTORS_DIR / f"{filename}.py"
    meta = _read_meta(path) if path.exists() else {}
    if meta.get("builtin", True):
        return {"ok": False, "msg": "内置因子不可删除"}
    if path.exists():
        path.unlink()
        return {"ok": True}
    return {"ok": False, "msg": "文件不存在"}


def new_factor_template() -> str:
    return FACTOR_TEMPLATE


# ══════════════════════════════════════════════════════════
# 多连接并行 K 线拉取
# ══════════════════════════════════════════════════════════
_KLINE_POOL_LOCK = threading.Lock()
_kline_pool: list = []   # 独立的 TdxHq_API 实例列表


def _init_kline_pool(n: int = 4) -> list:
    """
    建立 n 个独立 TDX 连接组成 K 线连接池。
    每个连接分配给一个线程，不共享 → 无锁竞争 → 真正并行。
    """
    global _kline_pool
    with _KLINE_POOL_LOCK:
        # 检查现有连接是否有效
        valid = []
        for api in _kline_pool:
            try:
                # 简单 ping：尝试获取 1 条数据
                r = api.get_security_bars(9, 1, "000001", 0, 1)
                if r is not None:
                    valid.append(api)
            except Exception:
                pass
        if len(valid) >= n:
            _kline_pool = valid
            return valid

        # 建立新连接
        try:
            from pytdx.hq import TdxHq_API
            from ths_quote import _SERVERS
        except ImportError:
            logger.warning("[FM] pytdx 未安装，无法建立K线连接池")
            return []

        new_pool = list(valid)
        for host, port in _SERVERS:
            if len(new_pool) >= n:
                break
            try:
                api = TdxHq_API(raise_exception=False)
                if api.connect(host, port):
                    new_pool.append(api)
                    logger.info(f"[FM] K线连接池: +1 ({host}:{port})，共 {len(new_pool)} 个")
            except Exception as e:
                logger.debug(f"[FM] 连接失败 {host}:{port}: {e}")

        _kline_pool = new_pool
        logger.info(f"[FM] K线连接池初始化完成: {len(new_pool)}/{n} 个连接")
        return new_pool


def _fetch_kline_one(args) -> tuple:
    """
    单只 K 线拉取（供 ThreadPoolExecutor 调用）。
    args = (code, days, api_obj)
    每个线程有专属 api_obj，无锁竞争。
    """
    code, days, api = args
    from ths_quote import _market
    try:
        market = _market(code)
        bars = api.get_security_bars(9, market, code, 0, min(days, 800))
        if not bars:
            return code, pd.DataFrame()
        rows = []
        for b in bars:
            if not b:
                continue
            rows.append({
                "date":   str(b.get("datetime", ""))[:10],
                "open":   round(float(b.get("open",  0) or 0), 2),
                "close":  round(float(b.get("close", 0) or 0), 2),
                "high":   round(float(b.get("high",  0) or 0), 2),
                "low":    round(float(b.get("low",   0) or 0), 2),
                "volume": int(b.get("vol", 0) or 0),
            })
        if not rows:
            return code, pd.DataFrame()
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        return code, df.sort_values("date").reset_index(drop=True)
    except Exception as e:
        logger.debug(f"[FM] K线失败 {code}: {e}")
        return code, pd.DataFrame()


def _prefetch_klines_parallel(universe: list, fallback_get_kline,
                               days: int = 60, n_workers: int = 4) -> dict:
    """
    并行预取全量 K 线（核心加速）：

    优先路径（快）：本地 CSV → data_manager.get_local()，多线程真正并行
    次级路径（中）：TDX 连接池（每线程独占一个连接，无锁）
    兜底路径（慢）：单一 TDX 客户端（串行，有锁）

    性能对比（600只）：
      串行 TDX：600 × 50ms = 30s
      4连接并行：600/4 × 50ms ≈ 7-10s（含调度开销）
    """
    pool = _init_kline_pool(n_workers)
    n_actual = len(pool)

    cache: dict = {}

    # ── 第一步：从本地 CSV 快速读取（真正并行，无网络延迟）────────────
    try:
        import data_manager as _dm
        local_miss = []
        def _read_local(code):
            try:
                df = _dm.get_local(code)
                if df is not None and not df.empty and len(df) >= 10:
                    return code, df.tail(days).reset_index(drop=True)
            except Exception:
                pass
            return code, None

        with ThreadPoolExecutor(max_workers=min(16, len(universe))) as ex:
            for code, df in ex.map(_read_local, universe):
                if df is not None:
                    cache[code] = df
                else:
                    local_miss.append(code)

        if cache:
            logger.info(f"[FM] 本地CSV命中: {len(cache)}/{len(universe)} 只")
    except Exception:
        local_miss = list(universe)

    if not local_miss:
        return cache

    # ── 第二步：TDX 连接池并行拉取（剩余未命中的）─────────────────
    if n_actual > 0:
        logger.info(f"[FM] TDX并行K线: {len(local_miss)} 只，{n_actual}连接...")

        # 轮询分配连接
        def _make_args(i, code):
            return code, days, pool[i % n_actual]

        args_list = [_make_args(i, code) for i, code in enumerate(local_miss)]

        with ThreadPoolExecutor(max_workers=n_actual) as ex:
            futures = [ex.submit(_fetch_kline_one, a) for a in args_list]
            for fut in as_completed(futures):
                try:
                    code, df = fut.result()
                    cache[code] = df
                except Exception:
                    pass

        hit_tdx = sum(1 for c in local_miss if not cache.get(c, pd.DataFrame()).empty)
        logger.info(f"[FM] TDX K线完成: {hit_tdx}/{len(local_miss)} 只有效")
    else:
        # ── 兜底：串行单客户端（连接池创建失败时）──────────────────
        logger.warning("[FM] 连接池不可用，退化为串行K线拉取")
        for code in local_miss:
            try:
                cache[code] = fallback_get_kline(code, days)
            except Exception:
                cache[code] = pd.DataFrame()

    filled = sum(1 for df in cache.values() if not df.empty)
    logger.info(f"[FM] K线预取总计: {filled}/{len(universe)} 只有效")
    return cache


# ══════════════════════════════════════════════════════════
# 行情缓存工具
# ══════════════════════════════════════════════════════════
def _make_cached_get_quotes(quote_cache: dict, fallback_fn):
    """
    返回一个 get_quotes(codes) -> DataFrame 函数。
    优先从 quote_cache 命中（O(1)），未命中才调 fallback_fn。
    """
    def _get_quotes_cached(codes_list: list) -> pd.DataFrame:
        if not codes_list:
            return pd.DataFrame()
        rows = []
        miss = []
        for c in codes_list:
            if c in quote_cache:
                q = quote_cache[c]
                rows.append({
                    "代码":  q["code"],
                    "名称":  q.get("name", ""),
                    "最新价": q.get("price", 0),
                    "涨跌幅": q.get("change_pct", 0),
                    "昨收":  q.get("last_close", 0),
                    "成交量": q.get("vol", 0),
                    "量比":  1.0,
                })
            else:
                miss.append(c)
        if miss:
            try:
                df_miss = fallback_fn(miss)
                if not df_miss.empty:
                    return pd.concat([pd.DataFrame(rows), df_miss], ignore_index=True)
            except Exception:
                pass
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    return _get_quotes_cached


# ══════════════════════════════════════════════════════════
# 因子执行入口
# ══════════════════════════════════════════════════════════
def run(filename: str, codes: list) -> dict:
    """
    执行因子选股（全市场覆盖 + 三重加速）。

    全市场模式流程：
      1. 共享行情缓存（5分钟TTL）→ 5000+只一次批量，命中缓存几乎零耗时
      2. 行情过滤 → 保留价格>3元、有成交量的活跃标的（~600只）
      3. 多连接并行 K 线 → 4个独立TDX连接，真正无锁并行，时间从30s→8s
      4. 因子扫描 → get_kline/get_quotes 均从缓存命中，近零请求

    指定codes模式：
      codes 非空 → 直接在给定股票池运行（< 5秒）
    """
    path = FACTORS_DIR / f"{filename}.py"
    if not path.exists():
        return {"ok": False, "msg": f"因子文件不存在: {filename}", "results": []}

    try:
        spec = importlib.util.spec_from_file_location("_factor_run", path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, "scan"):
            return {"ok": False, "msg": "因子文件缺少 scan() 函数", "results": []}

        import strategy_manager as sm

        # ── 确定扫描宇宙 + 行情缓存 ────────────────────────────────
        if not codes:
            # 全市场：_full_market_universe_with_quotes 返回代码+行情，
            # 复用5分钟行情缓存，避免二次全市场扫描
            universe, _quote_cache = sm._full_market_universe_with_quotes(max_codes=600)
            if not universe:
                try:
                    from config import STOCK_POOL
                    universe = STOCK_POOL
                    _quote_cache = {}
                except Exception:
                    pass
            if not universe:
                return {"ok": False, "msg": "无法获取全市场行情（TDX 未连接，akshare 也无法访问），请检查网络或手动指定股票池", "results": []}
        else:
            # 指定股票池：直接批量获取行情
            universe = codes
            _quote_cache = {}
            try:
                import ths_quote as _tq
                client = _tq.get_client()
                if client and client._ok:
                    qs = client.get_quotes(universe)
                    _quote_cache = {q["code"]: q for q in (qs or [])}
            except Exception as e:
                logger.warning(f"[FM] 指定股票池行情获取失败: {e}")

        logger.info(f"[FM] 因子 [{filename}] 开始扫描，宇宙={len(universe)} 只")

        # ── 行情预筛：过滤停牌和超低价（减少无效 K 线请求）──────────
        if _quote_cache:
            before = len(universe)
            universe = [c for c in universe if (
                _quote_cache.get(c, {}).get("price", 0) >= 2 and
                _quote_cache.get(c, {}).get("vol", 0) > 0 and
                _quote_cache.get(c, {}).get("last_close", 0) > 0
            )]
            if before != len(universe):
                logger.info(f"[FM] 行情预筛: {before} → {len(universe)} 只")

        # ── 构建 get_quotes（缓存版） ─────────────────────────────
        _, _raw_get_quotes = sm._build_data_fns(universe)
        get_quotes = _make_cached_get_quotes(_quote_cache, _raw_get_quotes)

        # ── 并行预取 K 线 ─────────────────────────────────────────
        meta      = _read_meta(path)
        kline_days = max(int(meta.get("period", 20)) * 3 + 10, 60)

        # 构建单客户端兜底的 get_kline
        _single_get_kline, _ = sm._build_data_fns(universe)

        _kline_cache = _prefetch_klines_parallel(
            universe,
            fallback_get_kline=_single_get_kline,
            days=kline_days,
            n_workers=4,
        )

        # 缓存版 get_kline（因子调用时直接命中，零网络请求）
        def get_kline_cached(code: str, days: int = 60) -> pd.DataFrame:
            if code in _kline_cache:
                df = _kline_cache[code]
                return df.tail(days).reset_index(drop=True) if not df.empty else df
            return _single_get_kline(code, days)

        # ── 执行因子扫描 ──────────────────────────────────────────
        results = mod.scan(universe, get_kline_cached, get_quotes)

        logger.info(f"[FM] 因子 [{filename}] 完成，命中 {len(results)} 只")
        return {
            "ok":      True,
            "results": results,
            "count":   len(results),
            "scanned": len(universe),
        }

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"因子执行出错: {e}\n{tb}")
        return {"ok": False, "msg": str(e), "traceback": tb, "results": []}
