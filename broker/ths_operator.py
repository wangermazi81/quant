import time
import logging
from config import THS_WINDOW_TITLE, DRY_RUN

# 兼容旧版 config（如未定义则用默认）
try:
    from config import THS_BUY_KEY, THS_SELL_KEY, THS_BACKEND
except ImportError:
    THS_BUY_KEY  = "f2"
    THS_SELL_KEY = "f3"
    THS_BACKEND  = "uia"

logger = logging.getLogger(__name__)


class THSOperator:
    def __init__(self):
        self.app = None
        self.main_win = None
        import os
        if os.name == 'nt' and not DRY_RUN:
            self._connect()
        else:
            logger.info("非 Windows 系统或处于演练模式，跳过同花顺窗口连接")

    def _connect(self):
        """连接交易客户端窗口。
        backend='win32' 适合 32 位老 GUI（xiadan.exe、同花顺独立委托）
        backend='uia'   适合 64 位现代 UI
        """
        import os
        if os.name != 'nt':
            return
        try:
            from pywinauto import Application, Desktop
            wins = Desktop(backend=THS_BACKEND).windows()
            for w in wins:
                title = w.window_text() if hasattr(w, "window_text") else ""
                if THS_WINDOW_TITLE in title:
                    self.app = Application(backend=THS_BACKEND).connect(handle=w.handle)
                    self.main_win = self.app.top_window()
                    logger.info(
                        f"已连接交易客户端: {title!r} (backend={THS_BACKEND}, "
                        f"buy_key={THS_BUY_KEY.upper()}, sell_key={THS_SELL_KEY.upper()})"
                    )
                    return
            logger.error(
                f"未找到含 {THS_WINDOW_TITLE!r} 字串的窗口，"
                f"请确认客户端已登录、或修改 config.py 的 THS_WINDOW_TITLE"
            )
        except Exception as e:
            logger.error(f"连接交易客户端失败 (backend={THS_BACKEND}): {e}")

    def _gui_submit(self, action: str, hotkey: str,
                    code: str, price: float, amount: int) -> bool:
        """共用 GUI 按键序列：focus → 按快捷键 → 填代码/价/量 → Enter 提交"""
        try:
            import pyautogui
            if self.main_win is not None:
                self.main_win.set_focus()
                time.sleep(0.3)
            else:
                logger.warning("main_win 为 None，依赖前台焦点是否在交易客户端")
            pyautogui.press(hotkey)             # 调起买入/卖出页
            time.sleep(0.8)
            # 股票代码
            pyautogui.hotkey('ctrl', 'a')
            pyautogui.typewrite(code, interval=0.05)
            time.sleep(0.5)
            # 价格
            pyautogui.press('tab')
            pyautogui.hotkey('ctrl', 'a')
            pyautogui.typewrite(str(price), interval=0.05)
            # 数量
            pyautogui.press('tab')
            pyautogui.hotkey('ctrl', 'a')
            pyautogui.typewrite(str(amount), interval=0.05)
            time.sleep(0.3)
            pyautogui.press('enter')            # 确认
            logger.info(f"已提交 {action} {code} @ {price} x {amount}股 (hotkey={hotkey.upper()})")
            return True
        except Exception as e:
            logger.error(f"{action} 操作失败: {e}")
            return False

    def buy(self, code: str, price: float, amount: int) -> bool:
        if DRY_RUN:
            logger.info(f"[模拟] 买入 {code} 价格:{price} 数量:{amount}")
            return True
        import os
        if os.name != 'nt':
            logger.warning(f"非 Windows 系统，无法执行实盘 GUI 买入: {code}")
            return False
        return self._gui_submit("买入", THS_BUY_KEY, code, price, amount)

    def sell(self, code: str, price: float, amount: int) -> bool:
        if DRY_RUN:
            logger.info(f"[模拟] 卖出 {code} 价格:{price} 数量:{amount}")
            return True
        import os
        if os.name != 'nt':
            logger.warning(f"非 Windows 系统，无法执行实盘 GUI 卖出: {code}")
            return False
        return self._gui_submit("卖出", THS_SELL_KEY, code, price, amount)
