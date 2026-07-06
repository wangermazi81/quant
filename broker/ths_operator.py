from __future__ import annotations

import time
import logging
import threading
import functools
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

try:
    from config import AUTO_CANCEL_UNFILLED, AUTO_CANCEL_DELAY
except ImportError:
    AUTO_CANCEL_UNFILLED = False  # 安全默认：不自动撤单
    AUTO_CANCEL_DELAY    = 15.0


def _gui_serialized(fn):
    """串行化所有驱动 xiadan GUI 的方法：同一时刻只允许一个线程操作交易窗口。

    必要性：自动撤单监控在后台线程跑，可能与「下单」「手动查实盘账户」并发操作同一个
    窗口——不串行会互相抢焦点/按错键。用可重入锁(RLock)，故 buy→下单后刷新资金 这类
    同线程嵌套调用不会自死锁。
    """
    @functools.wraps(fn)
    def _wrap(self, *a, **k):
        lock = getattr(self, "_gui_lock", None)
        if lock is None:
            return fn(self, *a, **k)
        with lock:
            return fn(self, *a, **k)
    return _wrap

try:
    from config import SCREENSHOT_RETENTION_DAYS, SCREENSHOT_MAX_FILES
except ImportError:
    SCREENSHOT_RETENTION_DAYS = 3
    SCREENSHOT_MAX_FILES      = 500

logger = logging.getLogger(__name__)


