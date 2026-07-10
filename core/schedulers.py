# -*- coding: utf-8 -*-
"""后台调度器（从 main.py 下沉，工单4；为工单5「调度进程独立」做准备）。

- run_scheduler：主调度循环（每秒巡检 running 用户 → 派发扫描线程；收盘自动终止）
- run_vibe_scheduler：Vibe Alpha 因子激活调度（30s 巡检，按用户自定义间隔跑 tick）
- _snap_all_paper_equity / _on_market_close：收盘净值快照 + 自动停机清信号
- start_background_services：一次性启动全部后台线程（幂等）

函数体与 main.py 原实现逐行等价。
"""
import logging
import threading
import time
from datetime import datetime

from app_state import (
    user_states, _user_state_lock, get_user_state, _persist_state, _add_log,
    _restore_all_user_states,
)
from config import SCAN_INTERVAL, DRY_RUN
from core import user_manager as um
from core import daban_strategy_runtime as daban_runtime
from core.trade_service import execute_buy
from broker import paper_broker as pb
from market.market_data import is_trading_time
from strategy_engine.scan_service import _scan_thread_worker

logger = logging.getLogger(__name__)

# ── Vibe Alpha 因子激活调度 ──────────────────────────────────────
_VIBE_INTERVAL_SEC = 120   # 默认 2 分钟（旧值 5 分钟太长，错过短线机会）
_VIBE_INTERVAL_MIN = 30    # 最快 30 秒一次（防接口压力）
_VIBE_INTERVAL_MAX = 600   # 最慢 10 分钟一次
_vibe_last_run: dict = {}  # uid -> ts


def _vibe_get_paper_cash(uid: int) -> float:
    """给 activator 用的 paper 可用资金回调"""
    try:
        broker = pb.get_broker(uid)
        return float(broker.cash) if broker else 0.0
    except Exception:
        return 0.0


def _vibe_compute_daily_pnl(uid: int) -> float | None:
    """计算用户当日浮动盈亏比例（持仓 mtm 比初始资金）
    返回 None 表示无法计算
    """
    try:
        broker = pb.get_broker(uid)
        if not broker:
            return None
        positions = broker.get_positions()
        if not positions:
            return 0.0
        total_pnl = 0.0
        for code, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            cost = float(pos.get("cost") or 0)
            shares = int(pos.get("shares") or 0)
            cur = float(pos.get("cur_price") or cost)
            total_pnl += (cur - cost) * shares
        # 用 paper_config.cash + 持仓市值 还原总资产 →  pnl / 总资产
        total_value = broker.cash + sum(
            float(p.get("cur_price") or p.get("cost") or 0) * int(p.get("shares") or 0)
            for p in positions.values() if isinstance(p, dict)
        )
        if total_value <= 0:
            return None
        return total_pnl / total_value
    except Exception as e:
        logger.debug(f"_vibe_compute_daily_pnl uid={uid} 失败: {e}")
        return None


def run_vibe_scheduler():
    """独立线程：每 30s 检查 vibe_activations enabled，达自定义间隔 + 交易时段则跑"""
    logger.info(f"[vibe.scheduler] 启动 默认间隔 {_VIBE_INTERVAL_SEC}s")
    while True:
        try:
            time.sleep(30)   # 检查频率（不是扫描频率）
            if not is_trading_time():
                continue
            from analytics.factors_zoo import activator as _act
            actives = _act.list_all_enabled()
            if not actives:
                continue
            now_ts = time.time()
            for cfg in actives:
                uid = cfg["user_id"]
                # 更新当日盈亏（每分钟一次，触发熔断）
                pnl = _vibe_compute_daily_pnl(uid)
                if pnl is not None:
                    _act.update_daily_pnl(uid, pnl)

                # 取每个激活配置自己的 interval_sec（用户可在激活时自定义）
                try:
                    _user_interval = int((cfg or {}).get("interval_sec") or _VIBE_INTERVAL_SEC)
                except (TypeError, ValueError):
                    _user_interval = _VIBE_INTERVAL_SEC
                _user_interval = max(_VIBE_INTERVAL_MIN, min(_VIBE_INTERVAL_MAX, _user_interval))

                # 满间隔才扫一次
                last = _vibe_last_run.get(uid, 0)
                if now_ts - last < _user_interval:
                    continue
                _vibe_last_run[uid] = now_ts
                try:
                    result = _act.run_tick(uid, execute_buy, lambda: _vibe_get_paper_cash(uid))
                    placed = result.get("placed", [])
                    if placed:
                        _add_log(uid, f"📚 Vibe调度: 开仓 {len(placed)} 只 (今日 {result.get('daily_count')}/{result.get('daily_limit')})")
                    elif result.get("phase") not in ("done", "skip_disabled"):
                        logger.info(f"[vibe.scheduler] uid={uid} phase={result.get('phase')}")
                except Exception as e:
                    logger.exception(f"[vibe.scheduler] uid={uid} run_tick 抛错")
        except Exception:
            logger.exception("[vibe.scheduler] 循环抛错")
            time.sleep(60)


