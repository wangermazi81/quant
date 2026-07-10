"""投研中心(research_center)：概念板块 / 产业链图谱 / 研报聚合 + 数据清洗。

挂在智能搜索页的两个新模式：
- 🧬 产业链：概念板块搜索 → 成分股行情 → AI 产业链图谱(上/中/下游环节映射成分股)
- 📑 研报：按股票聚合最新研报(标题/机构/评级/目标价/盈利预测)

数据清洗原则（用户只看到干净结果，接口输出不含抓取来源字段）：
- 文本去 HTML/空白、标题过短或营销味重的丢弃
- 标题+机构哈希去重、评级归一化(买入/增持/中性/减持/卖出)
- 数值列强制转数值，坏行剔除
所有上游抓取均容错，单源失败返回缓存或空。
"""
import contextlib
import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def no_proxy():
    """强制 requests 忽略系统代理（含 Windows 注册表代理）。

    本机常开 v2ray(127.0.0.1:10809)，requests 会经 urllib.getproxies() 从
    **注册表**捡到它——仅清环境变量挡不住。东财 push2 行情主机走海外出口
    会被随机掐(RemoteDisconnected)，国内源直连反而稳。
    在 merge_environment_settings 层面把 proxies 置空，注册表+环境变量全覆盖。
    LLM 调用走 urllib 不受此补丁影响。
    """
    import requests
    orig = requests.Session.merge_environment_settings

    def patched(self, url, proxies, stream, verify, cert):
        s = orig(self, url, proxies, stream, verify, cert)
        s["proxies"] = {}
        return s

    requests.Session.merge_environment_settings = patched
    try:
        yield
    finally:
        requests.Session.merge_environment_settings = orig

BASE_DIR  = Path(__file__).resolve().parent
CHAIN_DIR = BASE_DIR / "data" / "chains"
CACHE_DIR = BASE_DIR / "data" / "research_cache"

_cache_lock = threading.Lock()
_cache: dict = {}            # key -> (ts, data)


def _cached(key: str, ttl: int):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    return None


def _put_cache(key: str, data):
    with _cache_lock:
        _cache[key] = (time.time(), data)


# 磁盘兜底缓存：东财接口在代理环境时通时断，拉到一次就落盘；
# 拉取失败时宁可给旧数据也不给空白（概念/成分股结构变化很慢）。
def _disk_path(key: str) -> Path:
    return CACHE_DIR / (hashlib.md5(key.encode("utf-8")).hexdigest() + ".json")


def _disk_get(key: str):
    try:
        p = _disk_path(key)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _disk_put(key: str, data):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _disk_path(key).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _retry(fn, attempts: int = 3, wait: float = 2.0):
    """统一重试封装：直连(绕代理)优先，全部失败返回 None。"""
    for i in range(attempts):
        try:
            with no_proxy():
                r = fn()
            if r is not None:
                return r
        except Exception as e:
            logger.debug(f"[research] 第{i+1}次失败: {e}")
        if i < attempts - 1:
            time.sleep(wait)
    return None


# ──────────────────────────────────────────────────────────────
# 清洗工具
# ──────────────────────────────────────────────────────────────
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE  = re.compile(r"\s+")

_RATING_MAP = {
    "买入": "买入", "强烈推荐": "买入", "强推": "买入", "推荐": "买入", "强力买入": "买入",
    "增持": "增持", "谨慎增持": "增持", "优于大市": "增持", "跑赢行业": "增持", "强于大市": "增持",
    "中性": "中性", "持有": "中性", "观望": "中性", "同步大市": "中性",
    "减持": "减持", "弱于大市": "减持",
    "卖出": "卖出", "回避": "卖出",
}


def _clean_text(s) -> str:
    s = _TAG_RE.sub("", str(s or ""))
    return _WS_RE.sub(" ", s).strip()


def _norm_rating(s) -> str:
    s = _clean_text(s)
    for k, v in _RATING_MAP.items():
        if k in s:
            return v
    return s[:6] if s else ""


def _num(v, nd=2):
    try:
        f = float(v)
        if f != f:      # NaN
            return None
        return round(f, nd)
    except Exception:
        return None


def _col(df, *cands):
    for cand in cands:
        if cand in df.columns:
            return cand
    for c in df.columns:
        for cand in cands:
            if cand in str(c):
                return c
    return None


# ──────────────────────────────────────────────────────────────
# 概念过滤：只保留真正的产业/主题概念，清除交易信号类噪声
# ──────────────────────────────────────────────────────────────
_TRADE_SIGNAL_RE = re.compile(
    # 日内/近期涨停连板类（含"扳/板"两种写法）
    r"(昨日|今日|前日|上日|本日|当日|近\d+日).{0,6}(连[板扳]|涨停|涨幅|跌幅|首[板扳]|"
    r"二[板扳]|三[板扳]|四[板扳]|五[板扳])|"
    r"连续涨停|首次涨停|\d+连[板扳]|\d+天\d+[板扳]|炸板|换板|打板|封板|翻板|反包板|"
    # 资金流/技术面类
    r"(高|低)(换手率?|量比|振幅)|(主力|北向|南向|沪股通|深股通|外资)(资金|净|大流)|"
    # 排行榜/筛选类
    r"(周|月|年|季)(涨幅|跌幅).{0,4}(前|TOP|榜)|换手率.{0,4}(前|TOP)|量比.{0,4}(前|TOP)|"
    # 其他纯技术/市场结构类
    r"高送转|^\d+涨停$|跌停板|大单净|净流入前|净流出前"
)


