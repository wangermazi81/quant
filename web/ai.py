"""AI 分析 / 评分 / 策略生成 API 蓝图。"""
import json
import logging

from flask import Blueprint, request, jsonify, session

import ai_analyst as ai
import data_manager as dm
import stock_scorer as scorer
import user_manager as um
from app_state import BASE_DIR, require_auth, require_admin_perm, _add_log, _deduct
from app_security import rate_limit, user_key

logger = logging.getLogger(__name__)

bp = Blueprint("ai", __name__)

# AI 分析异步任务表（进程内，重启丢失）
import threading as _threading
_ai_tasks: dict = {}
_ai_tasks_lock = _threading.Lock()

DEFAULT_STRATEGY_TEMPLATES = [
    {
        "id": "ma_cross",
        "name": "均线金叉",
        "category": "趋势",
        "tags": ["均线", "金叉", "经典"],
        "desc": "MA5上穿MA20金叉，成交量放大1.5倍，涨幅不超8%，排除ST",
        "prompt": "均线金叉策略：MA5向上穿越MA20形成金叉，同时当日成交量是近20日均量的1.5倍以上，当日涨幅在0%到8%之间，排除ST和退市股，按成交量降序排列",
    },
    {
        "id": "macd_bottom",
        "name": "MACD底部金叉",
        "category": "技术",
        "tags": ["MACD", "金叉", "超跌"],
        "desc": "MACD在零轴下方金叉，DIF上穿DEA，红柱扩大，价格处于低位",
        "prompt": "MACD底部金叉策略：DIF在零轴以下且刚上穿DEA形成金叉，MACD柱由负转正或负柱缩短，近5日RSI从低位(低于35)回升，当日涨幅不超过5%，排除ST",
    },
    {
        "id": "volume_breakout",
        "name": "放量突破",
        "category": "突破",
        "tags": ["放量", "突破", "强势"],
        "desc": "放量突破60日最高点，成交量2倍以上，涨幅5-9%，强势突破形态",
        "prompt": "放量突破策略：股价突破近60日最高价，当日成交量是近5日均量的2倍以上，当日涨幅在3%到9.5%之间，收盘价在当日最高价附近（振幅小），排除ST，按涨幅排序",
    },
    {
        "id": "oversold_rebound",
        "name": "超跌反弹",
        "category": "反转",
        "tags": ["超跌", "反弹", "低位"],
        "desc": "RSI超卖区回升，近5日跌幅超10%，价格在布林下轨附近，寻底反弹",
        "prompt": "超跌低位反弹策略：RSI14低于30后当日回升到30以上，近5日累计跌幅超过10%，当日价格在近20日布林带下轨附近（偏差不超过3%），成交量比昨日放大，当日涨幅0%到4%",
    },
    {
        "id": "shrink_pullback",
        "name": "缩量回踩",
        "category": "趋势",
        "tags": ["缩量", "回踩", "均线"],
        "desc": "强势股回踩均线支撑，成交量萎缩，均线呈多头排列，等待再次起涨",
        "prompt": "缩量回踩均线策略：MA5>MA10>MA20多头排列，近3日股价从高点回落但不破MA20支撑，近3日成交量逐步萎缩（今日成交量低于5日均量的0.7倍），当日涨跌幅在-2%到2%之间，等待低吸机会",
    },
    {
        "id": "strong_leader",
        "name": "强势龙头",
        "category": "动量",
        "tags": ["龙头", "强势", "动量"],
        "desc": "近20日涨幅超20%，均线多头，今日量比大，具有板块龙头特征",
        "prompt": "强势龙头策略：近20日累计涨幅超过20%，均线呈多头排列（MA5>MA10>MA20>MA60），今日量比大于1.5，当日涨幅在1%到9.5%之间，股价大于10元，市值在50亿到500亿之间（按成交量过滤）",
    },
    {
        "id": "momentum_rotation",
        "name": "动量轮动",
        "category": "动量",
        "tags": ["动量", "轮动", "短线"],
        "desc": "近5日相对强度最强的个股，量能配合，适合短线动量策略",
        "prompt": "短线动量策略：计算近5日涨幅作为动量得分，过滤掉近5日涨幅超过25%（防止追高），成交量是20日均量的1.2倍以上，RSI在40到70之间（健康区间），股价大于5元，排除ST",
    },
    {
        "id": "bottom_pattern",
        "name": "底部形态",
        "category": "形态",
        "tags": ["底部", "W底", "反转"],
        "desc": "识别W底/双底形态，两次探底后反弹确认，适合中线布局",
        "prompt": "底部双底形态策略：近60日内价格形成两个相近低点（第二低点不低于第一低点的3%），第二次探底后当日上涨，近5日成交量逐步放大，当前价格在两低点均价以上，适合中线布局，排除ST",
    },
    {
        "id": "limit_up_pullback",
        "name": "涨停板回调",
        "category": "打板",
        "tags": ["涨停", "回调", "二板"],
        "desc": "前一日涨停，今日回调整理，缩量回踩，等待二次涨停机会",
        "prompt": "涨停板回调策略：昨日股价达到涨停（涨幅超过9.9%），今日回调幅度在0%到-3%之间，今日成交量明显小于昨日（小于昨日的0.6倍），距离前一个涨停日不超过3天，股价大于5元",
    },
    {
        "id": "hot_sector",
        "name": "热点题材",
        "category": "题材",
        "tags": ["热点", "题材", "涨停"],
        "desc": "近3日出现过涨停，处于活跃题材板块，换手率高，市场情绪热",
        "prompt": "热点题材策略：近5个交易日内至少出现1次涨停（涨幅>9.8%），今日换手率大于5%，今日成交量是近20日均量的1.5倍以上，股价在10元到100元之间，当日涨幅在-2%到5%之间，按换手率降序",
    },
    {
        "id": "three_buy_chan",
        "name": "缠论三买",
        "category": "缠论",
        "tags": ["缠论", "三买", "中线"],
        "desc": "缠论第三类买点：突破前一个中枢上方，成交量放大，趋势确认信号",
        "prompt": "缠论三类买点策略：近60日出现明显调整（跌幅超15%），之后形成新高（突破前期中枢高点），近5日股价站稳在20日均线以上，成交量从低点逐步放大，今日涨幅1%到8%，趋势确认，排除ST",
    },
    {
        "id": "mean_reversion",
        "name": "均值回归",
        "category": "反转",
        "tags": ["均值", "回归", "超买超卖"],
        "desc": "RSI或价格偏离均线过大后的均值回归机会，适合震荡市",
        "prompt": "均值回归策略：价格偏离20日均线超过-8%（即跌破均线8%以下），RSI14低于35的超卖区，近3日成交量缩量，今日出现上涨信号（涨幅大于0%），回归均线目标，排除ST和连续跌停股",
    },
    {
        "id": "market_temp",
        "name": "市场温度选股",
        "category": "综合",
        "tags": ["市场", "温度", "综合"],
        "desc": "根据大盘强弱自适应选股：牛市趋势追踪，熊市逆向防御，震荡低吸",
        "prompt": "市场温度综合策略：分析大盘趋势，如果近10日大盘指数上涨则选强势突破股（近5日涨幅>5%，量比>1.5），如果大盘下跌则选防御性低跌幅股（近5日跌幅<3%，股价稳定），优先选择涨跌幅适中（-1%到5%）的股票，排除ST",
    },
    {
        "id": "rsi_divergence",
        "name": "RSI背离",
        "category": "技术",
        "tags": ["RSI", "背离", "反转"],
        "desc": "价格创新低但RSI未创新低（底背离），或价格创新高RSI未创新高（顶背离），背离信号",
        "prompt": "RSI底部背离策略：近20日内股价创出阶段新低，但RSI14指标未同步创新低（形成底部背离信号），RSI低于45，当日出现反弹（涨幅0%到4%），成交量较前3日平均略有放大，排除ST",
    },
    {
        "id": "boll_squeeze",
        "name": "布林带收窄突破",
        "category": "突破",
        "tags": ["布林带", "收窄", "突破"],
        "desc": "布林带显著收窄后股价突破上轨，预示价格波动加大，方向性突破",
        "prompt": "布林带收窄突破策略：近5日布林带宽度（上轨-下轨）明显小于近20日平均带宽的70%（带宽收窄），今日股价突破布林上轨，成交量是近5日均量的1.5倍以上，当日涨幅在1%到9.5%之间，排除ST",
    },
    {
        "id": "second_third_board",
        "name": "二三板接力",
        "category": "打板",
        "tags": ["连板", "情绪", "接力"],
        "desc": "关注二板、三板晋级候选，结合封单、换手和市场情绪。",
        "prompt": "筛选昨日二板或三板个股，排除一字板开板风险过高标的，关注换手充分、封单稳定、板块情绪较强、竞价承接良好的候选，给出买入、撤单、止盈止损和持仓周期建议。",
    },
]


