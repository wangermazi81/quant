# -*- coding: utf-8 -*-

import json
import re
import time
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET


class MobileTHSOperator:
    """Android 同花顺执行器：uiautomator2 控屏，元素定位优先，坐标兜底。"""

    def __init__(self, profile_path, api, device=None):
        self.profile_path = Path(profile_path)
        self.profile = json.loads(self.profile_path.read_text(encoding="utf-8-sig"))
        self.api = api
        self.device = device
        self.device_serial = str(self.profile.get("device_serial", ""))

    def connect(self):
        if self.device is None:
            try:
                import uiautomator2 as u2
                self.device = u2.connect(self.device_serial or None)
            except Exception as exc:
                return {"ok": False, "msg": f"设备连接失败: {exc}"}
        try:
            _ = self.device.info
        except Exception as exc:
            return {"ok": False, "msg": f"设备未授权或离线: {exc}"}

        self._wake_device()
        self._dismiss_system_blocker()
        expected_package = str(self.profile.get("app_package", ""))
        current = self._ensure_trading_app_foreground(expected_package)
        if isinstance(current, dict) and not current.get("ok", True):
            return current
        serial = str(getattr(self.device, "serial", "") or getattr(self, "device_serial", ""))
        self._dismiss_system_blocker()
        self._dismiss_optional_dialogs()
        return {
            "ok": True,
            "device_serial": serial,
            "package": current.get("package", ""),
            "activity": current.get("activity", ""),
        }

    def open_app(self):
        return self.connect()


    def _wake_device(self):
        try:
            info = self.device.info or {}
            screen_on = info.get("screenOn")
        except Exception:
            screen_on = None
        self._screen_was_off = screen_on is False
        shell = getattr(self.device, "shell", None)
        if not callable(shell):
            return
        if screen_on is False:
            try:
                shell(["input", "keyevent", "WAKEUP"])
                time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
            except Exception:
                pass
        try:
            shell(["wm", "dismiss-keyguard"])
        except Exception:
            pass
    def _ensure_trading_app_foreground(self, expected_package):
        current = self.device.app_current() or {}
        if not expected_package or self._package_matches(current.get("package"), expected_package):
            return current
        if not self.profile.get("auto_launch_app", True):
            return {
                "ok": False,
                "msg": f"交易 APP 不在前台: current={current.get('package', '')} expected={expected_package}",
            }

        wait = float(self.profile.get("launch_wait_sec", 2.0))
        last_error = ""
        self._grant_runtime_permissions(expected_package)
        for _ in range(int(self.profile.get("launch_retry", 3))):
            self._dismiss_system_blocker()
            try:
                self.device.app_start(expected_package)
            except Exception as exc:
                last_error = str(exc)
                try:
                    self.device.shell(f"monkey -p {expected_package} -c android.intent.category.LAUNCHER 1")
                except Exception as shell_exc:
                    last_error = f"{last_error}; {shell_exc}"
            time.sleep(max(wait, 0.5))
            self._dismiss_system_blocker()
            current = self.device.app_current() or {}
            if self._package_matches(current.get("package"), expected_package):
                return current

        msg = f"交易 APP 不在前台: current={current.get('package', '')} expected={expected_package}"
        if last_error:
            msg = f"{msg}; launch_error={last_error}"
        return {"ok": False, "msg": msg}

    def _grant_runtime_permissions(self, package):
        permissions = self.profile.get("auto_grant_permissions") or [
            "android.permission.CAMERA",
            "android.permission.READ_EXTERNAL_STORAGE",
            "android.permission.WRITE_EXTERNAL_STORAGE",
            "android.permission.READ_MEDIA_IMAGES",
            "android.permission.READ_MEDIA_VIDEO",
            "android.permission.ACCESS_FINE_LOCATION",
            "android.permission.ACCESS_COARSE_LOCATION",
        ]
        for permission in permissions:
            try:
                self.device.shell(["pm", "grant", str(package), str(permission)])
            except Exception:
                pass

    def _dismiss_system_blocker(self):
        root = None
        xml_packages = set()
        try:
            current = self.device.app_current() or {}
        except Exception:
            current = {}
        package = str(current.get("package", ""))
        blockers = {"com.android.packageinstaller", "com.google.android.permissioncontroller"}
        if package not in blockers and getattr(self, "_screen_was_off", False):
            try:
                root = self._dump_xml_root()
                xml_packages = {
                    str(node.attrib.get("package", ""))
                    for node in root.iter("node")
                    if node.attrib.get("package")
                }
            except Exception:
                xml_packages = set()
        if package not in blockers and not (xml_packages & blockers):
            return
        if package == "com.android.packageinstaller" or "com.android.packageinstaller" in xml_packages:
            try:
                self.device.press("back")
                time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
                return
            except Exception:
                pass
        patterns = [
            r".*\u59cb\u7ec8\u5141\u8bb8.*",
            r".*\u4ec5\u5728\u4f7f\u7528\u4e2d\u5141\u8bb8.*",
            r".*\u5141\u8bb8.*",
            r".*\u786e\u5b9a.*",
            r"(?i).*allow.*",
            r"(?i).*ok.*",
        ]
        for pattern in patterns:
            try:
                target = self.device(textMatches=pattern)
                exists = getattr(target, "exists", False)
                if bool(exists() if callable(exists) else exists):
                    target.click()
                    time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
                    return
            except Exception:
                pass
        if root is not None:
            for item in self._text_items(root):
                if item["text"] in {"Allow", "OK", "\u786e\u5b9a", "\u5141\u8bb8", "\u59cb\u7ec8\u5141\u8bb8", "\u4ec5\u5728\u4f7f\u7528\u4e2d\u5141\u8bb8"}:
                    try:
                        self.device.click(item["x"], item["y"])
                        time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
                        return
                    except Exception:
                        pass
        try:
            self.device.press("back")
            time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
        except Exception:
            pass

    @staticmethod
    def _package_matches(current_package, expected_package):
        current = str(current_package or "")
        expected = str(expected_package or "")
        return bool(expected and (current == expected or expected in current))

    @staticmethod
    def _normalize_trade_mode(mode):
        value = str(mode or "").strip().lower()
        if value in {"live", "real", "a", "a股", "实盘"}:
            return "live"
        if value in {"simulation", "sim", "mock", "paper", "模拟", "模拟盘"}:
            return "simulation"
        return value

    def _joined_page_text(self):
        root = self._dump_xml_root()
        texts = [self._node_text(node) for node in root.iter("node")]
        joined = " ".join(text for text in texts if text)
        self._last_joined_page_text = joined
        return joined

    def _infer_trade_mode(self):
        try:
            joined = self._joined_page_text()
        except Exception:
            return "unknown"

        live_markers = ("国盛证券", "资金帐户", "资金账户", "添加账户", "账户管理", "证券开户")
        if any(marker in joined for marker in live_markers):
            return "live"

        simulation_markers = ("模拟炒股", "模拟盘", "模拟练习区", "模拟看板")
        simulation_dashboard_markers = (
            "总收益率",
            "当日参考盈亏",
            "模拟收益",
            "收益率排名",
            "排行榜",
            "牛人",
        )
        trade_actions = ("买 入", "卖 出", "买入", "卖出", "撤单", "持仓", "查询")
        if any(marker in joined for marker in simulation_markers):
            return "simulation"
        if (
            "模拟" in joined
            and any(marker in joined for marker in simulation_dashboard_markers)
            and any(action in joined for action in trade_actions)
        ):
            return "simulation"

        if any(action in joined for action in ("买 入", "卖 出", "买入", "卖出")):
            return "live"
        return "unknown"

    def _mode_switch_surface_visible(self):
        try:
            joined = self._joined_page_text()
        except Exception:
            return False
        return "A股" in joined and "模拟" in joined and ("基金" in joined or "期货" in joined or "港美股" in joined)

    def _trade_login_text_visible(self, joined):
        return "请输入交易密码" in str(joined or "") or "保持在线24小时" in str(joined or "")

    def _trade_login_visible(self):
        if self._trade_login_text_visible(getattr(self, "_last_joined_page_text", "")):
            return True
        locator = {"resource_id": "com.hexin.plat.android:id/weituo_edit_trade_password"}
        if self._exists_or_unknown(locator) is True:
            return True
        try:
            joined = self._joined_page_text()
        except Exception:
            return False
        return self._trade_login_text_visible(joined)

    def _dismiss_trade_login_for_simulation(self):
        if not self._trade_login_visible():
            return False
        try:
            self.device.press("back")
            time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
            self._dismiss_optional_dialogs()
            return True
        except Exception:
            return False

    def _trade_login_block_msg(self):
        return "手机同花顺停在券商交易登录页：先在手机输入交易密码并勾选保持在线24小时，再重试"

    def _ensure_mode_switch_surface(self):
        if self._mode_switch_surface_visible():
            return True
        wait = float(self.profile.get("ui_wait_sec", 0.35))
        for _ in range(2):
            try:
                self.device.press("back")
            except Exception:
                break
            time.sleep(max(wait, 0.2))
            self._dismiss_optional_dialogs()
            if self._mode_switch_surface_visible():
                return True
        return False

    def _wait_trade_mode(self, expected):
        timeout = float(self.profile.get("mode_switch_wait_sec", self.profile.get("page_marker_wait_sec", 3.0)))
        wait = max(float(self.profile.get("ui_wait_sec", 0.35)), 0.2)
        deadline = time.time() + max(timeout, 0.0)
        last = "unknown"
        while True:
            current = self._infer_trade_mode()
            if current == expected:
                return current
            if current != "unknown":
                last = current
            if time.time() >= deadline:
                return last if last != "unknown" else current
            time.sleep(wait)
    def switch_trade_mode(self, mode):
        normalized = self._normalize_trade_mode(mode)
        tabs = self.profile.get("mode_tabs", {})
        tab = tabs.get(normalized)
        if not tab:
            return {"ok": False, "msg": f"未知交易模式: {mode}", "mode": normalized or "unknown"}
        connected = self.connect()
        if not connected.get("ok"):
            return connected
        try:
            if self.profile.get("trade_tab"):
                self._click(self.profile["trade_tab"])
                time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
                self._dismiss_optional_dialogs()
            current = self._infer_trade_mode()
            if normalized == "simulation" and current == "live" and self._trade_login_visible():
                self._dismiss_trade_login_for_simulation()
                current = self._infer_trade_mode()
            if current == normalized:
                return {"ok": True, "msg": "手机同花顺模式切换完成", "mode": normalized, "current_mode": current}
            self._ensure_mode_switch_surface()
            self._click(tab)
            time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
            current = self._wait_trade_mode(normalized)
            if current not in {"unknown", normalized}:
                return {"ok": False, "msg": f"切换后模式不一致: expected={normalized} actual={current}", "mode": current}
            return {"ok": True, "msg": "手机同花顺模式切换完成", "mode": normalized, "current_mode": current}
        except Exception as exc:
            return {"ok": False, "msg": f"手机同花顺模式切换失败: {exc}", "mode": normalized}
    @staticmethod
    def _element_exists(element):
        exists = getattr(element, "exists", False)
        return bool(exists() if callable(exists) else exists)

    def _resolve(self, locator):
        if not locator:
            return None
        for profile_key, device_key in (("resource_id", "resourceId"), ("text", "text"), ("description", "description")):
            value = locator.get(profile_key)
            if not value:
                continue
            element = self.device(**{device_key: value})
            if self._element_exists(element):
                return element
        if locator.get("xpath"):
            element = self.device.xpath(locator["xpath"])
            if self._element_exists(element):
                return element
        if locator.get("relative"):
            return ("relative", locator["relative"])
        return None

    def _xy(self, relative):
        width, height = self.device.window_size()
        x_ratio, y_ratio = relative
        return int(width * float(x_ratio)), int(height * float(y_ratio))

    def _exists(self, locator):
        target = self._resolve(locator)
        if target is not None and not isinstance(target, tuple):
            return True
        return bool(self._locator_matches_hierarchy(locator))

    def _exists_or_unknown(self, locator):
        target = self._resolve(locator)
        if target is not None and not isinstance(target, tuple):
            return True
        matched = self._locator_matches_hierarchy(locator)
        if matched is None:
            return None
        return bool(matched)

    @staticmethod
    def _xpath_text_fragments(xpath):
        if not xpath:
            return []
        fragments = []
        patterns = (
            r"contains\(@(?:text|content-desc),\s*['\"]([^'\"]+)['\"]\)",
            r"@(?:text|content-desc)\s*=\s*['\"]([^'\"]+)['\"]",
        )
        for pattern in patterns:
            fragments.extend(re.findall(pattern, str(xpath)))
        return fragments

    @staticmethod
    def _resource_tail(resource_id):
        return str(resource_id or "").split("/")[-1]

    def _locator_matches_hierarchy(self, locator):
        if not locator:
            return False
        candidates = []
        for key in ("text", "description"):
            value = locator.get(key)
            if value:
                candidates.append(str(value))
        resource_id = locator.get("resource_id")
        if resource_id:
            candidates.append(self._resource_tail(resource_id))
        candidates.extend(self._xpath_text_fragments(locator.get("xpath")))
        if not candidates:
            return False
        try:
            root = self._dump_xml_root()
        except Exception:
            return None
        for node in root.iter("node"):
            node_values = [
                self._node_text(node),
                node.attrib.get("resource-id", ""),
                self._resource_tail(node.attrib.get("resource-id", "")),
            ]
            for candidate in candidates:
                if any(candidate == value or candidate in value for value in node_values if value):
                    return True
        return False

    def _wait_exists(self, locator, timeout_sec=None):
        deadline = time.time() + float(timeout_sec if timeout_sec is not None else self.profile.get("page_marker_wait_sec", 3.0))
        wait = float(self.profile.get("ui_wait_sec", 0.25))
        allow_unknown = bool(self.profile.get("allow_unverified_input_when_dump_fails", True))
        while True:
            exists = self._exists_or_unknown(locator)
            if exists is True or (exists is None and allow_unknown):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(max(wait, 0.05))

    def _dismiss_optional_dialogs(self):
        for locator in self.profile.get("dismiss_dialogs", []):
            try:
                if self._exists(locator):
                    self._click(locator)
                    time.sleep(float(self.profile.get("ui_wait_sec", 0.25)))
            except Exception:
                pass
    def _click(self, locator):
        target = self._resolve(locator)
        if target is None:
            raise RuntimeError("定位目标不存在")
        if isinstance(target, tuple) and target[0] == "relative":
            self.device.click(*self._xy(target[1]))
            return
        target.click()

    def _adb_keyboard_broadcast(self, action, text=None):
        args = ["am", "broadcast", "-a", action]
        if text is not None:
            args.extend(["--es", "msg", str(text)])
        try:
            self.device.shell(args)
            return True
        except Exception:
            return False

    def _clear_and_type_at_current_focus(self, value):
        text = str(value)
        try:
            self.device.set_fastinput_ime(True)
        except Exception:
            pass
        if self.profile.get("force_adb_keyboard_input", False):
            self._adb_keyboard_broadcast("ADB_CLEAR_TEXT")
            try:
                self.device.clear_text()
            except Exception:
                pass
            self._adb_keyboard_broadcast("ADB_INPUT_TEXT", text)
            try:
                self.device.send_keys(text, clear=True)
                return
            except Exception:
                pass
            safe = text.replace("%", "%25").replace(" ", "%s")
            try:
                self.device.shell(["input", "text", safe])
                return
            except Exception:
                pass
        try:
            self.device.clear_text()
        except Exception:
            self._adb_keyboard_broadcast("ADB_CLEAR_TEXT")
            try:
                for _ in range(32):
                    self.device.press("KEYCODE_DEL")
            except Exception:
                pass
        try:
            self.device.send_keys(text, clear=False)
        except Exception:
            if not self._adb_keyboard_broadcast("ADB_INPUT_TEXT", text):
                safe = text.replace("%", "%25").replace(" ", "%s")
                self.device.shell(["input", "text", safe])

    @staticmethod
    def _text_matches_value(actual, value):
        if actual is None:
            return True
        expected = str(value).strip()
        got = str(actual or "").strip()
        if not expected or not got:
            return False
        if expected.isdigit():
            return expected in re.sub(r"\D", "", got)
        return expected in got

    def _read_target_text(self, target):
        if target is None or isinstance(target, tuple):
            return None
        getter = getattr(target, "get_text", None)
        if callable(getter):
            return str(getter() or "").strip()
        if hasattr(target, "text"):
            return str(getattr(target, "text", "") or "").strip()
        return None

    def _hierarchy_contains_value(self, value):
        try:
            root = self._dump_xml_root()
        except Exception:
            return None
        for node in root.iter("node"):
            text = self._node_text(node)
            if text and self._text_matches_value(text, value):
                return True
        return False

    def _set_text(self, locator, value):
        target = self._resolve(locator)
        if target is None:
            raise RuntimeError("输入框定位失败")
        wait = float(self.profile.get("input_wait_sec", 0.12))
        attempts = max(1, int(self.profile.get("input_retry", 2)))
        last_text = None
        allow_dump_fallback = bool(self.profile.get("allow_unverified_input_when_dump_fails", True))
        for attempt in range(attempts):
            if isinstance(target, tuple) and target[0] == "relative":
                self.device.click(*self._xy(target[1]))
                time.sleep(wait)
                self._clear_and_type_at_current_focus(value)
                time.sleep(wait)
                seen = self._hierarchy_contains_value(value)
                if not self._should_strict_readback() or seen is True or (seen is None and allow_dump_fallback):
                    return
                last_text = "页面未回读到输入值"
                continue
            try:
                clear = getattr(target, "clear_text", None)
                if callable(clear):
                    clear()
                target.set_text(str(value))
            except Exception:
                try:
                    target.click()
                except Exception:
                    pass
                time.sleep(wait)
                self._clear_and_type_at_current_focus(value)
            time.sleep(wait)
            try:
                last_text = self._read_target_text(target)
            except Exception:
                last_text = None
                seen = self._hierarchy_contains_value(value)
                if not self._should_strict_readback() or seen is True or (seen is None and allow_dump_fallback):
                    return
            if self._text_matches_value(last_text, value):
                return
            try:
                target.click()
            except Exception:
                pass
            time.sleep(wait)
        raise RuntimeError(f"输入回读不一致: expected={value} actual={last_text}")

    def _get_text(self, locator):
        target = self._resolve(locator)
        if target is None or isinstance(target, tuple):
            return None
        getter = getattr(target, "get_text", None)
        if callable(getter):
            return str(getter() or "").strip()
        if hasattr(target, "text"):
            return str(getattr(target, "text", "") or "").strip()
        return None

    @staticmethod
    def _number(value):
        if value is None:
            return None
        try:
            return float(str(value).strip().replace(",", ""))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_order_status(status_text="", raw_texts=None):
        raw = " ".join(str(x) for x in (raw_texts or []) if x)
        text = f"{status_text or ''} {raw}"
        if any(word in text for word in ("全部撤单", "已撤", "废单", "拒单", "拒绝", "失败")):
            return "failed"
        if any(word in text for word in ("未成交", "未成", "已报", "待报", "委托", "待撤")):
            return "submitted"
        if any(word in text for word in ("已成", "全部成交", "全成", "成交")):
            return "executed"
        if "部成" in text:
            return "submitted"
        return "unknown"

    @staticmethod
    def _order_status_msg(row):
        status = row.get("status", "unknown")
        status_text = row.get("status_text") or ""
        direction = row.get("direction") or ""
        code = row.get("code") or ""
        name = row.get("name") or ""
        return f"手机委托页已回读到匹配委托: status={status} status_text={status_text} {direction} {code} {name}".strip()



    @staticmethod
    def _integer_or_float(value):
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return int(number) if number.is_integer() else number

    @staticmethod
    def _parse_bounds(bounds):
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", str(bounds or ""))
        if not match:
            return None
        return tuple(int(item) for item in match.groups())

    @classmethod
    def _center_from_bounds(cls, bounds):
        parsed = cls._parse_bounds(bounds)
        if not parsed:
            return None
        left, top, right, bottom = parsed
        return int((left + right) / 2), int((top + bottom) / 2)

    def _dump_xml_root(self):
        xml = self.device.dump_hierarchy()
        return ET.fromstring(xml)

    @staticmethod
    def _node_text(node):
        return str(node.attrib.get("text") or node.attrib.get("content-desc") or "").strip()

    def _text_items(self, root=None):
        root = root or self._dump_xml_root()
        items = []
        for node in root.iter("node"):
            text = self._node_text(node)
            if not text:
                continue
            bounds = node.attrib.get("bounds", "")
            center = self._center_from_bounds(bounds)
            if not center:
                continue
            parsed = self._parse_bounds(bounds)
            if not parsed:
                continue
            left, top, right, bottom = parsed
            items.append({
                "text": text,
                "bounds": bounds,
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "x": center[0],
                "y": center[1],
                "resource_id": node.attrib.get("resource-id", ""),
                "class": node.attrib.get("class", ""),
            })
        return items

    def _click_text(self, keyword, exact=False, root=None):
        matches = []
        for item in self._text_items(root):
            text = item["text"]
            matched = text == keyword if exact else keyword in text
            if matched:
                matches.append(item)
        if not matches:
            return False
        matches.sort(key=lambda item: (item["y"], item["x"]))
        item = matches[0]
        self.device.click(item["x"], item["y"])
        return True

    def _search_overlay_visible(self, items):
        return any(
            item["resource_id"].endswith("stock_search")
            or item["resource_id"].endswith("stockcode_tv")
            or item["text"] in {"最近搜索", "股票代码/简拼"}
            for item in items
        )

    def _dismiss_search_overlay(self):
        for _ in range(2):
            try:
                root = self._dump_xml_root()
                items = self._text_items(root)
            except Exception:
                return
            if not self._search_overlay_visible(items):
                return
            close_items = [
                item for item in items
                if item["resource_id"].endswith("close_btn") or item["text"] == "取消"
            ]
            if close_items:
                close_items.sort(key=lambda item: (item["y"], item["x"]))
                self.device.click(close_items[0]["x"], close_items[0]["y"])
            else:
                self.device.press("back")
            time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))

    def _search_overlay_closed(self):
        try:
            return not self._search_overlay_visible(self._text_items(self._dump_xml_root()))
        except Exception:
            return bool(self.profile.get("allow_unverified_input_when_dump_fails", True))

    def _select_stock_search_result(self, code):
        expected = str(code).zfill(6)
        wait = float(self.profile.get("input_wait_sec", 0.25))
        assume_enter_ok = bool(self.profile.get("assume_enter_selects_stock_result", True))
        for _ in range(max(1, int(self.profile.get("input_retry", 3)))):
            try:
                root = self._dump_xml_root()
                items = self._text_items(root)
            except Exception:
                if assume_enter_ok:
                    try:
                        self.device.press("enter")
                    except Exception:
                        pass
                    time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
                    return True
                return False
            matches = [
                item for item in items
                if item["text"] == expected and item["resource_id"].endswith("stockcode_tv")
            ]
            if not matches:
                matches = [item for item in items if item["text"] == expected]
            if matches:
                matches.sort(key=lambda item: (item["y"], item["x"]))
                item = matches[0]
                width, _height = self.device.window_size()
                for x, y in ((item["x"], item["y"]), (int(width * 0.45), item["y"])):
                    self.device.click(int(x), int(y))
                    time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
                    if self._search_overlay_closed():
                        return True
                continue
            if not self._search_overlay_visible(items):
                return True
            if assume_enter_ok:
                try:
                    self.device.press("enter")
                    time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
                    if self._search_overlay_closed():
                        return True
                except Exception:
                    pass
            time.sleep(wait)
        return False

    def _open_trade_home(self):
        connected = self.connect()
        if not connected.get("ok"):
            return connected
        try:
            if self.profile.get("trade_tab"):
                self._click(self.profile["trade_tab"])
            else:
                self._click_text("\u4ea4\u6613")
            time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
            self._dismiss_optional_dialogs()
            last_items = []
            for _ in range(3):
                items = self._text_items()
                last_items = items
                if any("\u603b\u8d44\u4ea7" in item["text"] for item in items):
                    return {"ok": True}
                if "com.android.packageinstaller" in " ".join(item.get("resource_id", "") for item in items):
                    self._dismiss_system_blocker()
                self.device.press("back")
                time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
                self._dismiss_system_blocker()
                self._dismiss_optional_dialogs()
            sample = " ".join(item["text"] for item in last_items[:12])
            return {"ok": False, "msg": f"\u624b\u673a\u540c\u82b1\u987a\u672a\u8fdb\u5165\u4ea4\u6613\u9996\u9875\uff0c\u5f53\u524d\u9875\u9762: {sample[:160]}", "source": "mobile_ui"}
        except Exception as exc:
            return {"ok": False, "msg": f"\u6253\u5f00\u4ea4\u6613\u9996\u9875\u5931\u8d25: {exc}"}

    def _open_trade_subtab(self, name):
        opened = self._open_trade_home()
        if not opened.get("ok"):
            return opened
        try:
            if name == "\u59d4\u6258":
                self._click_text("\u67e5\u8be2")
                time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
                root = self._dump_xml_root()
                self._click_text("\u5f53\u65e5\u59d4\u6258", root=root) or self._click_text("\u59d4\u6258", root=root)
            else:
                self._click_text(name)
            time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
            self._dismiss_optional_dialogs()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "msg": f"\u6253\u5f00\u4ea4\u6613\u5b50\u9875\u5931\u8d25: {name}: {exc}"}

    @staticmethod
    def _num_from_text(text):
        if text is None:
            return None
        value = str(text).replace(",", "").strip()
        match = re.search(r"[-+]?\d+(?:\.\d+)?", value)
        if not match:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None

    @staticmethod
    def _code_from_text(text):
        match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(text or ""))
        return match.group(1) if match else None

    def _value_by_resource_or_label(self, items, resource_id, label):
        for item in items:
            if resource_id and resource_id != "__no_resource__" and item["resource_id"].endswith(resource_id):
                number = self._num_from_text(item["text"])
                if number is not None:
                    return number
        labels = []
        for item in items:
            text = item["text"]
            if text == label or text.startswith(label) or label in text:
                direct = self._num_from_text(text.replace(label, "", 1))
                if direct is not None:
                    return direct
                labels.append(item)
        if not labels:
            return None
        label_item = labels[0]
        candidates = [
            item for item in items
            if (item["top"] >= label_item["bottom"] or abs(item["y"] - label_item["y"]) <= 70)
            and abs(item["x"] - label_item["x"]) < 260
            and self._num_from_text(item["text"]) is not None
            and item is not label_item
        ]
        candidates.sort(key=lambda item: (item["top"], abs(item["x"] - label_item["x"])))
        return self._num_from_text(candidates[0]["text"]) if candidates else None

    def _extract_rows_by_code(self, items, code_filter=None, max_rows=50):
        rows = []
        seen = set()
        for item in sorted(items, key=lambda node: (node["y"], node["x"])):
            code = self._code_from_text(item["text"])
            if not code or (code_filter and code != str(code_filter).zfill(6)):
                continue
            if (code, item["y"]) in seen:
                continue
            seen.add((code, item["y"]))
            band = [node for node in items if abs(node["y"] - item["y"]) <= 90]
            band.sort(key=lambda node: node["x"])
            texts = [node["text"] for node in band]
            numbers = [self._num_from_text(text) for text in texts]
            numbers = [number for number in numbers if number is not None]
            name = ""
            for text in texts:
                if text != code and not self._code_from_text(text) and re.search(r"[\u4e00-\u9fff]", text):
                    name = text
                    break
            rows.append({
                "code": code,
                "name": name,
                "raw_texts": texts,
                "numbers": numbers,
                "tap_x": item["x"],
                "tap_y": item["y"],
            })
            if max_rows and len(rows) >= max_rows:
                break
        return rows

    @staticmethod
    def _num_in_band(nodes, min_x, max_x):
        for node in sorted(nodes, key=lambda item: item["x"]):
            if min_x <= node["x"] <= max_x:
                number = MobileTHSOperator._num_from_text(node["text"])
                if number is not None:
                    return number
        return None

    def _extract_position_rows_by_layout(self, items, max_rows=50):
        skip_names = {
            "\u6301\u4ed3", "\u6301\u4ed3\u5206\u65f6", "\u6301\u4ed3\u7ba1\u7406",
            "\u5e02\u503c", "\u76c8\u4e8f", "\u6301\u4ed3/\u53ef\u7528", "\u6210\u672c/\u73b0\u4ef7",
        }
        rows = []
        anchors = []
        for item in items:
            text = item["text"].strip()
            if item["x"] > 280 or item["top"] < 1080 or item["top"] > 1900:
                continue
            if text in skip_names or self._num_from_text(text) is not None:
                continue
            if not re.search(r"[\u4e00-\u9fff]", text):
                continue
            anchors.append(item)
        anchors.sort(key=lambda item: item["top"])
        for anchor in anchors:
            row1 = [node for node in items if abs(node["top"] - anchor["top"]) <= 18]
            row2 = [node for node in items if 45 <= node["top"] - anchor["top"] <= 85]
            shares = self._num_in_band(row1, 500, 790)
            market_value = self._num_in_band(row2, 0, 280)
            if shares is None and market_value is None:
                continue
            rows.append({
                "code": "",
                "name": anchor["text"],
                "shares": self._integer_or_float(shares),
                "available_shares": self._integer_or_float(self._num_in_band(row2, 500, 790)),
                "cost_price": self._num_in_band(row1, 760, 1080),
                "current_price": self._num_in_band(row2, 760, 1080),
                "market_value": market_value,
                "profit_loss": self._num_in_band(row1, 250, 530),
                "profit_rate": self._num_in_band(row2, 250, 530),
                "raw_texts": [node["text"] for node in sorted(row1 + row2, key=lambda item: (item["top"], item["x"]))],
                "source": "mobile_ui",
            })
            if max_rows and len(rows) >= max_rows:
                break
        return rows


    @staticmethod
    def _id_suffix(item, suffix):
        return str(item.get("resource_id", "")).endswith(suffix)

    def _extract_order_rows_by_result_ids(self, items, code_filter=None, max_rows=50):
        rows = []
        anchors = [
            item for item in items
            if self._id_suffix(item, "result0") and item["y"] > 300
        ]
        anchors.sort(key=lambda item: item["y"])
        for anchor in anchors:
            band = [node for node in items if anchor["top"] - 20 <= node["top"] <= anchor["top"] + 140]
            band.sort(key=lambda node: (node["top"], node["x"]))
            by_suffix = {}
            for node in band:
                rid = node.get("resource_id", "")
                suffix = rid.rsplit("/", 1)[-1].rsplit(":id/", 1)[-1]
                by_suffix[suffix] = node["text"]
            raw_texts = [node["text"] for node in band]
            code = next((self._code_from_text(text) for text in raw_texts if self._code_from_text(text)), "")
            if code_filter and code and code != str(code_filter).zfill(6):
                continue
            raw = " ".join(raw_texts)
            status_text = by_suffix.get("result7", "")
            status = self._normalize_order_status(status_text, raw_texts)
            rows.append({
                "code": code,
                "name": by_suffix.get("result0", anchor["text"]),
                "time": by_suffix.get("result1", ""),
                "entrust_price": self._num_from_text(by_suffix.get("result2")),
                "filled_price": self._num_from_text(by_suffix.get("result3")),
                "entrust_shares": self._integer_or_float(self._num_from_text(by_suffix.get("first_tv"))),
                "filled_shares": self._integer_or_float(self._num_from_text(by_suffix.get("second_tv"))),
                "direction": by_suffix.get("result6", ""),
                "status": status,
                "status_text": status_text,
                "raw_texts": raw_texts,
                "tap_x": anchor["x"],
                "tap_y": anchor["y"],
            })
            if max_rows and len(rows) >= max_rows:
                break
        return rows


    def query_funds(self):
        opened = self._open_trade_home()
        if not opened.get("ok"):
            return opened
        try:
            items = self._text_items()
            account_text = next((item["text"] for item in items if "\u8d44\u91d1" in item["text"] and "\u5e10\u6237" in item["text"]), "")
            available = self._value_by_resource_or_label(items, "available_value_text", "\u53ef\u7528\u8d44\u91d1")
            if available is None:
                available = self._value_by_resource_or_label(items, "__no_resource__", "\u53ef\u7528")
            total_asset = self._value_by_resource_or_label(items, "capital_value_text", "\u603b\u8d44\u4ea7")
            market_value = self._value_by_resource_or_label(items, "shizhi_value_text", "\u603b\u5e02\u503c")
            profit_loss = self._value_by_resource_or_label(items, "yingkui_value_text", "\u6d6e\u52a8\u76c8\u4e8f")
            if available is None:
                opened = self._open_trade_subtab("\u6301\u4ed3")
                if opened.get("ok"):
                    items = self._text_items()
                    available = self._value_by_resource_or_label(items, "__no_resource__", "\u53ef\u7528")
                    if total_asset is None:
                        total_asset = self._value_by_resource_or_label(items, "__no_resource__", "\u603b\u8d44\u4ea7")
                    if market_value is None:
                        market_value = self._value_by_resource_or_label(items, "__no_resource__", "\u603b\u5e02\u503c")
                    if profit_loss is None:
                        profit_loss = self._value_by_resource_or_label(items, "__no_resource__", "\u6d6e\u52a8\u76c8\u4e8f")
            available_missing = available is None
            if all(value is None for value in (available, total_asset, market_value, profit_loss)) and not account_text:
                sample = " ".join(item["text"] for item in items[:16])
                return {
                    "ok": False,
                    "msg": f"\u624b\u673a\u8d44\u91d1\u9875\u672a\u8bfb\u5230\u8d26\u6237\u5b57\u6bb5\uff0c\u5f53\u524d\u9875\u9762: {sample[:180]}",
                    "available": available,
                    "total_asset": total_asset,
                    "market_value": market_value,
                    "profit_loss": profit_loss,
                    "account": account_text,
                    "available_missing": available_missing,
                    "source": "mobile_ui",
                    "captured_at": time.time(),
                }
            return {
                "ok": True,
                "available": available,
                "total_asset": total_asset,
                "market_value": market_value,
                "profit_loss": profit_loss,
                "account": account_text,
                "available_missing": available_missing,
                "msg": "?????????????" if available_missing else "",
                "source": "mobile_ui",
                "captured_at": time.time(),
            }
        except Exception as exc:
            return {"ok": False, "msg": f"\u624b\u673a\u8d44\u91d1\u56de\u8bfb\u5931\u8d25: {exc}", "source": "mobile_ui"}

    def query_positions(self, max_rows=50, navigate=True):
        if navigate:
            opened = self._open_trade_subtab("\u6301\u4ed3")
            if not opened.get("ok"):
                return opened
        else:
            connected = self.connect()
            if not connected.get("ok"):
                return connected
        try:
            items = self._text_items()
            joined = " ".join(item["text"] for item in items)
            if any(word in joined for word in ("\u6ca1\u6709\u6301\u4ed3", "\u6682\u65e0\u6301\u4ed3", "\u6ca1\u6709\u6570\u636e", "\u6682\u65e0\u6570\u636e")):
                return []
            positions = []
            for row in self._extract_rows_by_code(items, max_rows=max_rows):
                numbers = row.get("numbers", [])
                shares = numbers[0] if numbers else None
                positions.append({
                    "code": row["code"],
                    "name": row.get("name", ""),
                    "shares": self._integer_or_float(shares),
                    "available_shares": self._integer_or_float(numbers[1] if len(numbers) > 1 else None),
                    "cost_price": numbers[2] if len(numbers) > 2 else None,
                    "market_value": numbers[3] if len(numbers) > 3 else None,
                    "raw_texts": row.get("raw_texts", []),
                    "source": "mobile_ui",
                })
            if not positions:
                positions = self._extract_position_rows_by_layout(items, max_rows=max_rows)
            return positions
        except Exception as exc:
            return {"ok": False, "msg": f"\u624b\u673a\u6301\u4ed3\u56de\u8bfb\u5931\u8d25: {exc}", "source": "mobile_ui"}

    def query_today_orders(self, code_filter=None, max_rows=50, navigate=True):
        if navigate:
            opened = self._open_trade_subtab("\u59d4\u6258")
            if not opened.get("ok"):
                return opened
        else:
            connected = self.connect()
            if not connected.get("ok"):
                return connected
        try:
            items = self._text_items()
            joined = " ".join(item["text"] for item in items)
            if any(word in joined for word in ("\u6ca1\u6709\u59d4\u6258", "\u6682\u65e0\u59d4\u6258", "\u6ca1\u6709\u6570\u636e", "\u6682\u65e0\u6570\u636e")):
                return {"ok": True, "rows": [], "source": "mobile_ui", "captured_at": time.time()}
            rows = []
            for row in self._extract_rows_by_code(items, code_filter=code_filter, max_rows=max_rows):
                raw_texts = row.get("raw_texts", [])
                status = self._normalize_order_status("", raw_texts)
                rows.append({
                    "code": row["code"],
                    "name": row.get("name", ""),
                    "status": status,
                    "status_text": "",
                    "raw_texts": raw_texts,
                })
            if not rows:
                rows = self._extract_order_rows_by_result_ids(
                    items,
                    code_filter=code_filter,
                    max_rows=max_rows,
                )
            return {"ok": True, "rows": rows, "source": "mobile_ui", "captured_at": time.time()}
        except Exception as exc:
            return {"ok": False, "msg": f"\u624b\u673a\u59d4\u6258\u56de\u8bfb\u5931\u8d25: {exc}", "source": "mobile_ui"}

    def query_account_snapshot(self, max_rows=50):
        funds = self.query_funds()
        if isinstance(funds, dict) and not funds.get("ok"):
            return funds
        positions = self.query_positions(max_rows=max_rows)
        if isinstance(positions, dict) and not positions.get("ok"):
            return positions
        return {
            "ok": True,
            "available": funds.get("available"),
            "total_asset": funds.get("total_asset"),
            "market_value": funds.get("market_value"),
            "profit_loss": funds.get("profit_loss"),
            "account": funds.get("account", ""),
            "positions": positions,
            "source": "mobile_ui",
            "captured_at": time.time(),
        }

    def get_account(self):
        return self.query_funds()

    def get_positions(self, max_rows=50):
        return self.query_positions(max_rows=max_rows)

    @staticmethod
    def _price_text(price):
        return f"{float(price):.3f}".rstrip("0").rstrip(".")

    def _dump_artifacts(self, action):
        root = Path(self.profile.get("artifact_dir", "logs/mobile_capture/orders"))
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = root / f"{stamp}-{action.lower()}"
        result = {}
        try:
            shot = str(base.with_suffix(".png"))
            self.device.screenshot(shot)
            result["screenshot"] = shot
        except Exception as exc:
            result["screenshot_error"] = str(exc)
        try:
            xml = self.device.dump_hierarchy()
            xml_path = str(base.with_suffix(".xml"))
            Path(xml_path).write_text(xml, encoding="utf-8")
            result["window_xml"] = xml_path
        except Exception as exc:
            result["xml_error"] = str(exc)
        return result

    def _failed(self, action, msg):
        artifacts = {}
        try:
            artifacts = self._dump_artifacts(action)
        except Exception:
            pass
        return {"status": "failed", "msg": msg, "action": action, **artifacts}

    def _should_strict_readback(self):
        return bool(self.profile.get("strict_readback", False))

    def _validate_readback(self, page, code, price, amount):
        if not self._should_strict_readback():
            return None
        actual_code = self._get_text(page.get("code"))
        if actual_code is not None and str(code) not in actual_code:
            return f"代码回读不一致: expected={code} actual={actual_code}"
        actual_price = self._number(self._get_text(page.get("price")))
        if actual_price is not None and abs(actual_price - float(price)) > 0.001:
            return f"价格回读不一致: expected={price} actual={actual_price}"
        actual_amount = self._number(self._get_text(page.get("amount")))
        if actual_amount is not None and int(actual_amount) != int(amount):
            return f"数量回读不一致: expected={amount} actual={actual_amount}"
        return None

    def _validate_confirmation(self, action, code, price, amount):
        confirm = self.profile.get("confirm", {})
        marker = confirm.get("page_marker")
        marker_exists = self._exists_or_unknown(marker) if marker else True
        if marker_exists is False:
            return "委托确认页未出现"
        if marker_exists is None and not bool(self.profile.get("allow_unverified_input_when_dump_fails", True)):
            return "委托确认页未出现"
        if not self._should_strict_readback():
            return None
        expected_direction = (confirm.get("direction_text") or {}).get(action)
        actual_direction = self._get_text(confirm.get("direction"))
        if expected_direction and actual_direction and expected_direction not in actual_direction:
            return f"确认页方向不一致: expected={expected_direction} actual={actual_direction}"
        actual_code = self._get_text(confirm.get("code"))
        if actual_code and str(code) not in actual_code:
            return f"确认页代码不一致: expected={code} actual={actual_code}"
        actual_price = self._number(self._get_text(confirm.get("price")))
        if actual_price is not None and abs(actual_price - float(price)) > 0.001:
            return f"确认页价格不一致: expected={price} actual={actual_price}"
        actual_amount = self._number(self._get_text(confirm.get("amount")))
        if actual_amount is not None and int(actual_amount) != int(amount):
            return f"确认页数量不一致: expected={amount} actual={actual_amount}"
        return None

    def _submit(self, action, code, price, amount):
        code = str(code).strip()
        if not (code.isdigit() and len(code) == 6):
            return self._failed(action, f"证券代码格式错误: {code}")
        if float(price) <= 0 or int(amount) <= 0:
            return self._failed(action, "价格和数量必须大于 0")

        connected = self.connect()
        if not connected.get("ok"):
            return self._failed(action, connected["msg"])

        page = self.profile["buy" if action == "BUY" else "sell"]
        try:
            self._dismiss_search_overlay()
            if self.profile.get("trade_tab"):
                self._click(self.profile["trade_tab"])
                time.sleep(float(self.profile.get("ui_wait_sec", 0.25)))
                self._dismiss_optional_dialogs()
            if page.get("entry"):
                self._click(page["entry"])
                time.sleep(float(self.profile.get("ui_wait_sec", 0.25)))
            if self._trade_login_visible():
                return self._failed(action, self._trade_login_block_msg())
            if page.get("page_marker") and not self._wait_exists(page["page_marker"]):
                if self._trade_login_visible():
                    return self._failed(action, self._trade_login_block_msg())
                return self._failed(action, "交易页面标识缺失")

            price_text = self._price_text(price)
            self._set_text(page["code"], code)
            try:
                self.device.press("enter")
                time.sleep(float(self.profile.get("ui_wait_sec", 0.35)))
            except Exception:
                pass
            if not self._select_stock_search_result(code):
                return self._failed(action, f"股票搜索结果未找到或未选中: {code}")
            self._set_text(page["price"], price_text)
            self._set_text(page["amount"], int(amount))
            mismatch = self._validate_readback(page, code, price, amount)
            if mismatch:
                return self._failed(action, mismatch)

            self._click(page["submit"])
            time.sleep(float(self.profile.get("confirm_wait_sec", 0.6)))
            mismatch = self._validate_confirmation(action, code, price, amount)
            if mismatch:
                return self._failed(action, mismatch)

            mode = str(self.profile.get("confirm_mode", "manual")).lower()
            if mode == "auto":
                self._click(self.profile["confirm"]["final_confirm"])
                time.sleep(float(self.profile.get("after_confirm_wait_sec", 0.4)))
            artifacts = self._dump_artifacts(action)
            return {
                "status": "submitted",
                "msg": "已停在确认页" if mode == "manual" else "已点击最终确认",
                "action": action,
                "code": code,
                "exec_price": float(price),
                "exec_shares": int(amount),
                "device_serial": connected["device_serial"],
                "confirm_mode": mode,
                **artifacts,
            }
        except Exception as exc:
            return self._failed(action, f"手机下单步骤失败: {exc}")

    @staticmethod
    def _order_raw_text(row):
        parts = [
            row.get("code", ""),
            row.get("name", ""),
            row.get("direction", ""),
            row.get("status_text", ""),
            *(row.get("raw_texts") or []),
        ]
        return " ".join(str(part) for part in parts if part)

    @staticmethod
    def _order_target_action_text(action):
        value = str(action or "").upper()
        if value == "BUY":
            return "买入"
        if value == "SELL":
            return "卖出"
        return ""

    def _order_row_cancelled(self, row):
        raw = self._order_raw_text(row)
        return any(word in raw for word in ("全部撤单", "已撤", "撤单成功"))

    def _order_row_cancelable(self, row):
        if self._order_row_cancelled(row):
            return False
        raw = self._order_raw_text(row)
        if any(word in raw for word in ("全部成交", "已成", "废单", "拒单", "失败")):
            return False
        status = str(row.get("status") or "")
        return status == "submitted" or any(word in raw for word in ("未成交", "未成", "已报", "待报", "部成"))

    def _order_row_matches_target(self, row, code=None, action=None, name=None):
        target_code = str(code or "").strip().zfill(6) if code else ""
        row_code = str(row.get("code") or "").strip()
        raw = self._order_raw_text(row)
        if target_code and row_code and row_code != target_code:
            return False
        if name and str(name) not in raw:
            if target_code and row_code == target_code:
                pass
            else:
                return False
        action_text = self._order_target_action_text(action)
        if action_text and action_text not in raw:
            return False
        return True

    def _collect_today_order_rows(self, items, code_filter=None, max_rows=50):
        rows = self._extract_order_rows_by_result_ids(items, code_filter=code_filter, max_rows=max_rows)
        if rows:
            return rows
        rows = []
        for row in self._extract_rows_by_code(items, code_filter=code_filter, max_rows=max_rows):
            raw_texts = row.get("raw_texts") or []
            rows.append({
                "code": row.get("code", ""),
                "name": row.get("name", ""),
                "direction": "",
                "status": self._normalize_order_status("", raw_texts),
                "status_text": "",
                "raw_texts": raw_texts,
                "tap_x": row.get("tap_x"),
                "tap_y": row.get("tap_y"),
            })
        return rows

    def _select_cancel_target_row(self, rows, code=None, action=None, name=None):
        candidates = [row for row in rows if self._order_row_cancelable(row)]
        matched = [row for row in candidates if self._order_row_matches_target(row, code=code, action=action, name=name)]
        if matched:
            return matched[0]
        if not code and not action and not name and candidates:
            return candidates[0]
        if candidates and all(not row.get("code") for row in candidates):
            return candidates[0]
        return None

    def _click_cancel_dialog_button(self):
        wait = float(self.profile.get("ui_wait_sec", 0.35))
        deadline = time.time() + float(self.profile.get("confirm_wait_sec", 0.8)) + 2.0
        while time.time() < deadline:
            try:
                items = self._text_items()
            except Exception:
                items = []
            joined = " ".join(item["text"] for item in items)
            exact_buttons = [
                item for item in items
                if item["text"] == "撤单" and item["y"] > 500
            ]
            if exact_buttons:
                exact_buttons.sort(key=lambda item: (item["y"], item["x"]))
                self.device.click(exact_buttons[0]["x"], exact_buttons[0]["y"])
                return True
            if "委托撤单确认" in joined or "确认以上撤单" in joined:
                width, height = self.device.window_size()
                self.device.click(int(width * 0.5), int(height * 0.54))
                return True
            time.sleep(wait)
        return False

    def cancel_order(self, code=None, action=None, name=None):
        connected = self.connect()
        if not connected.get("ok"):
            return self._failed("CANCEL", connected["msg"])
        try:
            opened = self._open_trade_subtab("委托")
            if not opened.get("ok"):
                return self._failed("CANCEL", opened.get("msg", "打开委托页失败"))
            items = self._text_items()
            rows = self._collect_today_order_rows(items, code_filter=None, max_rows=50)
            target = self._select_cancel_target_row(rows, code=code, action=action, name=name)
            if not target:
                artifacts = self._dump_artifacts("CANCEL")
                return {
                    "status": "failed",
                    "msg": f"当日委托页未找到可撤委托: code={code or ''} name={name or ''}",
                    "action": "CANCEL",
                    "rows": rows[:8],
                    **artifacts,
                }

            tap_x = target.get("tap_x") or 520
            tap_y = target.get("tap_y") or 410
            self.device.click(int(tap_x), int(tap_y))
            time.sleep(float(self.profile.get("confirm_wait_sec", 0.8)))
            if not self._click_cancel_dialog_button():
                artifacts = self._dump_artifacts("CANCEL")
                return {
                    "status": "failed",
                    "msg": "已点击委托行，但撤单确认按钮未出现",
                    "action": "CANCEL",
                    "target": target,
                    **artifacts,
                }
            time.sleep(float(self.profile.get("after_confirm_wait_sec", 0.6)))

            try:
                refreshed = self.query_today_orders(code_filter=None, max_rows=50, navigate=False)
            except Exception as exc:
                refreshed = {"ok": False, "msg": str(exc), "rows": []}
            rows_after = refreshed.get("rows", []) if isinstance(refreshed, dict) else []
            matched_after = [
                row for row in rows_after
                if self._order_row_matches_target(row, code=code, action=action, name=name)
            ]
            if not matched_after and rows_after and target.get("name"):
                matched_after = [row for row in rows_after if target.get("name") in self._order_raw_text(row)]
            cancelled = [row for row in matched_after if self._order_row_cancelled(row)]
            artifacts = self._dump_artifacts("CANCEL")
            if cancelled:
                row = cancelled[0]
                return {
                    "status": "cancelled",
                    "msg": f"手机委托页已回读到目标委托撤单: {self._order_raw_text(row)}".strip(),
                    "action": "CANCEL",
                    "code": code,
                    "target": target,
                    "row": row,
                    **artifacts,
                }
            return {
                "status": "submitted",
                "msg": "已点击撤单确认，等待手机委托页刷新为撤单状态",
                "action": "CANCEL",
                "code": code,
                "target": target,
                "rows_after": rows_after[:8],
                **artifacts,
            }
        except Exception as exc:
            return self._failed("CANCEL", f"手机撤单步骤失败: {exc}")

    def buy(self, code, price, amount):
        return self._submit("BUY", code, price, amount)

    def sell(self, code, price, amount):
        return self._submit("SELL", code, price, amount)

    def reconcile_order(self, action, code, before_shares, order_ref=None, name=None):
        timeout_sec = float(self.profile.get("reconcile_timeout_sec", 20))
        poll_sec = float(self.profile.get("reconcile_poll_sec", 2))
        deadline = time.monotonic() + timeout_sec
        order_result = {"status": "unknown"}
        while True:
            try:
                order_result = self.api.get_order_status(order_ref=order_ref, code=code)
            except Exception as exc:
                order_result = {"ok": False, "status": "unknown", "msg": str(exc)}
            status = str(order_result.get("status", "unknown"))
            if status in {"executed", "submitted", "failed"}:
                return {**order_result, "status": status, "source": order_result.get("source", "orders_api")}
            if order_result.get("ok") is False or time.monotonic() >= deadline:
                break
            time.sleep(poll_sec)

        ui_order = self.query_today_orders(code_filter=code, navigate=True)
        if isinstance(ui_order, dict) and ui_order.get("ok"):
            for row in ui_order.get("rows", []):
                status = str(row.get("status", "unknown"))
                if status in {"executed", "submitted", "failed"}:
                    return {
                        "ok": True,
                        "status": status,
                        "msg": self._order_status_msg(row),
                        "source": "mobile_ui_orders",
                        "order": row,
                        "api_order": order_result,
                    }

        try:
            positions = self.api.get_positions()
        except Exception as exc:
            positions = {"ok": False, "msg": str(exc)}
        if isinstance(positions, dict) and positions.get("ok") is False:
            positions = self.query_positions()
        after_shares = 0
        if isinstance(positions, list):
            for position in positions:
                code_matched = str(position.get("code", "")) == str(code)
                name_matched = bool(name) and str(position.get("name", "")) == str(name)
                if code_matched or name_matched:
                    try:
                        after_shares = float(position.get("shares", 0) or 0)
                    except (TypeError, ValueError):
                        after_shares = 0
                    break
        try:
            before_value = float(before_shares)
        except (TypeError, ValueError):
            before_value = 0
        changed = after_shares > before_value if str(action).upper() == "BUY" else after_shares < before_value
        return {
            "status": "executed" if changed else "submitted",
            "msg": "\u6301\u4ed3\u5df2\u53d8\u5316" if changed else "\u59d4\u6258\u5df2\u63d0\u4ea4\uff0c\u6301\u4ed3\u6682\u672a\u53d8\u5316",
            "source": "mobile_ui_positions" if isinstance(positions, list) else "positions_api",
            "before_shares": before_value,
            "after_shares": after_shares,
            "order": order_result,
            "ui_order": ui_order,
        }