def _is_industry_concept(name: str) -> bool:
    """True = 产业/主题概念（保留）；False = 交易信号/技术面概念（过滤）。"""
    return not bool(_TRADE_SIGNAL_RE.search(name))


# ──────────────────────────────────────────────────────────────
# 概念板块
# 主通道 = 东财延迟行情集群(push2delay)直连：与被 IP 风控的 push2 主集群
# 不是同一组主机(2026-06-11 实测主集群 RemoteDisconnected 时它仍 200)，
# 数据相同仅行情延迟约15分钟，对投研场景无感。
# ──────────────────────────────────────────────────────────────
_EM_DELAY = "https://push2delay.eastmoney.com/api/qt/clist/get"
_EM_UA    = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
             "Referer": "https://quote.eastmoney.com/"}


def _em_clist(fs: str, fields: str, max_rows: int = 1000) -> list:
    """东财 clist 直连(延迟集群)，自动翻页（该集群单页上限100条）。失败抛异常。"""
    import requests
    out, pn = [], 1
    while len(out) < max_rows and pn <= 30:
        r = requests.get(_EM_DELAY,
                         params={"pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2,
                                 "invt": 2, "fid": "f3", "fs": fs, "fields": fields},
                         headers=_EM_UA, timeout=12)
        rows = (((r.json() or {}).get("data") or {}).get("diff")) or []
        out.extend(rows)
        if len(rows) < 100:
            break
        pn += 1
    return out


def _em_direct_concepts() -> list:
    """全部概念板块(BK代码+涨跌幅+涨跌家数)，直连延迟集群。"""
    rows = _em_clist("m:90 t:3", "f2,f3,f12,f14,f104,f105")
    out = []
    for x in rows:
        name = _clean_text(x.get("f14"))
        if not name:
            continue
        out.append({
            "name": name,
            "code": str(x.get("f12") or ""),
            "chg":  _num(x.get("f3")),
            "up":   int(x.get("f104") or 0),
            "down": int(x.get("f105") or 0),
        })
    out.sort(key=lambda d: d["chg"] if d["chg"] is not None else -999, reverse=True)
    return out or None


def get_concepts(q: str = "", limit: int = 40) -> list:
    """概念板块列表（按涨跌幅排序，10分钟缓存+磁盘兜底），q 为名称模糊过滤。

    源优先级：东财延迟集群直连 → akshare东财 → 磁盘缓存 → 同花顺名录。
    """
    data = _cached("concepts", 600)
    if data is None:
        data = _retry(_em_direct_concepts, attempts=2)

        if not data:
            def _fetch_ak():
                import akshare as ak
                import pandas as pd
                df = ak.stock_board_concept_name_em()
                if df is None or df.empty:
                    return None
                nc  = _col(df, "板块名称", "名称")
                cc  = _col(df, "板块代码", "代码")
                chg = _col(df, "涨跌幅")
                up  = _col(df, "上涨家数")
                dn  = _col(df, "下跌家数")
                df[chg] = pd.to_numeric(df[chg], errors="coerce")
                rows = []
                for _, r in df.dropna(subset=[chg]).sort_values(chg, ascending=False).iterrows():
                    rows.append({
                        "name": _clean_text(r[nc]),
                        "code": str(r[cc]) if cc else "",
                        "chg":  _num(r[chg]),
                        "up":   int(_num(r[up], 0) or 0) if up else None,
                        "down": int(_num(r[dn], 0) or 0) if dn else None,
                    })
                return rows or None
            data = _retry(_fetch_ak, attempts=2)

        if data:
            _put_cache("concepts", data)
            _disk_put("concepts", data)
        else:
            data = _disk_get("concepts") or []        # 接口全挂 → 上次成功的列表兜底
            if data:
                logger.info("[research] 概念列表用磁盘兜底缓存")
                _put_cache("concepts", data)
        if not data:
            data = _ths_concepts()                    # 最后兜底：同花顺名录(无涨跌幅)
            if data:
                logger.info("[research] 概念列表用同花顺备源")
                _put_cache("concepts", data)
    # 产业过滤：磁盘/缓存旧数据也会被清洗
    data = [d for d in data if _is_industry_concept(d["name"])]
    if q:
        ql = q.strip().lower()
        data = [d for d in data if ql in d["name"].lower()]
    return data[:limit]


def _em_bk_code(name: str) -> str:
    """概念名 → 东财 BK 板块代码（从概念列表缓存/直连解析）。"""
    pools = []
    cached = _cached("concepts", 600) or _disk_get("concepts") or []
    pools.append(cached)
    if not any(str(c.get("code", "")).startswith("BK") for c in cached):
        try:
            fresh = _retry(_em_direct_concepts, attempts=1) or []
            pools.append(fresh)
        except Exception:
            pass
    for pool in pools:
        for c in pool:
            if c.get("name") == name and str(c.get("code", "")).startswith("BK"):
                return c["code"]
    key = name.replace("概念", "").strip()
    for pool in pools:
        for c in pool:
            cn = c.get("name", "")
            if str(c.get("code", "")).startswith("BK") and key and (key in cn or cn in name):
                return c["code"]
    return ""


