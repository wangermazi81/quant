"""龙虎榜数据引擎：每日数据抓取 / 游资识别 / 游资图谱构建。

数据链路：
  东财 datacenter-web API（主）→ akshare 备源 → 磁盘缓存（兜底）

游资识别：内置约 150 条主要营业部→游资标签映射，模糊匹配。
缓存策略：盘中（<15:30）5 分钟；盘后当天缓存；历史数据永久磁盘存储。
"""
import json
import logging
import re
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
LHB_DIR  = BASE_DIR / "data" / "lhb"
LHB_DIR.mkdir(parents=True, exist_ok=True)

# ─── 公用工具（复用 research_center 的代理补丁 / 清洗函数）────────────
from research_center import no_proxy, _clean_text, _num


# ══════════════════════════════════════════════════════════════
# 游资营业部标签库
# ══════════════════════════════════════════════════════════════
YOUZAI_TAGS: dict[str, str] = {
    # ── 浙系 ──────────────────────────────────────────────────
    "中信证券杭州延安路":       "章盟主",
    "中信证券杭州钱江新城":     "章盟主",
    "中信证券杭州武林广场":     "章盟主",
    "光大证券金华宾虹路":       "江南神鹰/落升",
    "光大证券金华义乌":         "江南神鹰/落升",
    "光大证券金华":             "江南神鹰/落升",
    "浙商证券绍兴解放北路":     "绍兴游资",
    "浙商证券杭州解放路":       "杭帮",
    "国泰君安杭州城站":         "杭帮",
    "东方财富证券杭州":         "杭帮",
    "中信建投证券杭州":         "杭帮",
    "国信证券杭州":             "杭帮",
    "华泰证券杭州营业部":       "杭帮",
    # ── 苏南系 ────────────────────────────────────────────────
    "华泰证券武进":             "苏南帮",
    "华泰证券苏州工业园区":     "苏南帮",
    "华泰证券常州":             "苏南帮",
    "华泰证券苏州":             "苏南帮",
    "国泰君安淮安淮海路":       "苏南帮",
    "光大证券苏州观前":         "苏南帮",
    "银河证券北京学院南路":     "苏南帮",
    "国泰君安苏州观前":         "苏南帮",
    "国信证券苏州":             "苏南帮",
    "广发证券苏州":             "苏南帮",
    "中泰证券苏州":             "苏南帮",
    # ── 湖北游资 ──────────────────────────────────────────────
    "海通证券武汉紫阳东路":     "紫阳东路",
    "海通证券武汉武昌南湖":     "南湖游资",
    "海通证券武汉珞狮路":       "武汉游资",
    "招商证券武汉营业部":       "武汉游资",
    "国泰君安武汉营业部":       "武汉游资",
    "华泰证券武汉营业部":       "武汉游资",
    "中信证券武汉营业部":       "武汉游资",
    # ── 上海系 ────────────────────────────────────────────────
    "华泰证券上海武定路":       "炒新一族",
    "华泰证券上海中山南路":     "炒新一族",
    "华泰证券上海营业部":       "炒新一族",
    "申万宏源上海华山路":       "上海游资",
    "国泰君安上海营业部":       "上海游资",
    "国泰君安上海分公司":       "上海游资",
    "国泰君安上海浦建路":       "上海游资",
    "中信证券上海营业部":       "上海游资",
    "东方财富证券上海":         "上海游资",
    "申万宏源上海营业部":       "上海游资",
    # ── 深圳/广东系 ───────────────────────────────────────────
    "亚前海证券深圳分公司":     "粉葛",
    "华林证券深圳福田":         "粉葛",
    "中信证券深圳益田路":       "深圳游资",
    "广发证券深圳益田路":       "深圳游资",
    "平安证券深圳益田路":       "深圳游资",
    "华鑫证券深圳分公司":       "深圳华鑫",
    "招商证券深圳福田":         "深圳游资",
    "民生证券深圳深南大道":     "深圳游资",
    "国信证券深圳营业部":       "深圳游资",
    "广发证券深圳振兴路":       "T王",
    "广发证券深圳深南中路":     "T王",
    "广发证券深圳车公庙":       "T王",
    "广发证券深圳":             "T王",
    "国信证券佛山千灯湖":       "佛山游资",
    "广发证券广州天河":         "广州游资",
    "国金证券广州":             "广州游资",
    "招商证券广州":             "广州游资",
    "华泰证券广州营业部":       "广州游资",
    # ── 南京/江苏 ─────────────────────────────────────────────
    "东莞证券南京分公司":       "玉兰路",
    "华泰证券南京营业部":       "南京游资",
    "华泰证券南京新街口":       "南京游资",
    "国信证券南京营业部":       "南京游资",
    "中信证券南京营业部":       "南京游资",
    "招商证券南京营业部":       "南京游资",
    # ── 湖州系 ────────────────────────────────────────────────
    "华鑫证券湖州劳动路":       "湖州劳动路",
    "国信证券湖州":             "湖州游资",
    "招商证券湖州":             "湖州游资",
    "华泰证券湖州":             "湖州游资",
    # ── 安徽系 ────────────────────────────────────────────────
    "国泰君安合肥长江路":       "合肥游资",
    "中信证券合肥营业部":       "合肥游资",
    "方正证券合肥营业部":       "合肥游资",
    "华泰证券合肥营业部":       "合肥游资",
    "申万宏源合肥营业部":       "合肥游资",
    # ── 北京系 ────────────────────────────────────────────────
    "中信证券北京双井路":       "北京游资",
    "国信证券北京航天桥":       "北京游资",
    "国泰君安北京营业部":       "北京游资",
    "申万宏源北京营业部":       "北京游资",
    "华泰证券北京营业部":       "北京游资",
    # ── 山东系 ────────────────────────────────────────────────
    "兴业证券青岛营业部":       "青岛游资",
    "东方证券青岛营业部":       "青岛游资",
    "华泰证券济南营业部":       "济南游资",
    "中信证券济南营业部":       "济南游资",
    # ── 其他 ──────────────────────────────────────────────────
    "华鑫证券沈阳营业部":       "东北游资",
    "国信证券成都营业部":       "成都游资",
    "华泰证券成都营业部":       "成都游资",
    "招商证券重庆营业部":       "重庆游资",
    "国泰君安重庆营业部":       "重庆游资",
}