# ── 主调度循环（多用户并发扫描）─────────────────────────────────
DABAN_INTERVAL = 5

_last_scan: dict[int, float] = {}


def _snap_all_paper_equity():
    """收盘后为所有模拟盘用户拍一张净值快照（实时收盘价）。

    没有这一步，净值曲线只在'有成交的那天'才落点，不交易的日子全是空洞；
    且盘中快照可能被成本价覆盖。收盘统一补一张含浮盈的快照。"""
    try:
        import sqlite3 as _sq
        con = _sq.connect(str(pb.DB_PATH), timeout=10)
        all_uids = [r[0] for r in con.execute(
            "SELECT DISTINCT user_id FROM paper_positions")]
        con.close()
    except Exception as e:
        logger.warning(f"[收盘快照] 读取模拟盘用户失败: {e}")
        return
    for uid in all_uids:
        try:
            pb.get_broker(uid)._snap_equity()   # 不传价 → 内部自动拉实时行情
        except Exception as e:
            logger.debug(f"[收盘快照 uid={uid}] {e}")
    if all_uids:
        logger.info(f"[收盘快照] 已为 {len(all_uids)} 个模拟盘用户落净值点")


def _on_market_close():
    """收盘(15:00)自动终止所有用户的自动交易：停 running + 清残留未执行信号 + 持久化。
    避免收盘后桥接器还在拉 pending 信号去下单。仅在下午真收盘触发（午休 11:30 不触发）。"""
    _snap_all_paper_equity()    # 先落净值快照（与是否有用户在自动交易无关）
    with _user_state_lock:
        uids = [uid for uid, s in user_states.items() if s.get("running")]
    if not uids:
        return
    for uid in uids:
        try:
            s = get_user_state(uid)
            s["running"] = False
            # 清掉本地队列里 pending 的买单（收盘后不再执行）
            try:
                s["queue"] = [q for q in s.get("queue", []) if q.get("status") != "pending"]
            except Exception:
                pass
            _persist_state(uid, running=0)
            # 取消实盘 pending 交易信号，避免桥接器收盘后继续拉去下单
            try:
                n = um.cancel_pending_signals(uid)
            except Exception:
                n = 0
            _add_log(uid, f"🔔 收盘(15:00)自动终止：已停止自动交易"
                          + (f"，清理未执行实盘信号 {n} 条" if n else ""), "WARN")
        except Exception as e:
            logger.warning(f"[收盘终止 uid={uid}] {e}")
    logger.info(f"[收盘] 已自动终止 {len(uids)} 个用户的自动交易")


def run_scheduler():
    """
    主调度循环：
    - 每秒检查所有 running 用户
    - 普通策略：每 SCAN_INTERVAL 秒触发一次
    - 打板策略：每 DABAN_INTERVAL 秒触发一次
    - 收盘(15:00)自动终止所有自动交易（午休 11:30 不触发）
    """
    now_ts = time.time()
    _was_trading = is_trading_time()        # 收盘下降沿检测
    while True:
        now_ts = time.time()
        # ★ 收盘自动终止：交易时段→非交易时段 的下降沿，且已到下午收盘(>=15:00)
        _now_trading = is_trading_time()
        if _was_trading and not _now_trading:
            _tc = datetime.now()
            if _tc.hour * 100 + _tc.minute >= 1500:
                try:
                    _on_market_close()
                except Exception as _ce:
                    logger.warning(f"[收盘终止] 调度异常: {_ce}")
        _was_trading = _now_trading

        with _user_state_lock:
            active_uids = [uid for uid, s in user_states.items() if s.get("running")]

        for uid in active_uids:
            s       = user_states.get(uid, {})
            active  = s.get("active_strategy")
            is_dbn  = daban_runtime.is_daban_ride_strategy(active)
            interval = DABAN_INTERVAL if is_dbn else SCAN_INTERVAL
            last     = _last_scan.get(uid, 0)
            if now_ts - last >= interval:
                _last_scan[uid] = now_ts
                threading.Thread(target=_scan_thread_worker, args=(uid,), daemon=True).start()

        time.sleep(1)


