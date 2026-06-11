"""财经新闻聚合中心(news_center)

多源抓取 → SQLite 去重入库 → /api/news/feed 查询。
源：财联社电报(主)、东方财富/同花顺/新浪快讯(互备)、新闻联播文字稿(政策面)。
云端 akshare 常被东财掐连接，因此：所有抓取单源失败静默跳过、结果永远先入本地库，
前端只读库——抓取抖动用户无感。

后台线程 run_news_scheduler()：
- 交易时段每 3 分钟增量拉快讯，盘后/周末降频到 15 分钟
- 每晚 19:40 后抓当天《新闻联播》文字稿（政策面复盘素材）
- 每天清理 14 天前的旧新闻
"""
import hashlib
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH  = BASE_DIR / "data" / "news.db"

# 命中即标"重要"的关键词（标题或正文前200字）
_IMPORTANT_KW = (
    "央行", "降准", "降息", "证监会", "国常会", "政治局", "发改委", "财政部",
    "国务院", "重磅", "立案", "利率", "印花税", "汇金", "国家队", "养老金",
    "美联储", "加息", "关税", "中美", "国资委", "证金", "减持新规", "IPO暂停",
)

_db_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=15)
    c.execute("""CREATE TABLE IF NOT EXISTS news(
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        hash      TEXT UNIQUE,
        ts        TEXT,
        source    TEXT,
        title     TEXT,
        content   TEXT DEFAULT '',
        url       TEXT DEFAULT '',
        important INTEGER DEFAULT 0,
        tags      TEXT DEFAULT ''
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts DESC)")
    return c


# ──────────────────────────────────────────────────────────────
# 抓取辅助
# ──────────────────────────────────────────────────────────────
def _col(df, *cands):
    """akshare 不同版本列名常变，模糊匹配列名。"""
    for cand in cands:
        if cand in df.columns:
            return cand
    for c in df.columns:
        for cand in cands:
            if cand in str(c):
                return c
    return None


def _norm_ts(val, fallback_date: str = "") -> str:
    """归一化时间到 'YYYY-MM-DD HH:MM:SS'。"""
    s = str(val or "").strip()
    if not s:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    s = s.replace("T", " ").replace("/", "-")[:19]
    if len(s) <= 8 and ":" in s:                       # 只有时分秒
        day = fallback_date or datetime.now().strftime("%Y-%m-%d")
        s = f"{day} {s}"
    if len(s) == 10:                                   # 只有日期
        s = f"{s} 00:00:00"
    return s


def _mk_item(source: str, ts, title: str, content: str = "", url: str = "") -> dict | None:
    title   = str(title or "").strip()
    content = str(content or "").strip()
    if not title and not content:
        return None
    if not title:
        title = content[:60]
    if content == title:
        content = ""
    blob = (title + content[:200])
    important = 1 if any(k in blob for k in _IMPORTANT_KW) else 0
    # 跨源去重：标题前80字哈希（同一条新闻多平台转发只入一次）
    h = hashlib.md5(title[:80].encode("utf-8", errors="ignore")).hexdigest()
    return {
        "hash": h, "ts": _norm_ts(ts), "source": source,
        "title": title[:300], "content": content[:2000],
        "url": str(url or ""), "important": important,
    }


def _insert(items: list) -> int:
    items = [i for i in items if i]
    if not items:
        return 0
    with _db_lock:
        c = _conn()
        try:
            n0 = c.execute("SELECT COUNT(*) FROM news").fetchone()[0]
            c.executemany(
                "INSERT OR IGNORE INTO news(hash,ts,source,title,content,url,important) "
                "VALUES(:hash,:ts,:source,:title,:content,:url,:important)", items)
            c.commit()
            return c.execute("SELECT COUNT(*) FROM news").fetchone()[0] - n0
        finally:
            c.close()


# ──────────────────────────────────────────────────────────────
# 各数据源抓取器（单源失败静默返回空）
# ──────────────────────────────────────────────────────────────
def _fetch_cls() -> list:
    """财联社电报——A股快讯主源。"""
    import akshare as ak
    df = ak.stock_info_global_cls(symbol="全部")
    if df is None or df.empty:
        return []
    tc, cc = _col(df, "标题"), _col(df, "内容")
    dc, hc = _col(df, "发布日期"), _col(df, "发布时间")
    out = []
    for _, r in df.iterrows():
        ts = f"{r[dc]} {r[hc]}" if dc and hc else (r[dc] if dc else "")
        out.append(_mk_item("财联社", ts, r[tc] if tc else "", r[cc] if cc else ""))
    return out


def _fetch_em() -> list:
    """东方财富全球财经快讯。"""
    import akshare as ak
    df = ak.stock_info_global_em()
    if df is None or df.empty:
        return []
    tc, cc = _col(df, "标题"), _col(df, "摘要", "内容")
    hc, uc = _col(df, "发布时间", "时间"), _col(df, "链接")
    return [_mk_item("东方财富", r[hc] if hc else "", r[tc] if tc else "",
                     r[cc] if cc else "", r[uc] if uc else "")
            for _, r in df.iterrows()]


def _fetch_ths() -> list:
    """同花顺财经直播。"""
    import akshare as ak
    df = ak.stock_info_global_ths()
    if df is None or df.empty:
        return []
    tc, cc = _col(df, "标题"), _col(df, "内容")
    hc, uc = _col(df, "发布时间", "时间"), _col(df, "链接")
    return [_mk_item("同花顺", r[hc] if hc else "", r[tc] if tc else "",
                     r[cc] if cc else "", r[uc] if uc else "")
            for _, r in df.iterrows()]


def _fetch_sina() -> list:
    """新浪财经全球快讯（兜底源）。"""
    import akshare as ak
    df = ak.stock_info_global_sina()
    if df is None or df.empty:
        return []
    hc, cc = _col(df, "时间"), _col(df, "内容")
    return [_mk_item("新浪财经", r[hc] if hc else "", "", r[cc] if cc else "")
            for _, r in df.iterrows()]


def _fetch_futu() -> list:
    """富途牛牛快讯。"""
    import akshare as ak
    df = ak.stock_info_global_futu()
    if df is None or df.empty:
        return []
    tc, cc = _col(df, "标题"), _col(df, "内容", "摘要")
    hc, uc = _col(df, "发布时间", "时间"), _col(df, "链接")
    return [_mk_item("富途牛牛", r[hc] if hc else "", r[tc] if tc else "",
                     r[cc] if cc else "", r[uc] if uc else "")
            for _, r in df.iterrows()]


def _fetch_cx() -> list:
    """财新网财经新闻。"""
    import akshare as ak
    df = ak.stock_news_main_cx()
    if df is None or df.empty:
        return []
    tc = _col(df, "tag", "标题")
    cc = _col(df, "summary", "摘要", "内容")
    hc = _col(df, "pub_time", "发布时间", "时间")
    uc = _col(df, "url", "链接")
    return [_mk_item("财新网", r[hc] if hc else "", r[tc] if tc else "",
                     r[cc] if cc else "", r[uc] if uc else "")
            for _, r in df.iterrows()]


def _fetch_cjzc() -> list:
    """东方财富财经早餐（每日盘前要点汇总）。"""
    import akshare as ak
    df = ak.stock_info_cjzc_em()
    if df is None or df.empty:
        return []
    tc, cc = _col(df, "标题"), _col(df, "摘要", "内容")
    hc, uc = _col(df, "发布时间", "时间"), _col(df, "链接")
    return [_mk_item("财经早餐", r[hc] if hc else "", r[tc] if tc else "",
                     r[cc] if cc else "", r[uc] if uc else "")
            for _, r in df.iterrows()]


def _fetch_wscn() -> list:
    """华尔街见闻快讯（直连官方接口，akshare 无此源）。"""
    import requests
    r = requests.get(
        "https://api-one.wallstcn.com/apiv1/content/lives",
        params={"channel": "global-channel", "client": "pc", "limit": 50},
        timeout=10,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    items = ((r.json() or {}).get("data") or {}).get("items") or []
    out = []
    for it in items:
        content = (it.get("content_text") or "").strip()
        ts = datetime.fromtimestamp(it.get("display_time", 0)).strftime("%Y-%m-%d %H:%M:%S") \
            if it.get("display_time") else ""
        out.append(_mk_item("华尔街见闻", ts, it.get("title") or "", content, it.get("uri") or ""))
    return out


def _fetch_cctv(day: str = None) -> list:
    """新闻联播文字稿（19:40 后可取当天）。day: YYYYMMDD"""
    import akshare as ak
    day = day or datetime.now().strftime("%Y%m%d")
    df = ak.news_cctv(date=day)
    if df is None or df.empty:
        return []
    tc, cc = _col(df, "title", "标题"), _col(df, "content", "内容")
    ts = f"{day[:4]}-{day[4:6]}-{day[6:]} 19:30:00"
    return [_mk_item("新闻联播", ts, r[tc] if tc else "", r[cc] if cc else "")
            for _, r in df.iterrows()]


# 金十/财联社接口在 akshare 1.18.62 不可用（js_news 不存在、cls 返回404），暂不接入
_QUICK_SOURCES = (("东方财富", _fetch_em), ("同花顺", _fetch_ths),
                  ("新浪财经", _fetch_sina), ("富途牛牛", _fetch_futu),
                  ("财新网", _fetch_cx), ("财经早餐", _fetch_cjzc),
                  ("华尔街见闻", _fetch_wscn), ("财联社", _fetch_cls))

_last_fetch_ts = 0.0
_fetch_lock    = threading.Lock()


def fetch_quick() -> int:
    """拉一轮快讯（全部源并发直连，单源失败跳过）。返回新入库条数。"""
    global _last_fetch_ts
    _last_fetch_ts = time.time()
    from concurrent.futures import ThreadPoolExecutor
    try:
        from research_center import no_proxy
    except Exception:
        import contextlib
        no_proxy = contextlib.nullcontext
    total = 0
    # 全部为国内源，绕过系统代理直连更稳（见 research_center.no_proxy）
    with no_proxy(), ThreadPoolExecutor(max_workers=5, thread_name_prefix="news-src") as pool:
        futs = {pool.submit(fn): name for name, fn in _QUICK_SOURCES}
        for fut, name in futs.items():
            try:
                total += _insert(fut.result(timeout=30))
            except Exception as e:
                logger.debug(f"[news] {name} 抓取失败: {e}")
    if total:
        logger.info(f"[news] 快讯入库 {total} 条")
    return total


def maybe_fetch_async(min_gap: int = 60):
    """前端访问触发的增量抓取（限频 + 去重并发），保证打开页面就是准实时数据。"""
    global _last_fetch_ts
    with _fetch_lock:
        if time.time() - _last_fetch_ts < min_gap:
            return
        _last_fetch_ts = time.time()      # 先占位防并发重复触发
    threading.Thread(target=fetch_quick, daemon=True, name="news-on-demand").start()


def fetch_evening() -> int:
    """晚间素材：新闻联播文字稿。"""
    try:
        n = _insert(_fetch_cctv())
        if n:
            logger.info(f"[news] 新闻联播入库 {n} 条")
        return n
    except Exception as e:
        logger.debug(f"[news] 新闻联播抓取失败: {e}")
        return 0


def purge_old(days: int = 14):
    with _db_lock:
        c = _conn()
        try:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            c.execute("DELETE FROM news WHERE ts < ?", (cutoff,))
            c.commit()
        finally:
            c.close()


# ──────────────────────────────────────────────────────────────
# 查询（前端唯一读取入口）
# ──────────────────────────────────────────────────────────────
def query(source: str = "", q: str = "", important: bool = False,
          since: str = "", limit: int = 80, offset: int = 0) -> list:
    sql, args = "SELECT ts,source,title,content,url,important FROM news WHERE 1=1", []
    if source:
        sql += " AND source=?";            args.append(source)
    if important:
        sql += " AND important=1"
    if since:
        sql += " AND ts>=?";               args.append(since)
    if q:
        sql += " AND (title LIKE ? OR content LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY ts DESC LIMIT ? OFFSET ?"
    args += [min(int(limit), 200), max(int(offset), 0)]
    with _db_lock:
        c = _conn()
        try:
            rows = c.execute(sql, args).fetchall()
        finally:
            c.close()
    return [{"ts": r[0], "source": r[1], "title": r[2],
             "content": r[3], "url": r[4], "important": bool(r[5])} for r in rows]


def list_sources() -> list:
    with _db_lock:
        c = _conn()
        try:
            rows = c.execute("SELECT source, COUNT(*) FROM news GROUP BY source").fetchall()
        finally:
            c.close()
    return [{"source": r[0], "count": r[1]} for r in rows]


def important_digest(day: str, limit: int = 40) -> list:
    """某日重要新闻标题列表（喂给 AI 复盘）。day: YYYY-MM-DD"""
    items = query(important=True, since=f"{day} 00:00:00", limit=limit)
    items = [i for i in items if i["ts"][:10] == day]
    if len(items) < 10:    # 重要新闻太少时补普通新闻
        extra = [i for i in query(since=f"{day} 00:00:00", limit=limit)
                 if i["ts"][:10] == day and not i["important"]]
        items += extra[: limit - len(items)]
    return items


# ──────────────────────────────────────────────────────────────
# 后台调度线程
# ──────────────────────────────────────────────────────────────
def run_news_scheduler():
    logger.info("[news] 新闻聚合调度器启动")
    last_quick = 0.0
    last_evening_day = ""
    last_purge_day = ""
    while True:
        try:
            now   = datetime.now()
            today = now.strftime("%Y-%m-%d")
            hm    = now.hour * 100 + now.minute
            in_session = now.weekday() < 5 and 855 <= hm <= 1530
            interval   = 60 if in_session else 300   # 盘中1分钟,盘后5分钟（准实时）
            if time.time() - last_quick >= interval:
                last_quick = time.time()
                fetch_quick()
            # 19:40 后抓当天新闻联播（每天一次）
            if hm >= 1940 and last_evening_day != today:
                last_evening_day = today
                fetch_evening()
            if last_purge_day != today and hm >= 300:
                last_purge_day = today
                purge_old()
        except Exception as e:
            logger.warning(f"[news] 调度异常: {e}")
        time.sleep(30)