def _kline_is_stale(df, max_lag_days: int = 3) -> bool:
    """K 线缓存是否过期（最后一根距今超过 max_lag_days 个自然日）。
    过期则应重新从 pytdx 拉取最新数据，避免「本地 CSV 停留在多日前
    → MA/MACD/RSI 失真、与用实时数据的环境结论不一致」（曾导致同一只
    票一边『强烈买入』一边『观望』）。容忍 3 天兼顾长周末，正常盘后不会频繁重抓。"""
    try:
        import pandas as pd
        from datetime import date
        if df is None or getattr(df, "empty", True) or "date" not in getattr(df, "columns", []):
            return True
        last = pd.to_datetime(df["date"]).max().date()
        return (date.today() - last).days > max_lag_days
    except Exception:
        return False


@bp.route("/api/ai/config", methods=["GET"])
@require_admin_perm("ai_config")
def ai_get_config():
    """全局 AI 配置（仅管理员可查看/修改；普通用户的 AI 功能由后端用全局配置代答）。"""
    cfg = ai.load_config()
    key = cfg.get("api_key", "")
    if len(key) > 8:
        cfg["api_key_masked"] = key[:4] + "****" + key[-4:]
    else:
        cfg["api_key_masked"] = "****" if key else ""
    return jsonify({**cfg, "presets": ai.PROVIDER_PRESETS})


