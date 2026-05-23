import time
import logging
from config import THS_WINDOW_TITLE, DRY_RUN

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
        import os
        if os.name != 'nt':
            return
        try:
            from pywinauto import Application, Desktop
            wins = Desktop(backend="uia").windows()
            for w in wins:
                if THS_WINDOW_TITLE in w.window_text():
                    self.app = Application(backend="uia").connect(handle=w.handle)
                    self.main_win = self.app.top_window()
                    logger.info(f"已连接同花顺: {w.window_text()}")
                    return
            logger.error("未找到同花顺窗口，请确保同花顺已打开并登录")
        except Exception as e:
            logger.error(f"连接同花顺失败: {e}")

    def buy(self, code: str, price: float, amount: int) -> bool:
        if DRY_RUN:
            logger.info(f"[模拟] 买入 {code} 价格:{price} 数量:{amount}")
            return True
        import os
        if os.name != 'nt':
            logger.warning(f"非 Windows 系统，无法执行实盘 GUI 买入: {code}")
            return False
        try:
            import pyautogui
            self.main_win.set_focus()
            time.sleep(0.3)
            pyautogui.hotkey('F2')      # 同花顺买入快捷键
            time.sleep(0.8)
            # 输入股票代码
            pyautogui.hotkey('ctrl', 'a')
            pyautogui.typewrite(code, interval=0.05)
            time.sleep(0.5)
            # 输入价格
            pyautogui.press('tab')
            pyautogui.hotkey('ctrl', 'a')
            pyautogui.typewrite(str(price), interval=0.05)
            # 输入数量
            pyautogui.press('tab')
            pyautogui.hotkey('ctrl', 'a')
            pyautogui.typewrite(str(amount), interval=0.05)
            time.sleep(0.3)
            pyautogui.press('enter')    # 确认买入
            logger.info(f"已提交买入 {code} @ {price} x {amount}股")
            return True
        except Exception as e:
            logger.error(f"买入操作失败: {e}")
            return False

    def sell(self, code: str, price: float, amount: int) -> bool:
        if DRY_RUN:
            logger.info(f"[模拟] 卖出 {code} 价格:{price} 数量:{amount}")
            return True
        import os
        if os.name != 'nt':
            logger.warning(f"非 Windows 系统，无法执行实盘 GUI 卖出: {code}")
            return False
        try:
            import pyautogui
            self.main_win.set_focus()
            time.sleep(0.3)
            pyautogui.hotkey('F3')      # 同花顺卖出快捷键
            time.sleep(0.8)
            pyautogui.hotkey('ctrl', 'a')
            pyautogui.typewrite(code, interval=0.05)
            time.sleep(0.5)
            pyautogui.press('tab')
            pyautogui.hotkey('ctrl', 'a')
            pyautogui.typewrite(str(price), interval=0.05)
            pyautogui.press('tab')
            pyautogui.hotkey('ctrl', 'a')
            pyautogui.typewrite(str(amount), interval=0.05)
            time.sleep(0.3)
            pyautogui.press('enter')
            logger.info(f"已提交卖出 {code} @ {price} x {amount}股")
            return True
        except Exception as e:
            logger.error(f"卖出操作失败: {e}")
            return False