_COMPANY_NOISE_RE = re.compile(
    r"股份有限公司|有限责任公司|有限公司|证券营业部|营业部|证券交易|分公司|分支机构")


def _norm_branch(branch: str) -> str:
    """营业部全称 → 归一化短名。

    东财返回全称如"国泰海通证券股份有限公司武汉紫阳东路证券营业部"，
    去掉公司后缀噪声词后变成"国泰海通证券武汉紫阳东路"，才能跟标签库短键匹配。
    """
    return _COMPANY_NOISE_RE.sub("", branch or "").strip()


def _tag_branch(branch: str) -> str:
    """营业部名称 → 游资标签（精确匹配 → 归一化包含匹配）。"""
    if not branch:
        return ""
    if branch in YOUZAI_TAGS:
        return YOUZAI_TAGS[branch]
    nb = _norm_branch(branch)
    for k, v in YOUZAI_TAGS.items():
        nk = _norm_branch(k)
        if len(nk) >= 6 and (nk in nb or nb in nk):
            return v
    return ""


# ══════════════════════════════════════════════════════════════
# 缓存
# ══════════════════════════════════════════════════════════════
_cache: dict = {}
_lock = threading.Lock()
LHB_FINALIZE_HM = 1845


def _disk_path(yyyymmdd: str) -> Path:
    return LHB_DIR / f"lhb_{yyyymmdd}.json"


def _disk_get(yyyymmdd: str):
    try:
        p = _disk_path(yyyymmdd)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _disk_put(yyyymmdd: str, data: dict):
    try:
        _disk_path(yyyymmdd).write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _seat_detail_count(data: dict) -> int:
    stocks = data.get("stocks") if isinstance(data, dict) else None
    if not isinstance(stocks, list):
        return 0
    return sum(1 for s in stocks if isinstance(s, dict) and s.get("branches"))


def _lhb_payload_complete(data: dict) -> bool:
    stocks = data.get("stocks") if isinstance(data, dict) else None
    if not data.get("ok") or not isinstance(stocks, list) or not stocks:
        return False
    # 龙虎榜发布时可能先出股票列表、席位明细稍后才可查。
    # 自动预加载只有等到至少一只股票带席位后才算完成，避免缓存半成品图谱。
    return _seat_detail_count(data) > 0


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _lhb_cache_ready(data: dict) -> bool:
    return _lhb_payload_complete(data) and data.get("finalized") is not False


