"""AI 分析 / 评分 / 策略生成 / 决策仪表盘 API 蓝图。"""
import json
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, session

import ai_analyst as ai
import data_manager as dm
import stock_scorer as scorer
import user_manager as um
from app_state import BASE_DIR, require_auth, _add_log, _deduct

logger = logging.getLogger(__name__)

bp = Blueprint("ai", __name__)

# AI 分析异步任务表（进程内，重启丢失）
import threading as _threading
_ai_tasks: dict = {}
_ai_tasks_lock = _threading.Lock()


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
def ai_get_config():
    """全局 AI 配置（管理员设置的默认配置）。"""
    cfg = ai.load_config()
    key = cfg.get("api_key", "")
    if len(key) > 8:
        cfg["api_key_masked"] = key[:4] + "****" + key[-4:]
    else:
        cfg["api_key_masked"] = "****" if key else ""
    return jsonify({**cfg, "presets": ai.PROVIDER_PRESETS})


@bp.route("/api/ai/config", methods=["POST"])
def ai_save_config():
    d   = request.json
    cfg = ai.load_config()
    for k in ("provider", "base_url", "model", "enabled"):
        if k in d:
            cfg[k] = d[k]
    if d.get("api_key"):
        cfg["api_key"] = d["api_key"]
    ai.save_config(cfg)
    # 以全局配置为唯一来源：保存时顺手清掉当前用户残留的 per-user 旧配置，
    # 否则历史旧 key 会在分析/选股时顶掉这里刚存的新 key（曾导致"测试成功却 401"）。
    try:
        uid = session.get("user_id")
        if uid:
            um.delete_user_ai_config(uid)
    except Exception:
        pass
    return jsonify({"ok": True})


@bp.route("/api/stocks/score", methods=["POST"])
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
                        daily_dir = BASE_DIR / "data" / "daily"
                        daily_dir.mkdir(parents=True, exist_ok=True)
                        df.to_csv(daily_dir / f"{code}.csv", index=False, encoding="utf-8")
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
@require_auth
def ai_analyze():
    err = _deduct("ai_analysis", f"AI分析 {(request.json or {}).get('code','')}")
    if err:
        return jsonify(err), 402
    uid  = session["user_id"]
    d    = request.json
    code = d.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "请输入股票代码"})
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
                        daily_dir = BASE_DIR / "data" / "daily"
                        daily_dir.mkdir(parents=True, exist_ok=True)
                        df.to_csv(daily_dir / f"{code}.csv", index=False, encoding="utf-8")
                        logger.info(f"[ai_analyze] 已缓存 {code} K 线到 data/daily/{code}.csv ({len(df)} 行)")
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
@require_auth
def ai_analyze_async():
    """异步 AI 分析：立即返回 task_id，前端轮询进度和结果。"""
    import uuid as _uuid
    err = _deduct("ai_analysis", f"AI分析 {(request.json or {}).get('code','')}")
    if err:
        return jsonify(err), 402
    uid  = session["user_id"]
    d    = request.json or {}
    code = d.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "请输入股票代码"})
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
                                daily_dir = BASE_DIR / "data" / "daily"
                                daily_dir.mkdir(parents=True, exist_ok=True)
                                df.to_csv(daily_dir / f"{code}.csv", index=False, encoding="utf-8")
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


@bp.route("/api/ai/test", methods=["POST"])
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
@require_auth
def ai_gen_strategy():
    uid  = session["user_id"]
    d    = request.json or {}
    desc = d.get("description", "").strip()
    if not desc:
        return jsonify({"ok": False, "error": "请描述策略需求"})
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
        return jsonify({"ok": True, "templates": templates})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "templates": []})


