# -*- coding: utf-8 -*-
"""
本地行情接入模块（基于 pytdx / 通达信协议）
─────────────────────────────────────────────────────────
数据来源：通达信公共行情服务器（同花顺、各大券商均兼容）
延迟：< 0.5 秒（TCP 直连，无 HTTP 代理）
数据：实时价 + 昨收 + 5 档买卖盘 + 成交量/额
─────────────────────────────────────────────────────────
"""

import threading
import time
import logging
import json
from pathlib import Path
from typing import List, Tuple, Dict, Optional

logger = logging.getLogger(__name__)

# ── 股票名称缓存（从 stock_names.json 加载，避免每次查磁盘）──────
_name_cache: Dict[str, str] = {}

# ── 扫描进度（供 main.py 的 /api/status 读取）──────
_scan_prog: Dict[str, object] = {"running": False, "current": 0, "total": 0}

def get_scan_progress() -> Dict:
    return dict(_scan_prog)

def _load_name_cache():
    global _name_cache
    try:
        f = Path(__file__).parent.parent / "data" / "stock_names.json"
        if f.exists():
            _name_cache = json.loads(f.read_text(encoding="utf-8"))
            logger.debug(f"[TDX] 名称缓存已加载: {len(_name_cache)} 条")
    except Exception as e:
        logger.warning(f"[TDX] 名称缓存加载失败: {e}")

_load_name_cache()


def get_stock_name(code: str) -> str:
    """查询单只股票名称（供外部模块调用）"""
    return _name_cache.get(code, "")


# ── 公共行情服务器列表（通达信协议，免费）────────────────
_SERVERS = [
    ("218.75.126.9",  7709),
    ("119.147.212.81", 7709),
    ("210.51.39.201", 7709),
    ("106.120.74.86", 7711),
    ("112.74.214.43", 7709),
]

# 市场前缀（pytdx 规范）
_MARKET_SZ = 0   # 深交所
_MARKET_SH = 1   # 上交所
_MARKET_BJ = 2   # 北交所（部分服务器不支持，退化为深）


def _market(code: str) -> int:
    if code.startswith("6"):
        return _MARKET_SH
    if code.startswith(("8", "4")):
        return _MARKET_BJ
    return _MARKET_SZ


