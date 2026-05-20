# -*- coding: utf-8 -*-
"""策略管理器：加载、保存、执行策略文件"""

import json
import ast
import importlib.util
import traceback
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

STRATEGIES_DIR = Path(__file__).parent.parent / "strategies"
STRATEGIES_DIR.mkdir(exist_ok=True)

TEMPLATE = '''\
# META: {"name": "自定义策略", "desc": "在这里填写策略描述", "author": "用户", "builtin": false}

def scan(codes: list, get_kline, get_quotes) -> list:
    """
    策略入口函数（必须实现）

    参数：
        codes     : 股票代码列表，如 ["000001", "600519"]
        get_kline : 函数，get_kline(code, days=60) -> DataFrame
                    列名: date, open, close, high, low, volume
        get_quotes: 函数，get_quotes(codes) -> DataFrame
                    列名: 代码, 名称, 最新价, 涨跌幅, 成交量, 换手率

    返回：
        list of dict，每项包含：
            code   : 股票代码
            name   : 股票名称
            price  : 现价
            change : 涨跌幅(%)
            score  : 评分（越高越好）
            reason : 选股理由
    """
    quotes_df = get_quotes(codes)
    results = []

    for code in codes:
        try:
            df = get_kline(code, 60)
            if df.empty or len(df) < 22:
                continue

            close  = df['close']
            volume = df['volume']

            # ── 在此编写你的选股逻辑 ────────────────────────────
            ma5  = close.rolling(5).mean()
            ma20 = close.rolling(20).mean()

            if not (ma5.iloc[-1] > ma20.iloc[-1]):
                continue

            avg_vol   = volume.iloc[-21:-1].mean()
            vol_ratio = volume.iloc[-1] / avg_vol if avg_vol > 0 else 0
            if vol_ratio < 1.5:
                continue
            # ────────────────────────────────────────────────────

            row   = quotes_df[quotes_df['代码'] == code]
            name  = row.iloc[0]['名称'] if not row.empty else ''
            price = float(row.iloc[0]['最新价']) if not row.empty else float(close.iloc[-1])
            chg   = float(row.iloc[0]['涨跌幅']) if not row.empty else 0

            results.append({
                "code":   code,
                "name":   name,
                "price":  round(price, 2),
                "change": round(chg, 2),
                "score":  round(vol_ratio * 10, 1),
                "reason": f"MA5>{ma20.iloc[-1]:.2f}, 量比{vol_ratio:.1f}x"
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x['score'], reverse=True)
'''


def _read_meta(path: Path) -> dict:
    meta = {"name": path.stem, "desc": "", "author": "用户", "builtin": False}
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline().strip()
            if first.startswith("# META:"):
                meta.update(json.loads(first[7:].strip()))
    except Exception:
        pass
    return meta


