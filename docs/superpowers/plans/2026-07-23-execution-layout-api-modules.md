# Execution Layout API Modules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把见微 Alpha 的交易执行能力从顶栏和弹窗里拆出来，形成清晰的“交易执行”页面、统一执行状态接口、可维护的前端模块边界。

**Architecture:** 第一阶段只做兼容式重排：旧接口、旧函数、旧弹窗继续可用，新增独立交易执行页复用原 DOM 和函数。第二阶段新增 `/api/execution/state` 聚合策略、手机、电脑、仓位状态，前端逐步从散接口迁移到聚合接口。

**Tech Stack:** Flask 蓝图、原生 HTML/CSS/JS、现有 `api()` fetch 封装、SQLite 用户配置、Android 同花顺桥接、电脑 xiadan OCR。

## Global Constraints

- UI 保持原色风格，不切黑色主题。
- Android APK / USB / WiFi / 蓝牙 / 抓包代理方向全部保留。
- 旧接口继续兼容，先新增聚合接口，再逐步迁移前端。
- 顶栏只显示导航与红绿灯状态，具体操作进入页面或控制台。
- 仓位显示必须同时给出目标单仓、目标比例、实际持仓占比。

---

### Task 1: 页面结构第一阶段

**Files:**
- Modify: `index.html`
- Modify: `static/app/js/app-core.js`

**Interfaces:**
- Consumes: existing `switchPage(page, el)`, `openTodayOrders()`, `refreshMobileFunds()`, `refreshAccountSnapshot()`.
- Produces: top-level page id `page-execution`, nav tab `交易执行`, reusable execution console DOM.

- [ ] **Step 1: 顶栏新增“交易执行”导航**

在 `index.html` 顶栏导航中加入：

```html
<div class="ntab" onclick="switchPage('execution',this);openExecutionPage()">⚡ 交易执行</div>
```

- [ ] **Step 2: 顶栏保留红绿灯，不放具体操作按钮**

保留 `runBadge`、`交易控制台` 入口和用户区；后续把启动/停止按钮移入监控中心主操作区。

- [ ] **Step 3: 新增独立交易执行页**

创建 `page-execution`，复用现有手机/电脑交易控制卡片结构，不删除旧 `todayOrdersModal`。

- [ ] **Step 4: 新增 `openExecutionPage()`**

该函数只刷新交易执行状态：`loadTradeMode()`、`refreshLiveTradeBadge()`、`refreshBridgeProcessBadge()`、`refreshMobileProfileBadge()`、`loadTradeSettings('ts')`。

- [ ] **Step 5: 验证**

运行：

```powershell
node -e "const fs=require('fs'),vm=require('vm'); new vm.Script(fs.readFileSync('static/app/js/app-core.js','utf8')); console.log('JS_SYNTAX_OK')"
```

期望：输出 `JS_SYNTAX_OK`。

---

### Task 2: 统一执行状态接口骨架

**Files:**
- Modify: `web/bridge.py` or existing Flask blueprint owning execution/mobile bridge APIs
- Test: targeted `Invoke-RestMethod http://127.0.0.1:5001/api/execution/state`

**Interfaces:**
- Produces: `GET /api/execution/state`.

- [ ] **Step 1: 新增接口返回兼容聚合状态**

返回结构：

```json
{
  "ok": true,
  "strategy": {"running": true, "auto_execute": true, "active_strategy": "da_ban"},
  "execution": {"broker_type": "paper", "live_trade_enabled": false},
  "mobile": {"bridge_running": true, "connection_mode": "usb", "trade_mode": "simulation", "confirm_mode": "auto"},
  "desktop": {"xiadan_ready": false, "trade_mode": "paper"},
  "position_policy": {"mode": "strategy", "max_positions": 5, "target_ratio": 0.2, "target_amount": 40011.59, "max_order_amount": 50000}
}
```

- [ ] **Step 2: 验证接口可读**

运行：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:5001/api/execution/state' -TimeoutSec 5
```

期望：`ok=true` 且包含 `position_policy.target_amount`。

---

### Task 3: 模块拆分准备

**Files:**
- Modify: `static/app/js/app-core.js`
- Create later: `static/app/js/modules/trade-execution.js`
- Create later: `static/app/js/modules/position-policy.js`

**Interfaces:**
- Produces: front-end functions grouped by responsibility without changing behavior.

- [ ] **Step 1: 只加边界注释，不立即大拆文件**

在 `app-core.js` 中标出：交易执行、仓位策略、手机桥接、电脑交易四段。

- [ ] **Step 2: 后续分文件时保持全局函数名兼容**

继续暴露 `refreshMobileFunds()`、`refreshAccountSnapshot()`、`openTodayOrders()`，避免 HTML onclick 失效。

---

## Self-Review

- 覆盖页面结构优化：Task 1。
- 覆盖接口设计：Task 2。
- 覆盖模块设计：Task 3。
- 保留旧接口和旧弹窗，降低回归面。
- 第一阶段不动真实下单逻辑，只调整结构和状态聚合。
