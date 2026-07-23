import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from broker.mobile_ths_operator import MobileTHSOperator


def test_package_matches_accepts_panel_prefixed_package():
    assert MobileTHSOperator._package_matches("Panel:com.hexin.plat.android", "com.hexin.plat.android") is True


class FakeTarget:
    def __init__(self):
        self.exists = True
        self.clicked = False

    def click(self):
        self.clicked = True

    def clear_text(self):
        raise RuntimeError("ADB_KEYBOARD_CLEAR_TEXT failed")

    def set_text(self, value):
        raise RuntimeError("set_text failed after adb keyboard crash")


class FakeDevice:
    def __init__(self, target):
        self.target = target
        self.sent = []
        self.pressed = []
        self.fastinput = []

    def __call__(self, **kwargs):
        return self.target

    def set_fastinput_ime(self, value):
        self.fastinput.append(value)

    def clear_text(self):
        raise RuntimeError("broadcast ADB_KEYBOARD_CLEAR_TEXT failed")

    def press(self, key):
        self.pressed.append(key)

    def send_keys(self, text, clear=False):
        self.sent.append((text, clear))


class MissingTarget:
    exists = False


class RelativeDevice:
    def __init__(self, xml_values=None):
        self.clicked = []
        self.sent = []
        self.pressed = []
        self.fastinput = []
        self.xml_values = list(xml_values or [])

    def __call__(self, **kwargs):
        return MissingTarget()

    def xpath(self, value):
        return MissingTarget()

    def window_size(self):
        return 1000, 1000

    def click(self, x, y):
        self.clicked.append((x, y))

    def set_fastinput_ime(self, value):
        self.fastinput.append(value)

    def clear_text(self):
        pass

    def press(self, key):
        self.pressed.append(key)

    def send_keys(self, text, clear=False):
        self.sent.append((str(text), clear))

    def dump_hierarchy(self):
        value = self.xml_values.pop(0) if self.xml_values else ""
        return f"""<?xml version='1.0' encoding='UTF-8'?><hierarchy><node text=\"{value}\" resource-id=\"\" content-desc=\"\" bounds=\"[0,0][100,100]\" /></hierarchy>"""


def test_set_text_falls_back_when_adb_keyboard_clear_crashes():
    target = FakeTarget()
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = FakeDevice(target)
    op.profile = {"input_wait_sec": 0}

    op._set_text({"resource_id": "code"}, "600519")

    assert target.clicked is True
    assert ("600519", False) in op.device.sent
    assert op.device.pressed.count("KEYCODE_DEL") >= 24


def test_exists_does_not_treat_relative_coordinate_as_real_element():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = RelativeDevice()

    assert op._exists({"text": "委托确认", "relative": [0.5, 0.5]}) is False


def test_exists_uses_hierarchy_text_fallback_for_page_marker():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = RelativeDevice(xml_values=["PAGE_MARKER"])

    assert op._exists({"text": "PAGE_MARKER", "relative": [0.5, 0.5]}) is True


def test_wait_exists_polls_until_page_marker_is_visible():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = RelativeDevice(xml_values=["", "PAGE_MARKER"])
    op.profile = {"ui_wait_sec": 0}

    assert op._wait_exists({"text": "PAGE_MARKER"}, timeout_sec=0.2) is True


def test_relative_set_text_retries_until_value_is_visible_in_hierarchy():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = RelativeDevice(xml_values=["数量", "400"])
    op.profile = {"input_wait_sec": 0, "input_retry": 2, "strict_readback": True}

    op._set_text({"relative": [0.35, 0.32]}, 400)

    assert op.device.clicked == [(350, 320), (350, 320)]
    assert op.device.sent == [("400", False), ("400", False)]

class PartialInputTarget:
    def __init__(self):
        self.exists = True
        self.clicked = 0
        self.values = []
        self.text = ""

    def click(self):
        self.clicked += 1

    def clear_text(self):
        self.text = ""

    def set_text(self, value):
        self.values.append(str(value))
        self.text = "600" if len(self.values) == 1 else str(value)


def test_set_text_retries_when_readback_is_partial():
    target = PartialInputTarget()
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = FakeDevice(target)
    op.profile = {"input_wait_sec": 0, "input_retry": 2}

    op._set_text({"resource_id": "code"}, "600519")

    assert target.values == ["600519", "600519"]
    assert target.text == "600519"
    assert target.clicked >= 1

def test_order_row_status_maps_partial_cancel_to_submitted_not_failed():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    items = [
        {"resource_id": "com.hexin:id/result0", "text": "贵州茅台", "x": 10, "y": 350, "top": 350},
        {"resource_id": "com.hexin:id/result1", "text": "600519", "x": 80, "y": 350, "top": 350},
        {"resource_id": "com.hexin:id/result6", "text": "买入", "x": 200, "y": 350, "top": 350},
        {"resource_id": "com.hexin:id/result7", "text": "部成待撤", "x": 260, "y": 350, "top": 350},
    ]

    rows = op._extract_order_rows_by_result_ids(items, code_filter="600519")

    assert rows[0]["status"] == "submitted"
    assert rows[0]["status_text"] == "部成待撤"


