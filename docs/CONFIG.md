# 配置项清单（工单8，2026-07-11）

配置分三类来源，查配置先看这里。`.env` 在 `config.py` 导入时加载
（不覆盖已有环境变量），全部变量样例见 `.env.example`。

## 一、交易与运行开关（环境变量 / .env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `QUANT_LIVE_TRADING` | 0 | **实盘总开关**。不设=模拟盘（DRY_RUN=True），绝不触碰真实资金 |
| `QUANT_AUTO_CONFIRM_ORDER` | 0 | 委托自动按 Y 确认（真成交）。双闸：需与实盘开关同时=1 |
| `QUANT_AUTO_CANCEL_UNFILLED` | 0 | 未成交自动撤单。双闸同上 |
| `QUANT_MAX_ORDER_AMOUNT` | 50000 | 单笔买入金额上限（元），broker 层硬拒 |
| `THS_EMBED_SCHEDULER` | 1 | =0 拆分模式：数据组调度由 scheduler_main.py 独立进程承担 |
| `THS_NO_SCHEDULER` | — | =1 完全不起调度（测试/工具场景） |
| `QUANT_TEST_IMPORT` | — | =1 纯导入模式：不建表/不迁移/不连 xiadan（pytest 用） |
| `BACKTEST_MAX_CODES` | 800 | 全市场回测最大标的数（小内存机器可调低） |

## 二、Web/安全/邮件/AI（环境变量 / .env）

| 变量 | 说明 |
|---|---|
| `SECRET_KEY` | Flask session 密钥；不设则持久化到 data/.secret_key |
| `ADMIN_USERNAME` / `ADMIN_EMAIL` / `ADMIN_PASSWORD` | 首次启动默认管理员 |
| `JW_ROOT_ADMIN_USERNAME` | root 管理员用户名（权限体系顶点） |
| `CORS_ORIGINS` | 允许跨域来源（逗号分隔），不设=仅同源 |
| `SESSION_COOKIE_SECURE` / `SESSION_DAYS` | HTTPS Cookie / 会话有效期（天） |
| `LOGIN_MAX_FAILS` / `LOGIN_FAIL_WINDOW` / `LOGIN_LOCK_SECONDS` | 登录防爆破 |
| `ROOT_LOGIN_MAX_FAILS` / `ROOT_LOGIN_FAIL_WINDOW` / `ROOT_LOGIN_LOCK_SECONDS` | root 账号更严的防爆破 |
| `ANTI_CRAWL_ENABLED` / `ANTI_CRAWL_API_MAX_PER_MIN` / `ANTI_CRAWL_AUTH_FAIL_MAX` / `ANTI_CRAWL_SENSITIVE_MAX` / `ANTI_CRAWL_BLOCK_SECONDS` | 反爬限流（app_security） |
| `MAX_BONUS_ACCOUNTS_PER_IP` | 防薅羊毛：同 IP 送分账号上限（默认 3） |
| `ENABLE_SWAGGER` | =1 开 /apidocs/（生产默认关） |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM/FROM_NAME/SSL` + `APP_BASE_URL` | 找回密码邮件 |
| `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` / `AI_PROVIDER` | 全局 AI 配置（优先级高于网页设置）；另有 `OPENAI_API_KEY`/`DEEPSEEK_API_KEY`/`ANTHROPIC_API_KEY` 按 provider 兜底 |
| `TUSHARE_TOKEN` / `TUSHARE_REPLAY_API_KEY` / `TUSHARE_REPLAY_BASE_URL` | Tushare 数据源 |
| `KLINE_INTRADAY_CACHE_TTL` / `KLINE_ORDER_BOOK_CACHE_TTL` | 分时/盘口缓存秒数（默认 45/5） |
| `HEALTH_URL` / `HEALTH_HOST` / `HEALTH_MEM_ALERT_MB` | deploy/health_monitor.py 健康巡检 |
| `SECURITY_DB` | 安全事件库路径覆盖（app_security） |

## 三、运行时 JSON 配置（data/，网页后台可改，重启保留）

| 文件 | 读取模块 | 内容 |
|---|---|---|
| `data/ai_config.json` | analytics/ai_analyst | 全局 AI 接口（管理员在后台配置） |
| `data/ai_trader_config.json` | analytics/ai_trader | AI 自主交易的独立模型配置 |
| `data/ai_trader/{uid}.json` | analytics/ai_trader | 每用户 AI 交易设置 |
| `data/trade_settings.json` | broker/ths_operator | 实盘自动确认/自动撤单运行时开关 |
| `data/bridge_flags.json` | core/bridge_state | 实盘下单总闸（每用户） |
| `data/daban_peaks.json` | core/daban_state | 模拟盘持仓峰值（Ride 追踪止盈） |
| `data/search_config.json` | analytics/search_manager | 搜索/资讯源配置 |
| `data/datasource_config.json` | analytics/datasource_config、data_manager | 行情数据源选择（tushare/uqer 等） |
| `data/point_costs.json` | core/user_manager、web/admin | 积分价目（后台「积分设置」页） |
| `data/notify_config.json` | core/notification_manager、deploy/health_monitor | 推送通知配置 |

## 四、config.py 内的策略默认参数

`MARKET_FILTER`（全市场预过滤）、`STRATEGY`（均线/量能）、`RISK`（账户风控）、
`INITIAL_CAPITAL`/`POSITION_RATIO`/`MAX_POSITIONS`/`SINGLE_POSITION_MAX_RATIO`
（资金与仓位护栏）、`DABAN_*`（打板 Ride 卖出）、`PAPER_*`（模拟盘费率）、
`THS_*`（xiadan 窗口/快捷键校准）、`SCREENSHOT_*`（截图保留策略）。
均为策略/业务默认值，用户级设置在网页面板改（持久化到 users.db）。
