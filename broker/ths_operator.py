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
        """共用 GUI 按键序列：focus → 按快捷键 → 填代码/价/量 → Enter 提交

        关键点（基于 xiadan.exe 实测经验）：
        - 价格必须 2 位小数字符串（如 1.00 不是 1.0），否则 xiadan 弹
          "委托价格的小数部分应为 2 位" 对话框打断流程
        - 数量必须整数字符串（10000 不是 10000.0）
        - Ctrl+A 在某些控件不全选，用 Home+Shift+End 更可靠
        """
        try:
            import pyautogui

            # 价格/数量格式化（xiadan 严格要求）
            price_str  = f"{float(price):.2f}"   # 强制 2 位小数：1.0 → "1.00"
            amount_str = str(int(amount))        # 强制整数：100.0 → "100"

            def _clear_and_type(text: str):
                """清空当前输入框并输入新值（多重清空保险，针对 xiadan 老 Win32 控件）

                xiadan 价格字段对 Ctrl+A、Home/Shift+End/Delete 都不可靠，
                需要叠加多种方法 + Backspace 连发暴力清场。
                """
                # 第 1 层：选中并删除（兼容现代控件）
                pyautogui.hotkey('ctrl', 'a')
                time.sleep(0.05)
                pyautogui.press('delete')
                time.sleep(0.05)
                # 第 2 层：Home + Shift+End + Delete（兼容多数控件）
                pyautogui.press('home')
                pyautogui.hotkey('shift', 'end')
                pyautogui.press('delete')
                time.sleep(0.05)
                # 第 3 层：暴力 Backspace 连发 20 次（兜底，确保字段被清空）
                # 即使前两层都没生效，光标至少在某个位置，连续 backspace 删除字符
                pyautogui.press('end')                  # 光标先跳到行尾
                for _ in range(20):
                    pyautogui.press('backspace')
                time.sleep(0.15)
                # 输入新值
                pyautogui.typewrite(text, interval=0.06)

            if self.main_win is not None:
                self.main_win.set_focus()
                time.sleep(0.3)
            else:
                logger.warning("main_win 为 None，依赖前台焦点是否在交易客户端")

            pyautogui.press(hotkey)              # 调起买入/卖出页（焦点应自动到"代码"字段）
            time.sleep(1.0)                      # 给 xiadan 更多时间渲染下单界面

            _clear_and_type(code)                # 股票代码
            time.sleep(0.5)
            pyautogui.press('tab')               # → 价格字段

            _clear_and_type(price_str)           # 价格（2 位小数格式）
            time.sleep(0.3)
            pyautogui.press('tab')               # → 数量字段

            _clear_and_type(amount_str)          # 数量
            time.sleep(0.3)
            pyautogui.press('enter')             # 触发"委托确认"对话框（用户/桥接器再 Y/N）

            logger.info(
                f"已提交 {action} {code} @ {price_str} x {amount_str}股 "
                f"(hotkey={hotkey.upper()}) — xiadan 会弹委托确认框，需另行确认"
            )
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
