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


def get_meta(filename: str) -> dict:
    """读取策略 META（含 builtin 标记），供路由层判断是否对非 admin 隐藏代码。
    文件不存在返回默认（非内置）。"""
    path = STRATEGIES_DIR / f"{filename}.py"
    if path.exists():
        return _read_meta(path)
    return {"name": filename, "desc": "", "author": "用户", "builtin": False}


# ── 策略仓位规则（仓位规定写在策略 META 的 "position" 字段，下单链路据此执行）──
DEFAULT_POSITION_CONFIG = {
    "max_daily": 0,     # 每日最多开仓数；0 = 不限（沿用账户面板设置）
    "max_hold":  0,     # 最多同时持仓只数；0 = 沿用账户面板设置
    "sizing":    "equal",  # equal = 平均仓位（每仓 = 可用资金 / max_daily）
    "add_on":    False,    # 是否允许涨9%加仓追板；默认禁止（只有策略显式声明才开）
}


def get_position_config(filename: str) -> dict:
    """读取策略的仓位规则。示例 META：
    # META: {"name":"打板策略", ..., "position": {"max_daily":5,"max_hold":5,"sizing":"equal","add_on":false}}
    未声明 position 的策略返回默认值（不限额、不加仓，沿用账户面板设置）。"""
    cfg = dict(DEFAULT_POSITION_CONFIG)
    if filename:
        pc = get_meta(filename).get("position")
        if isinstance(pc, dict):
            cfg.update(pc)
    return cfg


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


# ─── 策略扫描进度（策略 scan 阶段，区别于 ths_quote 的 universe 拉行情阶段）───
# 用法：sm.get_scan_progress() 返回 {"running","stage","current","total","code"}
# 前端轮询 /api/status 可拿到，universe 阶段后接力显示这个，避免"进度卡在100%"
_strat_prog: dict = {
    "running": False,
    "stage":   "",          # "策略扫描" / ""
    "current": 0,           # 已处理票数
    "total":   0,           # 总票数
    "code":    "",          # 当前处理的票号（可选展示）
}


def get_scan_progress() -> dict:
    """对外暴露策略 scan 阶段进度（main.py /api/status 合并显示）"""
    return dict(_strat_prog)


def _build_data_fns(universe: list):
    """
    构建策略数据访问函数（TDX优先，支持全市场）。
    universe: 已预筛选的股票代码列表

    包装 get_kline，每次调用时上报策略 scan 阶段进度，让前端能看到实时
    "策略扫描 1234/5000" 而不是卡在 universe 拉完后的 "100%"。
    """
    import pandas as pd

    def _get_kline(code: str, days: int = 60) -> pd.DataFrame:
        # 进度计数（每只票被处理时 +1）
        _strat_prog["current"] += 1
        _strat_prog["code"] = code
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

    # ── 路径 5：磁盘持久化兜底（盘后/全部源挂时仍可跑策略）──────────
    # 行情源全挂时（如夜间 TDX/akshare 等都不响应），读上一次成功保存的票池
    try:
        from pathlib import Path
        cache_file = Path(__file__).parent.parent / "data" / "market_universe.json"
        if cache_file.exists():
            import json as _json
            try:
                data = _json.loads(cache_file.read_text(encoding="utf-8"))
            except ValueError:
                # 损坏的兜底文件（历史非原子写残留，报 "Extra data"）→ 删除待原子重建
                logger.warning("[SM] 磁盘兜底 universe 损坏，已删除待下次刷新重建")
                try:
                    cache_file.unlink()
                except OSError:
                    pass
                data = {}
            active = data.get("quotes", [])
            saved_ts = data.get("ts", 0)
            age_h = (_time.time() - saved_ts) / 3600
            if active:
                logger.warning(f"[SM] 实时源全挂，启用磁盘兜底 universe（{age_h:.1f}h 前保存，{len(active)} 只）")
                _mkt_cache["sorted_quotes"] = active
                _mkt_cache["ts"] = _time.time()
                return active
    except Exception as e:
        logger.warning(f"[SM] 磁盘兜底 universe 读取失败: {e}")

    logger.error("[SM] 全部数据源均不可用，无法获取全市场行情")
    return []


