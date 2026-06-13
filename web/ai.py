"""AI 分析 / 评分 / 策略生成 API 蓝图。"""
import json
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, session

import ai_analyst as ai
import data_manager as dm
import stock_scorer as scorer
import user_manager as um
from app_state import BASE_DIR, require_auth, require_admin, _add_log, _deduct
from app_security import rate_limit

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
@require_admin
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
@require_admin
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


@bp.route("/api/ai/detect_models", methods=["POST"])
@require_admin
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
@require_admin
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
        return jsonify({"ok": True, "templates": templates})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "templates": []})