@bp.route("/api/ai/config", methods=["POST"])
@require_admin_perm("ai_config")
def ai_save_config():
    d   = request.json
    cfg = ai.load_config()
    for k in ("provider", "base_url", "model", "enabled"):
        if k in d:
            cfg[k] = d[k]
    if d.get("api_key"):
        cfg["api_key"] = d["api_key"]
    ai.save_config(cfg)
    return jsonify({"ok": True})


@bp.route("/api/stocks/score", methods=["POST"])
@rate_limit(30, 60, scope="score")   # 同一 IP 每分钟最多 30 次，挡匿名刷算力
@require_auth                         # 原为无鉴权公开接口（前端无调用方），收紧为需登录
def api_stock_score():
    d    = request.json
    code = d.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "请输入股票代码"})
    df = dm.get_local(code)
    # 不存在 / 为空 / 过期 → 从 pytdx 拉最新并回写缓存（与 ai_analyze 口径一致）
    if df is None or df.empty or _kline_is_stale(df):
        try:
            import ths_quote as tq
            import pandas as pd
            client = tq.get_client()
            if client and client._ok:
                bars = client.get_kline(code, 120)
                if bars:
                    df = pd.DataFrame(bars)
                    df["date"] = pd.to_datetime(df["date"])
                    try:
                        merged = dm.merge_kline_cache(code, df)
                        if merged is not None and not merged.empty:
                            df = merged
                    except Exception:
                        pass
        except Exception:
            pass
    if df is None or df.empty:
        return jsonify({"ok": False, "error": "无数据，请先下载历史数据"})
    result         = scorer.score_stock(df)
    result["code"] = code
    result["name"] = dm.get_stock_name(code) or code
    return jsonify(result)


