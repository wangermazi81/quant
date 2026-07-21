# Android 手机同花顺混合执行器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 增加 Android 实盘执行器：买卖由 ADB/uiautomator2 操作手机，资金、持仓和委托状态读取交易 APP 的只读接口响应。

**Architecture:** `MobileTHSOperator` 负责 UI 和委托确认，`MobileTHSApi` 负责账户快照与成交核对，`CloudLocalBridge` 通过工厂选择桌面或 Android 执行器。抓包先产出稳定端点与字段映射，动态会话只保存在本机。

**Tech Stack:** Windows 11、Android Platform Tools/ADB、Python 3.12 (`D:\Anaconda\python.exe`)、uiautomator2 3.7.0、mitmproxy 12.2.3、requests、pytest 8+

## Global Constraints

- 手机通过 USB 连接，保持常亮、已解锁、交易 APP 已登录并在前台。
- 买卖只走 UI；资金、持仓和委托状态只走已验证的只读接口。
- 初始固定 `confirm_mode=manual`，真机验收后再切 `auto`。
- 账户数值不使用 OCR；截图/XML 只用于定位与诊断。
- Cookie、令牌、nonce、签名、原始抓包保存在 `logs/mobile_capture/`，不进入 Git。
- 保持现有 `THSOperator`、云端协议和 `submitted/executed/failed` 状态兼容。
- 提交使用 `git commit --only ... -- <paths>`，不带入已有暂存改动。

---

### Task 1: Android 工具链与设备探测

**Files:**
- Create: `requirements-android.txt`
- Create: `tools/install_android_tools.ps1`
- Create: `tools/android_probe.py`
- Test: `tests/test_android_probe.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `adb devices -l` 输出。
- Produces: `parse_adb_devices(text) -> list[dict]`、`probe_device(serial=None) -> dict`、`dump_artifacts(output_dir) -> dict`。

- [ ] **Step 1: 写失败测试**

测试授权设备解析，输入同时含 `device` 和 `unauthorized`，只返回授权设备及 `serial/model/product/device`。

- [ ] **Step 2: 验证测试失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_android_probe.py -v`

Expected: FAIL，缺少 `tools.android_probe`。

- [ ] **Step 3: 添加依赖与安装脚本**

`requirements-android.txt` 固定 `uiautomator2==3.7.0`、`mitmproxy==12.2.3`。安装脚本下载 Google 的 `platform-tools-latest-windows.zip` 至 `.android-sdk/`，再使用 `D:\Anaconda\python.exe -m pip install -r requirements-android.txt`。

`.gitignore` 增加 `.android-sdk/`、`logs/mobile_capture/`、`tools/mobile_ths_profile.json`、`tools/mobile_ths_api_profile.json`。

- [ ] **Step 4: 实现设备探测**

探测 Android 版本、屏幕尺寸、前台包名/Activity；`dump_artifacts()` 保存 `screen.png`、`window.xml`、`device.json`。授权设备数量不是 1 时返回设备列表和明确错误。

- [ ] **Step 5: 验证测试通过**

Run: `D:\Anaconda\python.exe -m pytest tests/test_android_probe.py -v`

Expected: PASS。

- [ ] **Step 6: 安装并验证真机**

Run: `powershell -ExecutionPolicy Bypass -File tools/install_android_tools.ps1`

Run: `.\.android-sdk\platform-tools\adb.exe devices -l`

Expected: 恰好一个设备状态为 `device`。

- [ ] **Step 7: 提交**

`git commit --only -m "feat: add Android device bootstrap and probe" -- requirements-android.txt tools/install_android_tools.ps1 tools/android_probe.py tests/test_android_probe.py .gitignore`

### Task 2: 抓包采集与协议分类

**Files:**
- Create: `tools/android_capture_addon.py`
- Create: `tools/android_capture_probe.py`
- Create: `tests/fixtures/mobile_capture_sample.jsonl`
- Test: `tests/test_android_capture_probe.py`

**Interfaces:**
- Consumes: mitmproxy HTTP/HTTPS/WebSocket 流和页面动作标记。
- Produces: `classify_record(record) -> str`、`summarize_records(records) -> dict`、`logs/mobile_capture/capture.jsonl`。

- [ ] **Step 1: 写失败测试**

