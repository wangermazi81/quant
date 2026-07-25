# 见微 Alpha 平台体检与瘦身路线 2026-07-26

## 目标

把平台从“功能堆叠可用”推进到“商业项目可维护”：同等功能下减少重复链路、减少前端巨型文件、隔离运行态数据、收紧安全边界、稳定服务器部署。

## 外部参照

- Freqtrade：内置 WebUI、回测、资金管理、策略优化和 dry-run 模式，说明交易平台要把“实盘/模拟/回测”作为统一交易生命周期，而不是散在不同按钮里。参考：https://github.com/freqtrade/freqtrade 和 https://github.com/freqtrade/freqtrade/blob/develop/docs/freq-ui.md
- Hummingbot Dashboard：Dashboard 通过独立 API 管理组合、策略配置、回测和多实例部署，说明 UI 应该调统一 API，不承载执行细节。参考：https://github.com/hummingbot/dashboard 和 https://hummingbot.org/dashboard/
- vn.py WebTrader：Web 服务采用 REST 主动调用 + WebSocket 被动推送，risk_manager 单独统计交易流控、下单数量、活动委托、撤单总数。参考：https://github.com/vnpy/vnpy 和 https://www.vnpy.com/docs/cn/community/app/web_trader.html

## 当前证据

- 前端文件过重：`static/app/js/app-core.js` 约 220KB，`index.html` 约 103KB，`static/app/js/admin-app.js` 约 121KB。
- 内联事件过多：`index.html` 约 210 个 `onclick/onchange/onkeydown/oninput`，`admin.html` 约 70 多个。CSP 因此还要放行 `script-src-attr 'unsafe-inline'`。
- 后端大文件过多：`broker/ths_operator.py` 约 2292 行，`analytics/ai_trader.py` 约 1585 行，`analytics/ai_analyst.py` 约 1563 行，`broker/mobile_ths_operator.py` 约 1537 行，`core/user_manager.py` 约 1485 行。
- 接口入口偏散：`web/admin.py` 约 50 个路由，`web/ai_trader.py` 约 22 个路由，`web/bridge.py` 约 19 个路由，`web/trade.py` 约 16 个路由。
- 手机交易有两套入口：旧 `/api/mobile_ths/*` 与新 `/api/bridge/mobile/*` 并存。兼容入口应保留，但核心执行应收敛到一个服务层，否则容易反复出现“修 A 坏 B”。
- 云端根目录存在历史遗留运行态文件：`users.db*`、`paper_trading.db*`、`ai_config.json`、`search_config.json`、旧散落模块、测试脚本、临时发布脚本。
- 云端体积：`data/` 约 1.1G，`backups/` 约 1.1G，属于需要保留但要按策略轮转的运行数据；根目录散落文件应归档。
- 安全基础已有：Cookie 硬化、CSP、安全响应头、敏感路径探测、IP block、粗粒度 API 限流都在 `app_security.py`。短板是内联事件、部分兼容接口和进程内限流。

## 第一批已动手

- `.gitignore` 补上本地和云端反复出现的运行态/临时产物：`tmp_*`、`.codex-*`、根目录旧数据库、根目录旧配置、旧散落模块、测试脚本、`bridge_stdout.log` 等。
- 新增 `deploy/archive_legacy_runtime.sh`：用于把云端根目录旧运行态文件归档到 `backups/legacy-root-files-<stamp>/`。默认 dry-run，`--apply` 才移动；脚本跳过 Git 已跟踪文件。
- `web/bridge.py` 手机执行入口做第一轮收敛：`/api/mobile_ths/*` 旧入口和 `/api/bridge/mobile/*` 新入口共用同一组手机操作 helper，删掉 `return` 后面的死代码。
- 手机后台任务状态新增结构化 `mobile_task.result`，前端读取手机资金时优先使用结构化结果，不再从 `msg` 字符串里猜账户数据。
- 手机账户读取 worker 不再在后台线程里碰 Flask `session`，用户 ID 在请求线程里提前捕获，减少“查资金/仓位偶发失败”的上下文问题。

## 当前已验证

- `D:\Anaconda\python.exe -m py_compile web\bridge.py`：通过。
- `D:\Anaconda\python.exe -m pytest tests\test_mobile_ths_account_endpoint.py -q`：`4 passed`。
- `D:\Anaconda\python.exe -m pytest tests\test_mobile_ths_operator.py -q`：`26 passed`。
- `node --check static\app\js\app-core.js`：通过。

## 优化路线

### 1. 交易执行链路收敛

- 新建统一服务层：`core/execution_gateway.py`。
- 前端、APK、策略信号统一调用同一组动作：`profile`、`set_connection`、`set_confirm_mode`、`set_trade_mode`、`query_account`、`submit_order`、`cancel_order`、`restart_bridge`。
- `/api/mobile_ths/*` 只做兼容壳，内部转调统一服务层；`/api/bridge/mobile/*` 做主入口。
- 统一返回结构：`ok`、`accepted`、`busy`、`status`、`action`、`task_id`、`msg`、`data`。

### 2. 前端瘦身

- 把 `app-core.js` 按功能拆成：`runtime-state.js`、`execution-console.js`、`market-panel.js`、`strategy-panel.js`、`chart-panel.js`。
- `index.html` 去内联事件，改成 `data-action` + 事件委托。
- 页面只保留结构，行为进 JS，样式进 CSS。目标是先把 `index.html` 压到 60KB 以下，`app-core.js` 压到 120KB 以下。
- CSP 收敛目标：移除 `script-src-attr 'unsafe-inline'`。

### 3. 后端瘦身

- `web/admin.py` 拆成用户、权限、安全、数据源、系统状态四个蓝图。
- `broker/mobile_ths_operator.py` 拆成设备连接、页面定位、交易动作、账户读取四层。
- `broker/ths_operator.py` 拆成窗口定位、快捷键/输入、下单、撤单、查询资金持仓。
- `core/user_manager.py` 拆出积分、权限、密码重置、用户资料，保持原 API 不变。

### 4. 运行态数据治理

- 服务器代码目录只放 Git 跟踪源码和必要运行目录：`data/`、`logs/`、`backups/`、`strategies/`、`factors/`。
- 根目录旧文件统一归档，不直接混在代码目录。
- 备份策略保留：SQLite 在线备份 `deploy/backup_quant_data.py`，`quant-data-backup.timer` 每日运行。
- 后续增加 `deploy/runtime_inventory.sh`，每次部署后输出“新增未跟踪文件分类”。

### 5. 商业化功能补强

- 增加策略生命周期：草稿、回测、模拟观察、实盘启用、暂停、回滚。
- 增加统一资金模型：电脑模拟盘、电脑实盘、手机模拟盘、手机实盘四个账户视图统一字段。
- 增加风控日历：每日开仓次数、撤单次数、单票最大仓位、总仓位、黑名单、冷却期。
- 增加执行审计：每条交易信号从产生、风控、下单、确认、成交/撤单的完整链路记录。
- 增加 WebSocket/SSE 推送：行情、任务状态、桥接器状态、下单结果用被动推送，减少前端轮询。

## 优先级

1. 运行态隔离和云端归档：收益高、改动低。
2. 统一手机/电脑交易执行服务：收益最高，能根治反复坏的问题。
3. 前端去内联事件和文件拆分：改善安全、维护、加载体验。
4. 后端大文件拆分：降低维护成本，但要配测试逐步做。
5. WebSocket/SSE 状态推送：提升体验，减少轮询压力。
