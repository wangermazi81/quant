# -*- coding: utf-8 -*-
"""
见微 Alpha 本地交易下单桥接器 (Cloud-Local Bridge)
部署环境: Windows 本地交易电脑 (与登录同花顺客户端的电脑一致)
功能: 24小时自动心跳轮询云端服务器交易信号，驱动本地同花顺下单，并自动反馈成交状态。
"""

import os
import sys
import time
import secrets
import json
import logging
import sqlite3
import importlib
import requests
from pathlib import Path

# ── 清除系统代理（避免 v2ray/clash 等本地代理拦截到云端服务器的请求）──
# 本地往云端 5000 端口走直连，不需要也不应该走代理
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy", "SOCKS_PROXY", "socks_proxy"):
    os.environ.pop(_k, None)

# ── 强制 stdout/stderr 用 UTF-8 ──────────────────────────────
# Windows 控制台/重定向默认 GBK，日志里的 emoji(🎉✨📈等)写不出会抛
# UnicodeEncodeError 刷屏、并可能影响轮询；用 errors="replace" 兜底，永不因日志崩。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── 自动设置系统路径，使其能够导入 ths_operator ────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import order_sizing


# ── 日志配置 ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "logs" / "bridge.log", encoding="utf-8") if (BASE_DIR / "logs").exists() else logging.FileHandler("bridge.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("Bridge")

# ── 默认配置（如果不存在 bridge_config.json 则使用默认值） ──────
CONFIG_FILE = Path(__file__).parent / "bridge_config.json"
DEFAULT_CONFIG = {
    "server_url": "http://127.0.0.1:5001",  # 本地调试用 5001，云端部署请改为 http://YOUR_SERVER_IP:5000
    "username": "admin",
    "password": "your_password_here",
    "poll_interval_sec": 1.0,
    "executor_type": "desktop",
    "android_profile": "tools/mobile_ths_profile.json",
    "android_api_profile": "tools/mobile_ths_api_profile.json",
    "android_session_path": "logs/mobile_capture/session.json",
    "confirm_mode": "manual",
    "device_timeout_sec": 10,
    "page_timeout_sec": 8,
    "reconcile_timeout_sec": 20,
    "api_session_max_age_sec": 300,
    "max_order_amount": 50000.0,
    "use_mobile_available_cash": True,
    "bridge_user_id": 1,
    "bridge_token": "",
    "execution_state_path": "logs/bridge_execution_state.db",
}


def _config_path(value):
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


def build_operator(config: dict):
    executor_type = str(config.get("executor_type", "desktop")).lower()
    if executor_type == "desktop":
        module = importlib.import_module("broker.ths_operator")
        return module.THSOperator()
    if executor_type == "android":
        api_module = importlib.import_module("broker.mobile_ths_api")
        operator_module = importlib.import_module("broker.mobile_ths_operator")
        api = api_module.MobileTHSApi(
            _config_path(config["android_api_profile"]),
            _config_path(config.get("android_session_path", "logs/mobile_capture/session.json")),
        )
        operator = operator_module.MobileTHSOperator(
            _config_path(config["android_profile"]),
            api,
        )
        operator.profile.update({
            "confirm_mode": config.get("confirm_mode", "manual"),
            "device_timeout_sec": config.get("device_timeout_sec", 10),
            "page_timeout_sec": config.get("page_timeout_sec", 8),
            "reconcile_timeout_sec": config.get("reconcile_timeout_sec", 20),
        })
        api.profile["session_max_age_sec"] = config.get("api_session_max_age_sec", 300)
        return operator
    raise ValueError(f"unknown executor_type={executor_type}")
def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
                logger.info(f"成功加载本地配置文件: {CONFIG_FILE}")
                merged = {**DEFAULT_CONFIG, **cfg}
                changed = False
                if not str(merged.get("bridge_token") or "").strip():
                    merged["bridge_token"] = secrets.token_urlsafe(32)
                    changed = True
                for key in DEFAULT_CONFIG:
                    if key not in cfg:
                        cfg[key] = merged[key]
                        changed = True
                if changed:
                    cfg["bridge_token"] = merged["bridge_token"]
                    with open(CONFIG_FILE, "w", encoding="utf-8") as wf:
                        json.dump(cfg, wf, indent=4, ensure_ascii=False)
                    logger.info("已补齐桥接配置缺失项")
                return merged
        except Exception as e:
            logger.warning(f"加载配置文件出错: {e}，将使用默认配置")
    else:
        # 首次运行自动生成模板配置文件
        try:
            DEFAULT_CONFIG["bridge_token"] = secrets.token_urlsafe(32)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
            logger.info(f"已自动生成默认桥接配置文件模版: {CONFIG_FILE}，请修改后使用！")
        except Exception as e:
            logger.warning(f"无法写入默认配置文件: {e}")
    return DEFAULT_CONFIG