def test_order_status_maps_unfilled_to_submitted_not_executed():
    assert MobileTHSOperator._normalize_order_status("未成交", ["买入", "平安银行"]) == "submitted"


def test_order_status_maps_fully_filled_to_executed_not_partial():
    assert MobileTHSOperator._normalize_order_status("全部成交", ["买入", "平安银行"]) == "executed"


def test_order_row_status_maps_rejected_to_failed():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    items = [
        {"resource_id": "com.hexin:id/result0", "text": "贵州茅台", "x": 10, "y": 350, "top": 350},
        {"resource_id": "com.hexin:id/result1", "text": "600519", "x": 80, "y": 350, "top": 350},
        {"resource_id": "com.hexin:id/result7", "text": "废单", "x": 260, "y": 350, "top": 350},
    ]

    rows = op._extract_order_rows_by_result_ids(items, code_filter="600519")

    assert rows[0]["status"] == "failed"


def test_value_by_label_reads_inline_available_cash():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    items = [
        {"resource_id": "", "text": "可用资金 12,345.67", "x": 100, "y": 100, "top": 80, "bottom": 120},
    ]

    assert op._value_by_resource_or_label(items, "__no_resource__", "可用资金") == 12345.67


def test_value_by_label_returns_none_when_available_cash_missing():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    items = [
        {"resource_id": "com.hexin:id/capital_value_text", "text": "0.01", "x": 100, "y": 100, "top": 80, "bottom": 120},
        {"resource_id": "", "text": "总资产", "x": 100, "y": 40, "top": 20, "bottom": 60},
    ]

    assert op._value_by_resource_or_label(items, "__no_resource__", "可用资金") is None


def test_value_by_label_keeps_zero_available_cash():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    items = [
        {"resource_id": "com.hexin:id/available_value_text", "text": "0.00", "x": 100, "y": 100, "top": 80, "bottom": 120},
    ]

    assert op._value_by_resource_or_label(items, "available_value_text", "可用资金") == 0.0


class AppDevice(RelativeDevice):
    def __init__(self, xml_values=None):
        super().__init__(xml_values=xml_values)
        self.started = []
        self.current = {"package": "", "activity": ""}
        self.info = {"ok": True}

    def app_current(self):
        return self.current

    def app_start(self, package):
        self.started.append(package)
        self.current = {"package": package, "activity": "Hexin"}


def test_open_app_launches_configured_android_package():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = AppDevice()
    op.profile = {"app_package": "com.hexin.plat.android", "launch_wait_sec": 0, "dismiss_dialogs": []}

    result = op.open_app()

    assert result["ok"] is True
    assert op.device.started == ["com.hexin.plat.android"]


def test_switch_trade_mode_clicks_configured_tab_and_reports_mode():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = AppDevice(xml_values=["买入 卖出", "A股 基金 期货 港美股 模拟", "模拟炒股"])
    op.profile = {
        "app_package": "com.hexin.plat.android",
        "launch_wait_sec": 0,
        "ui_wait_sec": 0,
        "dismiss_dialogs": [],
        "trade_tab": {"relative": [0.5, 0.9]},
        "mode_tabs": {"simulation": {"relative": [0.78, 0.066]}},
    }

    result = op.switch_trade_mode("simulation")

    assert result["ok"] is True
    assert result["mode"] == "simulation"
    assert (500, 900) in op.device.clicked
    assert (780, 66) in op.device.clicked


def test_switch_trade_mode_backs_out_of_subpage_before_clicking_mode_tab():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = AppDevice(xml_values=["买入 卖出", "当日委托 买入 未成交", "A股 基金 期货 港美股 模拟", "模拟炒股"])
    op.profile = {
        "app_package": "com.hexin.plat.android",
        "launch_wait_sec": 0,
        "ui_wait_sec": 0,
        "dismiss_dialogs": [],
        "trade_tab": {"relative": [0.5, 0.9]},
        "mode_tabs": {"simulation": {"relative": [0.78, 0.066]}},
    }

    result = op.switch_trade_mode("simulation")

    assert result["ok"] is True
    assert "back" in op.device.pressed
    assert (780, 66) in op.device.clicked


def test_infer_trade_mode_treats_live_account_page_as_live():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = RelativeDevice(xml_values=["A股 基金 期货 港美股 模拟 国盛证券 资金帐户 添加账户 账户管理 证券开户"])

    assert op._infer_trade_mode() == "live"