def _ths_concepts() -> list:
    """同花顺概念名录（备源，仅名称无涨跌幅；东财 push2 集群被 IP 风控时顶上）。"""
    hit = _cached("ths_concepts", 43200)
    if hit:
        return hit

    def _fetch():
        import akshare as ak
        df = ak.stock_board_concept_name_ths()
        if df is None or df.empty:
            return None
        nc = _col(df, "name", "概念名称", "名称")
        cc = _col(df, "code", "代码")
        return [{"name": _clean_text(r[nc]), "code": str(r[cc]) if cc else "",
                 "chg": None, "up": None, "down": None}
                for _, r in df.iterrows()] or None

    data = _retry(_fetch, attempts=2) or _disk_get("ths_concepts") or []
    if data:
        _put_cache("ths_concepts", data)
        _disk_put("ths_concepts", data)
    return data


def get_concept_stocks(name: str, limit: int = 60) -> dict:
    """概念成分股行情（10分钟缓存+磁盘兜底）。"""
    key = f"cons:{name}"
    hit = _cached(key, 600)
    if hit is not None:
        # 缓存可能是图谱生成前写入的 → 每次返回前都重新合并相关度标注
        _merge_relevance(name, hit.get("stocks", []))
        return hit

    def _fetch():
        import akshare as ak
        import pandas as pd
        df = ak.stock_board_concept_cons_em(symbol=name)
        if df is None or df.empty:
            return None
        cc  = _col(df, "代码")
        nc  = _col(df, "名称")
        pc  = _col(df, "最新价")
        chg = _col(df, "涨跌幅")
        tr  = _col(df, "换手率")
        mv  = _col(df, "总市值", "流通市值")
        df[chg] = pd.to_numeric(df[chg], errors="coerce")
        stocks = []
        for _, r in df.dropna(subset=[chg]).sort_values(chg, ascending=False).iterrows():
            price = _num(r[pc]) if pc else None
            if price is None:           # 停牌/坏行剔除
                continue
            stocks.append({
                "code":     str(r[cc]).zfill(6) if cc else "",
                "name":     _clean_text(r[nc]),
                "price":    price,
                "chg":      _num(r[chg]),
                "turnover": _num(r[tr]) if tr else None,
                "mcap":     round((_num(r[mv], 0) or 0) / 1e8, 1) if mv else None,   # 亿
            })
        return stocks or None

    # 源优先级：东财延迟集群直连 → akshare东财 → 新浪 → 磁盘
    stocks = _em_direct_cons(name)
    if not stocks:
        stocks = _retry(_fetch, attempts=2)
    if not stocks:
        stocks = _sina_cons(name)       # 独立第三源（老概念覆盖较全，新概念可能缺）
        if stocks:
            logger.info(f"[research] 成分股 {name} 用新浪备源")
    if stocks:
        out = {"ok": True, "name": name, "stocks": stocks[:limit]}
        _merge_relevance(name, out["stocks"])
        _put_cache(key, out)
        _disk_put(key, out)
        return out
    disk = _disk_get(key)               # 接口全挂 → 上次成功数据兜底（行情可能滞后）
    if disk:
        logger.info(f"[research] 成分股 {name} 用磁盘兜底缓存")
        disk["stale"] = True
        _merge_relevance(name, disk.get("stocks", []))
        return disk
    return {"ok": False, "name": name, "error": "成分股数据暂时拉取失败，请稍后重试", "stocks": []}


def _em_direct_cons(name: str):
    """东财延迟集群直连取概念成分股（主通道）。"""
    bk = _em_bk_code(name)
    if not bk:
        return None

    def _fetch():
        rows = _em_clist(f"b:{bk} f:!50", "f2,f3,f8,f12,f14,f20", max_rows=500)
        stocks = []
        for x in rows:
            price = _num(x.get("f2"))
            chg   = _num(x.get("f3"))
            sname = _clean_text(x.get("f14"))
            if not price or chg is None or not sname or "退" in sname:
                continue
            stocks.append({
                "code":     str(x.get("f12") or "").zfill(6),
                "name":     sname,
                "price":    price,
                "chg":      chg,
                "turnover": _num(x.get("f8")),
                "mcap":     round((_num(x.get("f20"), 0) or 0) / 1e8, 1) or None,
            })
        stocks.sort(key=lambda s: s["chg"], reverse=True)
        return stocks or None

    r = _retry(_fetch, attempts=2)
    if r:
        logger.info(f"[research] 成分股 {name} 走延迟集群直连({bk}, {len(r)}只)")
    return r


def _sina_sectors() -> list:
    """新浪概念板块名录（label 如 gn_jqr，12小时缓存）。"""
    hit = _cached("sina_sectors", 43200)
    if hit:
        return hit

    def _fetch():
        import akshare as ak
        df = ak.stock_sector_spot(indicator="概念")
        if df is None or df.empty:
            return None
        lc = _col(df, "label")
        nc = _col(df, "板块", "name")
        if not lc or not nc:
            return None
        return [{"label": str(r[lc]).strip(), "name": _clean_text(r[nc])}
                for _, r in df.iterrows()] or None

    data = _retry(_fetch, attempts=2) or _disk_get("sina_sectors") or []
    if data:
        _put_cache("sina_sectors", data)
        _disk_put("sina_sectors", data)
    return data


