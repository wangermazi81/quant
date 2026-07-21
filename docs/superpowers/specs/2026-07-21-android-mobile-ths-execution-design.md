# Android 手机同花顺实盘执行器设计

## 决策

在现有实盘桥接链路中新增 Android 混合执行器。买入、卖出通过 USB、ADB 和
`uiautomator2` 操作手机同花顺；可用资金、总资产、持仓和委托状态优先解析交易
APP 的接口响应。同时保留现有 Windows `xiadan` 执行器。

手机在交易时段保持常亮、已解锁、同花顺已登录并位于前台。委托确认支持
`manual` 和 `auto` 两种模式，使用配置切换。

## 范围

首版实现：

- Android USB 设备连接、授权和运行前检查。
- 同花顺买入、卖出 UI 自动操作。
- 账户资金、持仓列表和委托状态接口探测、解析及会话更新。
- 下单字段回读核对、手动/自动确认、接口成交回查。
- 抓包摘要、截图、页面 XML、结构化日志和桥接结果回传。

首版不包含锁屏解锁、账号登录、短信验证码、生物识别、当日委托、自动撤单、
多手机并行和 iOS。

## 架构

现有链路保持不变：

```text
策略/AI
  -> core/trade_service.py 写入实盘信号
  -> tools/ths_bridge.py 轮询信号
  -> desktop 或 android 执行器
  -> 回传 submitted / executed / failed
```

Android 接入只替换本地执行器，不修改策略生成和云端信号协议。UI 执行器只负责
交易页面和下单确认；只读接口适配器负责账户快照和成交核对，两者不互相复制职责。

### `broker/mobile_ths_operator.py`

新增 `MobileTHSOperator`：

```python
connect() -> dict
buy(code: str, price: float, amount: int) -> dict
sell(code: str, price: float, amount: int) -> dict
query_account_snapshot(max_rows: int = 50) -> dict
```

买卖返回与现有 `THSOperator` 兼容的 `status`、`msg`、`screenshot`，并增加
`device_serial`。`query_account_snapshot()` 委托给 `MobileTHSApi`；当接口会话失效
或响应结构不匹配时返回明确失败原因，不自动切换 OCR。

### `broker/mobile_ths_api.py`

新增 `MobileTHSApi`，只读取已识别的交易 APP 接口：

```python
connect() -> dict
get_account() -> dict
get_positions(max_rows: int = 50) -> list[dict]
get_order_status(order_ref: str | None = None, code: str | None = None) -> dict
get_account_snapshot(max_rows: int = 50) -> dict
```

适配器从配置读取端点特征、请求方法、必要请求头、字段路径和解码器名称。动态会话
数据从最近一次抓包解析结果加载，过期后通过手机 APP 正常刷新页面获取新会话，
不把 Cookie、令牌或签名值写死在代码和仓库配置中。

响应解码器首版支持 JSON；探测结果明确为 Protobuf 时增加对应 schema/字段映射；
WebSocket 则按消息方向和事件类型解析。仅捕获到不可解释的加密载荷时，先在 APP
进程内记录加密前响应对象，再实现同一标准数据模型的解码器。

### `tools/ths_bridge.py`

读取 `executor_type`：

- `desktop`：实例化现有 `THSOperator`。
- `android`：实例化 `MobileTHSOperator`。

信号轮询、登录、结果上报和重试继续复用现有实现。

### `tools/android_probe.py`

校准工具负责列出已授权设备、记录屏幕尺寸和 Android 版本、获取前台包名和
Activity、导出截图/UI XML、检查关键控件并生成定位配置。

### `tools/android_capture_probe.py`

抓包探测工具负责导入 PCAPdroid/mitmproxy 导出的会话，按主机、路径、方法和内容
类型筛选“资产页刷新”“持仓页刷新”“委托查询”产生的流量，脱敏后保存接口特征和
样例响应。探测输出固定区分 `json`、`protobuf`、`websocket`、`encrypted_payload`
和 `tls_pinning`，供后续选择解析路线。

### `tools/mobile_ths_profile.json`

保存实际设备序列号、包名、确认模式和页面选择器。设备信息由校准工具写入，
不在代码中硬编码。

### `tools/mobile_ths_api_profile.json`

保存账户、持仓和委托查询的接口特征、字段映射、解码器及会话缓存位置。配置只保留
稳定结构，不保存账户密码、完整 Cookie、访问令牌和设备签名值。

## 页面定位

定位优先级固定为：资源 ID、控件文本/描述、页面层级关系、局部截图模板、归一化
相对坐标。点击交易按钮前先验证当前页面唯一标志；填写后从控件回读代码、价格、
数量并逐项比较。任一字段不一致，返回 `failed`，不进入确认步骤。

## 买卖状态机

```text
设备检查
  -> 确认屏幕亮起、已解锁、同花顺在前台
  -> 进入交易页和买入/卖出页
  -> 填写代码、价格、数量
  -> 回读核对三个字段
  -> 点击委托按钮
  -> 核对确认页方向、代码、价格、数量
  -> 按 confirm_mode 处理
  -> 保存截图/XML并回传状态
```