def _is_admin() -> bool:
    """当前进程是否以管理员权限运行（Windows）。
    诊断「模拟按键送不进 xiadan」的头号原因：若同花顺以管理员运行而本进程不是，
    Windows UIPI（用户界面特权隔离）会静默拦截所有模拟按键/置顶请求 →
    下单按 Y 没反应、撤单 F3/Z/Del 无效 → 全部 failed。两者必须同权限。"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


_OCR_DIGIT_FIX = {'O': '0', 'o': '0', 'D': '0', 'Q': '0', 'U': '0',
                  'I': '1', 'l': '1', 'i': '1', '|': '1', 'J': '1',
                  'Z': '2', 'z': '2', 'R': '2',
                  'A': '4', 'S': '5', 's': '5', '$': '5',
                  'b': '6', 'G': '6', 'e': '6',
                  'T': '7', 'L': '7',
                  'B': '8', 'g': '9', 'q': '9', 'P': '9'}


def _as_stock_code(text: str):
    """把一个 OCR 词尝试归一成 6 位 A 股代码，容忍 OCR 常见字母↔数字混淆
    （O→0/S→5/B→8/l→1 等）。成功返回 6 位代码字符串(以 0/3/6/8 开头)，否则 None。
    用于 OCR 持仓表：原先严格 \\d{6} 会把被 OCR 轻微认错的代码整行丢掉。
    加了首位前缀校验(0/3/6/8)，避免把普通 6 位数字/乱码误当成代码。"""
    import re as _re
    if not text:
        return None
    t = _re.sub(r"\s+", "", str(text))
    if len(t) != 6:
        return None
    fixed = "".join(_OCR_DIGIT_FIX.get(ch, ch) for ch in t)
    if _re.fullmatch(r"\d{6}", fixed) and fixed[0] in "0368":
        return fixed
    return None


class THSOperator:
    _last_prune_ts = 0.0   # 全类共享：节流截图清理，避免每次下单都扫描整目录

    def __init__(self):
        self.app = None
        self.main_win = None
        # 资金缓存：{ts, data:{ok,mode,available,total_asset}}。事件驱动更新——
        # 下单成功后顺手读一次顶部工具栏写入；/api/account/funds 只读这个缓存，
        # 不再每次激活窗口，避免轮询反复抢占交易窗口焦点。
        self._funds_cache = None
        # 串行化 GUI 操作的可重入锁（下单/撤单/查询/监控线程共用，防并发抢窗口）
        self._gui_lock = threading.RLock()
        # 交易自动化开关（运行时可经网页/接口切换，持久化到 data/trade_settings.json）：
        #   auto_confirm_order   True=下单后自动按 Y 提交 / False=下单后等人工按 Y
        #   auto_cancel_unfilled True=未成交自动撤单 / False=手动撤单
        #   auto_cancel_delay    未成交多少秒后撤
        self.auto_confirm_order   = AUTO_CONFIRM_ORDER
        self.auto_cancel_unfilled = AUTO_CANCEL_UNFILLED
        self.auto_cancel_delay    = AUTO_CANCEL_DELAY
        self._load_trade_settings()
        import os
        if os.name == 'nt' and not DRY_RUN:
            self.is_admin = _is_admin()
            if self.is_admin:
                logger.info("[权限] 本程序以管理员身份运行 ✓")
            else:
                logger.warning("[权限] ⚠ 本程序非管理员运行！若同花顺是『以管理员身份运行』，"
                               "模拟按键会被 Windows UIPI 拦截 → 下单/撤单全部失败。"
                               "请用管理员身份重启本桥接器（与同花顺同权限）")
            self._connect()
        else:
            self.is_admin = False
            logger.info("非 Windows 系统或处于演练模式，跳过同花顺窗口连接")

    def _trade_settings_path(self):
        from pathlib import Path
        return Path(__file__).resolve().parent.parent / "data" / "trade_settings.json"

    def _load_trade_settings(self):
        """启动时从 data/trade_settings.json 覆盖默认开关（让网页设置重启后仍生效）。"""
        try:
            import json
            p = self._trade_settings_path()
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                if "auto_confirm_order" in d:
                    self.auto_confirm_order = bool(d["auto_confirm_order"])
                if "auto_cancel_unfilled" in d:
                    self.auto_cancel_unfilled = bool(d["auto_cancel_unfilled"])
                if "auto_cancel_delay" in d:
                    self.auto_cancel_delay = float(d["auto_cancel_delay"])
                logger.info(f"已加载交易自动化设置: 自动确认={self.auto_confirm_order} "
                            f"自动撤单={self.auto_cancel_unfilled} 撤单延迟={self.auto_cancel_delay}s")
        except Exception as e:
            logger.warning(f"加载交易设置失败（用默认）: {e}")

    def _save_trade_settings(self):
        try:
            import json
            p = self._trade_settings_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self.get_trade_settings(), ensure_ascii=False, indent=2),
                         encoding="utf-8")
        except Exception as e:
            logger.warning(f"保存交易设置失败: {e}")

    def get_trade_settings(self) -> dict:
        """返回当前交易自动化开关（供网页展示/回填）。"""
        return {
            "auto_confirm_order":   bool(self.auto_confirm_order),
            "auto_cancel_unfilled": bool(self.auto_cancel_unfilled),
            "auto_cancel_delay":    float(self.auto_cancel_delay),
        }

    def set_trade_settings(self, auto_confirm_order=None, auto_cancel_unfilled=None,
                           auto_cancel_delay=None) -> dict:
        """运行时更新交易自动化开关并持久化。None 表示该项不变。返回更新后的全部设置。"""
        if auto_confirm_order is not None:
            self.auto_confirm_order = bool(auto_confirm_order)
        if auto_cancel_unfilled is not None:
            self.auto_cancel_unfilled = bool(auto_cancel_unfilled)
        if auto_cancel_delay is not None:
            try:
                d = float(auto_cancel_delay)
                if 1.0 <= d <= 600.0:
                    self.auto_cancel_delay = d
            except (TypeError, ValueError):
                pass
        self._save_trade_settings()
        logger.info(f"交易自动化设置已更新: {self.get_trade_settings()}")
        return self.get_trade_settings()

    def _find_xiadan_hwnd(self):
        """用 win32 EnumWindows 枚举【所有】顶层窗口（含隐藏/最小化/被迷你交易条 SW_HIDE 的），
        找标题含 THS_WINDOW_TITLE 的下单主窗口句柄。

        为什么不用 pywinauto Desktop().windows()：它默认 visible_only=True。
        当同花顺被收成「迷你交易条(买卖撤查)」时，完整下单窗口被隐藏(IsWindowVisible=False)，
        Desktop().windows() 直接漏掉它 → 重连报「未找到窗口」→ 下单全部失败（今天 961 次失败的根因）。
        EnumWindows 不过滤可见性，能找到被隐藏的完整窗口，再 ShowWindow 还原即可。

        返回 (hwnd, title)，找不到返回 (None, None)。多个匹配时优先可见的。
        """
        import win32gui
        matches = []

        def _cb(hwnd, _):
            try:
                t = win32gui.GetWindowText(hwnd) or ""
            except Exception:
                return True
            if t and THS_WINDOW_TITLE in t:
                try:
                    vis = bool(win32gui.IsWindowVisible(hwnd))
                except Exception:
                    vis = False
                matches.append((hwnd, t, vis))
            return True

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception as e:
            logger.warning(f"EnumWindows 枚举窗口失败: {e}")
        if not matches:
            return None, None
        # 优先可见窗口；都隐藏则取第一个（被迷你条藏起来的完整窗口）
        matches.sort(key=lambda m: 0 if m[2] else 1)
        return matches[0][0], matches[0][1]

    def _connect(self):
        """连接交易客户端窗口（backend=win32，适合 32 位老 GUI xiadan.exe）。
        用 EnumWindows 兜底找窗口（含被迷你交易条隐藏的），连上后强制从最小化/隐藏还原成完整下单窗口。
        """
        import os
        if os.name != 'nt':
            return
        try:
            from pywinauto import Application
            import win32gui, win32con
            hwnd, title = self._find_xiadan_hwnd()
            if hwnd is None:
                logger.error(
                    f"未找到含 {THS_WINDOW_TITLE!r} 字串的窗口，"
                    f"请确认客户端已登录、未被关闭（迷你交易条/最小化已兼容）；"
                    f"或修改 config.py 的 THS_WINDOW_TITLE"
                )
                return
            # 若被隐藏(迷你交易条)/最小化，先还原成完整窗口，否则填单控件取不到、下单落空
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    logger.info(f"xiadan 主窗口被隐藏（疑似迷你交易条），ShowWindow 还原 handle={hwnd}")
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                    time.sleep(0.3)
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    time.sleep(0.3)
            except Exception as _re:
                logger.debug(f"还原 xiadan 窗口失败（不致命）: {_re}")
            self.app = Application(backend=THS_BACKEND).connect(handle=hwnd)
            # ★ 绑定到标题匹配的那个完整下单窗口本身，不用 top_window()（会抓到迷你条小窗）
            try:
                self.main_win = self.app.window(handle=hwnd)
                _ = self.main_win.window_text()   # 触发一次校验，失败则降级
            except Exception:
                self.main_win = self.app.top_window()
            self._xiadan_hwnd = hwnd
            logger.info(
                f"已连接交易客户端: {title!r} (handle={hwnd}, backend={THS_BACKEND}, "
                f"buy_key={THS_BUY_KEY.upper()}, sell_key={THS_SELL_KEY.upper()})"
            )
            return
        except Exception as e:
            logger.error(f"连接交易客户端失败 (backend={THS_BACKEND}): {e}")

    def _save_screenshot(self, label: str) -> str | None:
        """保存 xiadan 当前主窗口截图到 logs/screenshots/<时间戳>_<label>.png

        诊断用：让用户/排查者能看到桥接器实际按键后的 xiadan 状态。
        失败静默，不影响主流程。
        """
        try:
            import os, time as _t
            from pathlib import Path
            from datetime import datetime
            proj_root = Path(__file__).resolve().parent.parent
            scr_dir = proj_root / "logs" / "screenshots"
            scr_dir.mkdir(parents=True, exist_ok=True)
            self._prune_screenshots(scr_dir)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            fn = scr_dir / f"{ts}_{label}.png"
            # 优先抓 xiadan 主窗口截图（pywinauto），失败降级到全屏 pyautogui
            try:
                if self.main_win is not None:
                    img = self.main_win.capture_as_image()
                    img.save(str(fn))
                    return str(fn)
            except Exception:
                pass
            try:
                import pyautogui
                pyautogui.screenshot(str(fn))
                return str(fn)
            except Exception:
                pass
            return None
        except Exception as exc:
            logger.debug(f"截图失败 [{label}]: {exc}")
            return None

    def _prune_screenshots(self, scr_dir) -> None:
        """清理过期下单截图：按保留天数删旧，再按数量硬上限兜底。

        节流到最多每 5 分钟扫描一次，避免单次下单的多张截图反复全目录扫描。
        失败静默，不影响下单主流程。
        """
        import time as _t
        now = _t.time()
        if now - THSOperator._last_prune_ts < 300:
            return
        THSOperator._last_prune_ts = now
        try:
            files = list(scr_dir.glob("*.png"))
            cutoff = now - SCREENSHOT_RETENTION_DAYS * 86400
            removed = 0
            for p in files:
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                        removed += 1
                except Exception:
                    pass
            # 数量硬上限：留最新的 SCREENSHOT_MAX_FILES 张
            files = sorted(scr_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
            if len(files) > SCREENSHOT_MAX_FILES:
                for p in files[: len(files) - SCREENSHOT_MAX_FILES]:
                    try:
                        p.unlink()
                        removed += 1
                    except Exception:
                        pass
            if removed:
                logger.info(f"已清理过期下单截图 {removed} 张（保留 {SCREENSHOT_RETENTION_DAYS} 天 / 上限 {SCREENSHOT_MAX_FILES} 张）")
        except Exception as exc:
            logger.debug(f"截图清理失败: {exc}")

    def _read_edit_fields(self) -> dict:
        """读取 xiadan 当前买入/卖出页面的 Edit 字段实际值（诊断用）

        返回 {edit_idx: text}。失败返回 {}。
        Win32 xiadan 的 Edit 控件没稳定的 control_id，只能按顺序遍历可见 Edit。
        """
        try:
            from pywinauto import Application
            if self.main_win is None:
                return {}
            edits = []
            for ctl in self.main_win.descendants():
                try:
                    cls = ctl.class_name() if hasattr(ctl, "class_name") else ""
                    if cls and "Edit" in cls and ctl.is_visible():
                        txt = ctl.window_text() if hasattr(ctl, "window_text") else ""
                        edits.append(txt)
                except Exception:
                    continue
            return {i: t for i, t in enumerate(edits[:8])}   # 只看前 8 个
        except Exception as exc:
            logger.debug(f"读字段失败: {exc}")
            return {}

    def _gui_submit(self, action: str, hotkey: str,
                    code: str, price: float, amount: int) -> bool:
        """共用 GUI 按键序列：focus → 按快捷键 → 填代码/价/量 → Enter 提交

        关键点（基于 xiadan.exe 实测经验）：
        - 价格必须 2 位小数字符串（如 1.00 不是 1.0），否则 xiadan 弹
          "委托价格的小数部分应为 2 位" 对话框打断流程
        - 数量必须整数字符串（10000 不是 10000.0）
        - Ctrl+A 在某些控件不全选，用 Home+Shift+End 更可靠

        新增（2026-05-25）：
        - 关键步骤截图存 logs/screenshots/ 诊断
        - 字段填写后读 Edit 控件验证实际值，对不上 abort 返回 failed
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

            # ── 自动重连：main_win=None 说明启动时 xiadan 没开，现在重试 ──
            if self.main_win is None:
                logger.info("main_win 为空，尝试重新连接 xiadan...")
                try:
                    self._connect()
                except Exception as _re:
                    logger.error(f"重连 xiadan 失败: {_re}")
                if self.main_win is None:
                    return {
                        "status": "failed",
                        "msg": "xiadan 未启动或窗口标题不匹配 — 桥接器无法激活到前台",
                    }

            # ── 强制激活 xiadan 到前台（彻底破解最小化 + 焦点保护）──
            import win32gui, win32con, win32process
            import ctypes
            # ★ 句柄失效自愈：main_win 是懒加载 WindowSpecification，访问失效窗口的 .handle
            #   会直接抛异常（不只是 IsWindow=False），故先 try 取句柄，失败/失效都重连。
            try:
                hwnd = self.main_win.handle
                _valid = bool(win32gui.IsWindow(hwnd))
            except Exception:
                _valid = False
            if not _valid:
                logger.warning("xiadan 窗口 handle 失效，重连...")
                self._connect()
                if self.main_win is None:
                    return {"status": "failed", "msg": "xiadan 窗口已关闭，无法下单"}
                try:
                    hwnd = self.main_win.handle
                except Exception:
                    return {"status": "failed", "msg": "xiadan 重连后仍拿不到窗口句柄，请确认同花顺已登录"}

            activated = False
            user32 = ctypes.windll.user32

            # ★ 第 1 步：强制还原最小化 ★
            # 最小化时 GetWindowRect 返回屏幕外虚假坐标 (-32000,-32000)，
            # 任何 SetForegroundWindow / 鼠标点击都会失败。
            # 必须先还原 + sleep 0.5s 等 Windows 完成动画。
            iconic = bool(win32gui.IsIconic(hwnd))
            if iconic:
                logger.info("⚠️ xiadan 当前处于最小化状态，正在还原...")
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.5)            # 等还原动画
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)
                time.sleep(0.3)
                # 验证：还原后 IsIconic 应该 False，rect 应该回到屏幕内
                still_iconic = bool(win32gui.IsIconic(hwnd))
                rect = win32gui.GetWindowRect(hwnd)
                logger.info(f"还原后：iconic={still_iconic}, rect={rect}")
                if still_iconic or rect[0] < -10000:
                    # 还原失败：用 MoveWindow 强制设置位置
                    logger.warning("SW_RESTORE 未能让 xiadan 离开屏幕外，用 MoveWindow 强搬")
                    try:
                        win32gui.MoveWindow(hwnd, 100, 100, 1200, 800, True)
                        time.sleep(0.4)
                    except Exception as _me:
                        logger.warning(f"MoveWindow 失败: {_me}")

            # ★ 第 2 步：AttachThreadInput + SetForegroundWindow ★
            try:
                target_thread_id, _ = win32process.GetWindowThreadProcessId(hwnd)
                current_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
                if target_thread_id and target_thread_id != current_thread_id:
                    user32.AttachThreadInput(current_thread_id, target_thread_id, True)
                    try:
                        win32gui.BringWindowToTop(hwnd)
                        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                        win32gui.SetForegroundWindow(hwnd)
                    finally:
                        user32.AttachThreadInput(current_thread_id, target_thread_id, False)
                else:
                    win32gui.SetForegroundWindow(hwnd)
                time.sleep(0.3)
                activated = (win32gui.GetForegroundWindow() == hwnd)
                if activated: logger.info("✅ 方法1：AttachThreadInput 激活成功")
            except Exception as _e1:
                logger.warning(f"方法1 异常: {_e1}")

            # ★ 第 3 步：鼠标点击兜底（仅当窗口已还原到屏幕内）★
            if not activated:
                try:
                    rect = win32gui.GetWindowRect(hwnd)
                    if rect[0] > -1000 and rect[1] > -1000:
                        import pyautogui as _pg
                        cx = (rect[0] + rect[2]) // 2
                        cy = rect[1] + 15
                        _orig_pos = _pg.position()
                        logger.info(f"🖱 方法2：模拟鼠标点击 xiadan 标题栏 ({cx},{cy})")
                        _pg.click(cx, cy, duration=0.05)
                        time.sleep(0.3)
                        try: _pg.moveTo(_orig_pos[0], _orig_pos[1], duration=0.05)
                        except Exception: pass
                        activated = (win32gui.GetForegroundWindow() == hwnd)
                        if activated: logger.info("✅ 方法2：鼠标点击激活成功")
                    else:
                        logger.warning(f"窗口仍在屏幕外 rect={rect}，跳过鼠标点击")
                except Exception as _e2:
                    logger.warning(f"方法2 异常: {_e2}")

            # ★ 第 4 步：pywinauto.set_focus 最后兜底 ★
            if not activated:
                try:
                    self.main_win.set_focus()
                    time.sleep(0.3)
                    activated = (win32gui.GetForegroundWindow() == hwnd)
                    if activated: logger.info("✅ 方法3：pywinauto.set_focus 激活成功")
                except Exception as _e3:
                    logger.warning(f"方法3 异常: {_e3}")

            time.sleep(0.3)
            if not activated:
                actual_fg = win32gui.GetForegroundWindow()
                actual_title = ""
                try: actual_title = win32gui.GetWindowText(actual_fg)
                except: pass
                logger.error(
                    f"❌ xiadan 激活全部失败！当前前台 hwnd={actual_fg} ({actual_title}), "
                    f"目标 hwnd={hwnd}. 拒绝按键避免误操作"
                )
                return {
                    "status": "failed",
                    "msg": f"xiadan 激活失败，当前前台是 [{actual_title or '未知'}]。"
                           f"请手动点 xiadan 标题栏让它处于前台 + 取消最小化，然后重试",
                }

            # 截图：按 F1/F2 之前的初始状态
            self._save_screenshot(f"{action}_{code}_0_before_hotkey")

            # ★ 先切到对面再切回来，强制重置焦点（解决连续下单时 F1/F2 不重新聚焦的问题）
            _opposite = 'f2' if hotkey == 'f1' else 'f1'
            pyautogui.press(_opposite)
            time.sleep(0.3)
            pyautogui.press(hotkey)              # 调起买入/卖出页（焦点应自动到"代码"字段）
            time.sleep(0.5)                      # 等 xiadan 渲染下单界面
            self._save_screenshot(f"{action}_{code}_1_after_hotkey")

            _clear_and_type(code)                # 股票代码
            time.sleep(0.5)                      # 等 xiadan 接受代码 + 渲染股名（拉长 0.3→0.5）
            self._save_screenshot(f"{action}_{code}_2_after_code")

            # 字段诊断（仅日志，不 abort）
            # _read_edit_fields 用 pywinauto win32 backend，对 xiadan 自定义 Edit
            # 类名可能读不到（返回 {}）。但截图证明字段填入是成功的。
            # 不要因为读不到就 abort，让流程继续走到 Enter + 委托确认框。
            try:
                fields = self._read_edit_fields()
                logger.info(f"📋 填代码后字段诊断（仅参考）: {fields}")
            except Exception as _ve:
                logger.debug(f"字段诊断跳过: {_ve}")

            pyautogui.press('tab')               # → 价格字段
            time.sleep(0.6)                      # ★ 关键：等 xiadan 自动填市价到价格字段（拉长 0.5→0.6）
                                                 #   不等会和 Backspace 抢节奏 → 残留导致 typewrite 追加
            _clear_and_type(price_str)           # 价格（2 位小数格式）
            time.sleep(0.3)                      # 等价格输入完成被 xiadan 接受（拉长 0.2→0.3）
            self._save_screenshot(f"{action}_{code}_3_after_price")

            pyautogui.press('tab')               # → 数量字段
            time.sleep(0.6)                      # ★ Tab 后等更久, 避免快速 Tab 被吃掉导致
                                                 #   后续 typewrite 又写回价格字段 → "10.50100"
            _clear_and_type(amount_str)          # 数量
            time.sleep(0.3)
            self._save_screenshot(f"{action}_{code}_4_after_amount")

            # 提交前字段诊断（仅日志，不 abort）
            # 截图已经存了，事后排查用。让流程继续走 Enter，由 xiadan
            # 的"委托确认"对话框或错误对话框成为真正的验证关口。
            try:
                fields = self._read_edit_fields()
                logger.info(f"📋 按 Enter 之前字段诊断（仅参考）: {fields}")
            except Exception as _ve:
                logger.debug(f"字段诊断跳过: {_ve}")

            pyautogui.press('enter')             # 触发"委托确认"对话框
            self._save_screenshot(f"{action}_{code}_5_after_enter")

            # ── 自动确认委托（运行时开关 self.auto_confirm_order，可经网页切换）──
            if self.auto_confirm_order:
                time.sleep(AUTO_CONFIRM_DELAY)        # 等委托确认对话框弹出
                self._save_screenshot(f"{action}_{code}_6_before_press_y")
                # ★ 修复：委托确认框标题常为空，盲按 Y 经常确认不掉（按钮可能是"确定"=Enter，
                # 或焦点不在框上）。改为：定位 #32770 确认框 → 点它的确认按钮；点不到再 Enter+Y 兜底。
                confirmed = self._confirm_order_dialog()
                if not confirmed:
                    pyautogui.press('enter'); time.sleep(0.2)
                    pyautogui.press('y')
                logger.info(
                    f"已确认委托({'点按钮' if confirmed else 'Enter/Y兜底'}): {action} {code} "
                    f"@ {price_str} x {amount_str}股 (hotkey={hotkey.upper()}) — 正在检测 xiadan 反馈..."
                )
                # ── 关键：按 Y 之后检测 xiadan 反馈（错误对话框 / 委托成功）──
                result = self._check_post_submit_result(action, code, price_str, amount_str)
                self._save_screenshot(f"{action}_{code}_7_after_result_{result.get('status', 'unknown')}")
                return result
            else:
                # ★★★ 真实验证：必须能在 xiadan 找到"委托确认"对话框 ★★★
                # 不能盲目返回 submitted。按键可能被其他窗口拦截 → xiadan 根本没动
                # → 没弹出任何对话框 → 这是 failed 不是 submitted
                self._save_screenshot(f"{action}_{code}_6_awaiting_user_y")
                time.sleep(1.0)   # 等 xiadan 弹委托确认框

                dialog_found = None
                dialog_title = ""
                dialog_body = ""
                try:
                    from pywinauto import Desktop
                    dialogs = Desktop(backend='win32').windows(
                        class_name='#32770', visible_only=True
                    )
                    # 找含"确认"/"委托"等关键词的对话框
                    for d in dialogs:
                        try:
                            t = d.window_text() or ""
                        except Exception:
                            continue
                        if any(k in t for k in ("确认", "委托", "提示", "买入", "卖出")):
                            dialog_found = d
                            dialog_title = t
                            # 抓对话框内容文字，看是不是真的"委托确认"
                            try:
                                texts = []
                                for ctl in d.descendants():
                                    tt = ctl.window_text() if hasattr(ctl, "window_text") else ""
                                    if tt and tt.strip():
                                        texts.append(tt.strip())
                                dialog_body = " | ".join(texts)[:200]
                            except Exception:
                                pass
                            break
                except Exception as _de:
                    logger.warning(f"对话框检测异常: {_de}")

                if not dialog_found:
                    # ★ 没看到对话框 = 按键被其他窗口拦截 = failed
                    logger.error(
                        f"❌ Enter 后未检测到 xiadan 委托确认框 — "
                        f"按键可能被其他窗口（浏览器/IDE/PowerShell）抢走焦点，"
                        f"xiadan 实际未收到按键"
                    )
                    return {
                        "status": "failed",
                        "msg": f"按 Enter 后 xiadan 未弹出委托确认框。"
                               f"原因：按键期间焦点被其他窗口抢走，xiadan 没真正收到操作。"
                               f"请把 xiadan 放在屏幕醒目位置 + 下单后 15 秒不要碰鼠标键盘。",
                    }

                logger.info(
                    f"✅ 已检测到委托确认框 [{dialog_title}]: {dialog_body[:80]}"
                )
                logger.info(
                    f"已提交 {action} {code} @ {price_str} x {amount_str}股 "
                    f"(hotkey={hotkey.upper()}) — 等待用户在 xiadan 上手动按 Y/N"
                )
                return {
                    "status": "submitted",
                    "msg": f"✅ xiadan 已弹委托确认框 [{dialog_title}], 等待您在 xiadan 上手动按 Y/N",
                    "dialog_title": dialog_title,
                    "dialog_body": dialog_body,
                    "exec_price": float(price_str),
                    "exec_shares": int(amount_str),
                }
        except Exception as e:
            logger.error(f"{action} 操作失败: {e}")
            return {
                "status": "failed",
                "msg": f"{action} GUI 操作异常: {e}",
            }

    def _confirm_order_dialog(self) -> bool:
        """定位「委托确认」框（#32770，标题常为空，靠对话框内容识别）→ 点它的确认按钮。
        返回 True=已点确认按钮。比盲按 Y 可靠：不依赖热键猜测，也不依赖键盘焦点落在框上。
        点不到具名按钮时返回 False，由调用方用 Enter/Y 兜底。"""
        try:
            from pywinauto import Desktop
        except Exception:
            return False
        import time as _t
        # 等确认框出现，最多 ~12s（第一单 xiadan 首次响应可能很慢，3s 不够会漏点→走兜底失败）
        for _ in range(24):
            try:
                dialogs = Desktop(backend='win32').windows(class_name='#32770', visible_only=True)
            except Exception:
                _t.sleep(0.5); continue
            for d in dialogs:
                try:
                    texts = " ".join((c.window_text() or "") for c in d.descendants())
                except Exception:
                    continue
                # 委托确认框：内容含 委托/确认/买入/卖出/是否 之一（标题为空也能命中）
                if not any(k in texts for k in ("委托", "确认", "买入", "卖出", "是否")):
                    continue
                # 枚举对话框所有按钮，点文字含 确定/是/买入/卖出/确认 的那个（排除取消/否）
                try:
                    for ctl in d.descendants(class_name="Button"):
                        bt = (ctl.window_text() or "").strip()
                        if not bt:
                            continue
                        if any(k in bt for k in ("确定", "是", "买入", "卖出", "确认")) \
                           and not any(x in bt for x in ("取消", "否", "放弃")):
                            ctl.click_input()
                            logger.info(f"✅ 已点委托确认框按钮[{bt}]")
                            return True
                except Exception:
                    pass
            _t.sleep(0.5)
        return False

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

        meta = {
            "exec_price": float(price_str),
            "exec_shares": int(amount_str),
        }

        # ★ Bug fix 2026-05-25: 循环检测对话框出现（每 0.3s 查一次，最多 10 秒）★
        # 之前固定 sleep(1.5) 太短：券商响应 0.5~5 秒后才弹"提示：委托已成功提交"，
        # 1.5 秒时刻"委托确认框"刚关闭、"提示"还没弹 → 0 个对话框 → 假 failed
        from pywinauto import Desktop
        relevant_titles = ("提示信息", "委托提示", "提示", "错误", "警告", "委托确认")
        found_dialog = None
        for _attempt in range(33):   # 33 × 0.3s ≈ 10s
            time.sleep(0.3)
            try:
                dialogs = Desktop(backend='win32').windows(class_name='#32770', visible_only=True)
            except Exception as _de:
                continue
            for d in dialogs:
                try:
                    title = d.window_text() or ""
                except Exception:
                    continue
                matched = any(k in title for k in relevant_titles)
                if not matched:
                    # ★ 修复：委托确认/成功提示框的标题常为空，"委托确认"等字在内容区。
                    # 标题没命中就读内容文字再判，避免空标题对话框被漏判成 failed。
                    try:
                        body_txt = " ".join((c.window_text() or "") for c in d.descendants())
                    except Exception:
                        body_txt = ""
                    matched = any(k in body_txt for k in
                                  ("委托", "确认", "成功", "已提交", "资金不足", "余额不足",
                                   "涨停", "跌停", "废单", "失败", "错误", "提示"))
                if matched:
                    found_dialog = (d, title)
                    break
            if found_dialog:
                break

        if found_dialog:
            d, title = found_dialog
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

        # ★ Bug fix 2026-05-25: 按 Y 后没看到任何对话框 = 按键没生效 = failed ★
        # 之前默认 submitted 是乐观假设。实测：xiadan 委托表多次显示空 = 桥接器报 submitted
        # 但实际从未提交。按 Y 之后正常路径必然弹「委托成功」或「错误/提示」对话框。
        # 什么都看不到 = 按键被其他窗口拦截 / xiadan 状态异常。
        try:
            all_dialogs = []
            try:
                from pywinauto import Desktop
                all_dialogs = Desktop(backend='win32').windows(class_name='#32770', visible_only=True)
            except Exception:
                pass
            dialog_titles = []
            for d in all_dialogs:
                try:
                    dialog_titles.append(d.window_text() or "")
                except Exception:
                    pass
            logger.error(
                f"❌ 按 Y 后未检测到任何 #32770 对话框 ({action} {code} {amount_str}股 @{price_str}). "
                f"当前可见 #32770 窗口: {dialog_titles}"
            )
        except Exception:
            pass
        _perm_hint = ("" if getattr(self, "is_admin", False) else
                      "。⚠ 头号嫌疑：本程序非管理员运行——若同花顺是『以管理员身份运行』，"
                      "按键会被 Windows UIPI 静默拦截。请用管理员身份重启本桥接器（与同花顺同权限）")
        return {"status": "failed",
                "msg": f"按 Y 后 xiadan 未弹出任何对话框 — 按键可能被其他窗口拦截，xiadan 实际未收到操作。"
                       f"请确保 xiadan 在前台 + 下单时鼠标键盘不要碰" + _perm_hint,
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

    def query_today_orders(self, code_filter: str = None, max_rows: int = 50,
                           navigate: bool = False) -> dict:
        """切到 xiadan「当日委托」页面，截图 + 本地 OCR 解析成结构化委托列表。

        ⚠️ 背景（2026-05-26 探测确认）：
            xiadan 当日委托表是自定义自绘控件 `CVirtualGridCtrl`，
            - pywinauto 读不到数据（不是 SysListView32）
            - Ctrl+C 不响应（自绘控件不支持标准复制）
        因此走「截图 + OCR」路线：按 F4 → 截图 → Windows 自带 OCR(winocr)
        逐行识别 → 解析出 code/name/action/status/numbers → 切回 F1。
        OCR 难免误差，所以同时返回截图路径，前端可对照截图复核。
        未安装 winocr 时自动降级为「只截图」，orders 为空列表。
        启用方式：pip install winocr（中文 Windows 一般已自带 zh-Hans 语言包）。

        参数：
            code_filter: 只保留代码匹配的委托（OCR 成功后生效）
            max_rows:    最多返回多少条（OCR 成功后生效）
            navigate:    True 时在 F4 后尝试「自校准点击左侧『当日委托』节点」再截图，
                         以拿到真实成交状态（默认 False=只截 F4 默认的资金股票页）。
                         只点查询导航树、绝不点下单按钮；高 DPI 首次须现场核对。

        返回：
            {
              "ok": True,
              "screenshot": "logs/screenshots/today_orders_20260526_153012.png",
              "abs_path": "...",
              "orders": [{code,name,action,status,numbers,raw}, ...],
              "tabs":   ["资金股票","当日委托",...],  # OCR 检出的查询页签（校准用）
              "ocr_lines": ["...", ...],   # OCR 原始分行，便于排错/校准
              "msg": "..."
            }
        """
        nav = self._nav_to_today_orders if navigate else None
        cap = self._capture_query_page(nav=nav, fname_prefix="today_orders")
        if not cap.get("ok"):
            return cap
        fn       = cap["abs_path"]
        rel_path = cap["screenshot"]

        # 用「词坐标→行聚类」重建表格（自绘表格 OCR 后列会被拆成竖条）。
        # ⚠️ 实测：F4 默认进入「资金股票(持仓)」页，不是「当日委托」页！
        # 这里主要产出 持仓 + 可用资金，顺带按行跑一遍委托解析（若已导航到委托页则有值）。
        words     = self._ocr_grid_words(fn)
        rows      = self._cluster_rows(words)
        positions = self._parse_holdings_from_rows(rows)
        funds     = self._parse_funds_from_words(words)
        tabs      = [t["label"] for t in self._detect_query_tabs(words)]
        ocr_lines = [r["text"] for r in rows] or self._ocr_lines(fn)
        orders    = self._parse_orders_from_ocr(ocr_lines)
        if code_filter:
            cf = str(code_filter).strip()
            positions = [p for p in positions if cf in p.get("code", "")]
            orders    = [o for o in orders if cf in o.get("code", "")]
        if max_rows:
            positions = positions[:max_rows]
            orders    = orders[:max_rows]

        if positions or funds.get("available") is not None:
            msg = (
                f"已读取 F4 查询页：可用资金 {funds.get('available')}，"
                f"持仓 {len(positions)} 只（截图 {rel_path}，OCR 难免误差可对照截图复核）。"
                f"注意 F4 默认显示「资金股票/持仓」，如需「当日委托」成交状态需另行导航。"
            )
        elif words:
            msg = (
                f"已切到 F4 查询页并截图 → {rel_path} ; "
                f"OCR 已识别但未解析出表格行（可能空仓/无委托，或需现场校准），请打开图片核对"
            )
        else:
            msg = (
                f"已切到 F4 查询页并截图 → {rel_path} ; "
                f"⚠️ 本地 OCR 不可用（pip install winocr 后启用结构化解析），暂请打开图片查看"
            )
        return {
            "ok": True,
            "screenshot": rel_path,
            "abs_path": fn,
            "funds": funds,
            "positions": positions,
            "orders": orders,
            "tabs": tabs,
            "ocr_lines": ocr_lines,
            "msg": msg,
        }

    @_gui_serialized
    def _capture_query_page(self, nav=None, fname_prefix: str = "query",
                            page_key: str = "f4") -> dict:
        """激活 xiadan → 按 page_key 进目标页 →（可选 nav 切子页）→ 截图 → 切回 F1。

        只读：仅切页 + 截图，绝不按 Enter / 不提交委托。
        page_key: 进入哪个功能页，默认 'f4'(查询)；'f3'=撤单页 等。
        nav: 可选回调 nav(pyautogui, hwnd)，在 page_key 之后、截图之前执行，
             用于把查询区从默认「资金股票」切到「当日委托」等子页。
        返回 {ok:True, screenshot(相对路径), abs_path} 或 {ok:False, msg}。
        切回 F1 在 finally 里保证执行，避免影响下次下单。
        """
        if DRY_RUN:
            return {"ok": False, "msg": "DRY_RUN 模式无 xiadan"}
        import os
        if os.name != "nt":
            return {"ok": False, "msg": "非 Windows 系统"}

        import pyautogui
        # 关掉 fail-safe：本操作有限且短（F4→可选点击→截图→F1，无循环），不会失控；
        # 否则用户在那 2-3 秒里鼠标恰好移到屏幕角落就会中断读取（误以为功能坏了）。
        pyautogui.FAILSAFE = False
        if self.main_win is None:
            try:
                self._connect()
            except Exception as e:
                return {"ok": False, "msg": f"xiadan 未连接: {e}"}
        if self.main_win is None:
            return {"ok": False, "msg": "xiadan 窗口不可用"}

        try:
            import win32gui, win32con, win32process
            import ctypes
        except Exception as e:
            return {"ok": False, "msg": f"win32 不可用: {e}"}

        hwnd = self.main_win.handle
        if not win32gui.IsWindow(hwnd):
            try: self._connect()
            except Exception: pass
            if self.main_win is None:
                return {"ok": False, "msg": "xiadan 窗口已关闭"}
            hwnd = self.main_win.handle

        user32 = ctypes.windll.user32
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.4)
        try:
            target_tid, _ = win32process.GetWindowThreadProcessId(hwnd)
            cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            if target_tid and target_tid != cur_tid:
                user32.AttachThreadInput(cur_tid, target_tid, True)
                try:
                    win32gui.BringWindowToTop(hwnd)
                    win32gui.SetForegroundWindow(hwnd)
                finally:
                    user32.AttachThreadInput(cur_tid, target_tid, False)
            time.sleep(0.2)
        except Exception as _e:
            logger.debug(f"激活异常: {_e}")

        # 鼠标点击标题栏兜底激活（只点标题区，不点按钮，避免误触下单）
        if win32gui.GetForegroundWindow() != hwnd:
            try:
                rect = win32gui.GetWindowRect(hwnd)
                if rect[0] > -1000:
                    cx, cy = (rect[0] + rect[2]) // 2, rect[1] + 15
                    op = pyautogui.position()
                    pyautogui.click(cx, cy, duration=0.05)
                    time.sleep(0.2)
                    pyautogui.moveTo(op[0], op[1], duration=0.05)
            except Exception:
                pass

        if win32gui.GetForegroundWindow() != hwnd:
            return {"ok": False, "msg": "xiadan 未能激活到前台"}

        # 窗口尺寸守卫：xiadan 处于「迷你下单条 / 最小化残留」时只有 买卖撤查
        # 一条工具栏，F4 没有查询表格可截。与其 OCR 出一条工具栏不如明确报错，
        # 引导用户把窗口还原成完整尺寸（实测最小化后窗口仅 ~237x39）。
        try:
            _l, _t, _r, _b = win32gui.GetWindowRect(hwnd)
            win_w, win_h = _r - _l, _b - _t
        except Exception:
            win_w = win_h = 0
        if win_w < 600 or win_h < 400:
            return {
                "ok": False,
                "msg": (
                    f"xiadan 当前窗口过小（{win_w}x{win_h}），疑似处于"
                    f"「迷你下单条 / 最小化」状态，没有 F4 查询表格可读。"
                    f"请把『网上股票交易系统5.0』还原成完整窗口"
                    f"（双击迷你条标题 / 点右上角还原按钮，能看到资金持仓表格），"
                    f"保持可见后重试。"
                ),
            }

        try:
            pyautogui.press(page_key)
            time.sleep(1.0)   # 等 xiadan 渲染目标页
            if nav is not None:
                try:
                    nav(pyautogui, hwnd)
                    time.sleep(0.8)
                except Exception as e:
                    logger.warning(f"查询子页导航失败（仍截当前页）: {e}")

            from pathlib import Path
            from datetime import datetime
            proj_root = Path(__file__).resolve().parent.parent
            scr_dir = proj_root / "logs" / "screenshots"
            scr_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fn = scr_dir / f"{fname_prefix}_{ts}.png"
            saved = False
            try:
                if self.main_win is not None:
                    img = self.main_win.capture_as_image()
                    img.save(str(fn))
                    saved = True
            except Exception:
                pass
            if not saved:
                try:
                    pyautogui.screenshot(str(fn))
                    saved = True
                except Exception:
                    pass
            if not saved:
                return {"ok": False, "msg": "截图保存失败"}

            rel_path = str(fn.relative_to(proj_root)).replace("\\", "/")
            return {"ok": True, "screenshot": rel_path, "abs_path": str(fn)}
        except Exception as e:
            logger.exception(f"_capture_query_page 异常: {e}")
            return {"ok": False, "msg": f"调用异常: {e}"}
        finally:
            try:
                time.sleep(0.3)
                pyautogui.press(THS_BUY_KEY)
                time.sleep(0.3)
            except Exception:
                pass

    def _nav_to_today_orders(self, pyautogui, hwnd) -> bool:
        """自校准导航到「当日委托」子页（供 _capture_query_page 的 nav 回调）。

        流程：截当前 F4 查询页（窗口相对像素）→ OCR 定位左侧「当日委托」节点 →
        换算成屏幕坐标并点击。成功点击返回 True，未定位到返回 False（调用方继续
        截当前页，安全降级）。

        ⚠️ 安全：F4 后处于「查询视图」，左侧只有查询分类节点，点不到任何下单按钮；
            点击点再钳制在窗口矩形内，绝不点窗口外、绝不按 Enter、绝不提交委托。
        ⚠️ 高 DPI：窗口截图像素与屏幕坐标可能存在缩放差，已按 窗口宽高/截图宽高
            做线性换算；首次仍建议现场核对点击是否命中（看返回截图）。
        """
        try:
            import win32gui
        except Exception:
            return False
        try:
            img = self.main_win.capture_as_image()
        except Exception as e:
            logger.warning(f"导航截图失败: {e}")
            return False

        from pathlib import Path
        from datetime import datetime
        proj_root = Path(__file__).resolve().parent.parent
        tmp_dir = proj_root / "logs" / "screenshots"
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp = tmp_dir / f"_navtmp_{datetime.now().strftime('%H%M%S_%f')}.png"
            img.save(str(tmp))
        except Exception as e:
            logger.warning(f"导航临时图保存失败: {e}")
            return False
        try:
            words = self._ocr_grid_words(tmp)
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass

        tabs   = self._detect_query_tabs(words)
        target = next((t for t in tabs if t["label"] == "当日委托"), None)
        if not target:
            logger.info(f"导航：未定位到「当日委托」（检出页签 {[t['label'] for t in tabs]}）；保持当前页")
            return False

        try:
            rect = win32gui.GetWindowRect(hwnd)      # (l, t, r, b) 屏幕坐标
        except Exception:
            return False
        win_w, win_h = rect[2] - rect[0], rect[3] - rect[1]
        iw, ih = (img.width or win_w), (img.height or win_h)
        sx = (win_w / iw) if iw else 1.0
        sy = (win_h / ih) if ih else 1.0
        cx = rect[0] + (target["x"] + target["w"] / 2.0) * sx
        cy = rect[1] + (target["y"] + target["h"] / 2.0) * sy
        # 钳制在窗口内，绝不点到窗口外
        cx = min(max(cx, rect[0] + 2), rect[2] - 2)
        cy = min(max(cy, rect[1] + 2), rect[3] - 2)
        try:
            op = pyautogui.position()
            pyautogui.click(int(cx), int(cy), duration=0.05)
            time.sleep(0.2)
            pyautogui.moveTo(op[0], op[1], duration=0.05)
            logger.info(f"导航：已点击「当日委托」@屏幕({int(cx)},{int(cy)}) 缩放=({sx:.3f},{sy:.3f})")
            return True
        except Exception as e:
            logger.warning(f"导航点击失败: {e}")
            return False

    def query_account_snapshot(self, max_rows: int = 50) -> dict:
        """读 F4「资金股票」页：可用资金 / 总资产 + 持仓列表（只读，不下单）。

        这是「实盘资金 + 持仓」面板的数据源，已在真实 xiadan 上验证：
        可用资金从顶部工具栏 OCR、持仓靠词坐标行聚类，代码 100% 可靠
        （名称建议调用方用本地名称库按 code 反查纠正 OCR 误差）。

        返回 {ok, available, total_asset, positions:[{code,name,numbers,raw}],
              screenshot, abs_path, msg}
        """
        cap = self._capture_query_page(fname_prefix="account")
        if not cap.get("ok"):
            return cap
        fn       = cap["abs_path"]
        rel_path = cap["screenshot"]
        words     = self._ocr_grid_words(fn)
        rows      = self._cluster_rows(words)

        # ⚠️ 截图可能包含整个桌面（浏览器+xiadan），导致资金读到浏览器文字。
        # 检测并裁剪 xiadan 区域：找 F2/F3 菜单栏标记（只在 xiadan 窗口出现）
        words, fn = self._crop_to_xiadan_if_needed(words, fn)
        rows      = self._cluster_rows(words)

        positions = self._parse_holdings_from_rows(rows)
        if max_rows:
            positions = positions[:max_rows]
        funds = self._parse_funds_from_words(words)
        # 顺手同步资金缓存：手动刷新面板时，顶部「可用资金」药丸也随之更新（保持一致）
        if funds.get("available") is not None or funds.get("total_asset") is not None:
            import time as _t
            self._funds_cache = {"ts": _t.time(), "data": {
                "ok": True, "mode": "real",
                "available":   funds.get("available"),
                "total_asset": funds.get("total_asset"),
            }}
        return {
            "ok": True,
            "available":   funds.get("available"),
            "total_asset": funds.get("total_asset"),
            "positions":   positions,
            "screenshot":  rel_path,
            "abs_path":    fn,
            "msg": (
                f"实盘可用资金 {funds.get('available')}，总资产 {funds.get('total_asset')}，"
                f"持仓 {len(positions)} 只（OCR 自绘表格，数字列仅供参考，请对照截图）"
            ),
        }

    def _crop_to_xiadan_if_needed(self, words: list, img_path: str) -> tuple:
        """检测截图是否包含浏览器等非 xiadan 内容，若是则裁剪到 xiadan 区域。

        检测策略：xiadan 菜单栏有「卖出 [F2]」「撤单 [F3]」标记，浏览器中不会有。
        找到这些标记的 y 坐标，裁掉上方浏览器区域，重新 OCR。

        返回 (words, img_path)——如果裁剪了则返回新 OCR 词和裁剪图路径，
        否则原样返回。
        """
        import re
        # 找 F2/F3 标记（xiadan 菜单栏特征）
        f2_words = [w for w in words if "[F2" in w["text"]]
        f3_words = [w for w in words if "F3" in w["text"]]

        if not f2_words and not f3_words:
            # 没找到 xiadan 菜单标记 → 截图可能已经是纯 xiadan 窗口
            return words, img_path

        # 找 xiadan 菜单栏的 y 坐标（取 F2/F3 的最小 y）
        menu_ys = [w["y"] for w in f2_words + f3_words]
        xiadan_top = min(menu_ys) - 30  # 上方留 30px 余量

        if xiadan_top <= 30:
            # xiadan 已经在截图顶部，无需裁剪
            return words, img_path

        # 裁剪并重新 OCR
        from PIL import Image
        try:
            img = Image.open(str(img_path))
            cropped = img.crop((0, int(xiadan_top), img.width, img.height))
            # 保存裁剪图（覆盖原文件，后续引用一致）
            crop_path = img_path.replace(".png", "_xiadan.png").replace(".jpg", "_xiadan.jpg")
            cropped.save(crop_path)
            new_words = self._ocr_grid_words(crop_path)
            if new_words:
                logger.info(f"已裁剪 xiadan 区域 (y>={xiadan_top:.0f})，词数 {len(words)}→{len(new_words)}")
                return new_words, crop_path
        except Exception as e:
            logger.debug(f"裁剪 xiadan 区域失败，使用原图: {e}")
        return words, img_path

    def _ocr_lang(self) -> str:
        """挑一个可用的中文 OCR 语言标签（缓存）。

        winocr 依赖 Windows 自带 OCR 引擎，语言标签形如 zh-Hans-CN / en-US。
        优先简体中文，没有则退回第一个可用语言；探测失败默认 'zh-Hans-CN'。
        """
        cached = getattr(self, "_ocr_lang_cached", None)
        if cached:
            return cached
        lang = "zh-Hans-CN"
        try:
            from winrt.windows.media.ocr import OcrEngine
            tags = [L.language_tag for L in OcrEngine.available_recognizer_languages]
            picked = None
            for cand in ("zh-hans-cn", "zh-hans", "zh-cn", "zh"):
                for t in tags:
                    if t.lower().startswith(cand):
                        picked = t
                        break
                if picked:
                    break
            if not picked and tags:
                picked = tags[0]
            if picked:
                lang = picked
        except Exception as e:
            logger.debug(f"OCR 语言探测失败，用默认 {lang}: {e}")
        self._ocr_lang_cached = lang
        return lang

    @staticmethod
    def _ocr_result_to_lines(res) -> list:
        """把 winocr 返回结果统一成行文本列表（兼容 dict 与 对象 两种形态）。

        winocr 0.0.15 的 recognize_pil_sync 经 picklify() 返回 **dict**：
            {'text': '...', 'lines': [{'text': '...', 'words': [...]}, ...]}
        旧/别的版本可能直接返回 OcrResult 对象（.text/.lines）。两种都兜住。
        """
        def _get(o, k):
            if isinstance(o, dict):
                return o.get(k)
            return getattr(o, k, None)
        lines = []
        for ln in (_get(res, "lines") or []):
            t = (_get(ln, "text") or "")
            if t and t.strip():
                lines.append(t.strip())
        if not lines:
            txt = (_get(res, "text") or "")
            lines = [s.strip() for s in txt.splitlines() if s.strip()]
        return lines

    @staticmethod
    def _ocr_load_scaled(img_path):
        """打开截图并自适应放大到约 2000px 宽再交给 OCR。

        实测：xiadan 还原成中等窗口（如 1049×630）时，顶部资金行的小数点/小字
        会被 Windows OCR 丢掉（可用→石T用、208374.89→208374 89）。放大到 3000px
        后小字识别明显更准。返回 (PIL.Image, factor)；factor 为放大倍数，取词坐标的
        调用方需把坐标除以 factor 还原回原图像素（保证点击/聚类坐标系不变）。
        """
        from PIL import Image
        img = Image.open(str(img_path))
        w = img.width or 1
        factor = 1.0
        if w < 2000:
            factor = round(3000.0 / w, 3)
            if factor < 1.0:
                factor = 1.0
        if factor > 1.0:
            img = img.resize((int(img.width * factor), int(img.height * factor)), Image.LANCZOS)
        return img, factor

    def _ocr_text(self, img_path) -> str:
        """对截图做 OCR 返回识别文本。用 Windows 自带 OCR(winocr)，离线、支持中文。

        未安装 winocr 或失败时返回空串，不影响主流程（控件解析仍是首选）。
        启用方式：pip install winocr ；中文 Windows 一般已自带 zh-Hans 语言包。
        相比 tesseract(+200MB) 这个方案几乎零体积——直接调用系统 OCR 引擎。
        """
        try:
            import winocr
            img, _ = self._ocr_load_scaled(img_path)
            res = winocr.recognize_pil_sync(img, self._ocr_lang())
            txt = res.get("text") if isinstance(res, dict) else getattr(res, "text", "")
            return txt or ""
        except Exception as e:
            logger.debug(f"OCR 不可用或失败: {e}")
            return ""

    def _ocr_lines(self, img_path) -> list:
        """对截图做 OCR 返回「按行」文本列表（用于委托表逐行解析）。

        未安装 winocr 或失败时返回 []，不影响主流程。
        """
        try:
            import winocr
            img, _ = self._ocr_load_scaled(img_path)
            res = winocr.recognize_pil_sync(img, self._ocr_lang())
            return self._ocr_result_to_lines(res)
        except Exception as e:
            logger.debug(f"OCR 分行不可用或失败: {e}")
            return []

    # ── 基于「词坐标」重建表格（xiadan 自绘表格 OCR 后会把列拆成竖条，
    #    必须靠每个词的 bounding box 按 Y 聚成行、按 X 排成列才能还原）──

    def _ocr_grid_words(self, img_path) -> list:
        """OCR 返回每个词的坐标：[{x, y, w, h, text}, ...]（图像像素坐标）。

        winocr 的每个 word 带 bounding_rect{x,y,width,height}，这是把自绘表格
        还原成行列的关键。未装 winocr / 失败 → []。
        """
        try:
            import winocr
            img, factor = self._ocr_load_scaled(img_path)
            res = winocr.recognize_pil_sync(img, self._ocr_lang())
            def _g(o, k):
                return o.get(k) if isinstance(o, dict) else getattr(o, k, None)
            inv = 1.0 / factor if factor else 1.0
            lines = _g(res, "lines") or []
            words = []
            for ln in lines:
                for w in (_g(ln, "words") or []):
                    t = (_g(w, "text") or "").strip()
                    br = _g(w, "bounding_rect")
                    if not t or not br:
                        continue
                    try:
                        x = float(_g(br, "x")); y = float(_g(br, "y"))
                        ww = float(_g(br, "width") or 0); hh = float(_g(br, "height") or 0)
                    except Exception:
                        continue
                    # 坐标除以放大倍数还原回原截图像素（点击/聚类坐标系不变）
                    words.append({"x": x * inv, "y": y * inv,
                                  "w": ww * inv, "h": hh * inv, "text": t})
            return words
        except Exception as e:
            logger.debug(f"OCR 取词坐标失败: {e}")
            return []

    @staticmethod
    def _cluster_rows(words: list, y_tol: float = None) -> list:
        """把词按 Y 坐标聚成行，行内按 X 排序。

        返回 [{'y':行中心, 'words':[{x,text,...}按X排序], 'text':整行拼接}, ...]，按 Y 升序。
        y_tol 自适应：根据词高（h）的中位数动态计算，默认为行高的一半。
        xiadan 行高 ~42px → y_tol ~12；高分辨率截图行高更大 → y_tol 自动放大。
        """
        if not words:
            return []
        # 自适应 y_tol：用词高的中位数 × 0.6 作为行聚类容差
        if y_tol is None:
            heights = sorted(w.get("h", 20) for w in words if w.get("h", 0) > 5)
            if heights:
                median_h = heights[len(heights) // 2]
                y_tol = max(8.0, median_h * 0.6)
            else:
                y_tol = 12.0
        rows = []
        for w in sorted(words, key=lambda d: d["y"]):
            placed = False
            for r in rows:
                if abs(w["y"] - r["y"]) <= y_tol:
                    r["words"].append(w)
                    r["y"] = (r["y"] * r["n"] + w["y"]) / (r["n"] + 1)
                    r["n"] += 1
                    placed = True
                    break
            if not placed:
                rows.append({"y": w["y"], "n": 1, "words": [w]})
        for r in rows:
            r["words"].sort(key=lambda d: d["x"])
            r["text"] = " ".join(x["text"] for x in r["words"])
        rows.sort(key=lambda r: r["y"])
        return rows

    @staticmethod
    def _norm_num(s: str):
        """把 OCR 数字串规整成 float：去空格、全角点／逗号→半角，去千分位，
        修正常见 OCR 数字误认（O→0, l→1 等）。

        例：'208374 ． 89' → 208374.89 ；'1,304.56' → 1304.56 ；失败返回 None。
        """
        import re
        if not s:
            return None
        t = (s.replace(" ", "").replace("．", ".").replace("，", "")
               .replace(",", "").replace("。", ".").replace("·", "."))
        # 修正常见 OCR 数字误认（仅在数字上下文中）
        t = (t.replace("O", "0").replace("o", "0")
               .replace("l", "1").replace("I", "1").replace("i", "1")
               .replace("S", "5").replace("s", "5")
               .replace("B", "8").replace("G", "6"))
        m = re.search(r"-?\d+(?:\.\d+)?", t)
        if not m:
            return None
        try:
            return float(m.group(0))
        except ValueError:
            return None

    @staticmethod
    def _detect_code_column(rows: list, tol: float = 30.0):
        """找「证券代码列」的 X 中心：统计所有纯 6 位数字词的 x，取最大簇中心。

        这样能把代码列（如 x≈383）和顶部资产数字（如 208374，x≈494/760）区分开，
        避免把资产金额误当成股票代码。返回 (x_center, members) 或 (None, [])。
        """
        import re
        xs = []
        for r in rows:
            for w in r["words"]:
                if _as_stock_code(w["text"]):
                    xs.append(w["x"])
        if not xs:
            return None, []
        xs.sort()
        best_center, best_members = None, []
        for c in xs:
            members = [x for x in xs if abs(x - c) <= tol]
            if len(members) > len(best_members):
                best_members = members
                best_center = sum(members) / len(members)
        return best_center, best_members

    def _parse_holdings_from_rows(self, rows: list, code_x=None, x_tol: float = 30.0) -> list:
        """从 F4「资金股票」表的行里抽持仓。

        策略：先定位代码列 X（_detect_code_column），只认代码列里有 6 位代码的行为
        持仓行；代码右边第一段中文当股票名；行内其余数字按出现顺序当作
        余额/可用/成本/市值/盈亏等（列名因券商而异，故只给有序数字 + 整行原文，
        前端可对照截图，必要时按真实表头映射）。
        """
        import re
        # ⚠️ 不再用 skip_re 过滤整行！
        # xiadan 左侧菜单文本（「市价委托」「批量下单」等）与右侧行情表共享 Y 坐标，
        # OCR 后会被聚到同一行。用 skip_re 过滤会误杀含「市价」「批量」的持仓行。
        # 改为：先找所有 6 位股票代码，再只提取代码右侧的数字（左菜单数字被自然排除）。
        if code_x is None:
            code_x, _ = self._detect_code_column(rows, x_tol)
        if code_x is None:
            code_x = 0
        holdings = []
        for r in rows:
            ws = r["words"]
            code_word = None; code = None
            # 先在整行找代码（放宽 x_tol 到 50，应对 OCR 坐标偏移）
            for w in ws:
                _c = _as_stock_code(w["text"])
                if _c and abs(w["x"] - code_x) <= max(x_tol, 50):
                    code_word = w; code = _c
                    break
            # 降级：如果在代码列找不到，行内任意位置找 6 位代码
            if not code_word:
                for w in ws:
                    _c = _as_stock_code(w["text"])
                    if _c:
                        code_word = w; code = _c
                        break
            if not code_word:
                continue
            # 只取代码右侧的词（排除左菜单区域）
            right = [w for w in ws if w["x"] > code_word["x"]]
            # 名称：代码右侧「连续的中文词」拼起来（OCR 把汉字拆成单字词，
            # 如 杭/电/股/份），遇到数字列就停——避免把后面的「上海A股」也吃进来。
            # 改进：允许中英文混合（如「A」「ST」），长度放宽到 6 个字符
            name_parts = []
            for w in right:
                t = w["text"].strip()
                if not t:
                    continue
                # 跳过纯数字（股票代码列或数字列）
                if re.fullmatch(r"\d{6}", t):
                    continue
                # 中文/英文/ST 等股票名片段
                if re.fullmatch(r"[一-鿿A-Za-z*]{1,6}", t) and len(t) <= 6:
                    name_parts.append(t)
                elif name_parts:
                    break  # 已开始收名字又遇到非纯中文(数字等) → 名字结束
            name = "".join(name_parts)
            # 行内数字（规整后）作为参考列——只取代码右侧的
            nums = []
            for w in right:
                v = self._norm_num(w["text"])
                if v is not None and not re.fullmatch(r"\d{6}", w["text"]):
                    nums.append(v)
            holdings.append({
                "code": code, "name": name, "numbers": nums,
                "raw": re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", r["text"]),
            })
        return holdings

    def _parse_funds_from_words(self, words: list) -> dict:
        """从顶部工具栏词里抽「可用 / 资产」金额。

        xiadan 工具栏自绘控件 OCR 后，每个汉字常被拆成独立词（如「可用」→
        「可」+「用」），多行工具栏（菜单行+资金行+持仓行）y 跨度可达 100+px。
        本方法改用「词坐标」策略：先找标签关键词所在行，再取同行右侧最近的数字词，
        相邻数字词自动合并并做小数点恢复。
        """
        import re
        if not words:
            return {"available": None, "total_asset": None}

        top_y = min(w["y"] for w in words)
        # 覆盖完整工具栏区域（菜单行+资金行+持仓行，y 跨度可达 150+px）
        top_words = [w for w in words if w["y"] <= top_y + 200]

        # ── 按行聚类 ──
        rows = self._cluster_rows(top_words)

        def _norm(s: str) -> str:
            """折叠空格 + 全角转半角"""
            s = (s.replace("．", ".").replace("，", "").replace("，", "")
                  .replace("：", ":").replace("．", "."))
            return re.sub(r"\s+", "", s)

        def _label_in_row(row_words: list, *label_parts) -> bool:
            """检查一行词中是否包含标签的每个字（允许被 OCR 拆成独立词）。"""
            texts = [_norm(w["text"]) for w in row_words]
            joined = "".join(texts)
            return all(part in joined for part in label_parts)

        def _nums_right_of(row_words: list, anchor_x: float) -> list:
            """取锚点右侧同行的所有数字词（x 距离 < 500px），合并相邻数字并恢复小数。"""
            right = sorted([w for w in row_words if w["x"] > anchor_x + 5],
                           key=lambda d: d["x"])
            nums = []
            prev_x = None
            prev_is_dot = False
            for w in right:
                t = _norm(w["text"])
                # 只取纯数字（含小数点）
                is_dot = (t == ".")
                is_num = bool(re.fullmatch(r"\d+\.?\d*", t))
                if not is_num and not is_dot:
                    continue
                # 相邻数字词（x 差距 < 30px）可能是同一个金额被拆开（如 4783 + .87）
                if prev_x is not None and w["x"] - prev_x < 30 and nums:
                    if is_dot:
                        # 小数点词，标记下一个数字是小数部分
                        prev_is_dot = True
                        prev_x = w["x"] + w.get("w", 0)
                        continue
                    elif prev_is_dot:
                        # 小数点后的数字，拼接时加小数点
                        nums[-1] = nums[-1] + "." + t
                        prev_is_dot = False
                    else:
                        # 无小数点的相邻数字合并（如 4783 + 87 → 4783.87）
                        # 判断：前一个是4+位整数，当前是2位 → 可能是小数部分
                        prev_n = nums[-1]
                        if "." not in prev_n and len(prev_n) >= 3 and len(t) == 2:
                            nums[-1] = prev_n + "." + t
                        else:
                            nums[-1] = prev_n + t
                else:
                    nums.append(t)
                    prev_is_dot = False
                prev_x = w["x"] + w.get("w", 0)
            result = []
            for n in nums:
                try:
                    result.append(float(n))
                except ValueError:
                    pass
            return result

        # ── 逐行找「可用」和「资产」──
        available = None
        total_asset = None

        for row in rows:
            ws = row["words"]
            row_text = "".join(_norm(w["text"]) for w in ws)

            # 匹配「可用」（含 OCR 误识别修正）
            row_fixed = (row_text.replace("可田", "可用").replace("可甩", "可用")
                         .replace("石T用", "可用").replace("可月", "可用")
                         .replace("可目", "可用"))
            if available is None and "可用" in row_fixed:
                # 找「可」字的 x 坐标作为锚点
                for w in ws:
                    if _norm(w["text"]) == "可":
                        nums = _nums_right_of(ws, w["x"])
                        # 跳过「全」「额」等非数字，取第一个真正的大额数字
                        for n in nums:
                            if n >= 10:
                                available = round(n, 2)
                                break
                        break

            # 匹配「总资产」
            row_fixed2 = (row_text.replace("总资广", "总资产").replace("总资全", "总资产")
                          .replace("资全", "资产").replace("资广", "资产")
                          .replace("资厂", "资产"))
            if total_asset is None and "总资产" in row_fixed2:
                for w in ws:
                    if _norm(w["text"]) == "总":
                        nums = _nums_right_of(ws, w["x"])
                        for n in nums:
                            if n >= 10:
                                total_asset = round(n, 2)
                                break
                        break

        # 降级：取工具栏区域所有大额数字
        if available is None or total_asset is None:
            all_nums = []
            for row in rows:
                for w in row["words"]:
                    t = _norm(w["text"])
                    try:
                        v = float(t)
                        if v >= 100:
                            all_nums.append(v)
                    except ValueError:
                        pass
            if total_asset is None and all_nums:
                total_asset = max(all_nums)
            if available is None and all_nums:
                # 可用资金通常比总资产小
                smaller = [n for n in all_nums if n < (total_asset or float("inf"))]
                available = max(smaller) if smaller else min(all_nums)

        return {"available": available, "total_asset": total_asset}

        # ── 逻辑校验：可用资金不应超过总资产 ──────────────────
        if avail is not None and total is not None and avail > total:
            # 可用 > 总资产 = 不合理，说明小数点恢复出错
            # 尝试把 avail 除以 100 看是否合理
            if avail / 100 > 1 and abs(avail / 100 - total) / max(total, 1) < 0.5:
                logger.warning(f"[funds] 可用资金({avail})>总资产({total})，自动修正 ÷100 → {avail/100}")
                avail = avail / 100
            else:
                logger.warning(f"[funds] 可用资金({avail})>总资产({total})，数据可能有误")

        return {"available": avail, "total_asset": total}

    # 查询区左侧导航节点（用于定位/导航到「当日委托」等子页）
    _QUERY_TAB_KEYS = [
        "资金股票", "当日委托", "当日成交", "可撤单",
        "历史委托", "历史成交", "新股申购", "对账单",
    ]

    @staticmethod
    def _detect_query_tabs(words: list) -> list:
        """从 F4 查询页 OCR 词里定位左侧导航节点（当日委托/当日成交/...）及坐标。

        Windows OCR 把「当 日 委 托」拆成单字词，故按行聚类后在行内找「拼起来含
        该标签」的最短连续词窗，取其包围盒——坐标精准到标签本身，便于换算屏幕坐标
        点击。返回 [{label, x, y, w, h}]（窗口/图像相对像素，x/y 为左上角）。READ-ONLY。
        """
        import re
        if not words:
            return []
        rows = THSOperator._cluster_rows(words)

        def _window_for(ws_sorted, key):
            # 在按 x 排序的词里找最短「连续词窗」，其拼接含 key（忽略空格）
            n = len(ws_sorted)
            for i in range(n):
                joined = ""
                for j in range(i, n):
                    joined += ws_sorted[j]["text"]
                    if key in joined.replace(" ", ""):
                        return ws_sorted[i:j + 1]
                    if len(joined) > len(key) + 4:
                        break
            return None

        tabs = []
        seen = set()
        for r in rows:
            collapsed = re.sub(r"\s+", "", r["text"])
            for key in THSOperator._QUERY_TAB_KEYS:
                if key in seen or key not in collapsed:
                    continue
                win = _window_for(r["words"], key)
                if not win:
                    continue
                x0 = min(w["x"] for w in win)
                y0 = min(w["y"] for w in win)
                x1 = max(w["x"] + w["w"] for w in win)
                y1 = max(w["y"] + w["h"] for w in win)
                tabs.append({"label": key, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0})
                seen.add(key)
        return tabs

    # 委托状态关键词：长词在前，避免「已成」先于「已成交」、或漏掉「全部撤单」造成误判。
    # 撤单类（全部撤单/部分撤单/已撤单/部成已撤）必须在「按成交量推断」之前命中，
    # 否则已撤单(成交量=0)会被兜底逻辑错标成「已报」。
    _ORDER_STATUS_KEYS = [
        "全部成交", "部分成交", "全部撤单", "部分撤单", "部成已撤",
        "已成交", "已撤单", "未成交",
        "已成", "部成", "已撤", "已报", "废单",
    ]

    # 合法 A 股代码前缀（用于把真代码和「合同编号/委托时间/价格」等乱码 6 位数区分开）：
    #   深主板/中小 00[0-3] | 创业板 30[0-2] | 沪主板 60[0-5]/608 | 科创 68[89] |
    #   北交 8[3-9]x·4xx·92x | 深B 20x | 沪B 90x
    _VALID_CODE_RE = __import__("re").compile(
        r"^(00[0-3]|30[0-2]|60[0-58]|68[89]|8[3-9]\d|4\d\d|92\d|20\d|90\d)\d{3}$"
    )

    @staticmethod
    def _pick_stock_code(text: str):
        """从一行文本里挑出最像 A 股代码的 6 位数字，避开「委托时间/合同编号」误判。

        两层筛选：
          1) 先按「合法 A 股代码前缀」过滤——能挡掉 615328/609767 这类合同编号
             乱码（61/609 不是合法前缀），也挡掉交易时段 09-15 开头的委托时间；
          2) 合法集合里再优先首位 3/6/8/9（绝不是 HHMMSS 时间），否则取第一个；
          3) 没有合法代码时退回老逻辑（首位 3/4/6/8/9，再退第一个）。
        """
        import re
        toks = re.findall(r"(?<!\d)(\d{6})(?!\d)", text)
        if not toks:
            return None
        valid = [t for t in toks if THSOperator._VALID_CODE_RE.match(t)]
        if valid:
            for t in valid:
                if t[0] in "3689":
                    return t
            return valid[0]
        for t in toks:
            if t[0] in "34689":
                return t
        return toks[0]

    def _parse_orders_from_ocr(self, lines: list) -> list:
        """把 OCR 的委托表文本行解析成结构化委托列表。

        Windows OCR 两个固定怪癖已在此处理：
          1) 中日韩字符之间会插空格（贵 州 茅 台）→ 先折叠相邻汉字间的空格；
          2) 数字/字母偶有误识（5↔S 等）→ 无法全自动纠正，故始终保留截图供核对。

        每条委托一般含 6 位证券代码；据此过滤噪声行，再尽力提取：
            code   6 位代码（避开委托时间误判，见 _pick_stock_code）
            name   代码后紧跟的中文（股票名）
            action 买入/卖出（含「买」「卖」单字兜底）
            status 命中 _ORDER_STATUS_KEYS 的第一个（长词优先）；无文字状态时
                   按 成交量/委托量 推断 已报/部分成交/全部成交
            order_qty/filled_qty/order_price  委托数量/成交数量/委托价格
                   （状态或方向词之后按列顺序提取，已对真实委托行校准）
            numbers 行内所有数字（价格/数量等，供前端/核对参考）
            raw    折叠汉字间空格后的整行（比原始 OCR 更易读，仍忠于内容）
        """
        import re
        orders = []
        for orig in (lines or []):
            if not orig or not orig.strip():
                continue
            # 折叠「相邻汉字之间」的空格：贵 州 茅 台 → 贵州茅台（不动数字/字母间空格）
            line = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", orig.strip())
            # 跳过顶部资金工具栏/菜单行（含这些词的绝不是委托行），避免把资产金额
            # （如 208374）误当成委托代码——空委托表时尤其容易误判。
            if re.search(r"资产|可用|新股申购|帮助|反馈|设置|登录|退出|涨停|跌停", line):
                continue
            code = self._pick_stock_code(line)
            if not code:
                continue
            # 方向（两字形式优先，避免股票名里的「买/卖」误判）
            action = ""
            if "买入" in line:
                action = "买入"
            elif "卖出" in line:
                action = "卖出"
            elif "买" in line:
                action = "买入"
            elif "卖" in line:
                action = "卖出"
            # 股票名 = 代码之后、方向词(买/卖)之前 的那段中文
            # （折叠空格会把「名称 买卖」连成一片，所以用方向词当右边界切出名称）
            name = ""
            idx = line.find(code)
            tail = line[idx + len(code):] if idx >= 0 else line
            mdir = re.search(r"[买卖]", tail)
            name_zone = tail[:mdir.start()] if mdir else tail
            nm = re.search(r"[一-鿿]{2,8}", name_zone)
            if nm:
                name = nm.group(0)
            # 状态（长词优先，避免「已成」抢先于「已成交」）
            status = ""
            for k in self._ORDER_STATUS_KEYS:
                if k in line:
                    status = k
                    break
            # 结构化「委托数量/成交数量/委托价格」：在 状态(备注) 或 方向词 之后，
            # xiadan 当日委托列顺序固定为 委托数量 成交数量 委托价格 成交均价 …
            # （已对真实行校准：未成交 100 0 10.930 0.000 … → 100/0/10.93）
            order_qty = filled_qty = order_price = None
            anchor = -1
            if status and status in line:
                anchor = line.find(status) + len(status)
            elif mdir:
                anchor = idx + len(code) + mdir.end()
            if anchor >= 0:
                tail2 = line[anchor:].replace("．", ".").replace("。", ".")
                tail2 = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", tail2)  # 合拢小数点
                tnums = re.findall(r"\d+\.\d+|\d+", tail2)
                if len(tnums) >= 1: order_qty   = self._norm_num(tnums[0])
                if len(tnums) >= 2: filled_qty  = self._norm_num(tnums[1])
                if len(tnums) >= 3: order_price = self._norm_num(tnums[2])
            # 无文字状态时按 成交/委托 数量推断（已报/全部成交/部分成交）
            if not status and order_qty is not None and filled_qty is not None:
                if filled_qty <= 0:
                    status = "已报"
                elif filled_qty >= order_qty:
                    status = "全部成交"
                else:
                    status = "部分成交"
            numbers = re.findall(r"\d+\.?\d*", line)
            orders.append({
                "code": code, "name": name, "action": action, "status": status,
                "order_qty": order_qty, "filled_qty": filled_qty,
                "order_price": order_price, "numbers": numbers, "raw": line,
            })
        return orders

    @_gui_serialized
    def query_funds(self) -> dict:
        """尽力读取 xiadan「可用资金/资金余额」，方便下单前知道还能买多少。

        ⚠️ 限制（与 query_today_orders 同源）：
            xiadan 的资金/持仓表是自绘控件 CVirtualGridCtrl，pywinauto 读不到。
            但买入页(F1)顶部的「可用金额/资金余额」通常是普通 Static/Edit 控件，
            本方法切到 F1 → 遍历这些控件 → 正则解析金额。读不到则回退截图，
            并附带抓到的原始文本，便于用户/前端核对、必要时现场校准。

        ⚠️ 只读不下单：仅按 F1 切页 + 读控件文本，全程不按 Enter，不提交任何委托。

        返回:
            成功 {ok:True, available:float, screenshot:str, raw:{texts:[...]}, msg}
            失败 {ok:False, screenshot:str, raw:{texts:[...]}, msg}
        """
        if DRY_RUN:
            return {"ok": False, "msg": "DRY_RUN 模式无 xiadan"}
        import os, re
        if os.name != "nt":
            return {"ok": False, "msg": "非 Windows 系统"}

        import pyautogui
        # 关掉 fail-safe：本操作有限且短（F4→可选点击→截图→F1，无循环），不会失控；
        # 否则用户在那 2-3 秒里鼠标恰好移到屏幕角落就会中断读取（误以为功能坏了）。
        pyautogui.FAILSAFE = False
        if self.main_win is None:
            try:
                self._connect()
            except Exception as e:
                return {"ok": False, "msg": f"xiadan 未连接: {e}"}
        if self.main_win is None:
            return {"ok": False, "msg": "xiadan 窗口不可用"}

        try:
            import win32gui, win32con, win32process
            import ctypes
        except Exception as e:
            return {"ok": False, "msg": f"win32 不可用: {e}"}

        # 激活 xiadan 到前台（与 query_today_orders 同款逻辑）
        hwnd = self.main_win.handle
        if not win32gui.IsWindow(hwnd):
            try: self._connect()
            except Exception: pass
            if self.main_win is None:
                return {"ok": False, "msg": "xiadan 窗口已关闭"}
            hwnd = self.main_win.handle

        user32 = ctypes.windll.user32
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.4)
        try:
            target_tid, _ = win32process.GetWindowThreadProcessId(hwnd)
            cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            if target_tid and target_tid != cur_tid:
                user32.AttachThreadInput(cur_tid, target_tid, True)
                try:
                    win32gui.BringWindowToTop(hwnd)
                    win32gui.SetForegroundWindow(hwnd)
                finally:
                    user32.AttachThreadInput(cur_tid, target_tid, False)
            time.sleep(0.2)
        except Exception as _e:
            logger.debug(f"激活异常: {_e}")

        # 鼠标点击标题栏兜底激活（只点标题区，不点按钮，避免误触下单）
        if win32gui.GetForegroundWindow() != hwnd:
            try:
                rect = win32gui.GetWindowRect(hwnd)
                if rect[0] > -1000:
                    cx, cy = (rect[0] + rect[2]) // 2, rect[1] + 15
                    op = pyautogui.position()
                    pyautogui.click(cx, cy, duration=0.05)
                    time.sleep(0.2)
                    pyautogui.moveTo(op[0], op[1], duration=0.05)
            except Exception:
                pass

        if win32gui.GetForegroundWindow() != hwnd:
            return {"ok": False, "msg": "xiadan 未能激活到前台"}

        try:
            # 切到买入页 F1（可用资金标签通常在此页顶部）
            pyautogui.press(THS_BUY_KEY)
            time.sleep(0.8)

            # 遍历可见 Static/Edit 控件文本
            texts = []
            try:
                for ctl in self.main_win.descendants():
                    try:
                        cls = ctl.class_name() if hasattr(ctl, "class_name") else ""
                        if cls and ("Static" in cls or "Edit" in cls) and ctl.is_visible():
                            t = (ctl.window_text() or "").strip()
                            if t:
                                texts.append(t)
                    except Exception:
                        continue
            except Exception as _e:
                logger.debug(f"遍历资金控件失败: {_e}")

            def _num(s: str):
                m = re.search(r"(\d[\d,]*\.?\d*)", (s or "").replace(",", ""))
                try:
                    return float(m.group(1)) if m else None
                except Exception:
                    return None

            available = None
            joined = " ".join(texts)
            # 形如「可用金额 12345.67」「可用资金：12345.67」「资金余额 12345.67」
            m = re.search(r"(可用金额|可用资金|资金余额|可用余额|可用)[^\d\-]{0,6}(\d[\d,]*\.?\d*)", joined)
            if m:
                available = _num(m.group(2))
            else:
                # 退化：关键字所在控件本身含数字，或紧邻下一个控件是数字
                for i, t in enumerate(texts):
                    if any(k in t for k in ("可用金额", "可用资金", "资金余额", "可用余额")):
                        v = _num(t)
                        if v is not None and v > 0:
                            available = v; break
                        if i + 1 < len(texts):
                            v = _num(texts[i + 1])
                            if v is not None:
                                available = v; break

            # 截图（无论成败都给图，便于核对/校准）
            from pathlib import Path
            from datetime import datetime
            proj_root = Path(__file__).resolve().parent.parent
            scr_dir = proj_root / "logs" / "screenshots"
            scr_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fn = scr_dir / f"funds_{ts}.png"
            rel_path = ""
            try:
                if self.main_win is not None:
                    self.main_win.capture_as_image().save(str(fn))
                else:
                    pyautogui.screenshot(str(fn))
                rel_path = str(fn.relative_to(proj_root)).replace("\\", "/")
            except Exception:
                pass

            # 控件没解析到 → OCR 兜底（Windows 自带 OCR，需 pip install winocr）
            source   = "control" if available is not None else ""
            ocr_text = ""
            if available is None:
                ocr_text = self._ocr_text(fn)
                if ocr_text:
                    m2 = re.search(r"(可用金额|可用资金|资金余额|可用余额|可用)[^\d\-]{0,6}(\d[\d,]*\.?\d*)", ocr_text)
                    if m2:
                        available = _num(m2.group(2))
                        source = "ocr"

            if available is not None and available >= 0:
                return {
                    "ok": True, "available": round(available, 2), "source": source,
                    "screenshot": rel_path, "raw": {"texts": texts[:30]},
                    "msg": f"已读取 xiadan 可用资金 ¥{available:,.2f}（来源:{source}，请对照截图核对）",
                }
            hint = ("未能解析可用资金：控件读不到（自绘控件）"
                    + ("，OCR 也未识别出金额；" if ocr_text
                       else "，且 OCR 不可用（请 pip install winocr 启用）；")
                    + "已截图，请打开图片查看；可把截图中资金区文字发我以校准正则。")
            return {
                "ok": False, "source": "none", "screenshot": rel_path,
                "raw": {"texts": texts[:30], "ocr": (ocr_text[:300] if ocr_text else "")},
                "msg": hint,
            }
        except Exception as e:
            logger.exception(f"query_funds 异常: {e}")
            return {"ok": False, "msg": f"调用异常: {e}"}

    def _activate_main_win(self):
        """激活 xiadan 主窗口到前台。返回 (ok, hwnd, msg)。供撤单等需前台的操作复用。
        含：连接兜底、还原最小化、AttachThreadInput 置前、标题栏点击兜底、尺寸守卫、关 fail-safe。"""
        import pyautogui
        pyautogui.FAILSAFE = False
        if self.main_win is None:
            try:
                self._connect()
            except Exception as e:
                return False, None, f"xiadan 未连接: {e}"
        if self.main_win is None:
            return False, None, "xiadan 窗口不可用"
        try:
            import win32gui, win32con, win32process, ctypes
        except Exception as e:
            return False, None, f"win32 不可用: {e}"
        hwnd = self.main_win.handle
        if not win32gui.IsWindow(hwnd):
            try:
                self._connect()
            except Exception:
                pass
            if self.main_win is None:
                return False, None, "xiadan 窗口已关闭"
            hwnd = self.main_win.handle
        user32 = ctypes.windll.user32
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.4)
        try:
            target_tid, _ = win32process.GetWindowThreadProcessId(hwnd)
            cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            if target_tid and target_tid != cur_tid:
                user32.AttachThreadInput(cur_tid, target_tid, True)
                try:
                    win32gui.BringWindowToTop(hwnd)
                    win32gui.SetForegroundWindow(hwnd)
                finally:
                    user32.AttachThreadInput(cur_tid, target_tid, False)
            time.sleep(0.2)
        except Exception as _e:
            logger.debug(f"激活异常: {_e}")
        if win32gui.GetForegroundWindow() != hwnd:
            try:
                rect = win32gui.GetWindowRect(hwnd)
                if rect[0] > -1000:
                    cx, cy = (rect[0] + rect[2]) // 2, rect[1] + 15
                    op = pyautogui.position()
                    pyautogui.click(cx, cy, duration=0.05)
                    time.sleep(0.2)
                    pyautogui.moveTo(op[0], op[1], duration=0.05)
            except Exception:
                pass
        if win32gui.GetForegroundWindow() != hwnd:
            return False, hwnd, "xiadan 未能激活到前台"
        try:
            _l, _t, _r, _b = win32gui.GetWindowRect(hwnd)
            if (_r - _l) < 600 or (_b - _t) < 400:
                return False, hwnd, f"窗口过小({_r-_l}x{_b-_t})，疑似迷你/最小化，请还原完整窗口"
        except Exception:
            pass
        return True, hwnd, "ok"

    def _confirm_cancel_dialog(self, pyautogui) -> bool:
        """撤单后确认弹窗（#32770，"您确认要撤销这N笔委托吗? 是(&Y)/否(&N)"）。

        修复：旧版盲按 y+Enter —— 弹窗未聚焦时按键落到别的窗口，撤单永远卡在
        确认框上（日志里"您确认要撤销这(2)笔委托吗"原样回传就是这样来的）。
        现在：①优先直接点弹窗里的「是/确定」按钮（BM_CLICK 消息，无需焦点）；
              ②找不到按钮再 set_focus 后按 y+Enter 兜底；
              ③弹窗可能慢出，轮询最多 4 次（约 2 秒）。"""
        try:
            from pywinauto import Desktop
            for _try in range(4):
                dialogs = Desktop(backend="win32").windows(class_name="#32770", visible_only=True)
                for d in dialogs:
                    try:
                        t = d.window_text() or ""
                        body = " ".join((c.window_text() or "") for c in d.descendants())
                    except Exception:
                        continue
                    if not any(k in (t + body) for k in ("撤单", "撤销", "撤回", "确认", "是否")):
                        continue
                    clicked = False
                    try:
                        for c in d.descendants(class_name="Button"):
                            bt = (c.window_text() or "").replace("&", "").strip()
                            if bt in ("是", "是(Y)", "确定", "确认", "Yes", "OK") or bt.startswith("是"):
                                try:
                                    c.click()          # 发 BM_CLICK 消息，无需窗口聚焦
                                except Exception:
                                    c.click_input()    # 降级：真实鼠标点击
                                clicked = True
                                break
                    except Exception:
                        pass
                    if not clicked:
                        try:
                            d.set_focus()
                            time.sleep(0.15)
                        except Exception:
                            pass
                        pyautogui.press("y")
                        time.sleep(0.2)
                        pyautogui.press("enter")
                    logger.info(f"已确认撤单对话框: {t}（按钮点击={clicked}）")
                    time.sleep(0.4)
                    return True
                time.sleep(0.5)    # 弹窗未出现，等一会再查
        except Exception as e:
            logger.debug(f"撤单确认框处理异常: {e}")
        return False

    @staticmethod
    def _on_cancel_page(words) -> bool:
        """OCR 词里是否含撤单页专属标记，用于确认 F3 已真正切到撤单页（而非停在买入页）。

        撤单页顶部工具栏有「全撤/撤最后/撤相同/撤买/撤卖/撤单不需要确认」，买入页没有，
        据此判别 F3 是否生效，避免在错误页面上误操作。
        """
        import re
        txt = re.sub(r"\s+", "", " ".join((w.get("text", "") if isinstance(w, dict) else "")
                                          for w in (words or [])))
        return any(k in txt for k in ("撤单不需要确认", "撤最后", "撤相同", "全撤", "撤买", "撤卖"))

    @_gui_serialized
    def cancel_order(self, code: str, contract_no=None, dry_run: bool = True) -> dict:
        """精确撤单：进撤单页(F3) → OCR 定位目标可撤行 → 撤掉 → 校验只撤了那一笔。

        撤法（按可靠性自动选）：
          - 全场仅 1 笔可撤 → 全撤(Z)，纯热键最稳；
          - 多笔但我方代码只此 1 笔（其余是别的代码=多信号并发）→ 选中该行 → 撤单(Del)，
            按代码唯一定位，精确撤这一笔、不动别的；
          - 同一代码有多笔可撤 → 必须用 contract_no 精确认定那一行，否则不撤、报警。
        安全：撤单页只列「可撤委托」（已成/已撤不在其中）；撤后重读校验「目标消失 +
              其余仍在」，若可撤数骤降>1 判过撤、返回 over_cancel 报警；dry_run=True
              只定位不撤；撤完刷新资金缓存；finally 切回 F1。
        contract_no：同代码多笔时用于精确认定那一单（从当日委托抓到的合同编号）。
        返回 {ok, action:'cancelled'|'locate'|'none'|'abort'|'over_cancel'|'cancel_failed',
              total_cancellable, mine, after?, msg, screenshot}
        """
        if DRY_RUN:
            return {"ok": False, "msg": "DRY_RUN 模式无 xiadan"}
        import os, re
        if os.name != "nt":
            return {"ok": False, "msg": "非 Windows 系统"}
        import pyautogui, win32gui

        ok, hwnd, amsg = self._activate_main_win()
        if not ok:
            return {"ok": False, "msg": f"撤单前激活失败: {amsg}"}

        from pathlib import Path
        from datetime import datetime
        proj_root = Path(__file__).resolve().parent.parent
        scr_dir = proj_root / "logs" / "screenshots"
        try:
            # ── 进撤单页并校验（F3 可能因焦点未生效；最多重试 3 次，验证页面专属标记）──
            rel, words, on_page = None, None, False
            scr_dir.mkdir(parents=True, exist_ok=True)
            for attempt in range(3):
                pyautogui.press("f3")
                time.sleep(1.0)
                img = self.main_win.capture_as_image()
                fn = scr_dir / f"cancel_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{attempt}.png"
                img.save(str(fn))
                rel = str(fn.relative_to(proj_root)).replace("\\", "/")
                words = self._ocr_grid_words(str(fn))
                if self._on_cancel_page(words):
                    on_page = True
                    break
                logger.warning(f"F3 未进撤单页(第{attempt+1}次)，重试")
                time.sleep(0.5)
            if not on_page:
                return {"ok": False, "action": "abort", "screenshot": rel,
                        "msg": "未能进入撤单页(F3 无效/焦点异常)，为安全未撤"}

            rows = self._cluster_rows(words)
            # 列头行 Y：只认列头下方的网格行（排除顶部行情图里的 000001 等干扰）
            header_y = None
            for r in rows:
                tt = re.sub(r"\s+", "", r["text"])
                if "证券代码" in tt or "委托时间" in tt:
                    header_y = r["y"]
                    break
            grid = [r for r in rows if (header_y is None or r["y"] > header_y + 5)]

            # 可撤单行 = 网格里含合法股票代码的行（排除汇总/资金行）
            cancellable = []
            for r in grid:
                line = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", r["text"])
                if re.search(r"汇总|合计|资产|可用|新股申购|帮助", line):
                    continue
                rc = self._pick_stock_code(line)
                if not rc:
                    continue
                cnos = [n for n in re.findall(r"\d{5,8}", line) if n != rc]
                cancellable.append({"code": rc, "contract": (cnos[-1] if cnos else None),
                                    "y": r["y"], "line": line, "row": r})

            def _match(c):
                if c["code"] != code:
                    return False
                if not contract_no:
                    return True
                cc, cn = (c["contract"] or ""), str(contract_no)
                return bool(cc) and (cc == cn or cn.startswith(cc) or cc.startswith(cn[:6]))
            mine = [c for c in cancellable if _match(c)]
            info = {"total_cancellable": len(cancellable),
                    "mine": [{"code": c["code"], "contract": c["contract"]} for c in mine]}

            if not mine:
                return {"ok": True, "action": "none", "screenshot": rel,
                        "msg": f"撤单页无 {code}(合同{contract_no}) 的可撤单（可能已成交/已撤）", **info}

            if dry_run:
                return {"ok": True, "action": "locate", "screenshot": rel,
                        "msg": f"[dry_run] 命中我方可撤单 {len(mine)} 笔；撤单页共 {len(cancellable)} 笔可撤", **info}

            # ── 执行撤单 ──
            # 方法选择：
            #   全场唯一可撤 → 全撤(Z)，纯热键最稳；
            #   我方代码只此一笔(其余是别的代码) → 选中该行 → 撤单(Del)（多笔并发精确撤）；
            #   同代码多笔 → 靠合同编号唯一确定那一行；确定不了 → 不撤报警。
            target, method = None, None
            if len(cancellable) == 1 and len(mine) == 1:
                method = "全撤"
            elif len(mine) == 1:
                target, method = mine[0], "选行"
            else:
                if contract_no:
                    cn = str(contract_no)
                    for c in mine:
                        cc = c["contract"] or ""
                        if cc and (cc == cn or cn.startswith(cc) or cc.startswith(cn[:6])):
                            target, method = c, "选行"
                            break
                if target is None:
                    return {"ok": False, "action": "abort", "screenshot": rel,
                            "msg": f"{code} 有 {len(mine)} 笔同代码可撤单、合同编号无法唯一确定({contract_no})，为安全未撤",
                            **info}

            before_codes = [c["code"] for c in cancellable]
            tgt_contract = (target["contract"] if target else
                            (mine[0]["contract"] if mine else None))

            if method == "全撤":
                pyautogui.press("z")                 # 全撤(Z)
                time.sleep(0.6)
                self._confirm_cancel_dialog(pyautogui)
                time.sleep(1.0)
            else:
                # 选中目标行（点 证券代码 单元格，稳选中、不碰最左勾选框）→ Del → 确认
                rect = win32gui.GetWindowRect(hwnd)
                iw = img.width or (rect[2] - rect[0])
                ih = img.height or (rect[3] - rect[1])
                sx = (rect[2] - rect[0]) / iw
                sy = (rect[3] - rect[1]) / ih
                rw = target["row"]["words"]
                cwd = next((w for w in rw if re.fullmatch(r"\d{6}", w["text"]) and w["text"] == code), None)
                x_px = cwd["x"] if cwd else (min(w["x"] for w in rw) + 80)
                cx = min(max(int(rect[0] + (x_px + 10) * sx), rect[0] + 2), rect[2] - 2)
                cy = min(max(int(rect[1] + target["y"] * sy), rect[1] + 2), rect[3] - 2)
                op = pyautogui.position()
                pyautogui.click(cx, cy, duration=0.05)       # 选中目标行
                time.sleep(0.3)
                pyautogui.press("delete")                    # 撤单(Del) 撤选中行
                time.sleep(0.6)
                self._confirm_cancel_dialog(pyautogui)
                time.sleep(1.0)
                try:
                    pyautogui.moveTo(op[0], op[1], duration=0.05)
                except Exception:
                    pass

            # ── 校验：重截撤单页，确认「只撤了目标这一笔、其余仍在」（防过撤）──
            after = []
            try:
                time.sleep(0.4)
                fn2 = scr_dir / f"cancel_{datetime.now().strftime('%Y%m%d_%H%M%S')}_chk.png"
                self.main_win.capture_as_image().save(str(fn2))
                w2 = self._cluster_rows(self._ocr_grid_words(str(fn2)))
                hy2 = None
                for r in w2:
                    if re.search(r"证券代码|委托时间", re.sub(r"\s+", "", r["text"])):
                        hy2 = r["y"]
                        break
                for r in w2:
                    if hy2 is not None and r["y"] <= hy2 + 5:
                        continue
                    line2 = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", r["text"])
                    if re.search(r"汇总|合计|资产|可用|新股申购|帮助", line2):
                        continue
                    rc2 = self._pick_stock_code(line2)
                    if rc2:
                        cnos2 = [n for n in re.findall(r"\d{5,8}", line2) if n != rc2]
                        after.append({"code": rc2, "contract": (cnos2[-1] if cnos2 else None)})
            except Exception:
                pass
            try:
                self._read_toolbar_funds()                     # 撤后刷新资金缓存
            except Exception:
                pass

            after_codes = [c["code"] for c in after]
            if tgt_contract:
                target_gone = not any(
                    (c["contract"] or "") and (
                        c["contract"] == str(tgt_contract)
                        or str(tgt_contract).startswith(c["contract"])
                        or (c["contract"] or "").startswith(str(tgt_contract)[:6]))
                    for c in after)
            else:
                target_gone = before_codes.count(code) > after_codes.count(code)
            over_cancel = len(after) < len(cancellable) - 1    # 撤掉数 > 1 → 过撤！

            if over_cancel:
                logger.error(f"[撤单] 过撤警告：撤前 {len(cancellable)} 笔 → 撤后 {len(after)} 笔")
                return {"ok": False, "action": "over_cancel", "screenshot": rel, "after": after,
                        "msg": f"⚠️ 可撤数从 {len(cancellable)} 骤降到 {len(after)}，疑似误撤多笔，请立即人工核对！",
                        **info}
            if target_gone:
                return {"ok": True, "action": "cancelled", "screenshot": rel,
                        "msg": (f"已撤 {code}(合同{tgt_contract}) [{method}]，"
                                f"确认目标消失、其余 {len(after)} 笔仍在"), **info}
            return {"ok": False, "action": "cancel_failed", "screenshot": rel, "after": after,
                    "msg": f"已发撤单但 {code} 仍在撤单页，可能未生效，请手动核对", **info}
        except Exception as e:
            logger.exception(f"cancel_order 异常: {e}")
            return {"ok": False, "msg": f"撤单异常: {e}"}
        finally:
            try:
                time.sleep(0.3)
                pyautogui.press(THS_BUY_KEY)
                time.sleep(0.3)
            except Exception:
                pass

    @_gui_serialized
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
        r = self._gui_submit("买入", THS_BUY_KEY, code, price, amount)
        self._after_trade_refresh_funds(r)   # 下单成功→顺手刷新资金缓存（事件驱动）
        self._start_order_monitor(code, "买入", r)   # 启动 N 秒未成交自动撤单监控（开关控制）
        return r

    @_gui_serialized
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
        r = self._gui_submit("卖出", THS_SELL_KEY, code, price, amount)
        self._after_trade_refresh_funds(r)   # 下单成功→顺手刷新资金缓存（事件驱动）
        self._start_order_monitor(code, "卖出", r)   # 启动 N 秒未成交自动撤单监控（开关控制）
        return r

    def _after_trade_refresh_funds(self, result: dict):
        """下单成功后顺手刷新资金缓存。

        事件驱动核心：只有订单确实提交(submitted/executed)、资金已变时才刷；失败单
        不动资金也不刷。窗口此刻已在前台（刚下完单），故只截图 OCR 顶部工具栏即可拿到
        资产/可用，无需再切 F4、不额外弹窗、与下单同线程（不会和下单争用 GUI）。
        全程 try 兜住——刷新失败绝不影响下单结果。
        """
        try:
            if not isinstance(result, dict):
                return
            if result.get("status") not in ("submitted", "executed"):
                return
            time.sleep(0.6)   # 给 xiadan 一点时间更新顶部「可用」（下单冻结资金）
            self._read_toolbar_funds()
        except Exception as e:
            logger.debug(f"下单后刷新资金缓存失败（不影响下单）: {e}")

    @_gui_serialized
    def _read_toolbar_funds(self) -> dict:
        """轻量读顶部工具栏「资产/可用」→ 更新 self._funds_cache，返回 {available,total_asset}。

        要求窗口已在前台（下单后即满足）。只截当前页 + OCR 顶部一条，不切 F4。
        供 _after_trade_refresh_funds 调用；也可被强制刷新路径复用。
        """
        import time as _t
        from pathlib import Path
        from datetime import datetime
        if self.main_win is None:
            return {}
        img = self.main_win.capture_as_image()
        proj_root = Path(__file__).resolve().parent.parent
        scr_dir = proj_root / "logs" / "screenshots"
        scr_dir.mkdir(parents=True, exist_ok=True)
        tmp = scr_dir / f"_fundstmp_{datetime.now().strftime('%H%M%S_%f')}.png"
        try:
            img.save(str(tmp))
            words = self._ocr_grid_words(tmp)
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass
        funds = self._parse_funds_from_words(words)
        if funds.get("available") is not None or funds.get("total_asset") is not None:
            self._funds_cache = {"ts": _t.time(), "data": {
                "ok": True, "mode": "real",
                "available":   funds.get("available"),
                "total_asset": funds.get("total_asset"),
            }}
            logger.info(f"资金缓存已更新: 可用={funds.get('available')} 总资产={funds.get('total_asset')}")
        return funds

    def _start_order_monitor(self, code: str, action: str, result: dict):
        """下单成功后启动后台监控线程：等 AUTO_CANCEL_DELAY 秒，未成交则自动撤单+刷新资金。

        仅当 self.auto_cancel_unfilled=True（运行时开关）且订单确实提交时启动。
        后台线程跑，不阻塞下单返回；GUI 操作经 _gui_lock 串行，不会和别的下单/查询撞车。
        """
        if not self.auto_cancel_unfilled:
            return
        if not isinstance(result, dict) or result.get("status") not in ("submitted", "executed"):
            return
        try:
            t = threading.Thread(target=self._order_monitor_worker,
                                 args=(code, action), daemon=True)
            t.start()
            logger.info(f"[监控] 已启动 {code} {action} 的 {self.auto_cancel_delay}s 未成交自动撤单监控")
        except Exception as e:
            logger.warning(f"[监控] 启动 {code} 监控线程失败（不影响下单）: {e}")

    def _order_monitor_worker(self, code: str, action: str):
        """等 AUTO_CANCEL_DELAY 秒后查撤单页：该 code 若仍可撤(未成交/部成)→撤掉+刷新资金。

        撤单页只列「可撤委托」：该 code 在列=未成交→撤；不在列=已成交/已撤→无需撤。
        撤单走 cancel_order（仅当全场唯一可撤单时用全撤Z，多笔为安全跳过并记日志）。
        全程 try 兜住，绝不影响别的流程。
        """
        try:
            time.sleep(self.auto_cancel_delay)
            r = self.cancel_order(code, dry_run=False)
            act = (r or {}).get("action")
            msg = (r or {}).get("msg", "")
            # 已撤(cancelled)路径 cancel_order 内部已刷新资金；其余情况(成交/中止)这里补刷一次，
            # 保证「成交导致的资金变化」也及时进缓存（如卖单成交→可用回升）。
            if act != "cancelled":
                try:
                    self._read_toolbar_funds()
                except Exception:
                    pass
            if act == "cancelled":
                logger.info(f"[监控] {code} {action} 超时未成交 → 已自动撤单，资金已刷新")
            elif act == "none":
                logger.info(f"[监控] {code} {action} 不在可撤列表（已成交 或 未成功提交，二者皆无单可撤）"
                            f" → 无需撤单（已刷新资金）。如需确认是否成交请查「当日成交」")
            elif act == "abort":
                logger.warning(f"[监控] {code} {action} 撤单页有多笔可撤、为安全未自动撤：{msg}")
            else:
                logger.warning(f"[监控] {code} {action} 自动撤单未确认成功：{msg}")
        except Exception as e:
            logger.exception(f"[监控] {code} 自动撤单线程异常: {e}")