@bp.route("/api/ai/analyze", methods=["POST"])
@rate_limit(60, 3600, scope="ai", key_func=user_key, message="AI 调用过于频繁，请稍后再试")
@require_auth
def ai_analyze():
    uid  = session["user_id"]
    d    = request.get_json(silent=True) or {}
    code = str(d.get("code") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "invalid code"}), 400
    err = _deduct("ai_analysis", f"AI分析 {code}")
    if err:
        return jsonify(err), 402
    name = dm.get_stock_name(code) or code
    df   = dm.get_local(code)
    # 修复：本地 CSV 不存在 / 为空 / 行数 < 30（"半残"）/ 过期（停留多日前）都触发 pytdx 补抓
    # 原逻辑只在 None 或 empty 时补抓，半残 CSV 会绕过 pytdx 让 ai_analyst 直接报"数据不足"
    # ★ 新增 _kline_is_stale：旧 CSV 会让指标失真、且与用实时数据的环境结论不一致
    if df is None or df.empty or len(df) < 30 or _kline_is_stale(df):
        try:
            import ths_quote as tq
            import pandas as pd
            client = tq.get_client()
            if client and client._ok:
                bars = client.get_kline(code, 120)
                if bars:
                    df = pd.DataFrame(bars)
                    df["date"] = pd.to_datetime(df["date"])
                    # 拉到数据后保存到本地 CSV 作为缓存
                    # 下次同一只票分析时秒读，不必每次都打 TDX 服务器
                    try:
                        merged = dm.merge_kline_cache(code, df)
                        if merged is not None and not merged.empty:
                            df = merged
                        logger.info(f"[ai_analyze] 已合并缓存 {code} K 线到 data/daily/{code}.csv ({len(df)} 行)")
                    except Exception as _e:
                        logger.debug(f"[ai_analyze] 缓存 {code} CSV 失败（不致命）: {_e}")
        except Exception:
            pass

    realtime_price = realtime_change = None
    try:
        import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            qs = client.get_quotes([code])
            if qs:
                q  = qs[0]
                rp = float(q.get("price") or q.get("now") or 0)
                if rp > 0:
                    realtime_price  = round(rp, 2)
                    realtime_change = round(float(q.get("change_pct", 0)), 2)
                    qname = q.get("name", "")
                    if qname and name == code:
                        name = qname
    except Exception:
        pass

    _add_log(uid, f"AI分析: {code} {name}")

    # 配置优先级：全局配置（网页『AI设置』所编辑、『测试连接』所用）为权威，
    # 仅全局没填 key 时才用历史 per-user 配置兜底——避免旧 per-user key 顶掉新配置导致 401。
    cfg = ai.resolve_config(um.get_user_ai_config(uid) or {})

    result = ai.run_analysis(code, name, df, config_override=cfg)

    if realtime_price and result.get("ok"):
        result["realtime_price"]  = realtime_price
        result["realtime_change"] = realtime_change
        if isinstance(result.get("indicators"), dict):
            result["indicators"]["price"] = realtime_price
        sc = result.get("score", {})
        if isinstance(sc.get("indicators"), dict):
            sc["indicators"]["price"] = realtime_price

    action    = result.get("summary", {}).get("action") or result.get("score", {}).get("signal") or "—"
    score_val = result.get("score", {}).get("total", "—")
    if result.get("ok"):
        _add_log(uid, f"AI分析完成: {code} {name} → {action}（评分{score_val}）")
    else:
        _add_log(uid, f"AI分析失败: {code} — {result.get('error','')}", "error")
    # 修复：递归把 NaN/Inf 换成 None，否则 Python jsonify 输出字面量 NaN，
    # 浏览器 JSON.parse 拒收（SyntaxError），前端误显示"分析失败"
    return jsonify(ai._json_safe(result))