def _lhb_today_finalized(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    hm = now.hour * 100 + now.minute
    return hm >= LHB_FINALIZE_HM


def _cache_ttl(cached: dict) -> int:
    if cached.get("finalized") is True:
        return 86400
    if cached.get("complete"):
        return 60
    return 300 if datetime.now().hour < 16 else 60


def _fmt(yyyymmdd: str) -> str:
    d = yyyymmdd.replace("-", "")
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


# ══════════════════════════════════════════════════════════════
# 数据抓取
# ══════════════════════════════════════════════════════════════

def _merge_stock_rows(rows: list) -> list:
    merged: dict = {}
    for it in rows:
        code = it["code"]
        cur = merged.get(code)
        if cur is None:
            merged[code] = it
            continue
        main, extra = ((cur, it) if abs(cur.get("net_amt") or 0) >= abs(it.get("net_amt") or 0)
                       else (it, cur))
        rs, er = main.get("reason", ""), extra.get("reason", "")
        if er and er not in rs:
            main["reason"] = (rs + " / " + er)[:60] if rs else er
        merged[code] = main
    out = list(merged.values())
    out.sort(key=lambda x: x.get("net_amt") or -9e9, reverse=True)
    return out


def _fetch_lhb_stocks_eastmoney(yyyymmdd: str) -> list:
    import requests

    date_str = _fmt(yyyymmdd)
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://data.eastmoney.com/stock/tradedetail.html",
        "Accept": "application/json",
    }
    params = {
        "sortColumns": "SECURITY_CODE,TRADE_DATE",
        "sortTypes": "1,-1",
        "pageSize": "5000",
        "pageNumber": "1",
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": (
            "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,EXPLAIN,CLOSE_PRICE,CHANGE_RATE,"
            "BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,BILLBOARD_DEAL_AMT,"
            "ACCUM_AMOUNT,TURNOVERRATE,EXPLANATION"
        ),
        "source": "WEB",
        "client": "WEB",
        "filter": f"(TRADE_DATE<='{date_str}')(TRADE_DATE>='{date_str}')",
    }

    try:
        with no_proxy():
            r = requests.get(
                "https://datacenter-web.eastmoney.com/api/data/v1/get",
                params=params,
                headers=hdrs,
                timeout=10,
            )
        result = (r.json() or {}).get("result") or {}
        pages = int(result.get("pages") or 0)
        if pages <= 0:
            return []
        data_rows = list(result.get("data") or [])
        for page in range(2, pages + 1):
            params["pageNumber"] = str(page)
            with no_proxy():
                r = requests.get(
                    "https://datacenter-web.eastmoney.com/api/data/v1/get",
                    params=params,
                    headers=hdrs,
                    timeout=10,
                )
            data_rows.extend(((r.json() or {}).get("result") or {}).get("data") or [])

        out = []
        for d in data_rows:
            code = str(d.get("SECURITY_CODE") or "").zfill(6)
            name = _clean_text(d.get("SECURITY_NAME_ABBR", ""))
            if not code.isdigit() or not name:
                continue
            net = _num(d.get("BILLBOARD_NET_AMT"))
            out.append({
                "code": code,
                "name": name,
                "chg": _num(d.get("CHANGE_RATE")),
                "net_amt": round(net / 10000, 2) if net else None,
                "buy_amt": round((_num(d.get("BILLBOARD_BUY_AMT")) or 0) / 10000, 2),
                "sell_amt": round((_num(d.get("BILLBOARD_SELL_AMT")) or 0) / 10000, 2),
                "close": _num(d.get("CLOSE_PRICE")),
                "turnover": _num(d.get("TURNOVERRATE")),
                "reason": _clean_text(d.get("EXPLANATION") or d.get("EXPLAIN") or "")[:30],
                "vol": round((_num(d.get("BILLBOARD_DEAL_AMT")) or 0) / 1e8, 2),
                "youzai": [],
                "branches": [],
            })
        return _merge_stock_rows(out)
    except Exception as e:
        logger.warning(f"[lhb] eastmoney stock list {yyyymmdd} failed: {e}")
        return []