`manual` 模式停在核对完成的委托确认页，不点击最终确认，回传 `submitted`。

`auto` 模式点击最终确认。识别到委托受理后先调用 `get_order_status()`；接口明确
返回全部成交时回传 `executed`，已报或部分成交时回传 `submitted`，废单或拒绝时
回传 `failed`。委托接口暂时无结果时，再比较提交前后的持仓数量；页面提示只作为
最后兜底，不以点击成功代表成交。

## 资金和持仓

`query_account_snapshot()` 返回：

```python
{
    'ok': True,
    'available': 0.0,
    'total_asset': 0.0,
    'positions': [{
        'code': '000001',
        'name': '股票名称',
        'shares': 100,
        'available_shares': 100,
        'cost_price': 10.0,
        'cur_price': 10.1
    }],
    'source': 'app_api',
    'captured_at': '2026-07-21T10:30:00+08:00'
}
```

资金和持仓只从已识别接口响应读取，JSON/Protobuf/WebSocket 最终都归一化为上述
结构。股票代码、股数、价格和资产字段执行严格格式校验，名称只用于展示。接口
会话过期时让手机 APP 进入对应页面并刷新一次，再从新捕获的会话更新动态请求参数。

OCR 不属于首版账户数据主链路。诊断截图仅用于确认 APP 当前页面和接口刷新动作，
不参与资金、持仓数值计算。

## 抓包和协议识别

探测顺序固定为：

1. 使用 PCAPdroid 只采集交易 APP 流量，分别执行一次资产页刷新、持仓页刷新和
   委托查询，记录时间窗口和页面动作。
2. 导出 PCAP 或 SSL 日志到电脑，用 mitmproxy 按时间窗口、主机和内容类型定位
   请求与响应。
3. 普通 JSON 直接建立端点特征和字段映射；Protobuf 保存消息样本并建立 schema；
   WebSocket 按事件类型和请求序列配对。
4. 遇到证书固定时，在 APP 进程内观察 OkHttp、Cronet、WebView 或原生 TLS 的明文
   请求和响应；遇到应用层加密时，在解密函数返回点记录对象或字节数组。
5. 用同一账户页面连续刷新两次，区分稳定字段与动态令牌、时间戳、nonce、签名；
   只把稳定特征写入配置。

首版只复用资金、持仓、委托查询三个只读接口。买卖请求仍由 UI 执行，避免把未验证
的委托签名和风控字段引入实盘链路。

## 串行和防重复

- 执行器使用单个可重入锁，UI 买卖和会话刷新同一时间只运行一个。
- 桥接器处理一个信号期间不启动下一个手机操作。
- 每次提交前检查当前页面和最近执行记录，拦截重复点击。
- 自动确认后以委托接口为第一成交依据，以持仓变化为第二依据。

## 配置

`tools/bridge_config.json` 增加：

```python
{
    'executor_type': 'android',
    'android_profile': 'tools/mobile_ths_profile.json',
    'android_api_profile': 'tools/mobile_ths_api_profile.json',
    'confirm_mode': 'manual',
    'device_timeout_sec': 10,
    'page_timeout_sec': 8,
    'reconcile_timeout_sec': 20,
    'api_session_max_age_sec': 300
}
```

初始使用 `manual`。买入、卖出页面校准及资金、持仓、委托接口解析通过后，只把
配置切到 `auto`，交易流程代码保持不变。

## 诊断产物

每次执行按信号 ID 和时间建立目录，保存设备状态、交易页截图/XML、字段回读值、
确认页截图、接口端点摘要、响应类型、字段映射结果和最终状态。原始抓包和动态会话
数据保存在本机诊断目录，不提交到 Git。日志记录设备、信号、阶段、定位方式和耗时，
不记录交易密码、完整 Cookie、访问令牌和签名值。

## 测试和验收

- 单元测试覆盖配置解析、JSON/Protobuf 字段归一化、数值解析、状态映射、会话过期
  和失败分支。
- 桥接器测试覆盖 `desktop/android` 选择及结果回传。
- 手动模式分别运行一笔买入表单和卖出表单，只到确认页。
- 资金接口数值与手机显示一致，持仓代码和股数逐项一致。
- 自动模式确认委托受理后优先按委托接口更新状态，接口无结果时按持仓变化回查。
- 会话过期后触发 APP 页面刷新并恢复只读查询。
- USB 重连后能重新识别配置中的同一设备。
- 页面定位失败时保留截图/XML并记录明确失败阶段。
- 现有 Windows `THSOperator` 行为保持不变。

## 实施顺序

1. 安装 ADB 和 Python 依赖，完成 USB 授权。
2. 实现设备探测和买入、卖出页面校准工具。
3. 用 PCAPdroid/mitmproxy 采集资产、持仓和委托查询流量并完成协议分类。
4. 实现只读接口适配器、字段归一化和会话更新。
5. 实现 Android UI 连接、页面定位和手动确认模式。
6. 接入委托接口优先、持仓变化兜底的成交回查。
7. 接入桥接器执行器选择并完成真机验收。
8. 将确认模式从 `manual` 切换为 `auto`。