# ══════════════════════════════════════════════════════════
# TDX 行情客户端
# ══════════════════════════════════════════════════════════
class TDXQuoteClient:
    """
    线程安全的 pytdx 客户端封装。
    支持：
      - 批量实时行情（最多80只/批）
      - 全市场股票列表（沪深北）
      - 自动重连
    """

    def __init__(self):
        self._api   = None
        self._lock  = threading.Lock()
        self._host  = None
        self._port  = None
        self._ok    = False
        self._connect()

    def _connect(self) -> bool:
        try:
            from pytdx.hq import TdxHq_API
        except ImportError:
            logger.error("pytdx 未安装，请运行: pip install pytdx")
            return False

        for host, port in _SERVERS:
            try:
                api = TdxHq_API(raise_exception=False)
                if api.connect(host, port):
                    self._api  = api
                    self._host = host
                    self._port = port
                    self._ok   = True
                    logger.info(f"[TDX] 已连接行情服务器 {host}:{port}")
                    return True
            except Exception as e:
                logger.debug(f"[TDX] 连接失败 {host}:{port}: {e}")
        logger.error("[TDX] 所有服务器均连接失败")
        return False

    def _ensure(self) -> bool:
        if self._ok and self._api:
            return True
        return self._connect()

    def get_quotes(self, codes: List[str]) -> List[Dict]:
        """
        批量获取实时行情（每批最多 80 只）。
        返回列表，每项为 dict：
          code, name, price, last_close, open, high, low,
          vol, amount, bid1-5 (价/量), ask1-5 (价/量),
          change_pct, limit_up, limit_down
        """
        if not codes or not self._ensure():
            return []

        params = [(_market(c), c) for c in codes]
        results = []
        BATCH = 80
        total = len(params)
        _scan_prog.update({"running": True, "current": 0, "total": total})

        with self._lock:
            for i in range(0, total, BATCH):
                batch = params[i: i + BATCH]
                try:
                    raw = self._api.get_security_quotes(batch)
                    if raw:
                        results.extend(raw)
                except Exception as e:
                    logger.warning(f"[TDX] 行情批次失败: {e}")
                    self._ok = False
                    self._connect()
                _scan_prog["current"] = min(i + BATCH, total)

        _scan_prog["running"] = False

        quotes = [_parse_quote(r) for r in results if r]
        # 补充名称（pytdx get_security_quotes 不返回名称）
        for q in quotes:
            if not q.get("name"):
                q["name"] = _name_cache.get(q["code"], "")
        return quotes

    def get_all_stock_codes(self) -> List[str]:
        """
        获取全市场 A 股代码（5000+只）。
        优先从本地 stock_names.json 缓存读取（akshare 每天更新的全量名称表）。
        缓存不存在时降级到 TDX get_security_list（可能不全）。
        """
        # ── 优先：从本地名称缓存读取（最全）────────────────
        try:
            import json
            from pathlib import Path
            cache_file = Path(__file__).parent.parent / "data" / "stock_names.json"
            if cache_file.exists():
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                codes = [c for c in data.keys() if _is_valid_a_share(c, data[c])]
                if len(codes) > 1000:
                    logger.info(f"[TDX] 从名称缓存读取全市场代码: {len(codes)} 只")
                    return codes
        except Exception as e:
            logger.warning(f"[TDX] 读取名称缓存失败: {e}")

        # ── 降级：TDX get_security_list ──────────────────
        if not self._ensure():
            return []
        codes = []
        with self._lock:
            for market in (_MARKET_SH, _MARKET_SZ):
                offset = 0
                while True:
                    try:
                        lst = self._api.get_security_list(market, offset)
                        if not lst:
                            break
                        for item in lst:
                            code = str(item.get("code", "")).strip()
                            name = str(item.get("name", "")).strip()
                            if _is_valid_a_share(code, name):
                                codes.append(code)
                        offset += len(lst)
                        if len(lst) < 1000:
                            break
                    except Exception as e:
                        logger.warning(f"[TDX] 获取列表失败 market={market} offset={offset}: {e}")
                        break
        logger.info(f"[TDX] TDX列表获取代码: {len(codes)} 只")
        return codes

    def get_full_market_quotes(self) -> List[Dict]:
        """
        扫描全市场实时行情（用于打板策略）。
        约需 5-15 秒完成全市场 ~5000 只扫描。
        """
        codes = self.get_all_stock_codes()
        if not codes:
            return []
        logger.info(f"[TDX] 开始全市场扫描 {len(codes)} 只...")
        return self.get_quotes(codes)

    def get_kline(self, code: str, days: int = 120) -> List[Dict]:
        """
        获取日K线数据（从 TDX 服务器直接拉取，无需本地缓存）。
        返回列表，每项 dict: date, open, close, high, low, volume
        """
        if not self._ensure():
            return []
        market = _market(code)
        count   = min(days, 800)
        with self._lock:
            try:
                # pytdx category: 9 = 日K线
                bars = self._api.get_security_bars(9, market, code, 0, count)
                if not bars:
                    return []
                result = []
                for b in bars:
                    if not b:
                        continue
                    dt = str(b.get("datetime", ""))[:10]
                    result.append({
                        "date":   dt,
                        "open":   round(_f(b.get("open",  0)), 2),
                        "close":  round(_f(b.get("close", 0)), 2),
                        "high":   round(_f(b.get("high",  0)), 2),
                        "low":    round(_f(b.get("low",   0)), 2),
                        "volume": int(b.get("vol", 0)),
                    })
                return result
            except Exception as e:
                logger.warning(f"[TDX] K线获取失败 {code}: {e}")
                return []

    def get_index_quotes(self, index_pairs: list) -> list:
        """
        获取指数实时行情（market由调用方指定，避免 _market() 误判）。
        index_pairs: [(market, code), ...]
          上证指数 → (1, "000001")
          深证成指 → (0, "399001")
          创业板指 → (0, "399006")
        """
        if not index_pairs or not self._ensure():
            return []
        with self._lock:
            try:
                raw = self._api.get_security_quotes(index_pairs)
                return [_parse_quote(r) for r in (raw or []) if r]
            except Exception as e:
                logger.warning(f"[TDX] 指数行情失败: {e}")
                return []

    def get_index_kline(self, code: str, market: int, days: int = 60) -> list:
        """
        获取指数日K线（market由调用方指定）。
        上证指数 market=1，深证/创业 market=0。
        """
        if not self._ensure():
            return []
        count = min(days, 800)
        with self._lock:
            try:
                bars = self._api.get_security_bars(9, market, code, 0, count)
                if not bars:
                    return []
                result = []
                for b in bars:
                    if not b:
                        continue
                    dt = str(b.get("datetime", ""))[:10]
                    result.append({
                        "date":   dt,
                        "open":   round(_f(b.get("open",  0)), 2),
                        "close":  round(_f(b.get("close", 0)), 2),
                        "high":   round(_f(b.get("high",  0)), 2),
                        "low":    round(_f(b.get("low",   0)), 2),
                        "volume": int(b.get("vol", 0)),
                    })
                return result
            except Exception as e:
                logger.warning(f"[TDX] 指数K线失败 {code}: {e}")
                return []

    def disconnect(self):
        try:
            if self._api:
                self._api.disconnect()
        except Exception:
            pass
        self._ok = False


