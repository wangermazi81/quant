# 见微 Alpha — 多用户 A 股量化交易平台

Flask 后端 + 原生 JS 单页前端。功能：策略中心（编写/回测/激活自动扫描）、因子库
（自定义因子 + Alpha101/GTJA191/Qlib158 因子动物园）、模拟盘（paper trading）、
实盘桥接（Windows 本机 GUI 自动化操作 xiadan 下单）、AI 分析与 AI 自主交易、
每日复盘、产业链投研、多用户积分体系与管理后台。

- 本地：`http://127.0.0.1:5001`（launcher.py 启动）
- 云端：`https://jwalpha.top`（阿里云 ECS，gunicorn:5000 + nginx，systemd `quant-platform.service`）

## 目录结构

| 目录 | 职责 |
|---|---|
| `main.py` | Flask app 组装 + 交易/策略/因子等核心路由（蓝图化拆分进行中） |
| `web/` | 路由蓝图（auth/user/admin/pages/market/data/ai/search/notify/feedback/review/research/lhb/ai_trader） |
| `core/` | 用户/积分/公告/反馈/邮件/下单仓位/卖出规则/DB 迁移 |
| `market/` | 行情数据源（TDX/akshare/tushare）、市场统计、新闻 |
| `broker/` | 模拟盘撮合(paper_broker)、实盘 GUI 桥接(ths_operator)、券商接口抽象 |
| `strategy_engine/` | 策略基类/扫描/回测/策略管理（动态加载 `strategies/` 用户策略） |
| `analytics/` | AI 分析/AI 交易/因子管理/因子动物园/复盘引擎/组合优化/龙虎榜 |
| `strategies/` `factors/` | **用户自建**策略/因子文件（gitignore，云端用户各有自己的） |
| `index.html` `admin.html` `static/app/` | 前端：HTML 骨架 + 分文件 JS/CSS 模块（工单7 已拆分，无构建步骤） |
| `deploy/` | gunicorn/nginx/fail2ban 配置、服务器更新脚本、健康监控 |
| `tests/` | pytest 套件（460+ 用例） |
| `tools/` | 桥接器、窗口探测、DB 工具等独立脚本 |

## 快速开始

```powershell
# Windows 本机（解释器固定 D:\Anaconda\python.exe）
pip install -r requirements.txt -r requirements-dev.txt
python launcher.py          # 拉起 main.py 并打开浏览器
```

### 交易模式（重要）

默认**模拟盘**。实盘必须在本目录 `.env` 显式双开关：

```
QUANT_LIVE_TRADING=1        # 实盘总开关
QUANT_AUTO_CONFIRM_ORDER=1  # 委托自动确认（真成交，谨慎）
```

全部变量见 `.env.example`。启动日志会以 WARNING 级打印当前交易模式。

## 开发

```powershell
$env:QUANT_TEST_IMPORT='1'                 # 纯导入模式：不建表/不起调度/不连 xiadan
python -m pytest tests -q                  # 全量测试（约 100 秒）
python _verify_routes.py                   # 路由基线对比（重构必跑，应输出 IDENTICAL）
python -m ruff check .                     # lint
```

- 改后端 `.py` 需重启 5001 进程才生效（**必须以管理员身份重启**，否则旧管理员进程杀不掉）；
  改 `index.html`/`admin.html`/静态资源刷新即生效（服务端实时读盘）。
- 重构约定与工单清单：`../docs/2026-07-10-项目优化问题清单与方案.md`。

## 部署（云端）

```bash
bash /opt/quant/deploy/server_update.sh   # git 拉取 + 秘钥文件权限收紧 + 重启服务
```

CI（GitHub Actions）：ruff + 全量 pytest + 路由基线守卫，push main / PR 触发。
