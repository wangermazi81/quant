# 异常处理策略（工单6，2026-07-11）

历史教训：“云端选股返回 0 但没有任何报错”一类事故的温床就是静默吞错。
本项目按代码所在层级执行三档规则，ruff E722 已转正（禁止裸 `except:`）。

## 三档规则

| 档 | 范围 | 规则 |
|---|---|---|
| ① 资金/交易关键路径 | core/trade_service、broker/paper_broker、core/user_manager 写路径、web/trade、web/bridge、web/paper、core/daban_state、core/bridge_state | **禁止 `except Exception: pass`**。捕获必须 `logger.warning/error` 带上下文（uid/code/金额/表名），或上抛。凡是「状态没写上/没清掉会引发后续错误决策」的点（持仓峰值、台账、订单流水、用户设置）一律 warning 级 |
| ② 数据源降级链 | market/、analytics/data_manager、tushare_provider、search_manager 等 | 保留 try/except 降级结构（降级是特性），但每处补 `logger.warning("[数据源名] 参数: %s", e)`；纯显示层回退（实时价拉不到按成本价显示）可用 debug 级 |
| ③ GUI 自动化 | broker/ths_operator.py、tools/ths_bridge.py | **不改控制流**（重试/回退路径是实盘按键安全的一部分）。静默 pass 逐步补 logger.debug，绝不上抛新异常 |

## 允许保留静默 pass 的场景（须加注释说明理由）

1. **双重故障兜底**：已在 except 处理器内部的清理代码（如 scan_service
   崩溃处理器里的进度条复位）——再抛会掩盖原始异常。
2. **纯展示层可选增强**：失败后有明确的、无害的默认展示（如 OCR 持仓缓存
   写入失败——下轮扫描仍有 DB 去重兜底）。
3. **不可能失败的防御**（如对 list 做推导式的 try 包裹）——历史遗留，
   逐步清理，新代码不允许。

## 新代码检查清单

- [ ] 没有裸 `except:`（ruff E722 会挡）
- [ ] `except Exception` 后要么 log 带上下文、要么上抛、要么符合上面三个豁免场景之一并注释
- [ ] 涉及钱/持仓/信号/用户数据的失败 → `logger.warning` 起步
- [ ] 循环体内的 log 用 debug 级（防刷屏），聚合性失败在循环外 warning