@bp.route("/api/ai/analyze_async", methods=["POST"])
@rate_limit(60, 3600, scope="ai", key_func=user_key, message="AI 调用过于频繁，请稍后再试")
@require_auth
def ai_analyze_async():
    """异步 AI 分析：立即返回 task_id，前端轮询进度和结果。"""
    import uuid as _uuid
    uid  = session["user_id"]
    d    = request.get_json(silent=True) or {}
    code = str(d.get("code") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "invalid code"}), 400
    err = _deduct("ai_analysis", f"AI分析 {code}")
    if err:
        return jsonify(err), 402
    task_id = _uuid.uuid4().hex[:12]
    with _ai_tasks_lock:
        _ai_tasks[task_id] = {"status": "pending", "uid": uid, "code": code}
    _add_log(uid, f"AI分析(异步): {code}... task={task_id}")

    def _worker():
        try:
            with _ai_tasks_lock:
                _ai_tasks[task_id]["status"] = "running"
            name = dm.get_stock_name(code) or code
            df   = dm.get_local(code)
            if df is None or df.empty or len(df) < 30 or _kline_is_stale(df):
                try:
                    import ths_quote as tq
                    import pandas as pd
                    client = tq.get_client()
                    if client and client._ok:
                        bars = client.get_kline(code, 120)
                        if bars:
                            df = pd.DataFrame(bars)
                            df["date"] = pd.to_datetime(df["date"])
                            try:
                                merged = dm.merge_kline_cache(code, df)
                                if merged is not None and not merged.empty:
                                    df = merged
                            except Exception:
                                pass
                except Exception:
                    pass
            realtime_price = realtime_change = None
            try:
                import ths_quote as tq
                client = tq.get_client()
                if client and client._ok:
                    qs = client.get_quotes([code])
                    if qs:
                        q  = qs[0]
                        rp = float(q.get("price") or q.get("now") or 0)
                        if rp > 0:
                            realtime_price  = round(rp, 2)
                            realtime_change = round(float(q.get("change_pct", 0)), 2)
                            qname = q.get("name", "")
                            if qname and name == code:
                                name = qname
            except Exception:
                pass
            cfg = ai.resolve_config(um.get_user_ai_config(uid) or {})
            result = ai.run_analysis(code, name, df, config_override=cfg)
            if realtime_price and result.get("ok"):
                result["realtime_price"]  = realtime_price
                result["realtime_change"] = realtime_change
                if isinstance(result.get("indicators"), dict):
                    result["indicators"]["price"] = realtime_price
                sc = result.get("score", {})
                if isinstance(sc.get("indicators"), dict):
                    sc["indicators"]["price"] = realtime_price
            action    = result.get("summary", {}).get("action") or result.get("score", {}).get("signal") or "—"
            score_val = result.get("score", {}).get("total", "—")
            if result.get("ok"):
                _add_log(uid, f"AI分析完成: {code} {name} → {action}（评分{score_val}）")
            else:
                _add_log(uid, f"AI分析失败: {code} — {result.get('error','')}", "error")
            with _ai_tasks_lock:
                _ai_tasks[task_id].update({"status": "done", "result": ai._json_safe(result)})
        except Exception as e:
            logger.exception(f"[uid={uid}] async AI分析失败 task={task_id}")
            with _ai_tasks_lock:
                _ai_tasks[task_id].update({"status": "error", "error": str(e)})

    _threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id})


@bp.route("/api/ai/analyze_progress/<task_id>")
@require_auth
def ai_analyze_progress(task_id):
    """查询异步 AI 分析状态（前端可 1-2s 轮询）"""
    with _ai_tasks_lock:
        task = _ai_tasks.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "任务不存在"})
    snap = {"ok": True, "task_id": task_id, "status": task.get("status")}
    if task.get("status") == "error":
        snap["error"] = task.get("error", "未知错误")
    return jsonify(snap)


@bp.route("/api/ai/analyze_result/<task_id>")
@require_auth
def ai_analyze_result(task_id):
    """获取异步 AI 分析结果"""
    with _ai_tasks_lock:
        task = _ai_tasks.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "任务不存在"})
    if task["status"] == "done":
        return jsonify(task.get("result", {"ok": False}))
    if task["status"] == "error":
        return jsonify({"ok": False, "error": task.get("error", "未知错误")})
    return jsonify({"ok": False, "status": task["status"], "msg": "分析进行中"})