def _sina_cons(name: str):
    """新浪概念成分备源——独立于东财/同花顺的第三数据源。

    名称模糊匹配：东财/同花顺概念名(如"机器人概念")与新浪板块名(如"机器人")
    互相包含即视为同一板块；匹配不上返回 None 交给磁盘缓存兜底。
    """
    key = name.replace("概念", "").replace("板块", "").strip()
    if not key:
        return None
    sec = None
    for s in _sina_sectors():
        if s["name"] == name or s["name"] == key:
            sec = s
            break
        if key and (key in s["name"] or s["name"] in key):
            sec = sec or s
    if not sec:
        return None

    def _fetch():
        import akshare as ak
        import pandas as pd
        df = ak.stock_sector_detail(sector=sec["label"])
        if df is None or df.empty:
            return None
        cc  = _col(df, "code")
        nc  = _col(df, "name")
        pc  = _col(df, "trade")
        chg = _col(df, "changepercent")
        tr  = _col(df, "turnoverratio")
        mv  = _col(df, "mktcap")              # 万元
        df[chg] = pd.to_numeric(df[chg], errors="coerce")
        stocks = []
        for _, r in df.dropna(subset=[chg]).sort_values(chg, ascending=False).iterrows():
            price = _num(r[pc]) if pc else None
            if not price:                      # 0/停牌剔除
                continue
            sname = _clean_text(r[nc])
            if "退" in sname:                  # 退市/XX退 剔除
                continue
            stocks.append({
                "code":     str(r[cc]).zfill(6) if cc else "",
                "name":     sname,
                "price":    price,
                "chg":      _num(r[chg]),
                "turnover": _num(r[tr]) if tr else None,
                "mcap":     round((_num(r[mv], 0) or 0) / 1e4, 1) if mv else None,
            })
        return stocks or None

    return _retry(_fetch, attempts=2)


def _merge_relevance(name: str, stocks: list):
    """把产业链图谱里 AI 标注的「与概念相关度」合并进成分股行。"""
    chain = load_chain(name)
    if not chain:
        return
    rel = {}
    for it in chain.get("relevance", []):
        m = re.search(r"(\d{6})", str(it.get("stock", "")))
        if m:
            rel[m.group(1)] = {"level": it.get("level", ""), "reason": it.get("reason", "")}
    # 出现在产业链环节里的个股，至少算"重要"
    seg_codes = set()
    for seg in chain.get("segments", []):
        for item in seg.get("items", []):
            for s in item.get("stocks", []):
                m = re.search(r"(\d{6})", str(s))
                if m:
                    seg_codes.add(m.group(1))
    for s in stocks:
        r = rel.get(s.get("code"))
        if r:
            s["rel_level"], s["rel_reason"] = r["level"], r["reason"]
        elif s.get("code") in seg_codes:
            s["rel_level"], s["rel_reason"] = "重要", "出现在产业链环节中"


# ──────────────────────────────────────────────────────────────
# AI 产业链图谱
# ──────────────────────────────────────────────────────────────
_KG_FILE = BASE_DIR / "data" / "kg" / "product_tree.csv"
_kg_rows: list | None = None


def _kg_vocab(concept: str, cap: int = 30) -> str:
    """产业链产品层级词表(来自开源 KGData 项目)中与概念相关的环节词，
    喂给 LLM 做环节划分参考。无命中返回空串。"""
    global _kg_rows
    try:
        if _kg_rows is None:
            import csv
            _kg_rows = []
            if _KG_FILE.exists():
                with open(_KG_FILE, encoding="utf-8-sig") as f:
                    for row in csv.DictReader(f):
                        _kg_rows.append((row.get("name", ""), row.get("pname", "")))
        key = concept.replace("概念", "").replace("板块", "").strip()
        if not key or not _kg_rows:
            return ""
        hits = [f"{p}→{n}" for n, p in _kg_rows
                if key in n or key in p or (len(key) >= 2 and (n in key or p in key))]
        return "；".join(hits[:cap])
    except Exception:
        return ""