def _fetch_lhb_stocks(yyyymmdd: str) -> list:
    """akshare 获取当日龙虎榜股票列表（带基本数据）。"""
    try:
        with no_proxy():
            import akshare as ak
            # akshare ≥1.18: start_date/end_date 参数（旧版 date= 已废弃）
            df = ak.stock_lhb_detail_em(start_date=yyyymmdd, end_date=yyyymmdd)
        if df is None or df.empty:
            return _fetch_lhb_stocks_eastmoney(yyyymmdd)

        def _col(df, *names):
            for n in names:
                if n in df.columns:
                    return n
            for c in df.columns:
                for n in names:
                    if n in str(c):
                        return c
            return None

        code_c   = _col(df, "代码", "code", "股票代码")
        name_c   = _col(df, "名称", "name", "股票名称")
        chg_c    = _col(df, "涨跌幅", "chg", "涨跌幅(%)")
        # akshare 1.18.x 列名带"汇总"后缀，兼容旧版
        net_c    = _col(df, "净额汇总", "净额", "净买额", "净买入额", "龙虎榜净买额")
        buy_c    = _col(df, "买入额汇总", "买入额", "买入总额")
        sell_c   = _col(df, "卖出额汇总", "卖出额", "卖出总额")
        close_c  = _col(df, "收盘价", "最新价")
        turn_c   = _col(df, "换手率")
        reason_c = _col(df, "上榜原因", "上榜理由", "原因")
        amt_c    = _col(df, "成交额", "成交金额")

        out = []
        for _, r in df.iterrows():
            code = str(r[code_c] if code_c else "").zfill(6)
            name = _clean_text(r[name_c]) if name_c else ""
            if not code.isdigit() or not name:
                continue
            net = _num(r[net_c]) if net_c else None
            # net_amt 单位：万元
            out.append({
                "code":    code,
                "name":    name,
                "chg":     _num(r[chg_c]) if chg_c else None,
                "net_amt": round(net / 10000, 2) if net else None,
                "buy_amt": round((_num(r[buy_c]) or 0) / 10000, 2) if buy_c else None,
                "sell_amt":round((_num(r[sell_c]) or 0) / 10000, 2) if sell_c else None,
                "close":   _num(r[close_c]) if close_c else None,
                "turnover":_num(r[turn_c]) if turn_c else None,
                "reason":  _clean_text(r[reason_c])[:30] if reason_c else "",
                "vol":     round((_num(r[amt_c]) or 0) / 1e8, 2) if amt_c else None,
                "youzai":  [],
                "branches":[]
            })
        # 同一股票可能因多个"上榜原因"被东财列成多行（如"连续三日偏离"+"日涨幅偏离7%"）
        # → 按代码去重：保留净额绝对值最大的主榜行，把其余原因合并进来，
        #   避免榜单重复显示同一股、图谱出现重复节点和重复连线。
        merged: dict = {}
        for it in out:
            code = it["code"]
            cur = merged.get(code)
            if cur is None:
                merged[code] = it
                continue
            main, extra = ((cur, it) if abs(cur.get("net_amt") or 0) >= abs(it.get("net_amt") or 0)
                           else (it, cur))
            rs, er = main.get("reason", ""), extra.get("reason", "")
            if er and er not in rs:
                main["reason"] = (rs + " / " + er)[:60] if rs else er
            merged[code] = main
        return _merge_stock_rows(out)
    except Exception as e:
        logger.warning(f"[lhb] 股票列表 {yyyymmdd} 失败: {e}")
        return _fetch_lhb_stocks_eastmoney(yyyymmdd)


