# 同花顺量化自动交易

## 环境要求
- Windows 10/11
- Python 3.8（路径：`D:\软件用\python3.8.10\python.exe`）
- 同花顺普通版已打开并登录交易账户

## 安装
```
pip install pywinauto pyautogui akshare pandas pillow schedule
```

## 文件结构
```
ths_auto_trader/
├── config.py          # 策略参数、资金管理、窗口配置
├── strategy.py        # 均线+量能选股策略
├── ths_operator.py    # 同花顺窗口控制（买入/卖出）
├── main.py            # 主程序（定时轮询）
├── detect_window.py   # 窗口控件探测工具
└── README.md
```

## 快速开始

### 1. 确认配置
编辑 `config.py`：
- `DRY_RUN = True`   → 演练模式（推荐先用这个测试）
- `DRY_RUN = False`  → 实盘模式（谨慎！）
- `STOCK_POOL`       → 自定义股票池，留空则自动扫描

### 2. 运行主程序
```
D:\软件用\python3.8.10\python.exe main.py
```

### 3. 关键快捷键
- `F2` → 同花顺买入对话框
- `F3` → 同花顺卖出对话框
- `Ctrl+C` → 停止程序

## 策略说明
| 信号 | 条件 |
|------|------|
| 买入 | MA5 上穿 MA20（金叉）且当日成交量 > 20日均量 × 1.5 |
| 卖出（止损）| 浮亏 ≥ 5% |
| 卖出（止盈）| 浮盈 ≥ 10% |
| 卖出（趋势）| MA5 下穿 MA20（死叉）|

## 重要提示
1. 实盘前务必在 `DRY_RUN=True` 下完整测试
2. 同花顺窗口必须保持前台可见（不要最小化）
3. 买卖对话框识别依赖标题关键字，不同券商版本可能需要调整
4. 程序不记录持仓（重启清零），长期使用需对接数据库
5. 本程序仅供学习研究，不构成投资建议，市场有风险

## 调试工具
```
# 探测同花顺窗口控件结构
D:\软件用\python3.8.10\python.exe detect_window.py

# 探测买卖对话框控件
D:\软件用\python3.8.10\python.exe detect_trade_dialog.py
```
