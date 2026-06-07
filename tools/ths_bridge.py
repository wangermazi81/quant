# -*- coding: utf-8 -*-
"""
见微 Alpha 本地交易下单桥接器 (Cloud-Local Bridge)
部署环境: Windows 本地交易电脑 (与登录同花顺客户端的电脑一致)
功能: 24小时自动心跳轮询云端服务器交易信号，驱动本地同花顺下单，并自动反馈成交状态。
"""

import os
import sys
import time
import json
import logging
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

try:
    from broker.ths_operator import THSOperator
except ImportError:
    # 兼容直接从同级目录或父级目录查找
    sys.path.insert(0, str(BASE_DIR / "broker"))
    from ths_operator import THSOperator

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
    "poll_interval_sec": 1.0
}

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                logger.info(f"成功加载本地配置文件: {CONFIG_FILE}")
                return {**DEFAULT_CONFIG, **cfg}
        except Exception as e:
            logger.warning(f"加载配置文件出错: {e}，将使用默认配置")
    else:
        # 首次运行自动生成模板配置文件
        try:
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
        self.poll_interval = float(self.config["poll_interval_sec"])
        
        self.session = requests.Session()
        self.operator = None
        self.is_connected_ths = False

    def init_ths(self):
        """初始化同花顺 GUI 自动化连接"""
        logger.info("正在尝试连接本地同花顺客户端...")
        try:
            self.operator = THSOperator()
            self.is_connected_ths = True
            logger.info("🚀 同花顺操作员初始化完成，已处于待命下单状态")
        except Exception as e:
            self.is_connected_ths = False
            logger.error(f"❌ 初始化本地同花顺操作器失败: {e}")

    def login(self) -> bool:
        """登录云端服务器获取 Session"""
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

    def report_result(self, signal_id: int, status: str, msg: str):
        """上报成交或失败结果给云端"""
        url = f"{self.server_url}/api/bridge/signals/{signal_id}/report"
        try:
            resp = self.session.post(url, json={"status": status, "msg": msg}, timeout=5)
            if resp.status_code == 200 and resp.json().get("ok"):
                logger.info(f"✅ 信号 ID={signal_id} 执行结果成功上报至云端")
            else:
                logger.error(f"❌ 信号 ID={signal_id} 结果上报云端失败: {resp.text}")
        except Exception as e:
            logger.error(f"上报信号 ID={signal_id} 结果出现异常: {e}")

    def execute_signal(self, signal: dict):
        """在本地同花顺上执行交易信号"""
        sig_id = signal["id"]
        code = signal["code"]
        name = signal.get("name", "")
        action = signal["action"]
        price = signal["price"]
        shares = signal["shares"]
        
        logger.info(f"🔔 抓取到新实盘交易信号: ID={sig_id} | {action} | {code} ({name}) | {shares}股 @{price:.2f}")

        # 检查是否连接到同花顺
        if not self.is_connected_ths or self.operator is None:
            self.init_ths()
            if not self.is_connected_ths or self.operator is None:
                logger.error("❌ 本地未检测到正常运行的同花顺，拒绝执行并汇报失败")
                self.report_result(sig_id, "failed", "本地未启动或无法连接同花顺客户端")
                return

        # 执行下单 — operator.buy/sell 现在返回 dict 而非 bool
        logger.info(f"🛠 正在唤起同花顺界面执行 {action} 操作...")
        try:
            if action == "BUY":
                result = self.operator.buy(code, price, shares)
            elif action == "SELL":
                result = self.operator.sell(code, price, shares)
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
            # 富信息日志：让本地网页运行日志能看到对话框具体内容
            if result.get("dialog_title"):
                msg = f"{msg} | 对话框=[{result['dialog_title']}] {result.get('dialog_body','')}"

            icon = {"executed": "🌟", "submitted": "📤", "failed": "❌"}.get(status, "❓")
            logger.info(f"{icon} 实盘 {action} 结果: status={status} | {msg}")
            self.report_result(sig_id, status, msg)
        except Exception as e:
            logger.error(f"❌ 本地执行下单发生异常: {e}")
            self.report_result(sig_id, "failed", f"本地发生执行异常: {str(e)}")

    def run(self):
        """主轮询循环"""
        logger.info("==============================================")
        logger.info("   见微 Alpha 本地同花顺交易下单桥接客户端启动")
        logger.info("==============================================")
        
        # 1. 初始化同花顺
        self.init_ths()
        
        # 2. 登录云端服务器
        connected = False
        while not connected:
            connected = self.login()
            if not connected:
                logger.info("10秒后重新尝试连接云端服务器...")
                time.sleep(10)
        
        logger.info("✨ 桥接器准备就绪，进入 24H 交易信号轮询监听态...")
        
        # 3. 轮询循环
        error_count = 0
        while True:
            try:
                signals = self.fetch_signals()
                if signals:
                    logger.info(f"收到 {len(signals)} 个待处理实盘信号")
                    for sig in signals:
                        self.execute_signal(sig)
                
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