def _fetch_seat_detail(code: str, date_str: str) -> list:
    """东财席位明细 API：一只股票当日所有营业部买卖数据。

    主源：datacenter-web RPT_BILLBOARD_DAILYDETAILSBUY/SELL（买卖榜分开拉再合并）
    备源：akshare stock_lhb_stock_detail_em(flag=买入/卖出)
    """
    import requests
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://data.eastmoney.com/stock/lhb.html",
        "Accept": "application/json",
    }

    # ── 方法1: datacenter-web 买/卖榜明细（实测有效的报表名）──────
    seats: dict = {}   # name -> {name,buy,sell,net,dir}
    for report_name, side in (("RPT_BILLBOARD_DAILYDETAILSBUY",  "B"),
                              ("RPT_BILLBOARD_DAILYDETAILSSELL", "S")):
        try:
            r = requests.get(
                "https://datacenter-web.eastmoney.com/api/data/v1/get",
                params={
                    "reportName": report_name,
                    "columns": "ALL",
                    "filter":  f'(SECURITY_CODE="{code}")(TRADE_DATE=\'{date_str}\')',
                    "pageNumber": 1, "pageSize": 50,
                    "sortTypes": -1,
                    "sortColumns": "BUY" if side == "B" else "SELL",
                    "source": "WEB", "client": "WEB",
                },
                headers=hdrs, timeout=8,
            )
            data = ((r.json() or {}).get("result") or {}).get("data") or []
            for d in data:
                nm = _clean_text(d.get("OPERATEDEPT_NAME", ""))
                if not nm:
                    continue
                buy  = (_num(d.get("BUY"))  or 0) / 10000
                sell = (_num(d.get("SELL")) or 0) / 10000
                ent = seats.setdefault(nm, {"name": nm, "buy": 0.0, "sell": 0.0,
                                            "net": 0.0, "dir": side})
                # 同一营业部可能同时在买/卖榜出现，字段值相同，取最大即可
                ent["buy"]  = max(ent["buy"], buy)
                ent["sell"] = max(ent["sell"], sell)
                ent["net"]  = ent["buy"] - ent["sell"]
                ent["dir"]  = "B" if ent["buy"] >= ent["sell"] else "S"
        except Exception:
            pass
    if seats:
        return sorted(seats.values(), key=lambda x: x["net"], reverse=True)

    # ── 方法2: akshare 个股席位明细（买入榜+卖出榜）──────────────
    try:
        with no_proxy():
            import akshare as ak
            ymd = date_str.replace("-", "")
            for flag in ("买入", "卖出"):
                try:
                    df = ak.stock_lhb_stock_detail_em(symbol=code, date=ymd, flag=flag)
                except Exception:
                    continue
                if df is None or df.empty:
                    continue
                for _, r in df.iterrows():
                    nm = _clean_text(r.get("交易营业部名称", r.get("营业部名称", "")))
                    if not nm:
                        continue
                    b = (_num(r.get("买入金额", 0)) or 0) / 10000
                    s = (_num(r.get("卖出金额", 0)) or 0) / 10000
                    ent = seats.setdefault(nm, {"name": nm, "buy": 0.0, "sell": 0.0,
                                                "net": 0.0, "dir": "B"})
                    ent["buy"]  = max(ent["buy"], b)
                    ent["sell"] = max(ent["sell"], s)
                    ent["net"]  = ent["buy"] - ent["sell"]
                    ent["dir"]  = "B" if ent["buy"] >= ent["sell"] else "S"
        if seats:
            return sorted(seats.values(), key=lambda x: x["net"], reverse=True)
    except Exception:
        pass

    return []


# ══════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════

def get_lhb_data(date: str = None, force_refresh: bool = False) -> dict:
    """获取龙虎榜全量数据（股票 + 席位 + 游资图谱标注）。

    date: YYYY-MM-DD 或 YYYYMMDD，默认今日。
    返回结构::

        {ok, date, stocks:[{code,name,chg,net_amt,buy_amt,sell_amt,reason,
                            youzai:[str], branches:[{name,buy,sell,net,tag}]}],
         youzai_stat:[{name,stocks:[str],total_net}],
         total}
    """
    if not date:
        yyyymmdd = _today()
    else:
        yyyymmdd = date.replace("-", "")
    date_str = _fmt(yyyymmdd)
    is_today = (yyyymmdd == _today())

    # 历史数据直读磁盘
    if not is_today and not force_refresh:
        disk = _disk_get(yyyymmdd)
        if _lhb_cache_ready(disk or {}):
            return disk

    # 内存缓存（盘中 5 分钟刷新；盘后不过期）
    if not force_refresh:
        with _lock:
            hit = _cache.get(yyyymmdd)
            if hit:
                ts, cached = hit
                ttl = _cache_ttl(cached)
                if time.time() - ts < ttl:
                    return cached

    # ── 1. 股票列表 ─────────────────────────────────────────
    stocks = _fetch_lhb_stocks(yyyymmdd)
    if not stocks:
        if not force_refresh:
            disk = _disk_get(yyyymmdd)
            if disk and (is_today or _lhb_cache_ready(disk or {})):
                logger.info(f"[lhb] {date_str} 用磁盘缓存")
                return disk
        return {"ok": False, "date": date_str, "stocks": [],
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "error": "龙虎榜数据暂未发布或接口暂时拉取失败，请15:30后重试"}

    # ── 2. 席位明细（取前40只，避免过多请求）────────────────
    top_n = min(40, len(stocks))
    for i, s in enumerate(stocks[:top_n]):
        try:
            with no_proxy():
                branches = _fetch_seat_detail(s["code"], date_str)
            for b in branches:
                b["tag"] = _tag_branch(b["name"])
            s["branches"] = branches
            buy_branches = [b for b in branches if b.get("buy", 0) > b.get("sell", 0)]
            s["youzai"] = list({b["tag"] for b in buy_branches if b["tag"]})
        except Exception as e:
            logger.debug(f"[lhb] 席位 {s['code']} 失败: {e}")
        if i < top_n - 1:
            time.sleep(0.05)

    # ── 3. 游资活跃统计 ──────────────────────────────────────
    yz_stat: dict = {}
    for s in stocks:
        for yz in s.get("youzai", []):
            if not yz:
                continue
            yz_stat.setdefault(yz, {"name": yz, "stocks": [], "total_net": 0.0})
            yz_stat[yz]["stocks"].append(s["name"])
            yz_stat[yz]["total_net"] += round(s.get("net_amt") or 0, 2)
    youzai_stat = sorted(yz_stat.values(), key=lambda x: x["total_net"], reverse=True)

    out = {
        "ok":          True,
        "date":        date_str,
        "stocks":      stocks,
        "youzai_stat": youzai_stat,
        "total":       len(stocks),
        "seat_detail_count": _seat_detail_count({"stocks": stocks}),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source":       "eastmoney_akshare",
    }
    out["complete"] = _lhb_payload_complete(out)
    out["finalized"] = (not is_today) or _lhb_today_finalized()
    out["status"] = "final" if out["finalized"] else "updating"

    with _lock:
        _cache[yyyymmdd] = (time.time(), out)
    if out["complete"]:
        _disk_put(yyyymmdd, out)

    logger.info(f"[lhb] {date_str} 龙虎榜 {len(stocks)} 只，游资 {len(youzai_stat)} 组")
    return out