覆盖 `json`、`protobuf`、`websocket`、`tls_pinning`、`encrypted_payload` 五种分类，并验证相同端点两次响应能识别稳定键和变化键。

- [ ] **Step 2: 验证测试失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_android_capture_probe.py -v`

Expected: FAIL，缺少分类模块。

- [ ] **Step 3: 实现采集插件**

`android_capture_addon.py` 在 `response()` 和 `websocket_message()` 中追加 JSONL，字段固定为时间、动作、方法、主机、路径、状态码、内容类型、请求/响应头、文本或 Base64 响应体、WebSocket 标记。`CAPTURE_ACTION=account|positions|orders` 标记当前页面动作。

- [ ] **Step 4: 实现分类摘要**

Run: `D:\Anaconda\python.exe tools/android_capture_probe.py --input logs/mobile_capture/capture.jsonl --output logs/mobile_capture/probe-report.json`

报告按 `action + host + path + method` 分组，输出类型、次数、响应大小、JSON 顶层键和变化字段；请求头值只保存 SHA-256 摘要。

- [ ] **Step 5: 验证测试通过**

Run: `D:\Anaconda\python.exe -m pytest tests/test_android_capture_probe.py -v`

Expected: PASS。

- [ ] **Step 6: 真机采集三个动作**

Run: `D:\Anaconda\Scripts\mitmweb.exe --listen-host 0.0.0.0 --listen-port 8080 -s tools/android_capture_addon.py`

手机代理指向电脑 `8080`，资产、持仓、当日委托各刷新两次。生成报告后确定每个动作的协议类型和稳定端点。

- [ ] **Step 7: 提交**

`git commit --only -m "feat: add Android traffic capture probe" -- tools/android_capture_addon.py tools/android_capture_probe.py tests/fixtures/mobile_capture_sample.jsonl tests/test_android_capture_probe.py`

### Task 3: 只读账户接口适配器

**Files:**
- Create: `broker/mobile_ths_api.py`
- Create: `tools/mobile_ths_api_profile.example.json`
- Test: `tests/test_mobile_ths_api.py`

**Interfaces:**
- Consumes: `MobileTHSApi(profile_path, session_path, transport=None, refresh_session=None)`。
- Produces: `get_account()`、`get_positions(max_rows=50)`、`get_order_status(order_ref=None, code=None)`、`get_account_snapshot(max_rows=50)`。

- [ ] **Step 1: 写失败测试**

注入假 transport，覆盖账户/持仓字段归一化、逗号数值、空字符串、缺字段、401/403、会话超过 300 秒、刷新后只重试一次。委托状态归一化为 `executed/submitted/failed/unknown`。

- [ ] **Step 2: 验证测试失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_mobile_ths_api.py -v`

Expected: FAIL，缺少 `MobileTHSApi`。

- [ ] **Step 3: 写配置样例**

配置固定包含 `account`、`positions`、`orders`；每项包含方法、URL 特征、解码器、请求头/体、数据路径和字段映射。解码器只使用 Task 2 已确认的 `json/protobuf/websocket`。

- [ ] **Step 4: 实现最小适配器**

默认使用 `requests.Session`，动态会话从 `logs/mobile_capture/session.json` 合并。401、403 或会话过期时调用一次 `refresh_session()` 后重试一次。必需数值缺失返回 `ok=False` 和字段名，不用零值伪装成功。

- [ ] **Step 5: 验证测试通过**

Run: `D:\Anaconda\python.exe -m pytest tests/test_mobile_ths_api.py -v`

Expected: PASS。

- [ ] **Step 6: 提交**

`git commit --only -m "feat: add read-only mobile account API" -- broker/mobile_ths_api.py tools/mobile_ths_api_profile.example.json tests/test_mobile_ths_api.py`

### Task 4: Android UI 下单执行器

**Files:**
- Create: `broker/mobile_ths_operator.py`
- Create: `tools/mobile_ths_profile.example.json`
- Test: `tests/test_mobile_ths_operator.py`

**Interfaces:**
- Consumes: `MobileTHSOperator(profile_path, api, device=None)`。
- Produces: `connect()`、`buy(code, price, amount)`、`sell(code, price, amount)`、`query_account_snapshot(max_rows=50)`。

- [ ] **Step 1: 写失败测试**

