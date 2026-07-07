# -*- coding: utf-8 -*-
"""
AI 自主交易引擎 (AI Autonomous Trading) —— 本地模块，不同步云端。
═══════════════════════════════════════════════════════════════════
分层多智能体（Lead Agent + Sub Agents）：

  🎯 主 Agent —— 首席交易员 (Lead/Head Trader)
       调度各子 Agent、汇总它们的结论，做出最终结构化交易裁决。

  子 Agents（角色不同、任务不同）：
    🔍 市场分析师   —— 大盘/板块/情绪环境（每轮一次，全队共享）
    📊 技术分析师   —— 个股技术面：K线/均线/指标/六维评分
    📰 基本面分析师 —— 财务/估值/消息面
    🐂 多方研究员   —— 看多论据
    🐻 空方研究员   —— 看空风险
    🛡️ 风控官       —— 仓位/止损/敞口硬约束（规则闸 + 提示）

闭环：候选池 → 团队分析 → 主Agent裁决 → 风控闸 → 执行 → 持仓回流。

执行与安全：
  • 模拟盘：全自动成交。
  • 实盘  ：每笔下单/撤单生成「待确认」，用户在面板点确认后才执行。
  • 硬风控：最多持仓 / 单票上限 / 当日交易上限 / 当日最大回撤熔断 / 一键急停。
  • 实时监控：独立轻量线程按 tick 对持仓做规则化止盈止损（无需 LLM）。

与 ai_analyst.py 的关系：复用其 _call_llm / 指标计算 / 评分 / 基本面，不重复造轮子。
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import json
import logging
import queue as _queue
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("ai_trader")

# 复用 ai_analyst 的 LLM / 指标 / 配置基建
import ai_analyst as _ai

try:
    import ai_candidate_ranker
except Exception:
    ai_candidate_ranker = None

try:
    import ai_research_store
except Exception:
    ai_research_store = None

try:
    import ai_trade_plan
except Exception:
    ai_trade_plan = None

try:
    import ai_position_sizer
except Exception:
    ai_position_sizer = None

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
TRADER_CFG_FILE = DATA_DIR / "ai_trader_config.json"      # 交易模块「单独配」的模型
SETTINGS_DIR = DATA_DIR / "ai_trader"                      # 每用户设置 {uid}.json
LOG_DIR = BASE_DIR / "logs"
DEFAULT_STRATEGY_CANDIDATE_STRATEGIES = [
    "daban_lianban_2plus_quality",
    "lianban_2plus",
    "lianban_leader",
    "da_ban",
]

# AI 独立模拟账户用虚拟 uid（与用户主账户隔离，同库不同 user_id）
AI_UID_OFFSET = 9_000_000

# ══════════════════════════════════════════════════════════════
# 1. 交易模块独立模型配置（与 ai_analyst 的全局 AI 配置分开）
# ══════════════════════════════════════════════════════════════
_DEFAULT_TRADER_CFG = {
    "provider": "deepseek",
    "api_key":  "",
    "base_url": "https://api.deepseek.com/v1",
    "model":    "deepseek-chat",
    "enabled":  False,
}


def load_trader_config() -> dict:
    cfg = dict(_DEFAULT_TRADER_CFG)
    try:
        if TRADER_CFG_FILE.exists():
            cfg.update(json.loads(TRADER_CFG_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        logger.warning(f"load_trader_config: {e}")
    return cfg


def save_trader_config(patch: dict) -> dict:
    cfg = load_trader_config()
    for k in ("provider", "base_url", "model", "enabled"):
        if k in patch:
            cfg[k] = patch[k]
    # api_key 留空表示不修改
    if patch.get("api_key"):
        cfg["api_key"] = patch["api_key"].strip()
    TRADER_CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRADER_CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def load_user_trader_config(uid: int) -> dict:
    """Load the AI-trading model config owned by one logged-in user.

    The platform/global trader config is intentionally not used as a fallback here:
    cloud AI trading must never spend or expose the operator's model key for another
    account.
    """
    cfg = dict(_DEFAULT_TRADER_CFG)
    try:
        import user_manager as um
        saved = um.get_user_ai_config(uid) or {}
        for k in ("provider", "api_key", "base_url", "model"):
            if saved.get(k):
                cfg[k] = saved[k]
        cfg["enabled"] = bool(saved.get("enabled")) if saved else False
    except Exception as e:
        logger.warning(f"load_user_trader_config uid={uid}: {e}")
    return cfg


def save_user_trader_config(uid: int, patch: dict) -> dict:
    cfg = load_user_trader_config(uid)
    for k in ("provider", "base_url", "model", "enabled"):
        if k in patch:
            cfg[k] = patch[k]
    if patch.get("clear_api_key"):
        cfg["api_key"] = ""
    elif patch.get("api_key"):
        cfg["api_key"] = str(patch["api_key"]).strip()
    try:
        import user_manager as um
        um.save_user_ai_config(
            uid,
            provider=cfg.get("provider", ""),
            api_key=cfg.get("api_key", ""),
            base_url=cfg.get("base_url", ""),
            model=cfg.get("model", ""),
            enabled=bool(cfg.get("enabled")),
        )
    except Exception as e:
        logger.warning(f"save_user_trader_config uid={uid}: {e}")
        raise
    return cfg


def test_trader_config(cfg: dict, uid: int | None = None) -> dict:
    """测试连接：发一句最小请求。返回 {ok, msg}。"""
    try:
        use = load_user_trader_config(uid) if uid is not None else load_trader_config()
        use = {**use, **{k: v for k, v in cfg.items() if v}}
        if not use.get("api_key"):
            return {"ok": False, "msg": "未填写 API Key"}
        reply = _ai._call_llm(
            use, [{"role": "user", "content": "回复两个字：在线"}],
            temperature=0.1, max_tokens=16, timeout=30,
        )
        return {"ok": True, "msg": f"连接成功，模型回复：{(reply or '').strip()[:20]}"}
    except Exception as e:
        return {"ok": False, "msg": f"连接失败：{e}"}


def list_providers() -> dict:
    return _ai.PROVIDER_PRESETS


def detect_models(api_key: str = "", base_url: str = "", uid: int | None = None) -> dict:
    """探测 Key + Base URL 下可用的全部对话模型（沿用 ai_analyst 的实现，与后台一致）。

    - key 不传则用已保存配置的 key；
    - base_url 不传则按 key 前缀猜服务商（sk-or-/gsk_/sk-ant-/AIza/xai-/nvapi-）带出官方
      Base URL；猜不中（裸 sk- 的中转站）要求填 Base URL。
    返回 {ok, models, total, base_url, provider_guess} 或 {ok:False, error}。
    """
    saved = load_user_trader_config(uid) if uid is not None else load_trader_config()
    api_key = (api_key or "").strip() or saved.get("api_key", "")
    base_url = (base_url or "").strip()
    if not api_key:
        return {"ok": False, "error": "请先填写 API Key"}
    provider_guess = _ai.guess_provider_by_key(api_key)
    if not base_url:
        if provider_guess:
            base_url = _ai.PROVIDER_PRESETS[provider_guess]["base_url"]
        else:
            return {"ok": False, "provider_guess": "",
                    "error": "无法从 Key 前缀识别服务商（中转站 Key 多为通用 sk- 前缀），"
                             "请先填写 Base URL（如 https://xxx.com/v1）再检测"}
    r = _ai.detect_models(base_url, api_key, timeout=10)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "探测失败"),
                "base_url": base_url, "provider_guess": provider_guess}
    return {"ok": True, "models": r["models"],
            "total": r.get("total", len(r["models"])),
            "base_url": base_url, "provider_guess": provider_guess}


# ══════════════════════════════════════════════════════════════
# 2. 每用户设置（候选池 / 节奏 / 风控参数）
# ══════════════════════════════════════════════════════════════
def _default_settings() -> dict:
    return {
        "watchlist":          [],     # 自选买入候选 ["600519", ...]
        "max_candidates":     6,      # 每轮最多分析只数（控成本）
        "interval_sec":       180,    # 主Agent决策轮间隔（秒）
        "monitor_sec":        20,     # 实时风控监控 tick（秒）
        "ignore_market_time": False,  # 测试用：忽略交易时间，盘后也能跑
        # ── 全市场量化初筛（无LLM，先海选再交团队深析）──
        "prescreen_enabled":  True,   # 开启全市场初筛作为买入候选来源
        "prescreen_fallback_only": True,
        "prescreen_universe": 1200,   # 初筛覆盖：按活跃度取全市场前 N 只
        "prescreen_top":      6,      # 初筛入选 top N 喂给团队
        "strategy_candidates_enabled": True,
        "strategy_candidate_strategies": list(DEFAULT_STRATEGY_CANDIDATE_STRATEGIES),
        "strategy_candidate_max_codes": 1200,
        "strategy_candidate_top": 6,
        "block_buy_on_degraded_data": True,
        "risk": {
            "max_positions":     5,      # 最多持仓只数
            "single_max_ratio":  0.20,   # 单票最大占比
            "per_trade_ratio":   0.20,   # 单笔买入占总资金比例
            "daily_trade_limit": 10,     # 当日最多交易笔数
            "daily_max_loss_pct": 0.05,  # 当日最大回撤熔断（占总资金）
            "min_confidence":    60,     # 主Agent信心阈值，低于不下单
            "consensus_min_votes": 2,    # 硬共识门：≥N票看多才允许买入（共4票：市场/技术/基本面/多空辩论）。
                                         # 注：市场环境票全队共享，弱/中性市里它常为非看多，故 4 票里要 3 票看多几乎不可能；
                                         # 默认 2（多2/空0 即可买）。设 1=激进、3=严格、4=极严。在「风控/候选」面板可调。
        },
    }


def load_settings(uid: int) -> dict:
    st = _default_settings()
    try:
        f = SETTINGS_DIR / f"{uid}.json"
        if f.exists():
            saved = json.loads(f.read_text(encoding="utf-8"))
            st.update({k: v for k, v in saved.items() if k != "risk"})
            if isinstance(saved.get("risk"), dict):
                st["risk"].update(saved["risk"])
    except Exception as e:
        logger.warning(f"load_settings uid={uid}: {e}")
    return st


def save_settings(uid: int, patch: dict) -> dict:
    st = load_settings(uid)
    for k, v in patch.items():
        if k == "risk" and isinstance(v, dict):
            st["risk"].update(v)
        elif k in st:
            st[k] = v
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    (SETTINGS_DIR / f"{uid}.json").write_text(
        json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    return st


# ══════════════════════════════════════════════════════════════
# 3. SSE 事件总线（独立于 app_state 的 /api/paper/stream）
# ══════════════════════════════════════════════════════════════
_subs: dict[int, list] = {}
_subs_lock = threading.Lock()


def subscribe(uid: int) -> _queue.Queue:
    q = _queue.Queue(maxsize=500)
    with _subs_lock:
        _subs.setdefault(uid, []).append(q)
    return q


def unsubscribe(uid: int, q) -> None:
    with _subs_lock:
        lst = _subs.get(uid, [])
        if q in lst:
            lst.remove(q)


def _push(uid: int, event: str, data: dict) -> None:
    with _subs_lock:
        dead = []
        for q in _subs.get(uid, []):
            try:
                q.put_nowait({"event": event, "data": data})
            except Exception:
                dead.append(q)
        for q in dead:
            _subs[uid].remove(q)


# ══════════════════════════════════════════════════════════════
# 4. 子 Agent 角色定义（思考过程 + 结论全程推 UI）
# ══════════════════════════════════════════════════════════════
ROLES = {
    "market":    {"icon": "🔍", "name": "市场分析师",   "kind": "sub"},
    "technical": {"icon": "📊", "name": "技术分析师",   "kind": "sub"},
    "fundamental": {"icon": "📰", "name": "基本面分析师", "kind": "sub"},
    "bull":      {"icon": "🐂", "name": "多方研究员",   "kind": "sub"},
    "bear":      {"icon": "🐻", "name": "空方研究员",   "kind": "sub"},
    "risk":      {"icon": "🛡️", "name": "风控官",       "kind": "sub"},
    "lead":      {"icon": "🎯", "name": "首席交易员·主Agent", "kind": "lead"},
}


def roles_meta() -> list:
    return [{"role": k, **v} for k, v in ROLES.items()]


def _agent_entry(role: str, conclusion: str, text: str) -> dict:
    """组装某 agent 对某票的留痕条目（供决策追溯）。"""
    meta = ROLES.get(role, {"icon": "🤖", "name": role})
    return {"role": role, "icon": meta["icon"], "name": meta["name"],
            "conclusion": conclusion or "", "text": text or ""}


def _marker(text: str, key: str) -> str:
    """从文本里抽取 [key:值] 标记，供方向投票解析（model 按提示在末尾输出）。"""
    if not text:
        return ""
    m = re.search(r"\[\s*" + re.escape(key) + r"\s*[:：]\s*([^\]\s，,]+)\s*\]", str(text))
    return m.group(1).strip() if m else ""


def _tally_votes(market_view, tech, fund, debate) -> dict:
    """方向投票：个股 3 票（技术/基本面/多空辩论）为买入主依据，
    市场环境单列、不计入个股票数，仅作分级修正（弱市加严/强市放松）。
    返回 {stock_long, stock_short, stock_neutral, market, detail}。"""
    def cls(v, longs, shorts):
        v = (v or "").strip()
        if any(w in v for w in longs):
            return "long"
        if any(w in v for w in shorts):
            return "short"
        return "neutral"
    # 市场环境票：marker 为 强/中/弱（单字），不混入个股票数
    market = cls(_marker(market_view, "环境"), ("强", "偏强"), ("弱", "偏弱"))
    stock = {
        "技术": cls(_marker(tech, "方向"), ("多", "偏多"), ("空", "偏空")),
        "基本面": cls(_marker(fund, "方向"), ("多", "支持"), ("空", "谨慎")),
        "多空辩论": cls(_marker(debate, "占优"), ("多", "多方"), ("空", "空方")),
    }
    sl = sum(1 for v in stock.values() if v == "long")
    ss = sum(1 for v in stock.values() if v == "short")
    sn = sum(1 for v in stock.values() if v == "neutral")
    return {"stock_long": sl, "stock_short": ss, "stock_neutral": sn,
            "market": market, "detail": {**stock, "市场环境": market}}


# ══════════════════════════════════════════════════════════════
# 5. 引擎（每用户一实例）：双线程 = 决策轮 + 实时风控监控
# ══════════════════════════════════════════════════════════════
class _Engine:
    def __init__(self, uid: int):
        self.uid = uid
        self.running = False
        self.round_thread: threading.Thread | None = None
        self.monitor_thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.round_no = 0
        self.last_round_ts = 0.0
        self.started_at = ""
        self.halted_reason = ""           # 熔断原因（非空=已熔断停机）
        self.once_running = False         # 单轮测试进行中
        self.today_trades = 0             # AI 当日已交易笔数（独立计数）
        self.today_date = ""
        self.pending: dict = {}           # pending_id -> 待确认决策（实盘）
        self.targets: dict = {}           # code -> {"stop":x, "target":y}（AI下单时设定）
        self.recent_decisions = deque(maxlen=120)
        self.recent_execs = deque(maxlen=120)
        self.agent_cards = deque(maxlen=60)   # 最近角色发言，供快照
        self.market_data_status = {"degraded": False, "reason": ""}
        self.last_wait_reason = ""
        self.lock = threading.Lock()

    # ── 控制 ──────────────────────────────────────────────
    def start(self) -> dict:
        cfg = load_user_trader_config(self.uid)
        if not cfg.get("enabled") or not cfg.get("api_key"):
            return {"ok": False, "msg": "请先在「⚙️ 模型设置」配置并启用 AI 交易模型"}
        if self.running:
            return {"ok": True, "msg": "AI 交易已在运行"}
        self.stop_flag.clear()
        self.running = True
        self.halted_reason = ""
        self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.round_thread = threading.Thread(target=self._round_loop, daemon=True,
                                             name=f"aitrader-round-{self.uid}")
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True,
                                               name=f"aitrader-monitor-{self.uid}")
        self.round_thread.start()
        self.monitor_thread.start()
        self._log("🤖 AI 交易已启动（首席交易员开始调度团队）")
        self._push_status()
        return {"ok": True, "msg": "AI 交易已启动"}

    def stop(self, reason: str = "") -> dict:
        self.running = False
        self.stop_flag.set()
        msg = f"⏸ AI 交易已停止{('：' + reason) if reason else ''}"
        self._log(msg, "warn")
        self._push_status()
        return {"ok": True, "msg": msg}

    def emergency_stop(self) -> dict:
        self.pending.clear()
        return self.stop("🛑 一键急停（已清空待确认）")

    def run_once(self) -> dict:
        """手动触发单轮团队分析（不进入常驻循环；盘后测试用）。"""
        if self.running:
            return {"ok": False, "msg": "AI 交易运行中，无需手动单轮"}
        cfg = load_user_trader_config(self.uid)
        if not cfg.get("enabled") or not cfg.get("api_key"):
            return {"ok": False, "msg": "请先在「⚙️ 模型设置」配置并启用 AI 交易模型"}

        def _job():
            self.stop_flag.clear()
            self.once_running = True
            self._push_status()
            try:
                self._run_round(load_settings(self.uid))
            except Exception as e:
                self._log(f"⚠ 单轮分析异常：{e}", "error")
            finally:
                self.once_running = False
                self.stop_flag.set()
                self._push_status()
        threading.Thread(target=_job, daemon=True,
                         name=f"aitrader-once-{self.uid}").start()
        return {"ok": True, "msg": "已触发单轮团队分析，结果在面板实时显示"}

    def stop_once(self) -> dict:
        """停止正在进行的单轮测试（不影响常驻引擎语义）。"""
        if not self.once_running:
            return {"ok": True, "msg": "当前无单轮测试在跑"}
        self.stop_flag.set()
        self._log("⏹ 单轮测试已请求停止（完成当前票后中止）", "warn")
        return {"ok": True, "msg": "已请求停止单轮测试"}

    def status(self) -> dict:
        st = load_settings(self.uid)
        analysis_ready, waiting_reason = self._market_gate(st)
        next_round_in_sec = 0
        if self.running and analysis_ready:
            next_round_in_sec = max(0, int(st.get("interval_sec", 0) - (time.time() - self.last_round_ts)))
        return {
            "running": self.running,
            "round_no": self.round_no,
            "started_at": self.started_at,
            "halted_reason": self.halted_reason,
            "once_running": self.once_running,
            "market_open": analysis_ready,
            "analysis_ready": analysis_ready and not self.halted_reason,
            "waiting_reason": waiting_reason if self.running and not analysis_ready else "",
            "next_round_in_sec": next_round_in_sec,
            "today_trades": self._today_trades(),
            "broker_mode": self._broker_mode(),
            "pending_count": len(self.pending),
            "settings": st,
        }

    def snapshot(self) -> dict:
        return {
            **self.status(),
            "roles": roles_meta(),
            "agent_cards": list(self.agent_cards),
            "decisions": list(self.recent_decisions)[-40:],
            "execs": list(self.recent_execs)[-40:],
            "pending": list(self.pending.values()),
        }

    # ── 工具 ──────────────────────────────────────────────
    def _broker_mode(self) -> str:
        try:
            import user_manager as um
            from config import DRY_RUN
            bt = um.get_user_broker_config(self.uid).get("broker_type", "paper")
            return "paper" if (DRY_RUN or bt == "paper") else "live"
        except Exception:
            return "paper"

    def _user_state(self):
        import app_state
        return app_state.get_user_state(self.uid)

    # ── AI 独立模拟账户（虚拟 uid 隔离，与主模拟盘分开）───────
    def _ai_uid(self):
        return AI_UID_OFFSET + self.uid

    def _ai_broker(self):
        import paper_broker as pb
        return pb.get_broker(self._ai_uid())

    def _account(self) -> dict:
        """统一账户视图(mode-aware)。paper=AI独立模拟账户；live=用户实盘账户。
        返回 {positions:{code:{name,shares,cost}}, cash, capital}。"""
        if self._broker_mode() == "paper":
            br = self._ai_broker()
            acc = br.get_account()
            poss = {p["code"]: {"name": p.get("name", p["code"]),
                                "shares": p.get("shares", 0),
                                "cost": p.get("cost_price", 0)}
                    for p in br.get_positions()}
            base = acc.get("equity") or acc.get("initial_capital") or 0
            return {"positions": poss, "cash": acc.get("cash", 0), "capital": base}
        s = self._user_state()
        cap = s.get("capital", 0)
        return {"positions": s.get("positions") or {}, "cash": cap, "capital": cap}

    def _today_trades(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.today_date != today:
            self.today_date = today
            self.today_trades = 0
        return self.today_trades

    def _bump_trades(self):
        self._today_trades()
        self.today_trades += 1

    def ai_account(self) -> dict:
        """AI 独立模拟账户快照（账户+持仓+统计，带实时价）。"""
        br = self._ai_broker()
        codes = [p["code"] for p in br.get_positions()]
        cur = {}
        if codes:
            try:
                import ths_quote as tq
                cli = tq.get_client()
                if cli and getattr(cli, "_ok", False):
                    for q in (cli.get_quotes(codes) or []):
                        c = q.get("code")
                        if c:
                            cur[c] = _to_float(q.get("price"))
            except Exception:
                pass
        return {"account": br.get_account(cur), "positions": br.get_positions(cur),
                "stats": br.get_stats(), "ai_uid": self._ai_uid()}

    def reset_ai_account(self, initial_capital=None) -> dict:
        try:
            res = self._ai_broker().reset(initial_capital)
            self.targets.clear()
            self.today_trades = 0
            self._log(f"🔄 AI 模拟账户已重置（初始资金¥{(initial_capital or 500000):,.0f}）", "warn")
            self._push_status()
            return {"ok": True, "msg": "AI 模拟账户已重置", **(res or {})}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def _log(self, msg: str, level: str = "info"):
        try:
            import app_state
            app_state._add_log(self.uid, msg, level.upper())
        except Exception:
            pass
        self._push("ai_log", {"time": datetime.now().strftime("%H:%M:%S"),
                              "msg": msg, "level": level})

    def _push(self, event: str, data: dict):
        _push(self.uid, event, data)

    def _push_status(self):
        self._push("ai_status", self.status())

    def _market_gate(self, st: dict) -> tuple[bool, str]:
        if st.get("ignore_market_time"):
            return True, ""
        try:
            from market_data import is_trading_time
            if is_trading_time():
                return True, ""
            return False, "当前非A股连续竞价时段，AI交易团队已启动但会等待开盘/午休结束后再自动分析。需要盘后测试可勾选「忽略交易时间」或点击「单轮测试」。"
        except Exception as e:
            return False, f"交易时间判断失败，暂停自动分析：{e}"

    def _audit(self, record: dict):
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_DIR / f"ai_trader_{self.uid}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": datetime.now().isoformat(), **record},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _agent_say(self, code, role, status, text="", conclusion=""):
        """推送某子/主Agent的思考状态到 UI，并入快照。"""
        meta = ROLES.get(role, {"icon": "🤖", "name": role})
        card = {"code": code, "role": role, "icon": meta["icon"], "name": meta["name"],
                "status": status, "text": text, "conclusion": conclusion,
                "time": datetime.now().strftime("%H:%M:%S")}
        if status == "done":
            self.agent_cards.append(card)
        self._push("ai_agent", card)

    # ── 决策主循环（LLM，主Agent调度团队）──────────────────
    def _round_loop(self):
        while not self.stop_flag.is_set():
            try:
                st = load_settings(self.uid)
                tradable, wait_reason = self._market_gate(st)
                if not tradable:
                    if wait_reason != self.last_wait_reason:
                        self.last_wait_reason = wait_reason
                        self._log(f"⏳ {wait_reason}", "warn")
                        self._push_status()
                    self.stop_flag.wait(5)
                    continue
                self.last_wait_reason = ""
                if tradable and (time.time() - self.last_round_ts) >= st["interval_sec"]:
                    self._run_round(st)
                    self.last_round_ts = time.time()
            except Exception as e:
                logger.exception(f"round_loop uid={self.uid}: {e}")
                self._log(f"⚠ 决策轮异常：{e}", "error")
            # 小步轮询，便于及时响应 stop / 到点开轮
            self.stop_flag.wait(5)

    def _run_round(self, st: dict):
        cfg = load_user_trader_config(self.uid)
        if not cfg.get("enabled") or not cfg.get("api_key"):
            return
        if self.halted_reason:
            return
        self.round_no += 1
        rid = self.round_no
        self._push("ai_round", {"phase": "start", "round_no": rid,
                                "time": datetime.now().strftime("%H:%M:%S")})

        # 🔍 市场分析师（每轮一次，全队共享上下文）
        market_view = self._agent_market(cfg)

        candidates = self._build_candidates(st)
        self._log(f"第 {rid} 轮：团队分析 {len(candidates)} 只 "
                  f"（持仓+自选，上限{st['max_candidates']}）")
        for code, name in candidates:
            if self.stop_flag.is_set():
                break
            try:
                decision = self._team_decide(cfg, code, name, market_view, st)
                if decision:
                    self._handle_decision(decision, st)
            except Exception as e:
                logger.exception(f"team_decide {code}: {e}")
                self._agent_say(code, "lead", "done",
                                conclusion=f"分析异常，跳过：{e}")
        self._push("ai_round", {"phase": "end", "round_no": rid,
                                "time": datetime.now().strftime("%H:%M:%S")})

    def _build_candidates(self, st: dict) -> list:
        s = self._user_state()
        seen, out = set(), []

        def add(code, name=None):
            code = str(code or "").strip()
            if code and code not in seen:
                seen.add(code)
                out.append((code, name or code))

        # 1) 持仓（卖出/持有判断优先）—— 模拟盘读 AI 独立账户、实盘读用户账户
        for code, p in self._account()["positions"].items():
            add(code, p.get("name", code))
        # 2) 策略库候选优先：AI只做复核和风控，不裸奔全市场。
        if st.get("strategy_candidates_enabled", True):
            for code, name in self._strategy_candidates(st):
                add(code, name)
        # 3) 自选买入候选（手动指定，优先保留）
        for code in st.get("watchlist", []):
            add(code, code)
        # 4) 主程序选股结果兜底
        for q in (s.get("queue") or []):
            add(q.get("code"), q.get("name") or q.get("code"))
        # 5) 全市场初筛只作最后兜底，避免行情源异常时污染AI买入候选。
        fallback_only = st.get("prescreen_fallback_only", True)
        if st.get("prescreen_enabled", True) and (not fallback_only or len(out) == 0):
            for code, name in self._prescreen(st):
                add(code, name)
        return out[: st.get("max_candidates", 6)]

    def _strategy_candidates(self, st: dict) -> list:
        try:
            import strategy_manager as sm
        except Exception as e:
            self._log(f"⚠ 策略库候选不可用：{e}", "warn")
            return []
        names = st.get("strategy_candidate_strategies") or []
        if isinstance(names, str):
            names = [x.strip() for x in names.split(",") if x.strip()]
        if names == ["daban_lianban_2plus_quality"]:
            names = list(DEFAULT_STRATEGY_CANDIDATE_STRATEGIES)
        top = int(st.get("strategy_candidate_top", st.get("max_candidates", 6)) or 6)
        max_codes = int(st.get("strategy_candidate_max_codes", 1200) or 1200)
        picks = []
        seen = set()
        for filename in names:
            try:
                res = sm.run(filename, [], max_codes=max_codes)
            except Exception as e:
                self._log(f"⚠ 策略候选 {filename} 执行失败：{e}", "warn")
                continue
            if not res or not res.get("ok"):
                self._log(f"⚠ 策略候选 {filename} 无有效结果：{(res or {}).get('msg', '')}", "warn")
                continue
            for row in res.get("results") or []:
                code = str((row or {}).get("code") or "").strip()
                if not code or code in seen:
                    continue
                seen.add(code)
                picks.append((code, (row or {}).get("name") or code))
                if len(picks) >= top:
                    self._log(f"AI strategy candidates: {filename} -> {len(picks)}")
                    return picks
        if picks:
            self._log(f"AI strategy candidates: {len(picks)}")
        return picks

    def _market_snapshot_degraded(self, universe: list, qc: dict) -> tuple[bool, str]:
        quotes = [qc.get(code) or {} for code in universe or []]
        usable = [q for q in quotes if q]
        if not usable:
            return True, "empty market snapshot"
        stub = 0
        for q in usable:
            code = str(q.get("code") or "")
            name = str(q.get("name") or "")
            price = _to_float(q.get("price"))
            chg = _to_float(q.get("change_pct"))
            amount = _to_float(q.get("amount")) or 0
            vol = _to_float(q.get("vol")) or _to_float(q.get("volume")) or 0
            if price == 10.0 and chg == 0.0 and amount <= 0 and vol <= 1000 and (not name or name == code):
                stub += 1
        if len(usable) >= 3 and stub / len(usable) >= 0.8:
            return True, "stub market quotes"
        return False, ""

    def _prescreen(self, st: dict) -> list:
        """全市场量化初筛（无 LLM）：从全市场行情快照按动量+流动性打分，取 top N。
        只做海选，真正的深度分析仍交给 6 个 Agent 团队。"""
        try:
            import strategy_manager as sm
            n_uni = int(st.get("prescreen_universe", 1200) or 1200)
            universe, qc = sm._full_market_universe_with_quotes(max_codes=n_uni)
        except Exception as e:
            self.market_data_status = {"degraded": True, "reason": str(e)[:120]}
            self._log(f"⚠ 全市场初筛取行情失败：{e}", "warn")
            return []
        if not universe:
            self.market_data_status = {"degraded": True, "reason": "empty market snapshot"}
            return []
        degraded, reason = self._market_snapshot_degraded(universe, qc)
        self.market_data_status = {"degraded": degraded, "reason": reason}
        if degraded:
            self._log(f"⚠ 全市场初筛行情源退化：{reason}，暂停使用全市场AI候选", "warn")
            return []
        top = int(st.get("prescreen_top", 6) or 6)
        if ai_candidate_ranker is not None:
            try:
                ranked = ai_candidate_ranker.rank_candidates(
                    universe,
                    qc,
                    market_context={},
                    top=top,
                    as_of_date=datetime.now().strftime("%Y-%m-%d"),
                )
                if ranked:
                    picks = [(r["code"], r.get("name") or r["code"]) for r in ranked[:top]]
                    self._log(f"AI research prescreen: {len(universe)} -> {len(picks)}")
                    return picks
            except Exception as e:
                logger.debug(f"ai_candidate_ranker fallback: {e}")
        scored = []
        for code in universe:
            q = qc.get(code) or {}
            name = q.get("name") or code
            price = _to_float(q.get("price")) or 0
            chg = _to_float(q.get("change_pct"))
            amount = _to_float(q.get("amount")) or 0     # 成交额（流动性）
            limit_up = _to_float(q.get("limit_up")) or 0
            nm = str(name).upper()
            if "ST" in nm or "退" in str(name):
                continue
            if price < 2 or price > 300 or chg is None:
                continue
            if limit_up and price >= limit_up * 0.999:   # 已封涨停，买不到
                continue
            if chg < -3 or chg > 9.3:                    # 过滤大跌 / 接近涨停追高
                continue
            # 初筛分：温和正动量 + 流动性（成交额开方降量纲）
            score = chg + (amount ** 0.5) / 1500.0
            scored.append((score, code, name))
        scored.sort(reverse=True)
        picks = [(c, nm) for _, c, nm in scored[:top]]
        self._log(f"🧮 全市场初筛：扫描{len(universe)}只 → 入选{len(picks)}只 "
                  f"（{', '.join(c for c, _ in picks)}）交团队深析")
        return picks

    # ── 各 Agent（LLM 调用 + 推送）────────────────────────
    def _llm(self, cfg, system, user, temperature=0.4, max_tokens=520):
        return _ai._call_llm(
            cfg,
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            temperature=temperature, max_tokens=max_tokens, timeout=120,
        )

    def _agent_market(self, cfg) -> str:
        self._agent_say(None, "market", "thinking")
        ctx = ""
        try:
            from market_context import get_market_summary_text
            ctx = get_market_summary_text() or ""
        except Exception:
            pass
        sys = ("你是A股市场分析师子Agent。基于大盘指数、板块与情绪，"
               "用4-6句话判断当前市场环境（强/中性/弱）、风险偏好与仓位基调。简洁、不空话。"
               "最后单独一行输出方向标记：[环境:强] 或 [环境:中] 或 [环境:弱]。")
        usr = f"当前市场背景数据：\n{ctx or '（实时大盘数据暂缺，按近期A股震荡格局保守判断）'}"
        try:
            text = self._llm(cfg, sys, usr, max_tokens=400) or "（市场分析无输出）"
        except Exception as e:
            text = f"市场分析失败：{e}"
        self._agent_say(None, "market", "done", text=text,
                        conclusion=text.strip().splitlines()[0][:60] if text else "")
        return text

    def _stock_context(self, code, name):
        """拉K线 + 指标 + 评分 + 实时行情，组装个股上下文。返回 (ctx_text, quote, ok)。"""
        from market_data import get_kline
        df = get_kline(code, period=80)
        if df is None or df.empty or len(df) < 30:
            return (f"{code} {name}：历史数据不足（需≥30日），无法技术分析。", {}, False)
        ind = _ai._calc_indicators(df)
        hist = _ai._price_history_text(df, days=12)
        try:
            from stock_scorer import score_stock
            score = score_stock(df)
        except Exception:
            score = {}
        quote = self._quote(code)
        cur = quote.get("price") or ind.get("price")
        ctx = (
            f"股票：{code} {name}\n"
            f"现价：{cur}  | 5日：{ind.get('chg_5d_pct')}%  20日：{ind.get('chg_20d_pct')}%\n"
            f"MA5/10/20：{ind.get('ma5')}/{ind.get('ma10')}/{ind.get('ma20')}  "
            f"RSI：{ind.get('rsi')}  MACD：{ind.get('macd_dif')}/{ind.get('macd_dea')}  "
            f"ATR：{ind.get('atr')}\n"
            f"六维评分：{score.get('total','—')}/100 信号:{score.get('signal','—')} "
            f"建议入场:{score.get('entry_price','—')} 止损:{score.get('stop_loss','—')} "
            f"目标:{score.get('target1','—')}\n"
            f"近12日价格：{hist}"
        )
        return (ctx, quote, True)

    def _quote(self, code) -> dict:
        try:
            import ths_quote as tq
            client = tq.get_client()
            if client and getattr(client, "_ok", False):
                qs = client.get_quotes([code]) or []
                if qs:
                    return qs[0]
        except Exception:
            pass
        return {}

    def _team_decide(self, cfg, code, name, market_view, st) -> dict | None:
        if self.stop_flag.is_set():
            return None
        ctx, quote, ok = self._stock_context(code, name)
        if not ok:
            self._agent_say(code, "technical", "done", conclusion=ctx)
            return None
        acct = self._account()
        holding = code in acct["positions"]
        hold_tag = "【当前持仓】请重点判断是否卖出/持有" if holding else "【未持仓】请判断是否买入"

        # 📊 技术分析师
        self._agent_say(code, "technical", "thinking")
        tech = self._safe_llm(cfg,
            "你是A股技术分析师子Agent。只看技术面（趋势/均线/量价/MACD/RSI/支撑压力），"
            "3-5句给出技术结论与方向偏好，先写一行【技术结论：偏多/偏空/中性 + 理由】，"
            "再在最末单独一行输出方向票：[方向:多] 或 [方向:空] 或 [方向:中]。",
            f"{ctx}\n市场环境：{market_view[:300]}", max_tokens=420)
        self._agent_say(code, "technical", "done", text=tech, conclusion=_last_line(tech))

        # 📰 基本面分析师
        self._agent_say(code, "fundamental", "thinking")
        fund_ctx = self._fund_text(code)
        fund = self._safe_llm(cfg,
            "你是A股基本面分析师子Agent。看估值/盈利/行业与消息面，3-4句结论，"
            "先写一行【基本面结论：支持/中性/谨慎 + 理由】，再在最末单独一行输出方向票："
            "[方向:多] 或 [方向:空] 或 [方向:中]。无数据则保守并给 [方向:中]。",
            f"{code} {name}\n{fund_ctx}", max_tokens=360)
        self._agent_say(code, "fundamental", "done", text=fund, conclusion=_last_line(fund))

        # 🐂🐻 多空辩论（一次调用产出两方，分别成卡）
        self._agent_say(code, "bull", "thinking")
        self._agent_say(code, "bear", "thinking")
        debate = self._safe_llm(cfg,
            "你是研究室主管，需同时给出多方和空方两份独立论述，并裁定哪方更占优。严格按格式：\n"
            "【多方】<2-3句看多论据>\n【空方】<2-3句看空风险>\n"
            "最后单独一行输出占优方：[占优:多] 或 [占优:空] 或 [占优:分歧]。",
            f"标的：{code} {name}\n技术：{_last_line(tech)}\n基本面：{_last_line(fund)}\n"
            f"市场：{market_view[:200]}", max_tokens=440)
        bull_txt, bear_txt = _split_debate(debate)
        self._agent_say(code, "bull", "done", text=bull_txt, conclusion=_first_sentence(bull_txt))
        self._agent_say(code, "bear", "done", text=bear_txt, conclusion=_first_sentence(bear_txt))

        # 🗳️ 共识门：个股 3 票（技术/基本面/多空辩论）为主，市场环境做分级修正
        votes = _tally_votes(market_view, tech, fund, debate)
        cmin = max(1, min(3, int(st["risk"].get("consensus_min_votes", 2))))
        mkt = votes["market"]
        # 市场环境分级修正：偏弱→门槛+1（更严，但强势个股3票全多仍可过）；偏强→-1（更松）；中性→不变
        eff = max(1, min(3, cmin + (1 if mkt == "short" else (-1 if mkt == "long" else 0))))
        sl, ss, sn = votes["stock_long"], votes["stock_short"], votes["stock_neutral"]
        buy_ok = sl >= eff and sl > ss
        mkt_cn = {"long": "偏强", "short": "偏弱", "neutral": "中性"}.get(mkt, "中性")
        consensus = {"long": sl, "short": ss, "neutral": sn, "market": mkt, "market_cn": mkt_cn,
                     "min": cmin, "eff_min": eff, "buy_ok": buy_ok, "detail": votes["detail"]}
        vote_txt = (f"个股方向票(技术/基本面/多空)：看多{sl}/看空{ss}/中性{sn}｜市场环境：{mkt_cn}"
                    f"（买入门槛：{mkt_cn}市需≥{eff}票个股看多且多>空 → "
                    f"{'达标 ✓' if buy_ok else '未达标 ✗，不得买入'}）")

        # 🛡️ 风控官（规则护栏 + 共识门结果）
        risk_ctx = self._risk_context(st, acct)
        self._agent_say(code, "risk", "done", text=risk_ctx + "\n" + vote_txt, conclusion=vote_txt)

        if self.stop_flag.is_set():
            return None
        # 🎯 主 Agent：首席交易员，汇总裁决 → 结构化指令
        self._agent_say(code, "lead", "thinking")
        lead_sys = (
            "你是首席交易员（主Agent），统领整个交易团队。综合下属子Agent的结论做最终裁决，"
            "严格输出 JSON（不要多余文字）：\n"
            '{"action":"BUY|SELL|HOLD","confidence":0-100,'
            '"entry":数字或null,"stop_loss":数字或null,"take_profit":数字或null,'
            '"reason":"一句话核心理由(40字内)"}\n'
            "纪律：信心不足或多空分歧大→HOLD；持仓且转弱/触及风险→SELL；"
            "未持仓且多方占优且技术面配合→BUY。"
            "★团队看多共识未达标时，绝对不得给 BUY（只能 HOLD 或对持仓 SELL）。务必尊重风控约束。")
        lead_usr = (
            f"{hold_tag}\n标的：{code} {name}\n现价：{quote.get('price') or '—'}\n"
            f"市场环境：{market_view[:260]}\n"
            f"技术：{_last_line(tech)}\n基本面：{_last_line(fund)}\n"
            f"多方：{_first_sentence(bull_txt)}\n空方：{_first_sentence(bear_txt)}\n"
            f"{vote_txt}\n风控约束：{risk_ctx}")
        raw = self._safe_llm(cfg, lead_sys, lead_usr, temperature=0.3, max_tokens=500)
        dec = _ai._safe_parse_json(raw) or {}
        action = str(dec.get("action", "HOLD")).upper()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"
        decision = {
            "id": uuid.uuid4().hex[:10],
            "code": code, "name": name,
            "action": action,
            "confidence": _to_int(dec.get("confidence"), 0),
            "entry": _to_float(dec.get("entry")),
            "stop_loss": _to_float(dec.get("stop_loss")),
            "take_profit": _to_float(dec.get("take_profit")),
            "reason": str(dec.get("reason", ""))[:80],
            "price": _to_float(quote.get("price")) or _to_float(dec.get("entry")),
            "holding": holding,
            "consensus": consensus,
            "time": datetime.now().strftime("%H:%M:%S"),
            "raw": raw[:400] if isinstance(raw, str) else "",
        }
        lead_concl = f"{action} · 信心{decision['confidence']} · {decision['reason']}"
        # 每个 agent 对该票的留痕（供决策时间线逐票追溯）
        decision["agents"] = [
            _agent_entry("market",      _first_sentence(market_view), market_view),
            _agent_entry("technical",   _last_line(tech),  tech),
            _agent_entry("fundamental", _last_line(fund),  fund),
            _agent_entry("bull",        _first_sentence(bull_txt), bull_txt),
            _agent_entry("bear",        _first_sentence(bear_txt), bear_txt),
            _agent_entry("risk",        vote_txt, risk_ctx + "\n" + vote_txt),
            _agent_entry("lead",        lead_concl, raw),
        ]
        self._agent_say(code, "lead", "done", text=raw, conclusion=lead_concl)
        return decision

    def _safe_llm(self, cfg, system, user, **kw):
        try:
            return self._llm(cfg, system, user, **kw) or "（无输出）"
        except Exception as e:
            return f"（调用失败：{e}）"

    def _fund_text(self, code) -> str:
        try:
            f = _ai._get_fundamentals(code)
            if isinstance(f, dict) and f:
                return "  ".join(f"{k}:{v}" for k, v in list(f.items())[:12])
        except Exception:
            pass
        return "（基本面数据暂缺）"

    def _risk_context(self, st, acct=None) -> str:
        acct = acct or self._account()
        r = st["risk"]
        pos_n = len(acct["positions"])
        cap = acct["capital"]
        return (f"账户权益¥{cap:,.0f}（现金¥{acct['cash']:,.0f}）；当前持仓{pos_n}/{r['max_positions']}只；"
                f"单票上限{r['single_max_ratio']*100:.0f}%；单笔买入{r['per_trade_ratio']*100:.0f}%；"
                f"当日已交易{self._today_trades()}/{r['daily_trade_limit']}笔；"
                f"当日回撤熔断线{r['daily_max_loss_pct']*100:.0f}%。")

    # ── 决策落地：风控硬闸 → 执行(模拟) / 待确认(实盘)──────
    def _handle_decision(self, d: dict, st: dict):
        acct = None
        if ai_trade_plan is not None or ai_position_sizer is not None:
            try:
                acct = self._account()
                holding = d.get("code") in (acct.get("positions") or {})
            except Exception:
                holding = bool(d.get("holding"))
            if ai_trade_plan is not None:
                try:
                    plan = ai_trade_plan.normalize_trade_plan(
                        d,
                        d.get("code", ""),
                        d.get("name", d.get("code", "")),
                        current_price=d.get("price"),
                        as_of_date=datetime.now().strftime("%Y-%m-%d"),
                        holding=holding,
                    )
                    d["plan"] = plan
                    for key in ("action", "confidence", "entry", "stop_loss", "take_profit", "reason", "price", "holding"):
                        d[key] = plan.get(key)
                except Exception as e:
                    logger.debug(f"ai_trade_plan normalize skipped: {e}")
            if ai_position_sizer is not None and d.get("action") == "BUY" and acct:
                try:
                    r = st.get("risk") or {}
                    d["position_size"] = ai_position_sizer.suggest_position_size(
                        acct.get("cash", 0),
                        acct.get("capital", 0),
                        d.get("price"),
                        d.get("confidence"),
                        {
                            "per_trade_ratio": r.get("per_trade_ratio"),
                            "single_max_ratio": r.get("single_max_ratio"),
                        },
                    )
                except Exception as e:
                    logger.debug(f"ai_position_sizer skipped: {e}")
        self.recent_decisions.append(d)
        self._push("ai_decision", d)
        self._audit({"type": "decision", **{k: d[k] for k in
                    ("code", "name", "action", "confidence", "reason")}})
        if ai_research_store is not None:
            try:
                rec = dict(d)
                rec.setdefault("as_of_date", datetime.now().strftime("%Y-%m-%d"))
                rec.setdefault("features", {})
                ai_research_store.record_decision(self.uid, rec)
            except Exception as e:
                logger.debug(f"ai_research_store record_decision skipped: {e}")

        if d["action"] == "HOLD":
            return
        acct = acct or self._account()
        ok, why = self._risk_gate(d, st, acct)
        if not ok:
            d2 = {**d, "blocked": why}
            self._push("ai_decision", d2)
            self._log(f"🛡️ 风控拦截 {d['code']} {d['action']}：{why}", "warn")
            return

        mode = self._broker_mode()
        if mode == "paper":
            self._execute(d, st, acct=acct)
        else:
            # 实盘：生成待确认，等用户点确认
            self.pending[d["id"]] = {**d, "mode": "live", "status": "pending"}
            self._push("ai_pending", self.pending[d["id"]])
            self._log(f"⏳ 实盘待确认：{d['action']} {d['code']} {d['name']} "
                      f"（信心{d['confidence']}）{d['reason']}", "warn")

    def _risk_gate(self, d: dict, st: dict, acct=None):
        acct = acct or self._account()
        r = st["risk"]
        if d["confidence"] < r["min_confidence"]:
            return False, f"信心{d['confidence']}<阈值{r['min_confidence']}"
        if self._today_trades() >= r["daily_trade_limit"]:
            return False, f"当日交易已达上限{r['daily_trade_limit']}笔"
        positions = acct["positions"]
        if d["action"] == "BUY":
            if st.get("block_buy_on_degraded_data", True) and self.market_data_status.get("degraded"):
                reason = self.market_data_status.get("reason") or "unknown"
                return False, f"market data degraded: {reason}; 已暂停AI新开仓"
            cons = d.get("consensus") or {}
            if cons and not cons.get("buy_ok", True):
                return False, (f"团队未达成看多共识（个股看多{cons.get('long',0)}/看空{cons.get('short',0)}，"
                               f"市场{cons.get('market_cn','中性')}，{cons.get('market_cn','本')}市需≥"
                               f"{cons.get('eff_min', cons.get('min','?'))}票个股看多）")
            if d["code"] in positions:
                return False, "已持有该票（本轮不加仓）"
            if len(positions) >= r["max_positions"]:
                return False, f"持仓已达上限{r['max_positions']}只"
            price = d.get("price") or 0
            if price <= 0:
                return False, "无有效买入价"
            budget = acct["capital"] * r["per_trade_ratio"]
            if budget < price * 100:
                return False, "可用预算不足一手"
            if acct["cash"] < price * 100:
                return False, "可用现金不足一手"
        elif d["action"] == "SELL":
            if d["code"] not in positions:
                return False, "未持有该票，无法卖出"
        return True, ""

    def _execute(self, d: dict, st: dict, source="auto", acct=None):
        """实际下单：模拟盘=AI 独立账户即时成交；实盘=用户主账户（经人工确认后调用）。"""
        acct = acct or self._account()
        r = st["risk"]
        code, name = d["code"], d["name"]
        price = d.get("price") or 0
        paper = self._broker_mode() == "paper"
        reason = f"AI交易·{d['reason']}"
        try:
            if d["action"] == "BUY":
                sized = d.get("position_size") or {}
                shares = int(sized.get("shares") or 0)
                if shares <= 0:
                    budget = acct["capital"] * r["per_trade_ratio"]
                    shares = max(int(budget / price / 100) * 100, 100) if price > 0 else 100
                if paper:
                    res = self._ai_broker().submit_buy(code, name, price, shares, reason=reason)
                else:
                    import main  # 延迟导入，避免循环依赖
                    res = main.execute_buy(self.uid, code, name, price, shares, reason=reason)
                if res.get("ok") and d.get("stop_loss"):
                    self.targets[code] = {"stop": d.get("stop_loss"),
                                          "target": d.get("take_profit")}
            else:  # SELL
                if paper:
                    res = self._ai_broker().submit_sell(code, price, sell_reason=reason)
                else:
                    import main
                    res = main.execute_sell(self.uid, code, price, sell_reason=reason)
                self.targets.pop(code, None)
            if res.get("ok"):
                self._bump_trades()
        except Exception as e:
            res = {"ok": False, "msg": str(e)}
        rec = {"code": code, "name": name, "action": d["action"],
               "price": price, "ok": bool(res.get("ok")),
               "msg": res.get("msg", "已成交" if res.get("ok") else "失败"),
               "mode": self._broker_mode(), "source": source,
               "time": datetime.now().strftime("%H:%M:%S")}
        self.recent_execs.append(rec)
        self._push("ai_exec", rec)
        self._push("ai_account", self.ai_account())
        self._audit({"type": "exec", **rec})
        icon = "✅" if rec["ok"] else "❌"
        self._log(f"{icon} AI {d['action']} {code} {name} @{price} {rec['msg']}",
                  "buy" if rec["ok"] else "error")
        self._push_status()
        return res

    # ── 实盘人工确认 ──────────────────────────────────────
    def confirm(self, pid: str, approve: bool) -> dict:
        d = self.pending.pop(pid, None)
        if not d:
            return {"ok": False, "msg": "该待确认已失效"}
        st = load_settings(self.uid)
        if approve:
            self._execute(d, st, source="confirmed")
            self._push("ai_pending_resolved", {"id": pid, "approved": True})
            return {"ok": True, "msg": "已确认执行"}
        self._log(f"🚫 已拒绝 AI {d['action']} {d['code']}", "warn")
        self._push("ai_pending_resolved", {"id": pid, "approved": False})
        return {"ok": True, "msg": "已拒绝"}

    # ── 实时风控监控线程（规则化止盈止损 + 熔断）──────────
    def _monitor_loop(self):
        while not self.stop_flag.is_set():
            try:
                st = load_settings(self.uid)
                from market_data import is_trading_time
                if st["ignore_market_time"] or is_trading_time():
                    self._monitor_once(st)
            except Exception as e:
                logger.debug(f"monitor uid={self.uid}: {e}")
            self.stop_flag.wait(max(5, load_settings(self.uid).get("monitor_sec", 20)))

    def _monitor_once(self, st):
        acct = self._account()
        positions = acct["positions"]
        if not positions:
            return
        codes = list(positions.keys())
        try:
            import ths_quote as tq
            client = tq.get_client()
            quotes = (client.get_quotes(codes) if client and getattr(client, "_ok", False) else []) or []
        except Exception:
            quotes = []
        qmap = {q.get("code"): q for q in quotes}
        total_float_pnl = 0.0
        for code, p in positions.items():
            q = qmap.get(code, {})
            price = _to_float(q.get("price"))
            cost = _to_float(p.get("cost"))
            shares = _to_float(p.get("shares"))
            if not price or not cost:
                continue
            total_float_pnl += (price - cost) * shares
            tgt = self.targets.get(code, {})
            stop = tgt.get("stop")
            target = tgt.get("target")
            reason = ""
            if stop and price <= stop:
                reason = f"触发AI止损线{stop}"
            elif target and price >= target:
                reason = f"触及AI目标位{target}"
            if reason:
                d = {"id": uuid.uuid4().hex[:10], "code": code,
                     "name": p.get("name", code), "action": "SELL",
                     "confidence": 100, "price": price, "holding": True,
                     "reason": reason, "stop_loss": None, "take_profit": None,
                     "entry": None, "time": datetime.now().strftime("%H:%M:%S"),
                     "by": "monitor"}
                self.targets.pop(code, None)  # 防重复触发
                self._log(f"🛡️ 实时监控：{code} {reason}，发起卖出", "warn")
                self._handle_decision(d, st)
        # 当日回撤熔断
        cap = acct["capital"] or 0
        limit = st["risk"]["daily_max_loss_pct"]
        if cap > 0 and total_float_pnl < -cap * limit and not self.halted_reason:
            self.halted_reason = f"当日浮亏触及熔断线（{limit*100:.0f}%）"
            self._log(f"🔥 {self.halted_reason}，自动停机", "error")
            self.stop(self.halted_reason)


# ── 引擎注册表 ──────────────────────────────────────────────
_engines: dict[int, _Engine] = {}
_engines_lock = threading.Lock()


def get_engine(uid: int) -> _Engine:
    with _engines_lock:
        if uid not in _engines:
            _engines[uid] = _Engine(uid)
        return _engines[uid]


# ══════════════════════════════════════════════════════════════
# 6. 文本解析小工具
# ══════════════════════════════════════════════════════════════
def _last_line(text: str) -> str:
    if not text:
        return ""
    lines = [l.strip() for l in str(text).splitlines() if l.strip()]
    return lines[-1][:120] if lines else ""


def _first_sentence(text: str) -> str:
    if not text:
        return ""
    t = str(text).strip()
    for sep in ("。", "\n", "；", ";"):
        if sep in t:
            return t.split(sep)[0][:120]
    return t[:120]


def _split_debate(text: str):
    bull, bear = "", ""
    if not text:
        return "（无多方输出）", "（无空方输出）"
    t = str(text)
    if "【空方】" in t:
        head, bear = t.split("【空方】", 1)
        bull = head.replace("【多方】", "").strip()
        bear = bear.strip()
    else:
        bull = t.replace("【多方】", "").strip()
        bear = "（未生成空方观点）"
    return (bull or "（无多方输出）"), (bear or "（无空方输出）")


def _to_float(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default
