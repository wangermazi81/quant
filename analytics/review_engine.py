"""每日复盘引擎(review_engine)

收盘后自动：采集复盘数据 → AI 总结(盘面研判/资金题材/要闻影响/明日预案) → JSON 归档。

数据块（每块独立容错，单块失败不影响整体）：
- market   : 指数/涨跌家数/板块涨跌（复用 web.market._get_market_review，TDX优先akshare兜底）
- zt       : 涨停池 + 连板天梯（akshare 涨停池，收盘后数据才稳定）
- sentiment: 情绪温度计——涨停/跌停/炸板率/连板高度/昨涨停今表现
- turnover : 两市成交额近20日趋势
- sectors  : 行业+概念板块涨跌幅榜
- news     : 当日重要新闻（news_center 本地库）

归档: data/review/YYYY-MM-DD.json
phase: live(盘中实时) → close(15:10收盘版) → evening(19:50晚间版,含新闻联播后重写预案)

调度线程 run_review_scheduler():
- 交易日 15:10 生成收盘版（失败每10分钟重试）
- 交易日 19:50 重新生成晚间版（此时新闻联播/晚间公告已入库，预案更准）
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR   = Path(__file__).resolve().parent.parent
REVIEW_DIR = BASE_DIR / "data" / "review"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _refresh_market_snapshot(date_str: str) -> bool:
    """Capture the latest Eastmoney whole-market snapshot before archiving review data."""
    try:
        import market_stats as _mstats

        _mstats.fetch_eastmoney_three_market_snapshot(date_str)
        return True
    except Exception as exc:
        logger.info(f"[review] market snapshot refresh skipped for {date_str}: {exc}")
        return False


# ──────────────────────────────────────────────────────────────
# 数据采集
# ──────────────────────────────────────────────────────────────
def _safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _collect_zt(compact: str) -> dict:
    """涨停池 + 跌停池 + 炸板池 + 昨日涨停表现 → 天梯与情绪指标。"""
    import akshare as ak
    out = {"zt_count": 0, "dt_count": 0, "zb_count": 0, "zb_rate": None,
           "max_height": 0, "ladder": [], "prev_zt_avg": None, "prev_zt_red_rate": None}

    df = None
    try:
        df = ak.stock_zt_pool_em(date=compact)
    except Exception as e:
        logger.debug(f"[review] 涨停池失败: {e}")
    if df is not None and not df.empty:
        out["zt_count"] = len(df)
        lb_col   = next((c for c in df.columns if "连板" in str(c)), None)
        name_col = next((c for c in df.columns if "名称" in str(c)), None)
        code_col = next((c for c in df.columns if "代码" in str(c)), None)
        ind_col  = next((c for c in df.columns if "行业" in str(c)), None)
        if lb_col and name_col:
            ladder = {}
            for _, r in df.iterrows():
                try:
                    h = int(_safe_float(r[lb_col], 1)) or 1
                except Exception:
                    h = 1
                ladder.setdefault(h, []).append({
                    "code": str(r[code_col]) if code_col else "",
                    "name": str(r[name_col]),
                    "industry": str(r[ind_col]) if ind_col else "",
                })
            out["max_height"] = max(ladder.keys()) if ladder else 0
            out["ladder"] = [{"height": h, "stocks": ladder[h][:30]}
                             for h in sorted(ladder.keys(), reverse=True)]

    try:
        df_dt = ak.stock_zt_pool_dtgc_em(date=compact)
        out["dt_count"] = 0 if df_dt is None else len(df_dt)
    except Exception as e:
        logger.debug(f"[review] 跌停池失败: {e}")

    try:
        df_zb = ak.stock_zt_pool_zbgc_em(date=compact)
        out["zb_count"] = 0 if df_zb is None else len(df_zb)
    except Exception as e:
        logger.debug(f"[review] 炸板池失败: {e}")

    if out["zt_count"] + out["zb_count"] > 0:
        out["zb_rate"] = round(out["zb_count"] * 100.0 / (out["zt_count"] + out["zb_count"]), 1)

    # 昨日涨停今日表现 → 赚钱效应（打板/接力胜率核心参考）
    try:
        df_prev = ak.stock_zt_pool_previous_em(date=compact)
        if df_prev is not None and not df_prev.empty:
            chg_col = next((c for c in df_prev.columns if "涨跌幅" in str(c)), None)
            if chg_col:
                import pandas as pd
                chg = pd.to_numeric(df_prev[chg_col], errors="coerce").dropna()
                if len(chg):
                    out["prev_zt_avg"]      = round(float(chg.mean()), 2)
                    out["prev_zt_red_rate"] = round(float((chg > 0).mean() * 100), 1)
    except Exception as e:
        logger.debug(f"[review] 昨日涨停池失败: {e}")
    return out


def _collect_turnover(compact: str | None = None) -> dict:
    """两市成交额：今日 + 近20日趋势（单位：亿元）。"""
    try:
        import market_stats as _mstats
        return _mstats.get_market_turnover(compact)
    except Exception as e:
        logger.debug(f"[review] market_stats 成交额失败: {e}")
        return {"today": None, "trend": [], "source": "none", "degraded": True, "reason": str(e)}


def _collect_sectors() -> dict:
    """行业 + 概念板块涨跌榜（各取前10/后5）。"""
    import akshare as ak
    import pandas as pd
    out = {"industry_top": [], "industry_bottom": [], "concept_top": []}

    def _rank(df, n_top, n_bottom=0):
        chg_col  = next((c for c in df.columns if "涨跌幅" in str(c)), None)
        name_col = next((c for c in df.columns if "名称" in str(c)), df.columns[0])
        if not chg_col:
            return [], []
        df = df.copy()
        df[chg_col] = pd.to_numeric(df[chg_col], errors="coerce")
        df = df.dropna(subset=[chg_col]).sort_values(chg_col, ascending=False)
        top = [{"name": str(r[name_col]), "chg": round(float(r[chg_col]), 2)}
               for _, r in df.head(n_top).iterrows()]
        bot = [{"name": str(r[name_col]), "chg": round(float(r[chg_col]), 2)}
               for _, r in df.tail(n_bottom).iterrows()] if n_bottom else []
        return top, bot

    # 东财板块接口在代理环境下单次请求时通时断 → 各重试2次
    for _try in range(2):
        try:
            # 注意：stock_board_industry_rank_em 在 akshare 1.18.x 不存在，正确接口是 name_em
            df_ind = ak.stock_board_industry_name_em()
            if df_ind is not None and not df_ind.empty:
                out["industry_top"], out["industry_bottom"] = _rank(df_ind, 10, 5)
                break
        except Exception as e:
            logger.debug(f"[review] 行业板块失败(第{_try+1}次): {e}")
            time.sleep(1.5)

    for _try in range(2):
        try:
            df_con = ak.stock_board_concept_name_em()
            if df_con is not None and not df_con.empty:
                out["concept_top"], _ = _rank(df_con, 10)
                break
        except Exception as e:
            logger.debug(f"[review] 概念板块失败(第{_try+1}次): {e}")
            time.sleep(1.5)
    return out


def collect(date_str: str = None) -> dict:
    """采集一天的完整复盘数据。每块独立容错。

    整体套 no_proxy：东财等国内源在注册表代理环境下随机被掐，直连最稳
    （根因与修复见 research_center.no_proxy 注释）。
    """
    date_str = date_str or _today()
    compact  = date_str.replace("-", "")
    data = {"date": date_str}

    try:
        from research_center import no_proxy
    except Exception:
        import contextlib
        no_proxy = contextlib.nullcontext

    with no_proxy():
        try:
            from web.market import _get_market_review
            data["market"] = _get_market_review(compact)
        except Exception as e:
            logger.warning(f"[review] 大盘数据失败: {e}")
            data["market"] = {}

        data["zt"]       = _collect_zt(compact)
        data["turnover"] = _collect_turnover(compact)
        data["sectors"]  = _collect_sectors()

    try:
        import news_center
        data["news"] = news_center.important_digest(date_str, limit=40)
    except Exception as e:
        logger.debug(f"[review] 新闻读取失败: {e}")
        data["news"] = []

    # 交易日判定：涨停/跌停/炸板池全空视为非交易日（周末已在调度层排除）
    zt = data["zt"]
    data["trading_day"] = bool(zt["zt_count"] or zt["dt_count"] or zt["zb_count"])

    # 情绪温度（0-100 粗略量化）：涨停-炸板-跌停 + 赚钱效应 + 涨跌家数
    try:
        score = 50.0
        score += min(zt["zt_count"], 120) * 0.25            # 涨停越多越热
        score -= min(zt["dt_count"], 60) * 0.5
        if zt["zb_rate"] is not None:
            score -= (zt["zb_rate"] - 30) * 0.4             # 炸板率高于30%降温
        if zt["prev_zt_avg"] is not None:
            score += zt["prev_zt_avg"] * 3                  # 昨涨停今表现
        b = (data.get("market") or {}).get("breadth") or {}
        if b.get("total"):
            score += (b.get("up", 0) - b.get("down", 0)) / b["total"] * 30
        data["sentiment_score"] = int(max(0, min(100, round(score))))
    except Exception:
        data["sentiment_score"] = None
    return data


# ──────────────────────────────────────────────────────────────
# AI 总结
# ──────────────────────────────────────────────────────────────
def _build_prompt(data: dict, phase: str) -> str:
    zt, mk = data.get("zt", {}), data.get("market", {})
    b      = mk.get("breadth") or {}
    sec    = data.get("sectors", {})
    lines  = [f"复盘日期：{data.get('date')}（{'晚间版,新闻联播与公告已出' if phase == 'evening' else '收盘版'}）", ""]

    idx = mk.get("indices") or {}
    if idx:
        lines.append("指数：" + "；".join(f"{k} {v.get('price')}({v.get('change'):+.2f}%)"
                                          for k, v in idx.items() if isinstance(v, dict)))
    if b.get("total"):
        lines.append(f"涨跌家数：上涨{b.get('up')} / 下跌{b.get('down')}，共{b.get('total')}只")
    lines.append(f"涨停{zt.get('zt_count')}家，跌停{zt.get('dt_count')}家，炸板率{zt.get('zb_rate')}%，"
                 f"最高连板{zt.get('max_height')}板")
    if zt.get("prev_zt_avg") is not None:
        lines.append(f"昨日涨停股今日平均涨幅{zt.get('prev_zt_avg')}%，红盘率{zt.get('prev_zt_red_rate')}%（赚钱效应指标）")
    tv = data.get("turnover", {})
    if tv.get("today"):
        trend = tv.get("trend", [])
        prev  = trend[-2]["amount"] if len(trend) >= 2 else None
        lines.append(f"两市成交额{tv['today']:.0f}亿" + (f"（昨日{prev:.0f}亿）" if prev else ""))
    if data.get("sentiment_score") is not None:
        lines.append(f"情绪温度（0-100）：{data['sentiment_score']}")

    ladder = zt.get("ladder", [])
    if ladder:
        lines.append("\n连板天梯：")
        for lv in ladder[:6]:
            names = "、".join(f"{s['name']}({s['industry']})" if s.get("industry") else s["name"]
                              for s in lv["stocks"][:8])
            lines.append(f"  {lv['height']}板：{names}")

    if sec.get("industry_top"):
        lines.append("\n领涨行业：" + "、".join(f"{s['name']}({s['chg']:+.1f}%)" for s in sec["industry_top"][:8]))
    if sec.get("industry_bottom"):
        lines.append("领跌行业：" + "、".join(f"{s['name']}({s['chg']:+.1f}%)" for s in sec["industry_bottom"][:5]))
    if sec.get("concept_top"):
        lines.append("热门概念：" + "、".join(f"{s['name']}({s['chg']:+.1f}%)" for s in sec["concept_top"][:8]))

    news = data.get("news", [])
    if news:
        lines.append("\n今日要闻：")
        for n in news[:25]:
            tag = "★" if n.get("important") else "-"
            lines.append(f"  {tag} [{n.get('source')}] {n.get('title')}")
    return "\n".join(lines)


_last_ai_error = ""   # 最近一次 AI 总结失败原因（透传给前端状态接口）


def summarize(data: dict, phase: str = "close") -> str:
    """调用 LLM 生成复盘总结。无 API key 或失败时返回空串（数据归档不受影响）。"""
    global _last_ai_error
    try:
        import ai_analyst as aa
        cfg = aa.resolve_config()
        if not cfg.get("api_key"):
            logger.info("[review] 未配置 AI key，跳过 AI 总结")
            _last_ai_error = "未配置 AI API Key（前端「AI 设置」页配置）"
            return ""
        sys_prompt = (
            "你是资深A股复盘分析师，擅长情绪周期与题材轮动分析。"
            "根据用户提供的当日盘面数据写复盘，必须严格按以下四段输出，每段标题用【】包裹：\n"
            "【盘面研判】当日市场强弱、情绪周期位置（启动/发酵/高潮/退潮）、量能变化，3-5句\n"
            "【资金与题材】主线题材及梯队结构、资金承接情况、板块轮动方向，3-5句\n"
            "【要闻影响】结合今日要闻判断对明日盘面的影响，没有要闻就写'今日无重大要闻'，2-4句\n"
            "【明日预案】给出可操作预案：关注的板块方向、关注的连板梯队个股、需要回避的风险点，"
            "用条目列出（每条以'- '开头），5-8条\n"
            "语言精炼专业，不要免责声明，不要废话。"
        )
        # max_tokens 给足余量：推理型模型(MiMo等)思考过程占大量 completion tokens(实测1100+)
        reply = aa._call_llm(cfg, [
            {"role": "system", "content": sys_prompt},
            {"role": "user",   "content": _build_prompt(data, phase)},
        ], temperature=0.4, max_tokens=5000, timeout=240).strip()
        if reply:
            _last_ai_error = ""
        else:
            _last_ai_error = "LLM 返回空内容（服务商限流，可稍后重试）"
        return reply
    except Exception as e:
        logger.warning(f"[review] AI 总结失败: {e}")
        _last_ai_error = str(e)
        return ""


# ──────────────────────────────────────────────────────────────
# 归档 / 读取
# ──────────────────────────────────────────────────────────────
def _path(date_str: str) -> Path:
    return REVIEW_DIR / f"{date_str}.json"


def load(date_str: str) -> dict | None:
    p = _path(date_str)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_dates(limit: int = 60) -> list:
    if not REVIEW_DIR.exists():
        return []
    return sorted((p.stem for p in REVIEW_DIR.glob("????-??-??.json")), reverse=True)[:limit]


def generate(date_str: str = None, phase: str = "close", with_ai: bool = True) -> dict:
    """采集 + AI 总结 + 归档。非交易日不归档。"""
    date_str = date_str or _today()
    logger.info(f"[review] 生成复盘 {date_str} phase={phase} ai={with_ai}")
    _refresh_market_snapshot(date_str)
    data = collect(date_str)
    if not data.get("trading_day"):
        return {"ok": False, "error": "非交易日（涨停/跌停池均为空）", "date": date_str}

    ai_summary = summarize(data, phase) if with_ai else ""
    old = load(date_str) or {}
    if not ai_summary and old.get("ai_summary"):
        ai_summary = old["ai_summary"]          # AI 失败时保留上一版总结

    out = {
        "ok": True, "date": date_str, "phase": phase,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": data, "ai_summary": ai_summary,
    }
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    _path(date_str).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(f"[review] 复盘归档完成 {date_str} ({phase})")
    return out


# ──────────────────────────────────────────────────────────────
# 异步生成（前端「生成AI复盘」走这里：HTTP 立即返回，后台跑1-2分钟）
# ──────────────────────────────────────────────────────────────
# 注意：状态字段名用 success 而非 ok —— 接口层 jsonify({"ok":True, **state})
# 时 ok 会被状态里的 None 覆盖（曾导致前端永远报"启动失败"）
_gen_lock  = threading.Lock()
_gen_state = {"running": False, "date": "", "phase": "",
              "started_at": "", "finished_at": "", "error": "", "success": None}


def get_gen_status() -> dict:
    with _gen_lock:
        return dict(_gen_state)


def generate_async(date_str: str = None, phase: str = "manual", with_ai: bool = True) -> dict:
    """启动后台生成。已在跑则直接返回当前状态（不重复启动）。"""
    date_str = date_str or _today()
    with _gen_lock:
        if _gen_state["running"]:
            return dict(_gen_state)
        _gen_state.update({"running": True, "date": date_str, "phase": phase,
                           "started_at": datetime.now().strftime("%H:%M:%S"),
                           "finished_at": "", "error": "", "success": None})

    def _worker():
        try:
            result = generate(date_str, phase=phase, with_ai=with_ai)
            with _gen_lock:
                _gen_state["success"] = bool(result.get("ok"))
                if not result.get("ok"):
                    _gen_state["error"] = result.get("error", "生成失败")
                elif with_ai and not result.get("ai_summary"):
                    _gen_state["error"] = f"数据已归档，但 AI 总结失败：{_last_ai_error or '原因未知，查服务端日志'}"
        except Exception as e:
            logger.warning(f"[review] 异步生成失败: {e}")
            with _gen_lock:
                _gen_state["success"], _gen_state["error"] = False, str(e)
        finally:
            with _gen_lock:
                _gen_state["running"] = False
                _gen_state["finished_at"] = datetime.now().strftime("%H:%M:%S")

    threading.Thread(target=_worker, daemon=True, name="review-gen").start()
    return get_gen_status()


# ──────────────────────────────────────────────────────────────
# 调度线程：15:10 收盘版 → 19:50 晚间版
# ──────────────────────────────────────────────────────────────
def _lhb_preload_window(now: datetime) -> bool:
    hm = now.hour * 100 + now.minute
    return 1520 <= hm < 2300


def _lhb_retry_interval(now: datetime) -> int:
    hm = now.hour * 100 + now.minute
    return 60 if 1520 <= hm < 1630 else 300


def _maybe_preload_lhb(now: datetime, state: dict, now_ts: float | None = None) -> bool:
    if now.weekday() >= 5:
        return False
    today = now.strftime("%Y-%m-%d")
    if state.get("done_date") == today:
        return True

    if not _lhb_preload_window(now):
        return False

    now_ts = time.time() if now_ts is None else now_ts
    if state.get("last_date") != today:
        state["last_date"] = today
        state["last_try"] = 0.0
    interval = _lhb_retry_interval(now)
    if now_ts - float(state.get("last_try") or 0.0) < interval:
        return False

    state["last_try"] = now_ts
    try:
        import lhb_engine

        ok = bool(lhb_engine.preload_today())
        state["last_ok"] = ok
        state["last_checked_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        if ok:
            state["done_date"] = today
        return ok
    except Exception as e:
        state["last_ok"] = False
        state["last_error"] = str(e)[:200]
        state["last_checked_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        logger.warning(f"[review] 龙虎榜预加载异常: {e}")
        return False


def run_review_scheduler():
    logger.info("[review] 每日复盘调度器启动")
    last_try = 0.0
    lhb_state = {}
    while True:
        try:
            now = datetime.now()
            if now.weekday() < 5:
                _maybe_preload_lhb(now, lhb_state)
                hm    = now.hour * 100 + now.minute
                today = _today()
                cur   = load(today) or {}
                phase = cur.get("phase", "")
                # 15:10 收盘版（未生成时每10分钟重试，21:00 截止）
                if 1510 <= hm < 2100 and phase not in ("close", "evening"):
                    if time.time() - last_try >= 600:
                        last_try = time.time()
                        generate(today, phase="close")
                # 19:50 晚间版（已有收盘/手动版 → 带新闻联播重写一次）
                elif 1950 <= hm < 2300 and cur and phase != "evening":
                    if time.time() - last_try >= 600:
                        last_try = time.time()
                        generate(today, phase="evening")
        except Exception as e:
            logger.warning(f"[review] 调度异常: {e}")
        time.sleep(60)