覆盖设备未授权、APP 不在前台、页面标志缺失、代码/价格/数量回读不一致、确认页字段不一致，以及手动模式成功停在确认页返回 `submitted`。

- [ ] **Step 2: 验证测试失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_mobile_ths_operator.py -v`

Expected: FAIL，缺少 `MobileTHSOperator`。

- [ ] **Step 3: 写页面配置样例**

配置包含设备序列号、APP 包名、确认模式、买入页、卖出页、确认页。定位优先级固定为资源 ID、文本/描述、XPath、归一化相对坐标。

- [ ] **Step 4: 实现下单状态机**

顺序固定：连接和前台检查 -> 进入交易页 -> 填写三字段 -> 回读三字段 -> 点击委托 -> 回读确认页方向/代码/价格/数量 -> `manual` 停留或 `auto` 最终确认 -> 保存截图/XML。返回结构兼容现有桌面执行器并增加 `device_serial`。

`query_account_snapshot()` 直接委托给 `MobileTHSApi`，不解析 UI 文本。

- [ ] **Step 5: 验证测试通过**

Run: `D:\Anaconda\python.exe -m pytest tests/test_mobile_ths_operator.py -v`

Expected: PASS。

- [ ] **Step 6: 提交**

`git commit --only -m "feat: add Android UI order operator" -- broker/mobile_ths_operator.py tools/mobile_ths_profile.example.json tests/test_mobile_ths_operator.py`

### Task 5: 委托接口优先的成交回查

**Files:**
- Modify: `broker/mobile_ths_operator.py`
- Modify: `broker/mobile_ths_api.py`
- Test: `tests/test_mobile_ths_reconcile.py`

**Interfaces:**
- Consumes: `reconcile_order(action, code, before_shares, order_ref=None) -> dict`。
- Produces: 桥接器兼容的 `executed/submitted/failed`。

- [ ] **Step 1: 写失败测试**

覆盖：全部成交 -> `executed`；已报/部分成交 -> `submitted`；废单/拒绝 -> `failed`；接口 `unknown` 时买入股数增加或卖出股数减少 -> `executed`；两者无结论 -> `submitted`。

- [ ] **Step 2: 验证测试失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_mobile_ths_reconcile.py -v`

Expected: FAIL，缺少 `reconcile_order`。

- [ ] **Step 3: 实现有限轮询**

在 `reconcile_timeout_sec=20` 内每 2 秒查询委托状态。接口无结论时读取一次提交后持仓；页面弹窗只补充失败文本，不覆盖接口明确状态。

- [ ] **Step 4: 验证测试通过**

Run: `D:\Anaconda\python.exe -m pytest tests/test_mobile_ths_reconcile.py -v`

Expected: PASS。

- [ ] **Step 5: 提交**

`git commit --only -m "feat: reconcile mobile orders from app data" -- broker/mobile_ths_operator.py broker/mobile_ths_api.py tests/test_mobile_ths_reconcile.py`

### Task 6: 桥接器执行器选择

**Files:**
- Modify: `tools/ths_bridge.py`
- Modify: `tools/bridge_config.json`
- Test: `tests/test_ths_bridge_executor.py`

**Interfaces:**
- Consumes: `build_operator(config: dict)`。
- Produces: `executor_type=desktop|android` 的惰性执行器选择。

- [ ] **Step 1: 写失败测试**

断言 `desktop` 构造 `THSOperator`，`android` 构造 `MobileTHSOperator`，未知值抛出 `ValueError`，Android 依赖缺失只影响 Android 分支。