def _persist_universe_to_disk(quotes: list) -> None:
    """
    把成功拿到的全市场票池持久化到磁盘，作为下次行情源全挂时的兜底。
    在 _refresh_market_cache 各个成功分支调用即可。
    """
    if not quotes:
        return
    try:
        from pathlib import Path
        import json as _json, os as _os, tempfile as _tempfile
        cache_file = Path(__file__).parent.parent / "data" / "market_universe.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = _json.dumps({"ts": _time.time(), "quotes": quotes}, ensure_ascii=False)
        # 原子写：先写唯一临时文件，再 os.replace 原子改名。
        # 之前直接 write_text，多个扫描线程并发写同一文件 → 字节交错 → 读取报 "Extra data"。
        fd, tmp = _tempfile.mkstemp(dir=str(cache_file.parent), prefix=".universe.", suffix=".tmp")
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            _os.replace(tmp, cache_file)   # 同盘原子改名，最后一个写者整份生效，绝不交错
        finally:
            if _os.path.exists(tmp):
                try:
                    _os.remove(tmp)
                except OSError:
                    pass
        logger.debug(f"[SM] universe 已持久化到 {cache_file.name} ({len(quotes)} 只)")
    except Exception as e:
        logger.debug(f"[SM] universe 持久化失败（不致命）: {e}")


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
    quotes = _refresh_market_cache()
    # 拿到数据就顺手持久化一份，下次全挂时有兜底
    _persist_universe_to_disk(quotes)
    return quotes


# ── 全市场【未过滤】快照：供涨跌家数 / 两市成交额等市场统计用 ──────────────
# 注意：_get_market_quotes_cached() 返回的是**策略扫描用的过滤池**（price>3 且 vol>100，
# 还可能不含北交所），用它统计全市场涨跌家数/成交额会系统性偏小。市场统计必须用全量。
_full_mkt_cache = {"quotes": [], "ts": 0}


def _get_full_market_snapshot(force_refresh: bool = False) -> list:
    """全市场未过滤行情快照（含低价股，尽量含北交所），带 5 分钟缓存。
    每项含 code/price/change_pct/amount 等。供 web/market 涨跌家数、review 成交额求和。"""
    now = _time.time()
    if not force_refresh and _full_mkt_cache["quotes"] and (now - _full_mkt_cache["ts"]) < _MKT_CACHE_TTL:
        return _full_mkt_cache["quotes"]

    quotes = []
    # 路径 1：akshare 全量（东财 stock_zh_a_spot_em = 沪深京A股，与东财页面显示口径一致，
    #         含北交所/全价格，最完整）。优先它，才能与东财的成交总额/涨跌家数对得上。
    # ★务必用 research_center.no_proxy()：本机 v2ray 系统代理写在 Windows 注册表里，
    #   只 pop 环境变量没用（requests 仍从注册表捡代理掐掉东财 → 长期回退 TDX 致少算北交所）。
    try:
        import research_center as _rc, akshare as ak
        import pandas as pd
        with _rc.no_proxy():
            df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            cm = {"代码": "code", "名称": "name", "最新价": "price", "涨跌幅": "change_pct",
                  "昨收": "last_close", "成交额": "amount", "成交量": "vol"}
            keep = [c for c in cm if c in df.columns]
            d2 = df[keep].rename(columns=cm)
            for c in ("price", "change_pct", "last_close", "amount", "vol"):
                if c in d2.columns:
                    d2[c] = pd.to_numeric(d2[c], errors="coerce").fillna(0.0)
            quotes = d2.to_dict("records")
            logger.info(f"[SM] 全市场快照（akshare 东财全量）：{len(quotes)} 只")
    except Exception as e:
        logger.warning(f"[SM] 全市场快照 akshare 失败: {e}")

    # 路径 2：TDX 全量兜底（akshare 被东财风控/云端不可用时，不依赖东财仍能出数）
    if not quotes:
        try:
            import ths_quote as _tq
            client = _tq.get_client()
            if client and client._ok:
                all_codes = client.get_all_stock_codes()
                if all_codes:
                    quotes = client.get_quotes(all_codes) or []
                    logger.info(f"[SM] 全市场快照（TDX 兜底）：{len(quotes)} 只")
        except Exception as e:
            logger.warning(f"[SM] 全市场快照 TDX 失败: {e}")

    # 路径 3：退而求其次，用策略过滤池（至少不为空，仅作最后兜底）
    if not quotes:
        quotes = _get_market_quotes_cached()

    if quotes:
        _full_mkt_cache["quotes"] = quotes
        _full_mkt_cache["ts"] = now
    return quotes


