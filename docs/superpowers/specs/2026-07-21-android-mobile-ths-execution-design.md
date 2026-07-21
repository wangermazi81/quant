# Android 手机同花顺实盘执行器设计

## 决策

在现有实盘桥接链路中新增 Android 执行器，通过 USB、ADB 和
`uiautomator2` 操作手机同花顺。首版覆盖买入、卖出、资金查询和持仓查询，
同时保留现有 Windows `xiadan` 执行器。

手机在交易时段保持常亮、已解锁、同花顺已登录并位于前台。委托确认支持
`manual` 和 `auto` 两种模式，使用配置切换。

## 范围

首版实现：

- Android USB 设备连接、授权和运行前检查。
- 同花顺买入、卖出、可用资金、总资产和持仓查询。
- 下单字段回读核对、手动/自动确认、持仓变化回查。
- 截图、页面 XML、结构化日志和桥接结果回传。

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

Android 接入只替换本地执行器，不修改策略生成和云端信号协议。

### `broker/mobile_ths_operator.py`

新增 `MobileTHSOperator`：

```python
connect() -> dict
buy(code: str, price: float, amount: int) -> dict
sell(code: str, price: float, amount: int) -> dict
query_account_snapshot(max_rows: int = 50) -> dict
```

买卖返回与现有 `THSOperator` 兼容的 `status`、`msg`、`screenshot`，并增加
`device_serial`。

### `tools/ths_bridge.py`

读取 `executor_type`：

- `desktop`：实例化现有 `THSOperator`。
- `android`：实例化 `MobileTHSOperator`。

信号轮询、登录、结果上报和重试继续复用现有实现。

### `tools/android_probe.py`

校准工具负责列出已授权设备、记录屏幕尺寸和 Android 版本、获取前台包名和
Activity、导出截图/UI XML、检查关键控件并生成定位配置。

### `tools/mobile_ths_profile.json`

保存实际设备序列号、包名、确认模式和页面选择器。设备信息由校准工具写入，
不在代码中硬编码。

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

`auto` 模式点击最终确认。识别到委托受理后查询持仓变化：买入检查目标股数增加，
卖出检查目标股数减少；确认变化回传 `executed`，已受理但持仓尚未变化回传
`submitted`，明确错误提示回传 `failed`。

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
    'screenshot': '截图路径'
}
```

优先读取 UI 控件树；持仓表数据不完整时使用截图 OCR，并保留原始截图和识别文本。
股票代码和数值字段执行严格格式校验，名称只用于展示。

## 串行和防重复

- 执行器使用单个可重入锁，买卖和账户查询同一时间只运行一个。
- 桥接器处理一个信号期间不启动下一个手机操作。
- 每次提交前检查当前页面和最近执行记录，拦截重复点击。
- 自动确认后以持仓变化作为成交依据，不以点击成功代表成交。

## 配置

`tools/bridge_config.json` 增加：

```python
{
    'executor_type': 'android',
    'android_profile': 'tools/mobile_ths_profile.json',
    'confirm_mode': 'manual',
    'device_timeout_sec': 10,
    'page_timeout_sec': 8,
    'reconcile_timeout_sec': 20
}
```

初始使用 `manual`。买入、卖出、资金和持仓四项校准通过后，只把配置切到
`auto`，交易流程代码保持不变。

## 诊断产物

每次执行按信号 ID 和时间建立目录，保存设备状态、交易页截图/XML、字段回读值、
确认页截图、最终结果和资金持仓解析结果。日志记录设备、信号、阶段、定位方式和
耗时，不记录交易密码。

## 测试和验收

- 单元测试覆盖配置解析、字段归一化、数值解析、状态映射和失败分支。
- 桥接器测试覆盖 `desktop/android` 选择及结果回传。
- 手动模式分别运行一笔买入表单和卖出表单，只到确认页。
- 资金数值与手机显示一致，持仓代码和股数逐项一致。
- 自动模式确认委托受理后按持仓变化更新状态。
- USB 重连后能重新识别配置中的同一设备。
- 页面定位失败时保留截图/XML并记录明确失败阶段。
- 现有 Windows `THSOperator` 行为保持不变。

## 实施顺序

1. 安装 ADB 和 Python 依赖，完成 USB 授权。
2. 实现设备探测和页面校准工具。
3. 采集买入、卖出、资金、持仓四个页面。
4. 实现 Android 连接、页面定位和手动确认模式。
5. 实现资金持仓查询及成交回查。
6. 接入桥接器执行器选择并完成真机验收。
7. 将确认模式从 `manual` 切换为 `auto`。