def get_lhb_dates(limit: int = 30) -> list:
    """本地缓存的历史龙虎榜日期（最近 N 天，含今日）。"""
    dates = []
    try:
        for p in sorted(LHB_DIR.glob("lhb_*.json"), reverse=True):
            d = p.stem.replace("lhb_", "")
            if len(d) == 8 and d.isdigit():
                disk = _disk_get(d)
                if _lhb_cache_ready(disk or {}):
                    dates.append(_fmt(d))
            if len(dates) >= limit:
                break
    except Exception:
        pass
    today = _fmt(_today())
    if today not in dates:
        dates.insert(0, today)
    return dates[:limit]


def _recent_weekdays(today: datetime | None = None, days: int = 10) -> list[str]:
    today = today or datetime.now()
    out = []
    cur = today
    while len(out) < days:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
        cur -= timedelta(days=1)
    return out


def backfill_recent(
    today: datetime | None = None,
    days: int = 5,
    limit: int = 2,
    include_today: bool = False,
) -> dict:
    """补齐最近工作日缺失的龙虎榜完整缓存。

    调度器用于处理服务重启、东财接口短时失败、席位明细延迟发布等情况。
    只把带席位明细的完整数据视为 saved，避免把半成品长期落盘。
    """
    checked: list[str] = []
    saved: list[str] = []
    pending: list[str] = []
    skipped: list[str] = []
    attempts = 0

    today_dt = today or datetime.now()
    today_str = today_dt.strftime("%Y-%m-%d")
    for date_str in _recent_weekdays(today=today_dt, days=days):
        if not include_today and date_str == today_str:
            skipped.append(date_str)
            continue
        yyyymmdd = date_str.replace("-", "")
        disk = _disk_get(yyyymmdd)
        if _lhb_cache_ready(disk or {}):
            skipped.append(date_str)
            continue
        if attempts >= limit:
            break
        attempts += 1
        checked.append(date_str)
        try:
            data = get_lhb_data(date_str, force_refresh=True)
            if _lhb_cache_ready(data):
                saved.append(date_str)
            else:
                pending.append(date_str)
        except Exception as e:
            pending.append(date_str)
            logger.warning(f"[lhb] backfill {date_str} failed: {e}")

    return {"checked": checked, "saved": saved, "pending": pending, "skipped": skipped}


def preload_today() -> bool:
    """后台预加载今日数据（主要在 15:30 后触发）。"""
    try:
        data = get_lhb_data(force_refresh=True)
        return _lhb_cache_ready(data)
    except Exception as e:
        logger.warning(f"[lhb] 预加载失败: {e}")
        return False