def test_infer_trade_mode_treats_simulation_trade_page_as_simulation():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = RelativeDevice(xml_values=[
        "\u0041\u80a1 \u57fa\u91d1 \u671f\u8d27 \u6e2f\u7f8e\u80a1 \u6a21\u62df "
        "\u603b\u8d44\u4ea7 \u603b\u6536\u76ca\u7387 \u603b\u5e02\u503c \u5f53\u65e5\u53c2\u8003\u76c8\u4e8f "
        "\u4e70\u5165 \u5356\u51fa \u64a4\u5355 \u6301\u4ed3 \u67e5\u8be2"
    ])

    assert op._infer_trade_mode() == "simulation"

def test_switch_trade_mode_returns_success_when_already_in_target_mode():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = AppDevice(xml_values=["模拟炒股 买入 卖出"])
    op.profile = {
        "app_package": "com.hexin.plat.android",
        "launch_wait_sec": 0,
        "ui_wait_sec": 0,
        "dismiss_dialogs": [],
        "trade_tab": {"relative": [0.5, 0.9]},
        "mode_tabs": {"simulation": {"relative": [0.78, 0.066]}},
    }

    result = op.switch_trade_mode("simulation")

    assert result["ok"] is True
    assert result["current_mode"] == "simulation"
    assert (780, 66) not in op.device.clicked

class DumpFailRelativeDevice(RelativeDevice):
    def dump_hierarchy(self):
        raise RuntimeError("Killed")


def test_relative_set_text_allows_unverified_input_when_dump_fails():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = DumpFailRelativeDevice()
    op.profile = {
        "input_wait_sec": 0,
        "input_retry": 1,
        "strict_readback": True,
        "allow_unverified_input_when_dump_fails": True,
    }

    op._set_text({"relative": [0.35, 0.32]}, 100)

    assert op.device.clicked == [(350, 320)]
    assert op.device.sent == [("100", False)]


def test_validate_confirmation_allows_unknown_marker_when_dump_fails():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = DumpFailRelativeDevice()
    op.profile = {
        "strict_readback": True,
        "allow_unverified_input_when_dump_fails": True,
        "confirm": {
            "page_marker": {"text": "confirm", "relative": [0.5, 0.5]},
            "direction_text": {"BUY": "buy"},
        },
    }

    assert op._validate_confirmation("BUY", "000001", 10.5, 100) is None

def test_wait_exists_allows_unknown_marker_when_dump_fails():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = DumpFailRelativeDevice()
    op.profile = {
        "ui_wait_sec": 0,
        "allow_unverified_input_when_dump_fails": True,
    }

    assert op._wait_exists({"text": "code", "relative": [0.4, 0.2]}, timeout_sec=0) is True

def test_switch_to_simulation_closes_trade_login_layer_first():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = AppDevice(xml_values=[
        "请输入交易密码 保持在线24小时",
        "买入 卖出",
        "A股 基金 期货 港美股 模拟",
        "模拟炒股",
    ])
    op.profile = {
        "app_package": "com.hexin.plat.android",
        "launch_wait_sec": 0,
        "ui_wait_sec": 0,
        "dismiss_dialogs": [],
        "trade_tab": {"relative": [0.5, 0.9]},
        "mode_tabs": {"simulation": {"relative": [0.78, 0.066]}},
    }

    result = op.switch_trade_mode("simulation")

    assert result["ok"] is True
    assert "back" in op.device.pressed
    assert (780, 66) in op.device.clicked

class ScreenOffInstallerDevice(AppDevice):
    def __init__(self):
        super().__init__(xml_values=[])
        self.current = {"package": "com.hexin.plat.android", "activity": "Hexin"}
        self.info = {"screenOn": False}
        self.shell_calls = []

    def shell(self, args):
        self.shell_calls.append(tuple(args))

    def dump_hierarchy(self):
        return """<?xml version='1.0' encoding='UTF-8'?><hierarchy><node text="????" package="com.android.packageinstaller" resource-id="com.android.packageinstaller:id/ok_button" bounds="[60,1938][1020,2058]" /></hierarchy>"""


def test_connect_wakes_screen_and_dismisses_stale_installer_hierarchy():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = ScreenOffInstallerDevice()
    op.profile = {"app_package": "com.hexin.plat.android", "ui_wait_sec": 0, "dismiss_dialogs": []}

    result = op.connect()

    assert result["ok"] is True
    assert ("input", "keyevent", "WAKEUP") in op.device.shell_calls
    assert "back" in op.device.pressed


def test_open_trade_home_fails_when_trade_markers_never_appear():
    op = MobileTHSOperator.__new__(MobileTHSOperator)
    op.device = AppDevice(xml_values=["??", "??", "??"])
    op.profile = {"ui_wait_sec": 0, "dismiss_dialogs": [], "trade_tab": {"relative": [0.5, 0.9]}}
    op.connect = lambda: {"ok": True}

    result = op._open_trade_home()

    assert result["ok"] is False
    assert result["source"] == "mobile_ui"
    assert (500, 900) in op.device.clicked