# ── 解析单条行情 ──────────────────────────────────────────
def _parse_quote(r: dict) -> Dict:
    """
    pytdx 直接返回价格（元），成交量单位为手（100股），成交额单位为元。
    无需额外换算。
    """
    code       = str(r.get("code", ""))
    price      = round(_f(r.get("price",      0)), 2)
    last_close = round(_f(r.get("last_close", 0)), 2)
    open_p     = round(_f(r.get("open",       0)), 2)
    high       = round(_f(r.get("high",       0)), 2)
    low        = round(_f(r.get("low",        0)), 2)
    vol        = int(r.get("vol", 0))      # 手（100股）
    amount     = _f(r.get("amount", 0))   # 元

    change_pct = 0.0
    if last_close > 0:
        change_pct = round((price - last_close) / last_close * 100, 2)

    # 涨跌停价（精确到分）
    limit_rate = _limit_rate(code)
    limit_up   = round(last_close * (1 + limit_rate / 100), 2) if last_close > 0 else 0
    limit_down = round(last_close * (1 - limit_rate / 100), 2) if last_close > 0 else 0

    # 5 档买卖盘（价格已是元，数量单位：手）
    bids = []
    asks = []
    for i in range(1, 6):
        bids.append({
            "price": round(_f(r.get(f"bid{i}",     0)), 2),
            "vol":   int(r.get(f"bid_vol{i}", 0)),
        })
        asks.append({
            "price": round(_f(r.get(f"ask{i}",     0)), 2),
            "vol":   int(r.get(f"ask_vol{i}", 0)),
        })

    return {
        "code":        code,
        "price":       price,
        "last_close":  last_close,
        "open":        open_p,
        "high":        high,
        "low":         low,
        "vol":         vol,
        "amount":      amount,
        "change_pct":  change_pct,
        "limit_up":    limit_up,
        "limit_down":  limit_down,
        "limit_rate":  limit_rate,
        "bids":        bids,    # 买一 ~ 买五
        "asks":        asks,    # 卖一 ~ 卖五
        "server_time": r.get("servertime", ""),
    }


def _f(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _limit_rate(code: str) -> float:
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("8", "4")):
        return 30.0
    return 10.0


def _is_valid_a_share(code: str, name: str = "") -> bool:
    """
    过滤保留 A 股正股，排除：B股/ETF/基金/债券/可转债/指数
    """
    if len(code) != 6:
        return False

    # ── 按名称排除 ──────────────────────────────
    _excl_names = ("ETF", "LOF", "基金", "债", "转债", "期货", "指数", "港股")
    for kw in _excl_names:
        if kw in name:
            return False

    # ── 上证 A 股：6 开头，排除 B 股(900)和指数(000) ──
    if code.startswith("6"):
        # 900xxx = B股，000xxx = 指数，5xxxxx = ETF/基金
        if code.startswith(("900", "000", "500", "510", "511", "512",
                             "513", "515", "516", "517", "518", "519")):
            return False
        return True   # 600/601/603/605/688/689 等均纳入

    # ── 深证 A 股 ────────────────────────────────
    if code.startswith(("000", "001", "002", "003")):
        return True   # 主板
    if code.startswith(("300", "301")):
        return True   # 创业板
    if code.startswith("200"):
        return False  # B股

    # ── 北交所 ───────────────────────────────────
    if code.startswith(("83", "87", "43")):
        return True

    return False


# ══════════════════════════════════════════════════════════
# 全局单例
# ══════════════════════════════════════════════════════════
_client: Optional[TDXQuoteClient] = None
_client_lock = threading.Lock()


def get_client() -> Optional[TDXQuoteClient]:
    """获取全局 TDX 客户端单例（双重检查锁，避免初始化后不必要的锁争用）"""
    global _client
    # 快速路径：已初始化则直接返回（无锁）
    if _client is not None:
        return _client
    # 慢速路径：首次初始化加锁
    with _client_lock:
        if _client is None:
            try:
                _client = TDXQuoteClient()
            except Exception as e:
                logger.error(f"[TDX] 初始化失败: {e}")
        return _client


def is_available() -> bool:
    c = get_client()
    return c is not None and c._ok