def load_chain(name: str) -> dict | None:
    p = CHAIN_DIR / f"{name}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def generate_chain(name: str) -> dict:
    """LLM 基于成分股生成产业链上/中/下游图谱，永久缓存（可重新生成覆盖）。"""
    cons = get_concept_stocks(name, limit=80)
    if not cons.get("stocks"):
        return {"ok": False, "error": "该概念无成分股数据，无法生成产业链"}
    # 按市值取前40只喂给 LLM（控制 prompt 长度）
    stocks = sorted(cons["stocks"], key=lambda s: s.get("mcap") or 0, reverse=True)[:40]
    stock_lines = "、".join(f"{s['name']}({s['code']})" for s in stocks)

    try:
        from analytics import ai_analyst as aa
        cfg = aa.resolve_config()
        if not cfg.get("api_key"):
            return {"ok": False, "error": "未配置 AI API Key（前端「AI 设置」页配置）"}
        sys_prompt = (
            "你是A股产业链研究专家，请按专业产业库的标准输出产业链结构，"
            "严格输出 JSON（```json 包裹）：\n"
            '{"summary":"产业一句话概述(60字内)",'
            '"segments":[{"stage":"上游","weight":"该环节价值量占比估计(如 30%左右，不确定留空)",'
            '"items":[{"segment":"细分环节名","desc":"环节说明(25字内)",'
            '"stocks":[{"name":"股票名","code":"6位代码",'
            '"relation":"该公司与此环节的具体关联：产品/业务/市占地位/持股关系(35字内，尽量具体)",'
            '"level":"核心"}]}]},'
            '{"stage":"中游",...},{"stage":"下游",...}]}\n'
            "规则：股票只能从用户给的成分股里选，每个细分环节最多6只，一只股票可出现在多个环节；"
            "relation 写定性的具体关联（如'国内刻蚀设备主要供应商'/'间接持股上海微电子'），"
            "不掌握的精确数字禁止编造；level 只能取 核心/重要/一般"
            "（核心=主营业务就是该环节，重要=重要业务布局，一般=沾边/参股）；"
            "尽可能覆盖更多成分股；环节划分专业准确；不要输出 JSON 以外的任何文字。"
        )
        kg = _kg_vocab(name)
        user_msg = f"概念板块：{name}\n成分股：{stock_lines}"
        if kg:
            user_msg += f"\n产品环节参考词表(上级→下级)：{kg}"
        # max_tokens 给足余量：推理型模型(MiMo等)的思考过程会占掉大量
        # completion tokens(实测1100+)，给少了可见输出被截断成半截 JSON
        reply = aa._call_llm(cfg, [
            {"role": "system", "content": sys_prompt},
            {"role": "user",   "content": user_msg},
        ], temperature=0.2, max_tokens=8000, timeout=300)
        if not reply.strip():
            return {"ok": False, "error": "AI 返回空内容（服务商限流，请稍后重试）"}
        m = re.search(r"\{.*\}", reply, re.S)
        if not m:
            return {"ok": False, "error": "AI 返回格式异常，请重试"}
        graph = json.loads(m.group(0))
        _LEVELS = ("核心", "重要", "一般")

        def _norm_level(v):
            v = _clean_text(v)
            return next((x for x in _LEVELS if x in v), "一般")

        # 字段名归一化 + 个股条目统一为对象 {name, code, relation, level}
        # （兼容模型偶尔输出 name/description 代替 stage/desc、纯字符串个股）
        segments = []
        for seg in graph.get("segments", []):
            items = []
            for it in (seg.get("items") or seg.get("children") or []):
                stocks = []
                for sx in (it.get("stocks") or [])[:6]:
                    if isinstance(sx, dict):
                        nm = _clean_text(sx.get("name") or sx.get("stock") or "")
                        cd = re.sub(r"\D", "", str(sx.get("code") or ""))[:6]
                        if not cd:
                            mm = re.search(r"(\d{6})", nm)
                            cd = mm.group(1) if mm else ""
                        stocks.append({
                            "name": nm.split("(")[0].strip()[:12],
                            "code": cd,
                            "relation": _clean_text(sx.get("relation") or sx.get("reason") or "")[:60],
                            "level": _norm_level(sx.get("level")),
                        })
                    else:
                        nm = _clean_text(sx)
                        mm = re.search(r"(\d{6})", nm)
                        stocks.append({"name": nm.split("(")[0].strip()[:12],
                                       "code": mm.group(1) if mm else "",
                                       "relation": "", "level": "一般"})
                items.append({
                    "segment": _clean_text(it.get("segment") or it.get("name") or ""),
                    "desc":    _clean_text(it.get("desc") or it.get("description") or ""),
                    "stocks":  stocks,
                })
            segments.append({
                "stage":  _clean_text(seg.get("stage") or seg.get("name") or ""),
                "weight": _clean_text(seg.get("weight") or "")[:20],
                "items":  items,
            })

        # 相关度汇总：逐股取最高档（核心>重要>一般），供成分股表合并
        _rank = {"核心": 0, "重要": 1, "一般": 2}
        rel_map = {}
        for seg in segments:
            for it in seg["items"]:
                for sx in it["stocks"]:
                    if not sx["code"]:
                        continue
                    old = rel_map.get(sx["code"])
                    if old is None or _rank[sx["level"]] < _rank[old["level"]]:
                        rel_map[sx["code"]] = {"stock": f"{sx['name']}({sx['code']})",
                                               "level": sx["level"],
                                               "reason": sx["relation"]}
        for it in (graph.get("relevance") or []):     # 兼容旧字段
            mm = re.search(r"(\d{6})", str(it.get("stock", "")))
            if mm and mm.group(1) not in rel_map:
                rel_map[mm.group(1)] = {"stock": _clean_text(it.get("stock", "")),
                                        "level": _norm_level(it.get("level")),
                                        "reason": _clean_text(it.get("reason", ""))[:40]}
        relevance = list(rel_map.values())
        out = {"ok": True, "name": name, "summary": _clean_text(graph.get("summary", "")),
               "segments": segments, "relevance": relevance,
               "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        CHAIN_DIR.mkdir(parents=True, exist_ok=True)
        (CHAIN_DIR / f"{name}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        return out
    except json.JSONDecodeError:
        return {"ok": False, "error": "AI 返回的图谱解析失败，请重试"}
    except Exception as e:
        logger.warning(f"[research] 产业链生成失败 {name}: {e}")
        return {"ok": False, "error": f"生成失败：{e}"}


# ──────────────────────────────────────────────────────────────
# 研报聚合
# ──────────────────────────────────────────────────────────────
def get_reports(code: str, limit: int = 40) -> dict:
    """个股研报列表（6小时缓存，清洗+去重+评级归一化，不含抓取来源字段）。"""
    key = f"reports:{code}"
    hit = _cached(key, 21600)
    if hit is not None:
        return hit
    out = {"ok": True, "code": code, "reports": []}
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_research_report_em(symbol=code)
        if df is not None and not df.empty:
            tc  = _col(df, "报告名称", "标题")
            oc  = _col(df, "机构")
            rc  = _col(df, "东财评级", "评级")
            dc  = _col(df, "日期")
            seen = set()
            # 盈利预测列（动态年份，如 "2026-盈利预测-收益"）
            eps_cols = [c for c in df.columns if "盈利预测-收益" in str(c)][:2]
            for _, r in df.iterrows():
                title = _clean_text(r[tc]) if tc else ""
                org   = _clean_text(r[oc]) if oc else ""
                if len(title) < 6:                      # 垃圾/空标题
                    continue
                h = hashlib.md5(f"{title}|{org}".encode()).hexdigest()
                if h in seen:                           # 同题同机构去重
                    continue
                seen.add(h)
                item = {
                    "title":  title[:120],
                    "org":    org[:20],
                    "rating": _norm_rating(r[rc]) if rc else "",
                    "date":   _clean_text(r[dc])[:10] if dc else "",
                }
                eps = {}
                for c in eps_cols:
                    year = str(c)[:4]
                    v = _num(r[c])
                    if v is not None:
                        eps[year] = v
                if eps:
                    item["eps_forecast"] = eps
                out["reports"].append(item)
            out["reports"].sort(key=lambda x: x["date"], reverse=True)
            out["reports"] = out["reports"][:limit]
            out["total"]   = len(out["reports"])
            _put_cache(key, out)
    except Exception as e:
        logger.warning(f"[research] 研报拉取失败 {code}: {e}")
        out = {"ok": False, "code": code, "error": "研报数据暂时拉取失败，请稍后重试", "reports": []}
    return out


# ── 研报中心：全市场最新研报流（直连研报库接口，比逐股查询全且快）──────
_REPORT_PAGE = "https://data.eastmoney.com/report/info/{}.html"


def _fetch_reportapi(qtype: int = 0, code: str = "", page_size: int = 80, page_no: int = 1):
    """东方财富研报列表接口。qtype: 0=个股研报 1=行业研报 3=策略报告。"""
    import requests
    from datetime import timedelta
    end = datetime.now().strftime("%Y-%m-%d")
    beg = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    r = requests.get(
        "https://reportapi.eastmoney.com/report/list",
        params={"industryCode": "*", "pageSize": page_size, "industry": "*",
                "rating": "*", "ratingChange": "*", "beginTime": beg, "endTime": end,
                "pageNo": page_no, "fields": "", "qType": qtype, "orgCode": "",
                "rcode": "", "code": code or "*"},
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                 "Referer": "https://data.eastmoney.com/report/"})
    j = r.json()
    items = (j or {}).get("data") or None
    return items  # None = 请求失败；[] = 无更多数据；list = 正常


def _norm_em_report(it: dict) -> dict:
    """东财研报条目归一化为统一格式。"""
    info = str(it.get("infoCode") or "").strip()
    return {
        "title":    _clean_text(it.get("title"))[:120],
        "org":      _clean_text(it.get("orgSName") or it.get("orgName"))[:20],
        "rating":   _norm_rating(it.get("emRatingName") or it.get("sRatingName")
                                 or it.get("rating") or ""),
        "date":     str(it.get("publishDate") or "")[:10],
        "stock":    _clean_text(it.get("stockName") or ""),
        "code":     str(it.get("stockCode") or "")[:6],
        "industry": _clean_text(it.get("indvInduName") or it.get("industryName") or ""),
        "url":      _REPORT_PAGE.format(info) if info else "",
    }


def _fetch_cninfo_reports(code: str, limit: int = 40) -> list:
    """巨潮资讯研报（证监会官方公开披露平台，机构研报摘要，无需认证）。

    使用 CNINFO 公告查询 API，研究报告分类代码 010010。
    """
    import requests
    if not code:
        return []
    from datetime import timedelta
    # 沪市 sse / 深市 szse / 北交所 bse
    col = "szse" if code[:1] in "03" else ("bse" if code[:1] == "8" else "sse")
    end = datetime.now().strftime("%Y-%m-%d")
    beg = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    try:
        r = requests.post(
            "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            data={
                "stock":      code,
                "tabName":    "fulltext",
                "pageNum":    1,
                "pageSize":   limit,
                "column":     col,
                "category":   "010010",   # 研究报告
                "plate":      "",
                "seDate":     f"{beg}~{end}",
                "searchkey":  "",
                "secid":      "",
                "sortName":   "pubdate",
                "sortType":   "desc",
                "isHLtitle":  "true",
            },
            headers={
                "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer":         f"https://www.cninfo.com.cn/new/disclosure/stock?stockCode={code}",
                "Accept":          "application/json, text/javascript, */*; q=0.01",
                "Content-Type":    "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=15,
        )
        announcements = (r.json() or {}).get("announcements") or []
        out = []
        for it in announcements:
            title = _clean_text(it.get("announcementTitle") or "")
            if len(title) < 6:
                continue
            adj = str(it.get("adjunctUrl") or "")
            url = f"https://static.cninfo.com.cn/{adj}" if adj else ""
            out.append({
                "title":    title[:120],
                "org":      _clean_text(it.get("orgName") or "")[:20],
                "rating":   "",
                "date":     str(it.get("announcementTime") or "")[:10],
                "stock":    _clean_text(it.get("secName") or ""),
                "code":     code,
                "industry": "",
                "url":      url,
            })
        return out
    except Exception as e:
        logger.debug(f"[research] CNINFO 研报失败 {code}: {e}")
        return []


def _fetch_ths_reports(code: str = "", limit: int = 30) -> list:
    """同花顺研报中心（无 hexin-v 时静默返回空，不影响主流程）。"""
    import requests
    try:
        params = {"pageNo": 1, "pageSize": limit}
        if code:
            params["stockCode"] = code
        r = requests.get(
            "https://datacenter.10jqka.com.cn/reportcenter/index/",
            params=params,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer":    "https://datacenter.10jqka.com.cn/reportcenter/",
                "Accept":     "application/json, text/javascript, */*",
            },
            timeout=12,
        )
        if r.status_code != 200 or "data" not in r.text:
            return []
        j = r.json()
        items = ((j or {}).get("data") or {}).get("list") or []
        out = []
        for it in items:
            title = _clean_text(it.get("title") or "")
            if len(title) < 6:
                continue
            url_raw = str(it.get("url") or it.get("pdfUrl") or "")
            out.append({
                "title":    title[:120],
                "org":      _clean_text(it.get("broker") or it.get("author") or "")[:20],
                "rating":   _norm_rating(it.get("rating") or ""),
                "date":     str(it.get("publishDate") or it.get("date") or "")[:10],
                "stock":    _clean_text(it.get("stockName") or ""),
                "code":     str(it.get("stockCode") or code)[:6],
                "industry": "",
                "url":      url_raw,
            })
        return out
    except Exception:
        return []