def list_strategies() -> list:
    result = []
    for f in sorted(STRATEGIES_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        meta = _read_meta(f)
        result.append({"file": f.stem, **meta})
    return result


def get_code(filename: str) -> str:
    path = STRATEGIES_DIR / f"{filename}.py"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return TEMPLATE


_AST_BANNED_IMPORTS = {"os", "sys", "subprocess", "shutil", "socket", "requests",
                        "urllib", "http", "ftplib", "smtplib", "paramiko"}
_AST_BANNED_CALLS   = {"eval", "exec", "open", "compile", "__import__",
                        "breakpoint", "input", "getattr", "setattr", "delattr"}


def ast_check(code: str) -> dict:
    """
    对用户提交的策略/因子代码进行 AST 安全扫描。
    返回 {"ok": True} 或 {"ok": False, "msg": "..."}
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {"ok": False, "msg": f"语法错误: {e}"}

    for node in ast.walk(tree):
        # 检查 import xxx / from xxx import
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = ""
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name.split(".")[0]
                    if module in _AST_BANNED_IMPORTS:
                        return {"ok": False, "msg": f"禁止导入模块: {module}"}
            else:
                module = (node.module or "").split(".")[0]
                if module in _AST_BANNED_IMPORTS:
                    return {"ok": False, "msg": f"禁止导入模块: {module}"}

        # 检查函数调用
        if isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in _AST_BANNED_CALLS:
                return {"ok": False, "msg": f"禁止调用函数: {name}()"}

    return {"ok": True}


def save(filename: str, code: str) -> dict:
    safe = "".join(c for c in filename if c.isalnum() or c in "_-")
    if not safe:
        return {"ok": False, "msg": "文件名不合法"}
    # AST 安全检查
    chk = ast_check(code)
    if not chk["ok"]:
        return {"ok": False, "msg": f"代码安全检查未通过: {chk['msg']}"}
    path = STRATEGIES_DIR / f"{safe}.py"
    path.write_text(code, encoding="utf-8")
    return {"ok": True, "file": safe}


def delete(filename: str) -> dict:
    path = STRATEGIES_DIR / f"{filename}.py"
    meta = _read_meta(path) if path.exists() else {}
    if meta.get("builtin"):
        return {"ok": False, "msg": "内置策略不可删除"}
    if path.exists():
        path.unlink()
        return {"ok": True}
    return {"ok": False, "msg": "文件不存在"}


def rename(old_filename: str, new_filename: str) -> dict:
    """重命名策略文件（不可重命名内置策略）"""
    old_path = STRATEGIES_DIR / f"{old_filename}.py"
    if not old_path.exists():
        return {"ok": False, "msg": "原文件不存在"}
    meta = _read_meta(old_path)
    if meta.get("builtin"):
        return {"ok": False, "msg": "内置策略不可重命名"}
    safe = "".join(c for c in new_filename if c.isalnum() or c in "_-")
    if not safe:
        return {"ok": False, "msg": "新名称不合法"}
    new_path = STRATEGIES_DIR / f"{safe}.py"
    if new_path.exists():
        return {"ok": False, "msg": f"名称 '{safe}' 已存在"}
    old_path.rename(new_path)
    return {"ok": True, "file": safe}


def _build_data_fns(universe: list):
    """
    构建策略数据访问函数（TDX优先，支持全市场）。
    universe: 已预筛选的股票代码列表
    """
    import pandas as pd

    def _get_kline(code: str, days: int = 60) -> pd.DataFrame:
        # 1. 本地 CSV
        try:
            import data_manager as _dm
            df = _dm.get_local(code)
            if df is not None and not df.empty and len(df) >= 10:
                return df.tail(days).reset_index(drop=True)
        except Exception:
            pass
        # 2. TDX
        try:
            import ths_quote as _tq
            client = _tq.get_client()
            if client and client._ok:
                bars = client.get_kline(code, days)
                if bars:
                    df = pd.DataFrame(bars)
                    df["date"] = pd.to_datetime(df["date"])
                    return df.sort_values("date").reset_index(drop=True)
        except Exception:
            pass
        return pd.DataFrame()

    def _get_quotes(codes_list: list) -> pd.DataFrame:
        if not codes_list:
            return pd.DataFrame()
        try:
            import ths_quote as _tq
            client = _tq.get_client()
            if client and client._ok:
                qs = client.get_quotes(codes_list)
                if qs:
                    rows = [{
                        "代码":  q["code"],
                        "名称":  q.get("name", ""),
                        "最新价": q.get("price", 0),
                        "涨跌幅": q.get("change_pct", 0),
                        "昨收":  q.get("last_close", 0),
                        "成交量": q.get("vol", 0),
                        "量比":  1.0,
                    } for q in qs]
                    return pd.DataFrame(rows)
        except Exception:
            pass
        return pd.DataFrame()

    return _get_kline, _get_quotes


# ── 全市场行情共享缓存（5分钟TTL，因子库与策略库共用，避免重复扫描全市场）─
import time as _time
_mkt_cache = {
    "sorted_quotes": [],   # 全量行情，按成交量降序排列
    "ts": 0,
}
_MKT_CACHE_TTL = 300  # 5 分钟


def _refresh_market_cache() -> list:
    """
    刷新全市场行情缓存：
      1. 优先尝试 TDX 批量行情（~5000 只）
      2. TDX 不可用时，自动降级到 akshare（ak.stock_zh_a_spot_em）
      3. 过滤停牌/超低价，按成交量降序存入共享缓存
    返回：排序后的行情列表 [{code, price, vol, ...}, ...]
    """
    # ── 路径 1：TDX ────────────────────────────────────────────────
    try:
        import ths_quote as _tq
        client = _tq.get_client()
        if client and client._ok:
            all_codes = client.get_all_stock_codes()
            if all_codes:
                logger.info(f"[SM] 全市场宇宙：共 {len(all_codes)} 只，开始批量行情筛选...")
                quotes = client.get_quotes(all_codes)
                if quotes:
                    active = [q for q in quotes
                              if q.get("price", 0) > 3
                              and q.get("vol", 0) > 100
                              and q.get("last_close", 0) > 0]
                    active.sort(key=lambda q: q.get("vol", 0), reverse=True)
                    _mkt_cache["sorted_quotes"] = active
                    _mkt_cache["ts"] = _time.time()
                    logger.info(f"[SM] 全市场行情缓存刷新（TDX）：{len(active)} 只有效标的")
                    return active
    except Exception as e:
        logger.warning(f"[SM] TDX 全市场行情获取失败: {e}")

    # ── 路径 2：akshare 兜底 ────────────────────────────────────────
    try:
        import os as _os, akshare as ak
        # 清除代理，避免代理服务器拦截
        _proxy_bak = {k: _os.environ.pop(k, None)
                      for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                                "all_proxy", "ALL_PROXY")}
        logger.info("[SM] TDX 不可用，切换到 akshare 获取全市场行情（可能较慢）...")
        try:
            df = ak.stock_zh_a_spot_em()
        finally:
            # 还原代理设置
            for k, v in _proxy_bak.items():
                if v is not None:
                    _os.environ[k] = v
        if df is None or df.empty:
            raise ValueError("akshare 返回空数据")

        # akshare 列名映射 → 内部行情字典格式
        col_map = {
            "代码": "code",
            "名称": "name",
            "最新价": "price",
            "涨跌幅": "change_pct",
            "昨收": "last_close",
            "成交量": "vol",
            "量比": "volume_ratio",
        }
        keep = [c for c in col_map if c in df.columns]
        df2 = df[keep].rename(columns=col_map).copy()
        for col in ("price", "change_pct", "last_close", "vol", "volume_ratio"):
            if col in df2.columns:
                df2[col] = df2[col].apply(
                    lambda x: float(x) if x is not None and str(x).strip() not in ("", "-", "None") else 0.0
                )
        records = df2.to_dict("records")

        active = [q for q in records
                  if q.get("price", 0) > 3
                  and q.get("vol", 0) > 100
                  and q.get("last_close", 0) > 0]
        active.sort(key=lambda q: q.get("vol", 0), reverse=True)

        _mkt_cache["sorted_quotes"] = active
        _mkt_cache["ts"] = _time.time()
        logger.info(f"[SM] 全市场行情缓存刷新（akshare）：{len(active)} 只有效标的")
        return active

    except Exception as e:
        logger.warning(f"[SM] akshare 全市场行情获取失败: {e}")

    # ── 路径 3：baostock 兜底 ──────────────────────────────────────
    try:
        import baostock as bs
        import pandas as pd
        logger.info("[SM] 切换到 baostock 获取全市场行情...")
        lg = bs.login()
        if lg.error_code != '0':
            raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
        try:
            rs = bs.query_all_stock()
            if rs.error_code != '0':
                raise RuntimeError(f"baostock query 失败: {rs.error_msg}")
            rows = []
            while rs.next():
                row = rs.get_row_data()
                rows.append(row)
        finally:
            bs.logout()
        if not rows:
            raise ValueError("baostock 返回空数据")
        # baostock 返回的是代码列表，无实时行情，只能返回代码（无成交量排序）
        codes = []
        for row in rows:
            code_raw = row[0] if row else ""
            if code_raw.startswith("sz.") or code_raw.startswith("sh."):
                code = code_raw[3:]
                # 过滤指数、基金等
                if len(code) == 6 and code[0] in ("0", "3", "6"):
                    codes.append(code)
        if not codes:
            raise ValueError("baostock 未返回有效股票代码")
        # 构造轻量行情（无实时价，成交量为0，仍可用于代码宇宙）
        active = [{"code": c, "name": c, "price": 10.0, "vol": 1000,
                   "last_close": 10.0, "change_pct": 0.0} for c in codes]
        _mkt_cache["sorted_quotes"] = active
        _mkt_cache["ts"] = _time.time()
        logger.info(f"[SM] 全市场行情缓存刷新（baostock，仅代码列表）：{len(active)} 只")
        return active
    except ImportError:
        logger.debug("[SM] baostock 未安装，跳过")
    except Exception as e:
        logger.warning(f"[SM] baostock 全市场行情获取失败: {e}")

    # ── 路径 4：efinance 兜底 ──────────────────────────────────────
    try:
        import efinance as ef
        import pandas as pd
        logger.info("[SM] 切换到 efinance 获取全市场行情...")
        df = ef.stock.get_realtime_quotes()
        if df is None or df.empty:
            raise ValueError("efinance 返回空数据")
        col_map = {
            "股票代码": "code",
            "股票名称": "name",
            "最新价": "price",
            "涨跌幅": "change_pct",
            "昨日收盘": "last_close",
            "成交量": "vol",
        }
        keep = [c for c in col_map if c in df.columns]
        df2 = df[keep].rename(columns=col_map).copy()
        for col in ("price", "change_pct", "last_close", "vol"):
            if col in df2.columns:
                df2[col] = pd.to_numeric(df2[col], errors="coerce").fillna(0.0)
        records = df2.to_dict("records")
        active = [q for q in records
                  if q.get("price", 0) > 3
                  and q.get("vol", 0) > 100
                  and q.get("last_close", 0) > 0]
        active.sort(key=lambda q: q.get("vol", 0), reverse=True)
        _mkt_cache["sorted_quotes"] = active
        _mkt_cache["ts"] = _time.time()
        logger.info(f"[SM] 全市场行情缓存刷新（efinance）：{len(active)} 只有效标的")
        return active
    except ImportError:
        logger.debug("[SM] efinance 未安装，跳过")
    except Exception as e:
        logger.warning(f"[SM] efinance 全市场行情获取失败: {e}")

    logger.error("[SM] 全部数据源均不可用，无法获取全市场行情")
    return []


def _get_market_quotes_cached(force_refresh: bool = False) -> list:
    """
    获取全市场行情（带5分钟缓存）。
    返回按成交量降序排列的行情列表。
    """
    now = _time.time()
    if not force_refresh and _mkt_cache["sorted_quotes"] and (now - _mkt_cache["ts"]) < _MKT_CACHE_TTL:
        age = int(now - _mkt_cache["ts"])
        logger.info(f"[SM] 复用全市场行情缓存（{age}s前，{len(_mkt_cache['sorted_quotes'])}只）")
        return _mkt_cache["sorted_quotes"]
    return _refresh_market_cache()


def _full_market_universe(max_codes: int = 600, force_refresh: bool = False) -> list:
    """
    两阶段全市场选股宇宙（带5分钟缓存）：
      阶段1：TDX 批量行情获取所有 A 股实时数据（首次~60s，缓存命中<1s）
      阶段2：按成交量排序取前 max_codes 只
    返回：预筛选后的股票代码列表
    """
    active = _get_market_quotes_cached(force_refresh)
    if not active:
        return []
    result = [q["code"] for q in active[:max_codes]]
    logger.info(f"[SM] 预筛选后：{len(result)} 只进入策略扫描")
    return result


def _full_market_universe_with_quotes(max_codes: int = 600,
                                      force_refresh: bool = False) -> tuple:
    """
    同 _full_market_universe，但同时返回行情缓存字典。
    供因子库使用，避免二次批量行情请求。

    返回：(universe: list[str], quote_cache: dict[code -> quote_dict])
    """
    active = _get_market_quotes_cached(force_refresh)
    if not active:
        return [], {}
    top = active[:max_codes]
    universe = [q["code"] for q in top]
    quote_cache = {q["code"]: q for q in top}
    logger.info(f"[SM] 预筛选后：{len(universe)} 只（含行情缓存，因子免二次拉取）")
    return universe, quote_cache


def run(filename: str, codes: list) -> dict:
    """
    执行策略选股。
    - codes 为空 → 全市场扫描（两阶段：行情预筛选 → kline策略过滤）
    - codes 非空 → 指定股票池扫描
    """
    path = STRATEGIES_DIR / f"{filename}.py"
    if not path.exists():
        return {"ok": False, "msg": f"策略文件不存在: {filename}", "results": []}
    try:
        # AST 安全检查（内置策略豁免）
        meta = _read_meta(path)
        if not meta.get("builtin"):
            chk = ast_check(path.read_text(encoding="utf-8"))
            if not chk["ok"]:
                return {"ok": False, "msg": f"策略代码安全检查未通过: {chk['msg']}", "results": []}

        spec = importlib.util.spec_from_file_location("_strat_run", path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, "scan"):
            return {"ok": False, "msg": "策略文件缺少 scan() 函数", "results": []}

        # ── 确定扫描宇宙 ────────────────────────────────────────
        if not codes:
            # 全市场模式：构建预筛选宇宙（最活跃 600 只）
            universe = _full_market_universe(max_codes=600)
            if not universe:
                # TDX 不可用时降级到配置的股票池
                from config import STOCK_POOL
                universe = STOCK_POOL
                logger.warning("[SM] TDX不可用，降级到默认股票池")
        else:
            universe = codes

        get_kline, get_quotes = _build_data_fns(universe)
        # 打板策略传空列表触发其全市场TDX实时路径
        scan_codes = [] if filename == "da_ban" else universe
        results = mod.scan(scan_codes, get_kline, get_quotes)
        return {
            "ok":      True,
            "results": results,
            "count":   len(results),
            "scanned": len(universe),
        }
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"策略执行出错: {e}\n{tb}")
        return {"ok": False, "msg": str(e), "traceback": tb, "results": []}