- [ ] **Step 2: 验证测试失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_ths_bridge_executor.py -v`

Expected: FAIL，缺少 `build_operator`。

- [ ] **Step 3: 实现工厂和配置**

`DEFAULT_CONFIG` 增加 `executor_type`、`android_profile`、`android_api_profile`、`confirm_mode`、`device_timeout_sec=10`、`page_timeout_sec=8`、`reconcile_timeout_sec=20`、`api_session_max_age_sec=300`。`init_ths()` 调用工厂，轮询、登录、上报和旧版 bool 兼容逻辑保持原样。

- [ ] **Step 4: 验证范围测试**

Run: `D:\Anaconda\python.exe -m pytest tests/test_ths_bridge_executor.py tests/test_mobile_ths_operator.py tests/test_mobile_ths_api.py -v`

Expected: PASS。

- [ ] **Step 5: 提交**

`git commit --only -m "feat: select Android executor in trade bridge" -- tools/ths_bridge.py tools/bridge_config.json tests/test_ths_bridge_executor.py`

### Task 7: 真机校准、手动验收和自动模式

**Files:**
- Create local-only: `tools/mobile_ths_profile.json`
- Create local-only: `tools/mobile_ths_api_profile.json`
- Create local-only: `logs/mobile_capture/session.json`
- Create: `docs/android-mobile-ths-runbook.md`

**Interfaces:**
- Consumes: 已授权设备、抓包报告、完成的 UI/API 执行器。
- Produces: 可重复启动的本机配置和运行手册。

- [ ] **Step 1: 生成设备和页面配置**

Run: `D:\Anaconda\python.exe tools/android_probe.py --dump logs/mobile_capture/device-probe`

在买入、卖出、确认页各导出 XML/截图，把稳定资源 ID 和文本写入本机页面配置。

- [ ] **Step 2: 生成只读接口配置**

从 `probe-report.json` 选定资产、持仓、委托端点，把稳定 URL 特征、字段路径和解码器写入 API 配置；动态字段写入 `session.json`。

- [ ] **Step 3: 验证账户快照**

Run: `D:\Anaconda\python.exe -c "from broker.mobile_ths_api import MobileTHSApi; from pathlib import Path; print(MobileTHSApi(Path('tools/mobile_ths_api_profile.json'), Path('logs/mobile_capture/session.json')).get_account_snapshot())"`

Expected: `ok=True`，可用资金、总资产、持仓代码和股数与手机页面一致。

- [ ] **Step 4: 手动模式验收买卖**

保持 `confirm_mode=manual`，买入和卖出各跑一次最小手数表单。两次都停在最终确认页并返回 `submitted`，四个确认字段完全一致。

- [ ] **Step 5: 验证桥接器端到端**

Run: `D:\Anaconda\python.exe tools/ths_bridge.py`

发送测试信号，确认完成拉取、Android 表单填写、手动确认页和 `submitted` 上报；重启后确认 USB 与会话恢复。

- [ ] **Step 6: 写运行手册**

记录 USB 连接、设备探测、抓包代理、会话刷新、桥接器启动、诊断目录，以及从 `manual` 切换 `auto` 的验收条件。

- [ ] **Step 7: 自动模式成交回查**

本机配置切为 `auto`，执行一笔最小手数委托。委托接口优先给出状态；接口无结果时持仓变化兜底更新为 `executed`。

- [ ] **Step 8: 最终范围验证**

Run: `D:\Anaconda\python.exe -m pytest tests/test_android_probe.py tests/test_android_capture_probe.py tests/test_mobile_ths_api.py tests/test_mobile_ths_operator.py tests/test_mobile_ths_reconcile.py tests/test_ths_bridge_executor.py -v`

Expected: 全部 PASS。

- [ ] **Step 9: 提交手册**

`git commit --only -m "docs: add Android mobile trader runbook" -- docs/android-mobile-ths-runbook.md`

## Implementation Code Contracts

以下代码合同属于对应 Task 的实现步骤，执行时按测试补齐导入、日志和异常消息，不改变函数签名。

### Device Parser

```python
def parse_adb_devices(text: str) -> list[dict]:
    devices = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        meta = dict(item.split(":", 1) for item in parts[2:] if ":" in item)
        devices.append({
            "serial": parts[0],
            "state": parts[1],
            "model": meta.get("model", ""),
            "product": meta.get("product", ""),
            "device": meta.get("device", ""),
        })
    return devices
```

### Capture Classifier

```python
def classify_record(record: dict) -> str:
    if record.get("tls_error"):
        return "tls_pinning"
    if record.get("websocket"):
        return "websocket"
    content_type = str(record.get("content_type", "")).lower()
    if "json" in content_type:
        return "json"
    if "protobuf" in content_type or "proto" in content_type:
        return "protobuf"
    return "encrypted_payload"