@bp.route("/api/dashboard/generate", methods=["POST"])
@require_auth
def api_dashboard_generate():
    """
    决策仪表盘生成（批量评分 + 新闻情绪 + 可选AI摘要）。
    请求体: {"codes": ["000657", "600105"], "with_ai": true}
    """
    d       = request.json or {}
    codes   = [str(c).strip() for c in d.get("codes", []) if str(c).strip()]
    with_ai = bool(d.get("with_ai", True))
    if not codes or len(codes) > 20:
        return jsonify({"ok": False, "error": "codes 需要 1~20 个"})

    uid  = session["user_id"]
    try:
        cfg = ai.resolve_config(um.get_user_ai_config(uid) or {})
    except Exception:
        cfg = {}
    has_ai = bool(cfg.get("api_key")) and with_ai

    results = []
    import news_fetcher as _nf
    import traceback as _tb

    for code in codes:
        name = dm.get_stock_name(code) or code
        item = {
            "code": code, "name": name,
            "score": None, "signal": "N/A", "signal_color": "#888",
            "sentiment": "中性", "sentiment_score": 0,
            "news": [], "risks": [], "catalysts": [],
            "ai_summary": "", "ai_outlook": "", "latest": "",
        }

        # ── 六维评分 ────────────────────────────────────────
        try:
            df_k = dm.get_local(code)
            if df_k is not None and not df_k.empty:
                scored = scorer.score_stock(df_k)
                if scored.get("ok"):
                    item["score"]        = scored["total"]
                    item["signal"]       = scored["signal"]
                    item["signal_color"] = scored.get("signal_color", "#888")
                    item["risks"]        = scored.get("risks", [])
                    item["catalysts"]    = scored.get("catalysts", [])
        except Exception as ex:
            logger.debug(f"[dashboard] score {code}: {ex}")

        # ── 新闻 + 情绪 ─────────────────────────────────────
        try:
            nd = _nf.get_news_with_sentiment(code, limit=5)
            item["news"]            = nd.get("news", [])
            sent                    = nd.get("sentiment", {})
            item["sentiment"]       = sent.get("label", "中性")
            item["sentiment_score"] = sent.get("score", 0)
            if item["news"]:
                item["latest"] = item["news"][0].get("title", "")
        except Exception as ex:
            logger.debug(f"[dashboard] news {code}: {ex}")

        # ── 可选 AI 摘要 ─────────────────────────────────────
        if has_ai and item["news"]:
            try:
                news_text = "\n".join(f"- {n['title']}" for n in item["news"][:4])
                prompt = (
                    f"股票：{name}（{code}）\n"
                    f"六维评分：{item['score']}，信号：{item['signal']}\n"
                    f"近期新闻：\n{news_text}\n\n"
                    "请各用一句话回答：\n"
                    "舆情：<市场情绪摘要>\n"
                    "预期：<业绩/基本面预期>"
                )
                import ai_analyst as _aa
                reply = _aa._call_llm(cfg, [
                    {"role": "system", "content": "你是A股分析助手，中文简洁回答，每句话不超过50字。"},
                    {"role": "user",   "content": prompt},
                ], max_tokens=150, timeout=30)
                # 解析舆情/预期
                for line in reply.splitlines():
                    if line.startswith("舆情"):
                        item["ai_summary"] = line.split("：", 1)[-1].strip()
                    elif line.startswith("预期"):
                        item["ai_outlook"] = line.split("：", 1)[-1].strip()
                if not item["ai_summary"]:
                    item["ai_summary"] = reply.strip()[:80]
            except Exception as ex:
                logger.debug(f"[dashboard] AI summary {code}: {ex}")

        results.append(item)

    try:
        buy   = sum(1 for r in results if r["signal"] in ("买入", "强烈买入"))
        watch = sum(1 for r in results if r["signal"] == "观望")
        hold  = sum(1 for r in results if r["signal"] == "持有")
        sell  = sum(1 for r in results if r["signal"] == "卖出")

        return jsonify({
            "ok": True,
            "date":   datetime.now().strftime("%Y-%m-%d"),
            "time":   datetime.now().strftime("%H:%M"),
            "total":  len(results),
            "buy":    buy,
            "watch":  watch,
            "hold":   hold,
            "sell":   sell,
            "stocks": results,
        })
    except Exception as ex:
        logger.error(f"[dashboard] jsonify error: {ex}\n{_tb.format_exc()}")
        return jsonify({"ok": False, "error": f"序列化失败: {ex}"})