def _full_market_universe(max_codes: int = 0, force_refresh: bool = False) -> list:
    """
    两阶段全市场选股宇宙（带5分钟缓存）：
      阶段1：TDX 批量行情获取所有 A 股实时数据（首次~60s，缓存命中<1s）
      阶段2：按成交量排序后裁剪
    参数：
      max_codes: 上限。<=0 表示不裁剪（真·全市场，约 5000 只，扫描耗时 2-3 分钟）
                 > 0 取前 N 只（快速扫描）
    """
    active = _get_market_quotes_cached(force_refresh)
    if not active:
        return []
    if max_codes and max_codes > 0:
        result = [q["code"] for q in active[:max_codes]]
        logger.info(f"[SM] 预筛选后：{len(result)} 只进入策略扫描（上限 {max_codes}）")
    else:
        result = [q["code"] for q in active]
        logger.info(f"[SM] 全市场扫描：{len(result)} 只进入策略扫描（无上限）")
    return result


def _full_market_universe_with_quotes(max_codes: int = 0,
                                      force_refresh: bool = False) -> tuple:
    """
    同 _full_market_universe，但同时返回行情缓存字典。
    供因子库使用，避免二次批量行情请求。

    参数：
      max_codes: <=0 不裁剪（真·全市场），>0 取前 N 只活跃股
    返回：(universe: list[str], quote_cache: dict[code -> quote_dict])
    """
    active = _get_market_quotes_cached(force_refresh)
    if not active:
        return [], {}
    if max_codes and max_codes > 0:
        top = active[:max_codes]
    else:
        top = active
    universe = [q["code"] for q in top]
    quote_cache = {q["code"]: q for q in top}
    logger.info(f"[SM] 预筛选后：{len(universe)} 只（含行情缓存，因子免二次拉取）")
    return universe, quote_cache


def run(filename: str, codes: list, max_codes: int = 0) -> dict:
    """
    执行策略选股。
    - codes 为空 → 全市场扫描（两阶段：行情预筛选 → kline策略过滤）
    - codes 非空 → 指定股票池扫描
    - max_codes: 全市场模式下的扫描上限
        0  → 不裁剪（真·全市场约 5000 只，扫描 2-3 分钟）
        N  → 取按成交量排序的前 N 只活跃股（快速扫描）
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
            # 全市场模式：max_codes 由调用方控制（0=全市场无上限，>0=取活跃前 N 只）
            universe = _full_market_universe(max_codes=max_codes or 0)
            if not universe:
                # TDX 不可用时降级到配置的股票池
                from config import STOCK_POOL
                universe = STOCK_POOL
                logger.warning("[SM] TDX不可用，降级到默认股票池")
        else:
            universe = codes

        get_kline, get_quotes = _build_data_fns(universe)
        # ★ Bug fix 2026-05-25: 不再对 da_ban 写死 [] 屏蔽 universe ★
        # 之前 scan_codes=[] 导致 da_ban 在 _from_tdx 失败（盘后 TDX 关 5档盘口数据流）
        # 后降级路径拿不到任何股 → 0 命中。
        # 现在统一传 universe，让 da_ban 内部先试 _from_tdx，失败用 universe 降级。
        scan_codes = universe

        # ─── 策略 scan 阶段：初始化进度 ───
        # universe 阶段（ths_quote._scan_prog）完成后接力上报进度，让前端
        # 看到"策略扫描 1234/5018"而不是卡在 universe 阶段的 100%
        _strat_prog.update({
            "running": True,
            "stage":   "策略扫描",
            "current": 0,
            "total":   len(scan_codes) if scan_codes else len(universe),
            "code":    "",
        })
        try:
            results = mod.scan(scan_codes, get_kline, get_quotes)
        finally:
            _strat_prog["running"] = False

        return {
            "ok":      True,
            "results": results,
            "count":   len(results),
            "scanned": len(universe),
        }
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"策略执行出错: {e}\n{tb}")
        _strat_prog["running"] = False
        return {"ok": False, "msg": str(e), "traceback": tb, "results": []}
