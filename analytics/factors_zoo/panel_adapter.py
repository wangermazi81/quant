# -*- coding: utf-8 -*-
"""单股 K 线 → wide panel 适配层

把项目里现有的「逐只查 K 线」（factor_manager.scan 用的 get_kline(code, days)）
转换成 见微 Alpha 因子需要的 wide 面板格式：

    panel = {
        "open":   DataFrame(index=DatetimeIndex, columns=[code1, code2, ...]),
        "high":   ...,
        "low":    ...,
        "close":  ...,
        "volume": ...,
    }

设计：
  - 单股 DataFrame 列名兼容你 ths_quote / akshare 的中英文混用
  - 缺失列填 NaN（base.py 的算子全部 NaN-propagating，安全）
  - 日期对齐用 outer join（不同股票停牌日不同），缺失自动 NaN
  - 不缓存，调用方自己管 TTL（与 factor_manager 现有缓存解耦）
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 单股 DataFrame 可能的列名 → 标准列名
_COL_ALIASES = {
    "open": ["open", "Open", "开盘", "开盘价"],
    "high": ["high", "High", "最高", "最高价"],
    "low": ["low", "Low", "最低", "最低价"],
    "close": ["close", "Close", "收盘", "收盘价"],
    "volume": ["volume", "Volume", "vol", "成交量"],
    "amount": ["amount", "Amount", "成交额", "金额"],
    "vwap": ["vwap", "Vwap", "VWAP"],
}


def _pick_col(df: pd.DataFrame, canonical: str) -> pd.Series | None:
    """从单股 DataFrame 里挑出某个标准列（容忍中英文别名）"""
    for alias in _COL_ALIASES.get(canonical, [canonical]):
        if alias in df.columns:
            return df[alias]
    return None


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    """保证 index 是 DatetimeIndex"""
    if isinstance(df.index, pd.DatetimeIndex):
        return df
    for col in ("date", "Date", "trade_date", "日期", "datetime"):
        if col in df.columns:
            out = df.copy()
            out.index = pd.to_datetime(out[col])
            out.index.name = "date"
            return out
    # 默认拿第一列试
    try:
        out = df.copy()
        out.index = pd.to_datetime(df.iloc[:, 0])
        return out
    except Exception:
        return df  # 退路：原样返回，pandas 后面会报


def _load_one_stock(
    code: str,
    get_kline: Callable[[str, int], pd.DataFrame],
    days: int,
    needed: Sequence[str],
) -> tuple[str, dict[str, pd.Series] | None]:
    """单只股票的 K 线 → {canonical_col: Series}；失败返回 (code, None)"""
    try:
        df = get_kline(code, days)
    except Exception as exc:
        logger.debug(f"build_panel: {code} get_kline 抛错 {exc}")
        return code, None
    if df is None or len(df) == 0:
        return code, None
    df = _normalize_index(df)
    if not isinstance(df.index, pd.DatetimeIndex):
        return code, None
    df = df[~df.index.duplicated(keep="last")].sort_index()

    cols: dict[str, pd.Series] = {}
    for canon in needed:
        s = _pick_col(df, canon)
        if s is not None:
            cols[canon] = s.astype(np.float64)
    return code, (cols if cols else None)


def _persist_kline_to_cache(code: str, df: pd.DataFrame) -> None:
    """把 TDX 拉到的 K 线写到 data/daily/<code>.csv（merge 已有缓存）

    下次再算同一只股票就能命中本地，不必再走 TDX/akshare。
    失败静默，不影响主流程。
    """
    try:
        from pathlib import Path as _Path
        proj_root = _Path(__file__).resolve().parent.parent.parent
        cache_dir = proj_root / "data" / "daily"
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{code}.csv"

        if df is None or len(df) == 0:
            return

        # 标准化：拿 date / open / close / high / low / volume 列
        out = df.copy()
        if not isinstance(out.index, pd.DatetimeIndex):
            for col in ("date", "Date", "trade_date", "日期"):
                if col in out.columns:
                    out["date"] = pd.to_datetime(out[col])
                    break
        else:
            out = out.reset_index().rename(columns={"index": "date"})

        if "date" not in out.columns:
            return

        # 选标准列
        std_cols = {}
        for canon in ("open", "high", "low", "close", "volume"):
            s = _pick_col(out, canon)
            if s is not None:
                std_cols[canon] = s.values
        if not std_cols:
            return

        new_df = pd.DataFrame({"date": pd.to_datetime(out["date"]).values, **std_cols})

        # 与已有缓存合并：date 为主键，新数据覆盖旧
        if path.exists():
            try:
                old = pd.read_csv(path)
                old["date"] = pd.to_datetime(old["date"])
                merged = pd.concat([old, new_df], ignore_index=True)
                merged = merged.drop_duplicates(subset=["date"], keep="last")
                merged = merged.sort_values("date").reset_index(drop=True)
            except Exception:
                merged = new_df.sort_values("date").reset_index(drop=True)
        else:
            merged = new_df.sort_values("date").reset_index(drop=True)

        merged.to_csv(path, index=False)
    except Exception as exc:
        logger.debug(f"写 K 线缓存失败 {code}: {exc}")


def _try_prefetch_via_factor_manager(
    codes: Sequence[str],
    days: int,
    fallback_get_kline: Callable[[str, int], pd.DataFrame],
    n_workers: int = 4,
) -> dict[str, pd.DataFrame] | None:
    """软依赖：尝试用 factor_manager 的多连接 TDX K 线池预取
    成功返回 {code: DataFrame}；模块不可用或异常返回 None（让调用方降级）

    factor_manager._prefetch_klines_parallel 行为：
      1. 先并行读本地 data/daily/<code>.csv 缓存
      2. 未命中的走 N 个独立 TdxHq_API 连接并行拉
      3. 实在不行降级到 fallback_get_kline 串行
    覆盖率从 ~2%（仅本地缓存）跃升到 80%+（接通 TDX 池）
    """
    # 确保项目根在 sys.path（factor_manager._init_kline_pool 内部会
    # `from ths_quote import _SERVERS`，独立脚本运行时这行会失败）
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _proj_root = _Path(__file__).resolve().parent.parent.parent
        if str(_proj_root) not in _sys.path:
            _sys.path.insert(0, str(_proj_root))
        # ths_quote.py 在 market/ 子包里，需要把 market/ 也加进去
        _market_dir = _proj_root / "market"
        if _market_dir.exists() and str(_market_dir) not in _sys.path:
            _sys.path.insert(0, str(_market_dir))
    except Exception as exc:
        logger.debug(f"sys.path 准备失败: {exc}")

    try:
        from analytics import factor_manager as _fm
    except Exception as exc:
        logger.debug(f"factor_manager 不可用，降级: {exc}")
        return None
    fn = getattr(_fm, "_prefetch_klines_parallel", None)
    if fn is None:
        return None
    try:
        cache = fn(list(codes), fallback_get_kline, days=days, n_workers=n_workers)
        if not isinstance(cache, dict):
            return None
        return cache
    except Exception as exc:
        logger.warning(f"factor_manager._prefetch_klines_parallel 抛错，降级: {exc}")
        return None


def build_panel(
    codes: Sequence[str],
    get_kline: Callable[[str, int], pd.DataFrame],
    days: int = 60,
    needed: Sequence[str] = ("open", "high", "low", "close", "volume"),
    workers: int = 8,
    use_pool: bool = True,
) -> dict[str, pd.DataFrame]:
    """对每只股票调一次 get_kline，拼成 wide panel（并发版）。

    Args:
        codes:  股票代码列表
        get_kline:  fallback K 线源 get_kline(code, days) -> DataFrame
        days:   每只股票回看天数（要 ≥ 因子的 min_warmup_bars）
        needed: 要构建的标准列。默认 OHLCV。
        workers: 并发线程数（仅当 use_pool=False 或 pool 不可用时生效）
                 1=串行（兼容旧行为）；>=2 ThreadPool 并发拉 get_kline
        use_pool: True（默认）—— 优先尝试 factor_manager 的多连接 TDX 池
                  （本地 CSV → TDX 池并行 → fallback），命中率 ~80%+
                  False —— 直接走 get_kline + workers 并发
                  调用方一般保持 True；测试 fallback 路径时设 False

    Returns:
        dict[str, DataFrame]，每个 DataFrame 是 wide 格式
    """
    codes = list(codes)
    if not codes:
        return {}

    per_stock: dict[str, dict[str, pd.Series]] = {}

    # ── 路径 1：优先 factor_manager K 线池（命中率高）──
    if use_pool:
        kline_map = _try_prefetch_via_factor_manager(codes, days, get_kline, n_workers=4)
        if kline_map is not None:
            for code, df in kline_map.items():
                if df is None or len(df) == 0:
                    continue
                # 写本地 CSV 缓存（下次直接命中，避免重复拉 TDX/akshare）
                # 用 reset_index 前的原始 df（保留 date 列）
                try:
                    _persist_kline_to_cache(code, df)
                except Exception:
                    pass
                df = _normalize_index(df)
                if not isinstance(df.index, pd.DatetimeIndex):
                    continue
                df = df[~df.index.duplicated(keep="last")].sort_index()
                cols: dict[str, pd.Series] = {}
                for canon in needed:
                    s = _pick_col(df, canon)
                    if s is not None:
                        cols[canon] = s.astype(np.float64)
                if cols:
                    per_stock[code] = cols
            # 池路径走完直接拼面板返回（即使部分股票拉不到，不重试 fallback）
            return _assemble_panel(per_stock, needed)

    # ── 路径 2：fallback —— 直接调 get_kline ──
    if workers <= 1 or len(codes) <= 2:
        for code in codes:
            _, cols = _load_one_stock(code, get_kline, days, needed)
            if cols:
                per_stock[code] = cols
    else:
        n = min(workers, len(codes))
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = {ex.submit(_load_one_stock, c, get_kline, days, needed): c for c in codes}
            for fut in as_completed(futures):
                try:
                    code, cols = fut.result()
                except Exception as exc:
                    logger.debug(f"build_panel: worker 抛错 {exc}")
                    continue
                if cols:
                    per_stock[code] = cols

    return _assemble_panel(per_stock, needed)


def _assemble_panel(per_stock: dict, needed: Sequence[str]) -> dict[str, pd.DataFrame]:
    """{code: {canon: Series}} → {canon: DataFrame(dates × codes)}"""
    if not per_stock:
        return {}
    panel: dict[str, pd.DataFrame] = {}
    for canon in needed:
        series_map = {code: cols[canon] for code, cols in per_stock.items() if canon in cols}
        if not series_map:
            continue
        wide = pd.DataFrame(series_map)
        wide = wide.sort_index()
        panel[canon] = wide
    return panel


def latest_score(factor_df: pd.DataFrame, mode: str = "per_column") -> dict[str, float]:
    """从因子 DataFrame 取每只股票最新分数

    Args:
        factor_df: registry.compute 的输出（dates × codes）
        mode:
          - "per_column"（默认）：每只股票各自的最后一个非 NaN 值
            适合不同股票数据日期不齐的场景（停牌/新股/缓存不一致）
          - "last_row"：严格取最后一行的值（兼容旧接口，可能很多 NaN）

    返回:
        {code: score | None}，None 表示该股票无可用分数
    """
    if factor_df is None or factor_df.empty:
        return {}
    out: dict[str, float | None] = {}
    if mode == "last_row":
        last = factor_df.iloc[-1]
        for code, val in last.items():
            out[str(code)] = None if pd.isna(val) else float(val)
        return out
    # per_column 模式：每列 ffill 后取末值，再过 NaN → None
    for code in factor_df.columns:
        col = factor_df[code].dropna()
        if col.empty:
            out[str(code)] = None
        else:
            out[str(code)] = float(col.iloc[-1])
    return out