def _fetch_sina_reports(code: str = "", limit: int = 40) -> list:
    """新浪财经研报（逐股查询，HTML 提取，无需认证）。"""
    import requests
    if not code:
        return []
    # 交易所前缀
    prefix = "sh" if code[:1] in "69" else ("bj" if code[:1] == "8" else "sz")
    try:
        r = requests.get(
            "https://vip.stock.finance.sina.com.cn/q/go.php/vAnalystStudy/kind/stock/index.phtml",
            params={"num": limit, "stockid": f"{prefix}{code}"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer":    "https://finance.sina.com.cn/",
            },
            timeout=12,
        )
        r.encoding = "gbk"
        # 简单正则提取研报条目
        items = re.findall(
            r'<td[^>]*class="[^"]*title[^"]*"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>.*?'
            r'(\d{4}-\d{2}-\d{2}).*?<td[^>]*>([^<]*)</td>',
            r.text, re.S
        )
        out = []
        for href, title, date, org in items[:limit]:
            title = _clean_text(title)
            if len(title) < 6:
                continue
            out.append({
                "title":    title[:120],
                "org":      _clean_text(org)[:20],
                "rating":   "",
                "date":     date[:10],
                "stock":    "",
                "code":     code,
                "industry": "",
                "url":      href if href.startswith("http") else f"https:{href}",
            })
        return out
    except Exception:
        return []


