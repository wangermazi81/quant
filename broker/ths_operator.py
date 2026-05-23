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

try:
    from config import AUTO_CONFIRM_ORDER, AUTO_CONFIRM_DELAY, MAX_ORDER_AMOUNT
except ImportError:
    AUTO_CONFIRM_ORDER = False   # 安全默认：不自动按 Y
    AUTO_CONFIRM_DELAY = 0.8
    MAX_ORDER_AMOUNT   = 50000.0

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

            # 全局调速：pyautogui 默认每次按键后 sleep 100ms 太慢，改 20ms
            pyautogui.PAUSE = 0.02

            # 价格/数量格式化（xiadan 严格要求）
            price_str  = f"{float(price):.2f}"   # 强制 2 位小数：1.0 → "1.00"
            amount_str = str(int(amount))        # 强制整数：100.0 → "100"

            def _clear_and_type(text: str):
                """清空当前输入框并输入新值（多重清空保险，针对 xiadan 老 Win32 控件）

                xiadan 价格字段对 Ctrl+A、Home/Shift+End/Delete 都不可靠，
                需要叠加多种方法 + Backspace 连发暴力清场。
                """
                # 第 1 层：Ctrl+A 全选 + Delete（兼容现代控件）
                pyautogui.hotkey('ctrl', 'a')
                pyautogui.press('delete')
                # 第 2 层：Home + Shift+End + Delete（兼容多数控件）
                pyautogui.press('home')
                pyautogui.hotkey('shift', 'end')
                pyautogui.press('delete')
                # 第 3 层：Backspace 连击 20 次兜底（10 次有时不够清掉 xiadan 自动填的市价）
                pyautogui.press('end')
                pyautogui.press('backspace', presses=20, interval=0.01)
                # 输入新值
                pyautogui.typewrite(text, interval=0.03)

            # ── 三重激活 xiadan 窗口（pywinauto + win32api 双保险）──
            # pywinauto 64bit Python 自动化 32bit app set_focus 不稳定，
            # 加 win32api SetForegroundWindow / ShowWindow 强制激活
            activated = False
            if self.main_win is not None:
                try:
                    self.main_win.set_focus()
                    activated = True
                except Exception as _se:
                    logger.debug(f"pywinauto.set_focus 失败: {_se}")
                try:
                    import win32gui, win32con
                    hwnd = self.main_win.handle
                    if win32gui.IsIconic(hwnd):                      # 若最小化先还原
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(hwnd)               # 强制拉到前台
                    win32gui.BringWindowToTop(hwnd)
                    activated = True
                except Exception as _we:
                    logger.debug(f"win32 SetForegroundWindow 失败: {_we}")
                time.sleep(0.3)                                       # 给窗口激活时间
            if not activated:
                logger.warning("main_win 激活失败，依赖前台焦点是否在交易客户端")

            pyautogui.press(hotkey)              # 调起买入/卖出页（焦点应自动到"代码"字段）
            time.sleep(0.5)                      # 等 xiadan 渲染下单界面

            _clear_and_type(code)                # 股票代码
            pyautogui.press('tab')               # → 价格字段
            time.sleep(0.4)                      # ★ 关键：等 xiadan 自动填市价到价格字段
                                                 #   不等会和 Backspace 抢节奏 → 残留导致 typewrite 追加
            _clear_and_type(price_str)           # 价格（2 位小数格式）
            pyautogui.press('tab')               # → 数量字段
            time.sleep(0.2)                      # 等数量字段渲染
            _clear_and_type(amount_str)          # 数量
            time.sleep(0.15)
            pyautogui.press('enter')             # 触发"委托确认"对话框

            # ── 自动确认委托（AUTO_CONFIRM_ORDER 开关）──
            if AUTO_CONFIRM_ORDER:
                time.sleep(AUTO_CONFIRM_DELAY)        # 等委托确认对话框弹出
                pyautogui.press('y')                  # 按 Y =「是(Y)」=确认提交
                logger.info(
                    f"已按自动确认 Y: {action} {code} @ {price_str} x {amount_str}股 "
                    f"(hotkey={hotkey.upper()}) — 正在检测 xiadan 后续反馈..."
                )
                # ── 关键：按 Y 之后检测 xiadan 反馈（错误对话框 / 委托成功）──
                result = self._check_post_submit_result(action, code, price_str, amount_str)
                return result
            else:
                logger.info(
                    f"已提交 {action} {code} @ {price_str} x {amount_str}股 "
                    f"(hotkey={hotkey.upper()}) — xiadan 弹委托确认框，"
                    f"AUTO_CONFIRM_ORDER=False 需用户手动按 Y/N"
                )
                return {
                    "status": "submitted",
                    "msg": f"已提交 {action} {code} @ {price_str} x {amount_str}股, 等待用户手动按 Y/N",
                    "exec_price": float(price_str),
                    "exec_shares": int(amount_str),
                }
        except Exception as e:
            logger.error(f"{action} 操作失败: {e}")
            return {
                "status": "failed",
                "msg": f"{action} GUI 操作异常: {e}",
            }

    def _check_post_submit_result(self, action: str, code: str,
                                  price_str: str, amount_str: str) -> dict:
        """按 Y 之后检测 xiadan 弹出的对话框，识别错误原因或确认成功。

        返回 dict:
          {"status": "executed"|"submitted"|"failed", "msg": str,
           "exec_price": float, "exec_shares": int, "dialog_title": str (可选)}

        识别的错误类型：
          - 资金不足
          - 可卖余额不足（T+1 / 无持仓）
          - 价格偏离涨跌停
          - 证券代码错误
          - 其他对话框文字（保留原文）
        """
        import time
        import pyautogui
        time.sleep(1.5)   # 等 xiadan 处理 + 弹对话框（如有）

        meta = {
            "exec_price": float(price_str),
            "exec_shares": int(amount_str),
        }

        try:
            from pywinauto import Desktop
            # Win32 标准对话框类是 #32770
            dialogs = Desktop(backend='win32').windows(class_name='#32770', visible_only=True)
        except Exception as _de:
            logger.warning(f"对话框检测异常（跳过）: {_de}")
            return {"status": "submitted",
                    "msg": f"已提交（对话框检测异常: {_de}）",
                    **meta}

        # 标题白名单（xiadan 常见对话框标题）
        relevant_titles = ("提示信息", "委托提示", "提示", "错误", "警告", "委托确认")

        for d in dialogs:
            try:
                title = d.window_text() or ""
            except Exception:
                continue
            if not any(k in title for k in relevant_titles):
                continue
            # 提取对话框内所有可见文字
            texts = []
            try:
                for ctl in d.descendants():
                    t = ctl.window_text() if hasattr(ctl, "window_text") else ""
                    if t and t.strip() and t not in ("是(Y)", "否(N)", "确定", "取消"):
                        texts.append(t.strip())
            except Exception:
                pass
            body = " | ".join(texts) if texts else title
            logger.info(f"📋 检测到对话框 [{title}]: {body[:120]}")

            status, parsed_msg = self._parse_dialog_text(body, title)

            # 关闭对话框（用 Esc 安全，避免重复触发动作）
            try:
                pyautogui.press('escape')
                time.sleep(0.3)
            except Exception:
                pass

            return {"status": status,
                    "msg": parsed_msg,
                    "dialog_title": title,
                    "dialog_body": body[:200],
                    **meta}

        # 没找到任何错误对话框 = 委托可能已成功提交券商队列
        # 真实成交状态需阶段 2「查当日委托」才能确认
        return {"status": "submitted",
                "msg": f"已提交至券商, 未弹错误框 (精确状态待查当日委托) {action} {code} {amount_str}股 @{price_str}",
                **meta}

    @staticmethod
    def _parse_dialog_text(text: str, title: str) -> tuple:
        """根据对话框文字识别错误类型, 返回 (status, msg)"""
        # 资金不足
        if any(k in text for k in ("资金不足", "可用资金", "余额不足", "现金不足")):
            return "failed", "资金不足"
        # 持仓不足/T+1锁定
        if any(k in text for k in ("可卖余额", "无可卖", "持仓不足", "可卖股份", "可卖数量")):
            return "failed", "持仓不足或 T+1 锁定 (今日买入次日才可卖)"
        # 价格偏离
        if ("价格" in text or "委托价" in text) and \
           any(k in text for k in ("偏离", "超出", "涨跌停", "限制", "异常")):
            return "failed", "价格偏离涨跌停限制"
        # 证券代码错误
        if any(k in text for k in ("代码错误", "无效证券", "证券不存在", "无此代码")):
            return "failed", "证券代码错误"
        # 委托号 / 委托成功 标志
        if "委托" in text and ("成功" in text or "已发" in text or "受理" in text):
            return "executed", "委托已被券商受理"
        # 是否继续 类提示（小数位错误等）
        if "是否继续" in text or "是否确定" in text:
            return "submitted", f"待确认提示框: {text[:100]}"
        # 错误/警告类对话框 → failed
        if "错误" in title or "警告" in title:
            return "failed", text[:120] if text else f"未识别的{title}"
        # 默认（含"提示信息"标题但内容未知）
        return "submitted", text[:120] if text else f"未识别的{title}"

    def _amount_guard(self, action: str, code: str, price: float, amount: int):
        """金额上限护栏：单笔预估金额超过 MAX_ORDER_AMOUNT 直接拒单。
        返回 None = 通过, 否则返回 dict (failed) 给上层
        """
        est_amount = float(price) * int(amount)
        if est_amount > MAX_ORDER_AMOUNT:
            logger.error(
                f"❌ 金额护栏拒单: {action} {code} 预估 ¥{est_amount:,.0f} "
                f"> 上限 ¥{MAX_ORDER_AMOUNT:,.0f} (MAX_ORDER_AMOUNT)"
            )
            return {
                "status": "failed",
                "msg": f"金额护栏拒单: 预估 ¥{est_amount:,.0f} > 上限 ¥{MAX_ORDER_AMOUNT:,.0f}",
            }
        return None

    def buy(self, code: str, price: float, amount: int) -> dict:
        """返回 dict: {status, msg, exec_price, exec_shares, dialog_title?, dialog_body?}"""
        if DRY_RUN:
            logger.info(f"[模拟] 买入 {code} 价格:{price} 数量:{amount}")
            return {"status": "executed", "msg": "DRY_RUN 模拟成交",
                    "exec_price": float(price), "exec_shares": int(amount)}
        import os
        if os.name != 'nt':
            logger.warning(f"非 Windows 系统，无法执行实盘 GUI 买入: {code}")
            return {"status": "failed", "msg": "非 Windows 系统无法 GUI 自动化"}
        guard = self._amount_guard("买入", code, price, amount)
        if guard is not None:
            return guard
        return self._gui_submit("买入", THS_BUY_KEY, code, price, amount)

    def sell(self, code: str, price: float, amount: int) -> dict:
        """返回 dict: {status, msg, exec_price, exec_shares, dialog_title?, dialog_body?}"""
        if DRY_RUN:
            logger.info(f"[模拟] 卖出 {code} 价格:{price} 数量:{amount}")
            return {"status": "executed", "msg": "DRY_RUN 模拟成交",
                    "exec_price": float(price), "exec_shares": int(amount)}
        import os
        if os.name != 'nt':
            logger.warning(f"非 Windows 系统，无法执行实盘 GUI 卖出: {code}")
            return {"status": "failed", "msg": "非 Windows 系统无法 GUI 自动化"}
        guard = self._amount_guard("卖出", code, price, amount)
        if guard is not None:
            return guard
        return self._gui_submit("卖出", THS_SELL_KEY, code, price, amount)