# ══════════════════════════════════════════════════════════════
# 后台服务启动（工单5：按进程归属拆成两组，均幂等）
#
#   交易组 start_user_trading_schedulers —— 必须与 Web 同进程：
#     扫描调度/Vibe 读写 user_states 内存队列与持仓视图，跨进程会导致
#     监控页看不到队列 + 模拟盘重复买入（状态外置后才能拆，见方案文档阶段二）。
#
#   数据组 start_data_schedulers —— 可独立进程（scheduler_main.py）：
#     新闻聚合/每日复盘/概念日更/AI因子挖掘 只写各自 DB/文件，
#     Web 路由读 DB，零 user_states 耦合。这四个是后台重负载的大头。
#
#   start_background_services() = 两组全启（单进程模式，与拆分前行为一致）。
# ══════════════════════════════════════════════════════════════
_trading_lock = threading.Lock()
_trading_started = False
_data_lock = threading.Lock()
_data_started = False


def start_user_trading_schedulers():
    """交易组：自动扫描主循环 + Vibe 因子激活调度 + 启动期状态恢复/信号清理。"""
    global _trading_started
    with _trading_lock:
        if _trading_started:
            return
        _trading_started = True
    _restore_all_user_states()
    threading.Thread(target=run_scheduler, daemon=True).start()
    # Vibe Alpha 因子激活调度器（独立线程，每 60s 检查，5 分钟跑一次扫描）
    threading.Thread(target=run_vibe_scheduler, daemon=True, name="vibe-scheduler").start()
    # 启动时全量清理隔日遗留信号（兜底：内存态 today_date 初始化即今天时，
    # _reset_daily_if_needed 不触发，僵尸信号会继续累进资金检查）
    try:
        _n_expired = um.expire_stale_signals()
        if _n_expired:
            logger.info(f"启动清理：{_n_expired} 条隔日遗留交易信号已置为过期")
    except Exception as _ex:
        logger.warning(f"启动清理隔日信号失败: {_ex}")
    logger.info(f"交易调度已启动 DRY_RUN={'开启' if DRY_RUN else '关闭'}")


def start_data_schedulers():
    """数据组：新闻聚合 / 每日复盘 / 概念日更 / AI 因子挖掘（可跑在独立进程）。"""
    global _data_started
    with _data_lock:
        if _data_started:
            return
        _data_started = True
    # 财经新闻聚合（盘中每3分钟拉快讯，19:40 抓新闻联播）
    try:
        import news_center
        threading.Thread(target=news_center.run_news_scheduler, daemon=True, name="news-center").start()
    except Exception as _ne:
        logger.warning(f"新闻聚合调度器启动失败: {_ne}")
    # 每日复盘（交易日 15:10 收盘版 → 19:50 晚间版，含 AI 总结归档）
    try:
        from analytics import review_engine
        threading.Thread(target=review_engine.run_review_scheduler, daemon=True, name="review-engine").start()
    except Exception as _re:
        logger.warning(f"复盘调度器启动失败: {_re}")
    # 产业概念日更（08:30/15:10 自动刷新并过滤交易信号类，保证每日新概念可见）
    try:
        import research_center
        research_center.run_concept_scheduler()
    except Exception as _rc:
        logger.warning(f"概念日更调度器启动失败: {_rc}")
    # AI因子挖掘 → 次日候选池 → 竞价AI决策自动调度（仅使用root admin账号配置）
    try:
        from analytics import auto_factor_scheduler
        auto_factor_scheduler.start_background_scheduler()
    except Exception as _afs:
        logger.warning(f"AI因子自动调度器启动失败: {_afs}")
    logger.info("数据调度已启动（新闻/复盘/概念/AI因子挖掘）")


def start_background_services():
    """单进程模式：两组全启（与拆分前 start_background_services 行为一致）。"""
    start_user_trading_schedulers()
    start_data_schedulers()