def get_latest_reports(qtype: int = 0, code: str = "", q: str = "", limit: int = 60) -> list:
    """最新研报流（5 分钟缓存+磁盘兜底）。

    来源合并: 东方财富(3页共240条) + 巨潮资讯(官方) + 同花顺 + 新浪财经。
    清洗: 标题+机构哈希去重、评级归一化、短标题剔除。
    """
    key = f"latest_reports:{qtype}:{code}"
    data = _cached(key, 300)
    if data is None:
        normalized: list[dict] = []
        em_got_any = False

        # ── 源1: 东方财富(最多3页, 每页80条) ──────────────
        for pg in range(1, 4):
            raw = _retry(lambda p=pg: _fetch_reportapi(qtype, code, page_no=p), attempts=2)
            if raw is None:
                break          # 请求失败，不继续翻页
            if not raw:
                break          # 空页 = 没有更多数据
            em_got_any = True
            normalized.extend(_norm_em_report(it) for it in raw)

        # ── 源2: 巨潮资讯（个股查询时补充官方披露研报）──────
        if qtype == 0 and code:
            cninfo = _retry(lambda: _fetch_cninfo_reports(code, 40), attempts=2) or []
            normalized.extend(cninfo)

        # ── 源3: 同花顺（可能需要 hexin-v，失败静默）────────
        if qtype == 0:
            normalized.extend(_fetch_ths_reports(code, limit=30))

        # ── 源4: 新浪财经（个股查询）──────────────────────
        if qtype == 0 and code:
            normalized.extend(_fetch_sina_reports(code, limit=30))

        if not em_got_any and not normalized:
            # 全部来源失败 → 磁盘兜底
            data = _disk_get(key) or []
            if data:
                logger.info(f"[research] 研报 qtype={qtype} 用磁盘兜底缓存")
        else:
            seen, data = set(), []
            for it in normalized:
                title = it.get("title", "")
                org   = it.get("org", "")
                if len(title) < 6:
                    continue
                h = hashlib.md5(f"{title}|{org}".encode()).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                data.append(it)
            data.sort(key=lambda x: x.get("date", ""), reverse=True)
            if data:
                _put_cache(key, data)
                _disk_put(key, data)

    if q:
        ql = q.lower()
        data = [d for d in data
                if ql in (d.get("title","") + d.get("stock","") + d.get("code","")
                          + d.get("industry","") + d.get("org","")).lower()]
    return data[:limit]