@bp.route("/api/ai/detect_models", methods=["POST"])
@require_admin_perm("ai_config")
def ai_detect_models():
    """探测 Base URL + Key 下可用的全部模型（中转站/官方 OpenAI 兼容端点通用）。

    body: {api_key?, base_url?}
    - key 不传则用已保存配置的 key
    - base_url 不传则先按 key 前缀猜服务商（sk-or-/gsk_/sk-ant-/AIza/xai-/nvapi-），
      猜中自动带出官方 Base URL；猜不中（裸 sk- 的中转站）要求填 Base URL
    """
    d = request.json or {}
    saved = ai.load_config()
    api_key  = (d.get("api_key") or "").strip() or saved.get("api_key", "")
    base_url = (d.get("base_url") or "").strip()
    if not api_key:
        return jsonify({"ok": False, "error": "请先填写 API Key"})

    provider_guess = ai.guess_provider_by_key(api_key)
    if not base_url:
        if provider_guess:
            base_url = ai.PROVIDER_PRESETS[provider_guess]["base_url"]
        else:
            return jsonify({"ok": False,
                            "error": "无法从 Key 前缀识别服务商（中转站 Key 多为通用 sk- 前缀），"
                                     "请填写中转站的 Base URL（如 https://xxx.com/v1）再检测"})

    # timeout=10：本机开代理时 urllib 可能先走代理路径，卡满超时才降级直连；
    # 10 秒让"代理失败→直连成功"的总耗时控制在前端 30 秒超时之内
    r = ai.detect_models(base_url, api_key, timeout=10)
    if not r.get("ok"):
        return jsonify({"ok": False, "error": r.get("error", "探测失败"),
                        "base_url": base_url, "provider_guess": provider_guess})
    return jsonify({"ok": True, "models": r["models"], "total": r.get("total", len(r["models"])),
                    "base_url": base_url, "provider_guess": provider_guess})


@bp.route("/api/ai/test", methods=["POST"])
@require_admin_perm("ai_config")
def ai_test_conn():
    d   = request.json or {}
    cfg = ai.load_config()
    if d.get("api_key"):
        cfg["api_key"]  = d["api_key"]
    if d.get("base_url"):
        cfg["base_url"] = d["base_url"]
    if d.get("model"):
        cfg["model"]    = d["model"]
    if not cfg.get("api_key"):
        return jsonify({"ok": False, "error": "未配置 API Key，请先填写并保存"})
    if not cfg.get("base_url"):
        return jsonify({"ok": False, "error": "未配置 Base URL，请先填写并保存"})
    try:
        reply = ai._call_llm(cfg, [{"role": "user", "content": "请回复'OK'，不要任何其他内容。"}],
                             temperature=0.1, max_tokens=16)
        return jsonify({"ok": True, "reply": reply.strip(),
                        "model": cfg.get("model"), "base_url": cfg.get("base_url")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e),
                        "base_url": cfg.get("base_url"), "model": cfg.get("model")})


@bp.route("/api/ai/generate_strategy", methods=["POST"])
@rate_limit(60, 3600, scope="ai", key_func=user_key, message="AI 调用过于频繁，请稍后再试")
@require_auth
def ai_gen_strategy():
    uid  = session["user_id"]
    d    = request.json or {}
    desc = d.get("description", "").strip()
    if not desc:
        return jsonify({"ok": False, "error": "请描述策略需求"})
    err = _deduct("ai_strategy", f"AI写策略 {desc[:20]}")
    if err:
        return jsonify(err)
    _add_log(uid, f"AI写策略: {desc[:40]}...")
    # 全局配置为权威，per-user 仅兜底（与 ai_analyze 一致，避免旧 key 顶掉新配置）
    cfg = ai.resolve_config(um.get_user_ai_config(uid) or {})
    result = ai.generate_strategy(desc, config_override=cfg)
    if result.get("ok"):
        _add_log(uid, f"AI策略生成完成，共{result.get('length', len(result.get('code','')))}字符")
    else:
        _add_log(uid, f"AI写策略失败: {result.get('error','')}", "error")
    return jsonify(result)


@bp.route("/api/strategy_templates")
def api_strategy_templates():
    """获取预设策略模板列表"""
    tpl_file = BASE_DIR / "data" / "strategy_templates.json"
    try:
        templates = json.loads(tpl_file.read_text(encoding="utf-8"))
        if not isinstance(templates, list) or not templates:
            raise ValueError("strategy template payload is empty or invalid")
        return jsonify({"ok": True, "templates": templates, "source": "file"})
    except FileNotFoundError:
        return jsonify({"ok": True, "templates": DEFAULT_STRATEGY_TEMPLATES, "source": "builtin"})
    except Exception:
        logger.warning("Failed to load strategy templates; using built-in defaults", exc_info=True)
        return jsonify({"ok": True, "templates": DEFAULT_STRATEGY_TEMPLATES, "source": "builtin"})