```

### Read-Only API

```python
class MobileTHSApi:
    def __init__(self, profile_path, session_path, transport=None, refresh_session=None):
        self.profile_path = Path(profile_path)
        self.session_path = Path(session_path)
        self.transport = transport or requests.Session()
        self.refresh_session = refresh_session

    def connect(self) -> dict:
        return {"ok": self.profile_path.exists(), "profile": str(self.profile_path)}

    def get_account_snapshot(self, max_rows: int = 50) -> dict:
        account = self.get_account()
        positions = self.get_positions(max_rows=max_rows)
        if not account.get("ok"):
            return account
        return {
            "ok": True,
            "available": account["available"],
            "total_asset": account["total_asset"],
            "positions": positions,
            "source": "app_api",
            "captured_at": account["captured_at"],
        }
```

### UI Operator

```python
class MobileTHSOperator:
    def __init__(self, profile_path, api, device=None):
        self.profile = load_profile(profile_path)
        self.api = api
        self.device = device

    def buy(self, code: str, price: float, amount: int) -> dict:
        return self._submit("BUY", code, price, amount)

    def sell(self, code: str, price: float, amount: int) -> dict:
        return self._submit("SELL", code, price, amount)

    def query_account_snapshot(self, max_rows: int = 50) -> dict:
        return self.api.get_account_snapshot(max_rows=max_rows)
```

### Order Reconciliation

```python
def reconcile_order(self, action, code, before_shares, order_ref=None):
    deadline = time.monotonic() + self.reconcile_timeout_sec
    while time.monotonic() < deadline:
        result = self.api.get_order_status(order_ref=order_ref, code=code)
        if result["status"] in {"executed", "submitted", "failed"}:
            return result
        time.sleep(2)
    after = self.api.get_positions()
    after_shares = next((p["shares"] for p in after if p["code"] == code), 0)
    changed = after_shares > before_shares if action == "BUY" else after_shares < before_shares
    return {"status": "executed" if changed else "submitted", "msg": "持仓回查完成"}
```

### Bridge Factory

```python
def build_operator(config: dict):
    executor_type = config.get("executor_type", "desktop")
    if executor_type == "desktop":
        from broker.ths_operator import THSOperator
        return THSOperator()
    if executor_type == "android":
        from broker.mobile_ths_api import MobileTHSApi
        from broker.mobile_ths_operator import MobileTHSOperator
        api = MobileTHSApi(config["android_api_profile"], "logs/mobile_capture/session.json")
        return MobileTHSOperator(config["android_profile"], api)
    raise ValueError(f"未知 executor_type={executor_type}")
```

## Test Contracts

```python
def test_parse_adb_devices_keeps_only_authorized_devices():
    text = "List of devices attached\nABC device product:x model:M device:d transport_id:1\nXYZ unauthorized transport_id:2\n"
    assert parse_adb_devices(text) == [{
        "serial": "ABC", "state": "device", "model": "M", "product": "x", "device": "d"
    }]


@pytest.mark.parametrize(("record", "expected"), [
    ({"content_type": "application/json"}, "json"),
    ({"content_type": "application/x-protobuf"}, "protobuf"),
    ({"websocket": True}, "websocket"),
    ({"tls_error": "certificate verify failed"}, "tls_pinning"),
    ({"content_type": "application/octet-stream"}, "encrypted_payload"),
])
def test_classify_record(record, expected):
    assert classify_record(record) == expected


def test_account_snapshot_normalizes_fields(api):
    snapshot = api.get_account_snapshot()
    assert snapshot["ok"] is True
    assert snapshot["available"] == 12345.67
    assert snapshot["positions"][0]["code"] == "000001"
    assert snapshot["positions"][0]["shares"] == 100


def test_manual_submit_stops_on_confirmation(fake_device, api, profile_path):
    operator = MobileTHSOperator(profile_path, api, device=fake_device)
    result = operator.buy("000001", 10.1, 100)
    assert result["status"] == "submitted"
    assert fake_device.final_confirm_clicks == 0


@pytest.mark.parametrize(("api_status", "expected"), [
    ("executed", "executed"),
    ("submitted", "submitted"),
    ("failed", "failed"),
])
def test_reconcile_prefers_order_api(operator, api_status, expected):
    operator.api.get_order_status.return_value = {"status": api_status, "msg": "fixture"}
    assert operator.reconcile_order("BUY", "000001", 0)["status"] == expected


def test_build_operator_selects_android(monkeypatch, android_config):
    monkeypatch.setattr("broker.mobile_ths_operator.MobileTHSOperator", MockMobileOperator)
    assert isinstance(build_operator(android_config), MockMobileOperator)
```