def resolve_code(q: str) -> str:
    """把输入（代码或名称）解析为6位代码。"""
    q = (q or "").strip()
    if re.fullmatch(r"\d{6}", q):
        return q
    try:
        from analytics import data_manager as dm
        dm._ensure_name_cache()
        for code, name in (dm._load_name_cache() or {}).items():
            if q and q in str(name):
                return code
    except Exception:
        pass
    return ""


# ──────────────────────────────────────────────────────────────
# 产业链图谱后台自动生成（用户浏览概念时无图谱 → 排队生成，无需手动点按钮）
# ──────────────────────────────────────────────────────────────
_autogen_lock = threading.Lock()
_autogen_pending: list = []       # FIFO 待生成队列
_autogen_inflight: set = set()    # 排队中 + 生成中（去重）
_autogen_failed: dict = {}        # name -> 上次失败时间戳（30分钟冷却防止死循环烧token）
_autogen_thread = None


def enqueue_chain_autogen(name: str) -> bool:
    """概念无图谱时排队后台生成。返回 True=已在生成队列（前端可提示'生成中'）。"""
    global _autogen_thread
    if not name or load_chain(name):
        return False
    try:
        from analytics import ai_analyst as aa
        if not aa.resolve_config().get("api_key"):
            return False              # 未配置 AI，静默跳过
    except Exception:
        return False
    with _autogen_lock:
        if name in _autogen_inflight:
            return True
        if time.time() - _autogen_failed.get(name, 0) < 1800:
            return False
        if len(_autogen_pending) >= 50:
            return False
        _autogen_pending.append(name)
        _autogen_inflight.add(name)
        if _autogen_thread is None or not _autogen_thread.is_alive():
            _autogen_thread = threading.Thread(
                target=_autogen_worker, daemon=True, name="chain-autogen")
            _autogen_thread.start()
    return True


def _autogen_worker():
    while True:
        with _autogen_lock:
            if not _autogen_pending:
                return                # 队列空，线程退出（下次入队再拉起）
            name = _autogen_pending.pop(0)
        ok = False
        try:
            r = generate_chain(name)
            ok = bool(r.get("ok"))
            if not ok:
                logger.warning(f"[chain-autogen] {name} 失败: {r.get('error', '')[:60]}")
        except Exception as e:
            logger.warning(f"[chain-autogen] {name} 异常: {e}")
        with _autogen_lock:
            _autogen_inflight.discard(name)
            if not ok:
                _autogen_failed[name] = time.time()
        if ok:
            logger.info(f"[chain-autogen] {name} 图谱生成完成")
        time.sleep(3)                 # 队列间隔，避免连环打满 AI 接口


# ──────────────────────────────────────────────────────────────
# 概念日更调度器
# ──────────────────────────────────────────────────────────────
def run_concept_scheduler():
    """每日 08:30 / 15:10 自动刷新产业概念列表并写盘，保证新出现的概念当天可见。

    逻辑：
    - 清内存缓存 → 重新拉取东财延迟集群 → 过滤交易信号类 → 写回缓存+磁盘
    - 两个时间点：开盘前（8:30 概念列表最新）和收盘后（15:10 收录收盘后新增）
    """
    import datetime as _dt

    def _refresh():
        try:
            with _cache_lock:
                _cache.pop("concepts", None)
                _cache.pop("ths_concepts", None)
            fresh = _retry(_em_direct_concepts, attempts=3)
            if fresh:
                fresh = [d for d in fresh if _is_industry_concept(d["name"])]
                _put_cache("concepts", fresh)
                _disk_put("concepts", fresh)
                logger.info(f"[research] 概念日更完成，产业概念 {len(fresh)} 个")
            else:
                logger.warning("[research] 概念日更：东财拉取失败，等待下次")
        except Exception as e:
            logger.warning(f"[research] 概念日更异常: {e}")

    def _worker():
        while True:
            now = _dt.datetime.now()
            targets = []
            for h, m in [(8, 30), (15, 10)]:
                t = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if t <= now:
                    t += _dt.timedelta(days=1)
                targets.append(t)
            sleep_secs = min((t - now).total_seconds() for t in targets)
            time.sleep(max(60, sleep_secs))
            _refresh()

    t = threading.Thread(target=_worker, daemon=True, name="concept-daily")
    t.start()
    logger.info("[research] 概念日更调度器已启动（每日 08:30 / 15:10 刷新）")