class CloudLocalBridge:
    def __init__(self):
        self.config = load_config()
        self.server_url = self.config["server_url"].rstrip("/")
        self.username = self.config["username"]
        self.password = self.config["password"]
        self.bridge_token = (self.config.get("bridge_token") or "").strip()
        self.session_cookie = (self.config.get("session_cookie") or "").strip()
        self.session_cookie_name = self.config.get("session_cookie_name") or "session"
        self.poll_interval = float(self.config["poll_interval_sec"])
        self.execution_state_path = _config_path(
            self.config.get("execution_state_path", "logs/bridge_execution_state.db")
        )
        
        self.session = requests.Session()
        if self.bridge_token:
            self.session.headers.update({"X-Bridge-Token": self.bridge_token})
        self.operator = None
        self.is_connected_ths = False
        self.manual_confirm_pending = False

    def init_ths(self):
        """初始化同花顺 GUI 自动化连接"""
        logger.info("正在尝试连接本地同花顺客户端...")
        try:
            self.operator = build_operator(self.config)
            self.is_connected_ths = True
            logger.info("🚀 同花顺操作员初始化完成，已处于待命下单状态")
        except Exception as e:
            self.is_connected_ths = False
            logger.error(f"❌ 初始化本地同花顺操作器失败: {e}")

    def login(self) -> bool:
        """连接见微服务。优先走本机桥接 token，避开网页登录限流。"""
        if self.session_cookie:
            self.session.cookies.set(self.session_cookie_name, self.session_cookie, path="/")
            logger.info("Loaded local session cookie from bridge_config.json; skip password login")
            try:
                resp = self.session.get(f"{self.server_url}/api/auth/me", timeout=5)
                if resp.status_code == 200 and resp.json().get("ok"):
                    logger.info("Session cookie verified; bridge connected")
                    return True
                logger.error(f"Session cookie verification failed: HTTP {resp.status_code} {resp.text[:200]}")
                return False
            except Exception as e:
                logger.error(f"Session cookie verification error: {e}")
                return False

        if self.bridge_token:
            try:
                resp = self.session.get(f"{self.server_url}/api/auth/me", timeout=5)
                if resp.status_code == 200 and resp.json().get("ok"):
                    logger.info("桥接 token 验证通过，已连接见微服务")
                    return True
                logger.error(f"桥接 token 验证失败: HTTP {resp.status_code} {resp.text[:200]}")
                return False
            except Exception as e:
                logger.error(f"桥接 token 验证异常: {e}")
                return False

        if self.session_cookie:
            self.session.cookies.set(self.session_cookie_name, self.session_cookie, path="/")
            logger.info("Loaded local session cookie from bridge_config.json; skip password login")
            try:
                resp = self.session.get(f"{self.server_url}/api/bridge/signals", timeout=5)
                if resp.status_code == 200 and resp.json().get("ok"):
                    logger.info("Session cookie verified; bridge connected")
                    return True
                logger.error(f"Session cookie verification failed: HTTP {resp.status_code} {resp.text[:200]}")
                return False
            except Exception as e:
                logger.error(f"Session cookie verification error: {e}")
                return False

        login_url = f"{self.server_url}/api/auth/login"
        logger.info(f"正在尝试登录云端服务器: {login_url} (用户: {self.username})...")
        try:
            resp = self.session.post(
                login_url, 
                json={"username": self.username, "password": self.password}, 
                timeout=10
            )
            res = resp.json()
            if res.get("ok"):
                logger.info(f"🎉 登录成功！账户积分: {res.get('points')}")
                return True
            else:
                logger.error(f"❌ 登录失败: {res.get('error', '未知错误')}")
                return False
        except Exception as e:
            logger.error(f"❌ 无法连接到云端服务器: {e}")
            return False

    def fetch_signals(self) -> list:
        """向云端拉取 pending 交易信号"""
        url = f"{self.server_url}/api/bridge/signals"
        try:
            resp = self.session.get(url, timeout=5)
            if resp.status_code in (401, 403):
                logger.warning("🔑 Session 已过期，准备重新登录...")
                if self.login():
                    resp = self.session.get(url, timeout=5)
                else:
                    return []
            
            res = resp.json()
            if res.get("ok"):
                return res.get("signals", [])
            else:
                logger.error(f"拉取信号接口返回错误: {res.get('error')}")
                return []
        except Exception as e:
            logger.error(f"拉取交易信号失败: {e}")
            return []

    def report_result(self, signal_id: int, status: str, msg: str, exec_price=None):
        """上报成交或失败结果给云端。

        exec_price 是**真实成交均价**（执行器从今日委托/成交页读到）。云端台账据此
        记成本；拿不到才回退信号价。打板/追高时两者可差 1%-3%，用信号价当成本会让
        止损止盈基于错误基准（可能提前止盈、延迟止损）。
        """
        url = f"{self.server_url}/api/bridge/signals/{signal_id}/report"
        payload = {"status": status, "msg": msg}
        try:
            if exec_price is not None and float(exec_price) > 0:
                payload["exec_price"] = float(exec_price)
        except (TypeError, ValueError):
            pass
        try:
            resp = self.session.post(url, json=payload, timeout=5)
            if resp.status_code == 200 and resp.json().get("ok"):
                logger.info(f"✅ 信号 ID={signal_id} 执行结果成功上报至云端")
            else:
                logger.error(f"❌ 信号 ID={signal_id} 结果上报云端失败: {resp.text}")
        except Exception as e:
            logger.error(f"上报信号 ID={signal_id} 结果出现异常: {e}")

    @staticmethod
    def _num(value):
        try:
            return float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return 0.0

    def _clamp_buy_shares_by_mobile_cash(self, price, shares):
        if not self.config.get("use_mobile_available_cash", True):
            return int(shares), ""
        if self.operator is None or not hasattr(self.operator, "get_account"):
            return int(shares), ""
        account = self.operator.get_account()
        if not isinstance(account, dict) or account.get("ok") is False:
            return int(shares), ""
        raw_available = account.get("available")
        price_value = self._num(price)
        requested = int(int(shares) / order_sizing.LOT) * order_sizing.LOT
        max_order_amount = self._num(self.config.get("max_order_amount"))

        if raw_available is None or raw_available == "":
            safe, clamped_by_amount = order_sizing.clamp_to_max_order(requested, price_value, max_order_amount)
            if safe < order_sizing.LOT:
                return 0, f"单笔金额护栏低于一手: @{price_value:.2f}"
            msg = "手机未暴露可用资金，已按单笔金额护栏执行"
            if clamped_by_amount:
                msg = f"手机未暴露可用资金，单笔金额护栏夹仓到 {safe}股"
            return safe, msg

        available = self._num(raw_available)
        if available <= 0:
            return 0, "手机资金回读可用资金为0，本次未下单"
        by_cash = order_sizing.strict_lot_shares(available, price_value)
        if by_cash < order_sizing.LOT:
            return 0, f"手机可用资金不足一手: 可用¥{available:,.0f} @{price_value:.2f}"
        safe = min(requested, by_cash)
        safe, clamped_by_amount = order_sizing.clamp_to_max_order(safe, price_value, max_order_amount)
        if safe < order_sizing.LOT:
            return 0, f"手机资金/单笔金额护栏低于一手: 可用¥{available:,.0f} @{price_value:.2f}"
        if safe < requested:
            return safe, f"手机资金夹仓 {requested}->{safe}股，可用¥{available:,.0f}"
        if clamped_by_amount:
            return safe, f"单笔金额护栏夹仓到 {safe}股"
        return safe, ""

    def _execution_conn(self):
        path = Path(self.execution_state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS bridge_executions ("
            "signal_id INTEGER PRIMARY KEY, "
            "status TEXT NOT NULL, "
            "msg TEXT NOT NULL, "
            "exec_price REAL, "
            "manual_confirm INTEGER NOT NULL DEFAULT 0, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        return conn

    def _load_execution_result(self, signal_id: int):
        with self._execution_conn() as conn:
            row = conn.execute(
                "SELECT status, msg, exec_price, manual_confirm "
                "FROM bridge_executions WHERE signal_id=?",
                (int(signal_id),),
            ).fetchone()
        return dict(row) if row else None

    def _save_execution_result(self, signal_id: int, status: str, msg: str,
                               exec_price=None, manual_confirm: bool = False):
        with self._execution_conn() as conn:
            conn.execute(
                "INSERT INTO bridge_executions "
                "(signal_id, status, msg, exec_price, manual_confirm, updated_at) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(signal_id) DO UPDATE SET "
                "status=excluded.status, msg=excluded.msg, "
                "exec_price=excluded.exec_price, manual_confirm=excluded.manual_confirm, "
                "updated_at=CURRENT_TIMESTAMP",
                (int(signal_id), str(status), str(msg), exec_price, int(bool(manual_confirm))),
            )

    def _replay_execution_result(self, signal_id: int, saved: dict):
        status = saved["status"]
        msg = saved["msg"]
        exec_price = saved.get("exec_price")
        manual_confirm = bool(saved.get("manual_confirm"))
        if status == "executing":
            status = "submitted"
            msg = "本地执行中断，需券商对账确认最终状态"
            self._save_execution_result(signal_id, status, msg, exec_price, manual_confirm)
        self.report_result(signal_id, status, msg, exec_price=exec_price)
        return {"status": status, "manual_confirm": manual_confirm,
                "signal_id": signal_id, "msg": msg}
    def execute_signal(self, signal: dict):
        """在本地同花顺上执行交易信号"""
        sig_id = signal["id"]
        code = signal["code"]
        name = signal.get("name", "")
        action = signal["action"]
        price = signal["price"]
        shares = signal["shares"]

        saved = self._load_execution_result(sig_id)
        if saved:
            return self._replay_execution_result(sig_id, saved)
        
        logger.info(f"🔔 抓取到新实盘交易信号: ID={sig_id} | {action} | {code} ({name}) | {shares}股 @{price:.2f}")

        # 检查是否连接到同花顺
        if not self.is_connected_ths or self.operator is None:
            self.init_ths()
            if not self.is_connected_ths or self.operator is None:
                logger.error("❌ 本地未检测到正常运行的同花顺，拒绝执行并汇报失败")
                self.report_result(sig_id, "failed", "本地未启动或无法连接同花顺客户端")
                self._save_execution_result(sig_id, "failed", "本地未启动或无法连接同花顺客户端")
                return

        # 执行下单 — operator.buy/sell 现在返回 dict 而非 bool
        logger.info(f"🛠 正在唤起同花顺界面执行 {action} 操作...")
        self._save_execution_result(sig_id, "executing", "已开始调用本地交易执行器")
        before_shares = 0
        try:
            if hasattr(self.operator, "get_positions"):
                before_positions = self.operator.get_positions()
                if isinstance(before_positions, list):
                    for position in before_positions:
                        code_matched = str(position.get("code", "")) == str(code)
                        name_matched = bool(name) and str(position.get("name", "")) == str(name)
                        if code_matched or name_matched:
                            before_shares = float(position.get("shares", 0) or 0)
                            break
        except Exception as e:
            logger.warning(f"下单前持仓回读跳过: {e}")
        try:
            if action == "BUY":
                safe_shares, cash_msg = self._clamp_buy_shares_by_mobile_cash(price, shares)
                if safe_shares <= 0:
                    logger.error(f"❌ 实盘 BUY 拒单: {cash_msg}")
                    self.report_result(sig_id, "failed", cash_msg)
                    self._save_execution_result(sig_id, "failed", cash_msg)
                    return
                if safe_shares != int(shares):
                    logger.info(f"🛡 {cash_msg}")
                    shares = safe_shares
            if action == "BUY":
                result = self.operator.buy(code, price, shares)
            elif action == "SELL":
                result = self.operator.sell(code, price, shares)
            elif action == "CANCEL":
                if not hasattr(self.operator, "cancel_order"):
                    raise RuntimeError(f"执行器缺少 cancel_order: {type(self.operator).__name__}")
                result = self.operator.cancel_order(
                    code=code,
                    action=signal.get("target_action") or signal.get("target_side"),
                    name=name,
                )
            else:
                result = {"status": "failed", "msg": f"未知交易动作 action={action}"}
                logger.error(f"未知交易动作 action={action}")

            # 兼容旧版返回 bool 的情况
            if isinstance(result, bool):
                result = ({"status": "executed", "msg": "GUI 提交成功 (legacy bool 返回值)"}
                          if result else
                          {"status": "failed",   "msg": "GUI 提交失败 (legacy bool 返回值)"})

            status = result.get("status", "failed")
            msg    = result.get("msg", "未知")
            # 执行器若读到了真实成交均价就带上（mobile_ths_operator 会给 exec_price）
            exec_px = result.get("exec_price") or result.get("avg_price")
            # 富信息日志：让本地网页运行日志能看到对话框具体内容
            if result.get("dialog_title"):
                msg = f"{msg} | 对话框=[{result['dialog_title']}] {result.get('dialog_body','')}"

            manual_confirm = str(result.get("confirm_mode") or self.config.get("confirm_mode", "manual")).lower() == "manual"
            if action == "CANCEL":
                manual_confirm = False
            if status == "submitted" and manual_confirm:
                msg = "已停在手机确认页，等待手动最终确认"
            elif action != "CANCEL" and status == "submitted" and hasattr(self.operator, "reconcile_order"):
                reconcile = self.operator.reconcile_order(
                    action,
                    code,
                    before_shares,
                    order_ref=result.get("order_ref"),
                    name=name,
                )
                if isinstance(reconcile, dict):
                    status = reconcile.get("status", status)
                    msg = reconcile.get("msg", msg)
                    exec_px = reconcile.get("exec_price") or exec_px
                    logger.info(f"🔎 手机端回读对账: status={status} | source={reconcile.get('source', '')}")

            icon = {"executed": "??", "submitted": "??", "cancelled": "??", "failed": "?"}.get(status, "?")
            logger.info(f"{icon} 实盘 {action} 结果: status={status} | {msg}"
                        + (f" | 成交均价={exec_px}" if exec_px else " | 未读到成交价(按信号价记账)"))
            self.report_result(sig_id, status, msg, exec_price=exec_px)
            self._save_execution_result(sig_id, status, msg, exec_px, manual_confirm)
            return {"status": status, "manual_confirm": manual_confirm, "signal_id": sig_id, "msg": msg}
        except Exception as e:
            logger.error(f"❌ 本地执行下单发生异常: {e}")
            msg = f"本地执行异常，需券商对账: {str(e)}"
            self._save_execution_result(sig_id, "submitted", msg)
            self.report_result(sig_id, "submitted", msg)

    def run(self):
        """主轮询循环"""
        logger.info("==============================================")
        logger.info("   见微 Alpha 本地同花顺交易下单桥接客户端启动")
        logger.info("==============================================")
        
        # 1. 初始化同花顺
        self.init_ths()
        
        # 2. 登录云端服务器
        connected = False
        retry_wait = 5
        while not connected:
            connected = self.login()
            if not connected:
                logger.info(f"{retry_wait}秒后重新尝试连接见微服务...")
                time.sleep(retry_wait)
                retry_wait = min(60, retry_wait + 5)
        
        logger.info("✨ 桥接器准备就绪，进入 24H 交易信号轮询监听态...")
        
        # 3. 轮询循环
        error_count = 0
        while True:
            try:
                signals = self.fetch_signals()
                if signals:
                    logger.info(f"收到 {len(signals)} 个待处理实盘信号")
                    for sig in signals:
                        result = self.execute_signal(sig) or {}
                        if result.get("status") == "submitted" and result.get("manual_confirm"):
                            self.manual_confirm_pending = True
                            logger.info("Manual confirm mode: one order has been filled to the phone confirmation page; bridge exits to avoid overwriting it.")
                            return
                
                error_count = 0  # 正常执行，错误计数重置
            except Exception as e:
                error_count += 1
                logger.error(f"轮询循环发生未知异常 #{error_count}: {e}")
                if error_count >= 5:
                    logger.warning("连续发生5次轮询失败，尝试重新登录恢复 Session...")
                    self.login()
                    error_count = 0
            
            time.sleep(self.poll_interval)


if __name__ == "__main__":
    bridge = CloudLocalBridge()
    try:
        bridge.run()
    except KeyboardInterrupt:
        logger.info("👋 桥接器已被手动终止运行")
        sys.exit(0)
