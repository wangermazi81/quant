# 见微 Alpha 运维手册（RUNBOOK）

> 最后更新：2026-07-31。本机 `D:\同花顺量化交易\ths_auto_trader`（Windows 实盘），
> 云端 `/opt/quant`（阿里云 Ubuntu 24.04，1.6GB 内存，gunicorn + nginx）。

---

## 1. 关键坐标

| 项 | 值 |
|---|---|
| 域名 | `https://jwalpha.top`（规范入口，www 与 IP 均 301 跳此） |
| 服务器 | `root@47.100.109.173`（SSH 密钥登录，密钥 `C:\Users\张新春\.ssh\id_ed25519`） |
| 应用目录 | `/opt/quant`，Python venv `/opt/quant/venv` |
| systemd 服务 | `quant-platform`（gunicorn 单 worker 绑 `127.0.0.1:5000`，nginx 反代 443） |
| 运行用户 | `quant`（非 root）；代码与 venv 为 `root:root` 只读，只有 `data/`、`logs/` 等运行数据由 `quant` 写入 |
| Git 远端 | 本机 `origin`=GitHub、`gitee`=Gitee；服务器的 `origin` 指向 Gitee |

---

## 2. 部署 / 更新（收盘后做，改后端需重启）

```bash
# 本地：提交并推送
git add <files>; git commit -m "..."; git push gitee main; git push origin main

# 本地 PowerShell：从已审查 checkout 计算冻结研究源码 bundle（不要在服务器现算）
$env:QUANT_TEST_IMPORT = "1"
$RELAY_FACTOR_PROTOCOL_BUNDLE_SHA256 = python -c `
  "from analytics.relay_factor_mining import build_current_frozen_protocol_binding as b; print(b()['bundle_sha256'])"
$TARGET_SHA = git rev-parse HEAD
$DEPLOY_TOOL_BUNDLE_SHA256 = python -c `
  "from deploy.bootstrap_server_update import build_local_tool_bundle_sha256 as b; print(b('.'))"
scp deploy/bootstrap_server_update.py `
  "root@47.100.109.173:/root/jwalpha-bootstrap-$TARGET_SHA.py"

# 服务器：先把仓库外信任锚写入私有环境文件
ssh root@47.100.109.173
cd /opt/quant
sed -i "/^RELAY_FACTOR_PROTOCOL_BUNDLE_SHA256=/d" .env
printf '%s\n' \
  "RELAY_FACTOR_PROTOCOL_BUNDLE_SHA256=<上一步本地输出>" >> .env
chown root:quant .env
chmod 640 .env

# 服务器：用本地已审查且哈希锁定的 bootstrap 从 Gitee 安全自举更新
chmod 0700 /root/jwalpha-bootstrap-<TARGET_SHA>.py
/usr/bin/python3 -I /root/jwalpha-bootstrap-<TARGET_SHA>.py \
  --app-dir /opt/quant \
  --remote-url https://gitee.com/wangermazi81/quant.git \
  --branch main \
  --target-sha <TARGET_SHA> \
  --tool-bundle-sha256 <DEPLOY_TOOL_BUNDLE_SHA256>
rm -f /root/jwalpha-bootstrap-<TARGET_SHA>.py
```

> 不要直接从旧 checkout 运行 `deploy/server_update.sh`。调度器锁协议迁移必须先运行远端
> `bootstrap_server_update.py`；它会在更新工作树前停止旧调度线程、验证静默状态，再启动并
> 检查独立的 `quant-scheduler` 服务。
>
> PowerShell 5.1 给远程 bash 传脚本会破坏引号 → 用 `脚本 base64 → ssh host "echo <b64> | base64 -d | bash"`。
> 给 git 传多行提交信息：用文件 + `git commit -F file`（PowerShell here-string 含 `/` 会被沙箱误判）。

### 部署后验证（必查）
```bash
systemctl is-active quant-platform                                  # active
systemctl is-enabled quant-scheduler                                # enabled
systemctl is-active quant-scheduler                                 # active
SCHEDULER_PID=$(systemctl show quant-scheduler -p MainPID --value)
/usr/bin/python3 -I /opt/quant/deploy/check_scheduler_health.py \
  --app-dir /opt/quant --expected-pid "$SCHEDULER_PID"
curl -s -o /dev/null -w "%{http_code}" https://jwalpha.top/ --resolve jwalpha.top:443:127.0.0.1   # 200
journalctl -u quant-scheduler -n 50 --no-pager
tr '\0' '\n' < "/proc/$SCHEDULER_PID/environ" \
  | grep '^RELAY_FACTOR_PROTOCOL_BUNDLE_SHA256='
```

### 回滚
```bash
# 本地创建可审计的反向提交并推送两个远端，然后按上面的 bootstrap 流程重新部署。
git revert <错误提交>
git push origin main
git push gitee main
# nginx 配置回滚：备份在 /root/nginx-backup-*；证书迁移全量备份 /root/nginx-full-backup-*
```

---

## 3. ⚠️ 本地（Windows）安全重启大坑

本机 web 由 `daily_start.ps1` 经任务计划以**管理员/最高权限**启动 → 该进程**普通权限杀不动**
（`taskkill` 报 Access denied）。普通双击 `启动量化平台.bat` 的 kill_port 杀不掉旧进程 → 新进程绑不上 5001、旧代码继续答 404。

**正确重启 = 右键 `D:\同花顺量化交易\启动量化平台.bat` → 以管理员身份运行。**
诊断口诀：`/api/auth/me`=200 但新路由 404 且 5001 进程 taskkill 报 Access denied → 旧管理员进程没换掉。

---

## 4. 环境变量（服务器 `/opt/quant/.env`，systemd 注入）

| 变量 | 当前/默认 | 说明 |
|---|---|---|
| `SECRET_KEY` | 已设随机 | Flask session 密钥；不设则落 `data/.secret_key` |
| `SESSION_COOKIE_SECURE` | `1` | 会话 Cookie 仅 HTTPS + 发 HSTS。**必须 HTTPS 就绪后才设 1**，否则全员登录失效 |
| `CORS_ORIGINS` | 空 | 逗号分隔的跨域来源；空=仅同源 |
| `ENABLE_SWAGGER` | `0` | 生产关 Swagger（设 1 才暴露 /apidocs/） |
| `THS_NO_SCHEDULER` | 未设 | 设 1 禁用后台调度线程（测试/纯导入） |
| `QUANT_TEST_IMPORT` | 未设 | 设 1 让 main.py 可被纯导入（跳过建表/迁移/operator/调度）→ 供测试 |
| `RELAY_FACTOR_PROTOCOL_BUNDLE_SHA256` | 每个已审查 release 必设 | 冻结研究源码 bundle 的仓库外 SHA-256 信任锚；缺失或不匹配时 OOS evidence fail-closed，严禁在服务器 checkout 上自动生成 |
| `SCHEDULER_NOT_READY_GRACE_SECONDS` | `120` | 因子调度线程持续未 ready 的最大宽限；超时后独立调度主进程非零退出并由 systemd 重启 |
| `SCHEDULER_FACTOR_MAX_PROGRESS_AGE_SECONDS` | `3600` | 没有任务运行时，因子调度普通循环允许的最大无进展时长 |
| `SCHEDULER_FACTOR_MAX_JOB_AGE_SECONDS` | `21600` | 数据刷新/挖掘任务的独立最大运行时长；任务运行中不再被普通 1 小时阈值误杀 |
| `BACKTEST_MAX_CODES` | 未设(=800) | 全市场回测 TDX 补充标的上限。**OOM 复发时设 400** 降内存 |
| `LOGIN_MAX_FAILS` / `LOGIN_FAIL_WINDOW` / `LOGIN_LOCK_SECONDS` | 5 / 900 / 900 | 登录防爆破：15 分钟内失败 5 次锁 15 分钟 |
| `APP_BASE_URL` | `https://jwalpha.top` | 找回密码链接根地址 |
| `SMTP_*` | 未配 | 找回密码邮件（当前走后台审批，无需邮件；配了才发邮件自助） |

---

## 5. 测试（改 auth/风控/下单/回测后必跑）

```bash
cd ths_auto_trader
python -m pytest tests/ -q          # 全量，应 ~171 passed / 5 skipped
python -m pytest tests/test_user_manager.py -v   # 单模块
```
- 关键路径已覆盖：认证/积分/找回密码、风控限额止损、限流防爆破、仓位手数护栏、
  打板 Ride 卖出、下单编排集成(execute_buy/sell/do_scan)、回测引擎。
- CI：push main/PR 时 GitHub Actions 自动跑 user_manager/risk/app_security 三个纯模块
  （`.github/workflows/tests.yml`，只装 flask+pytest 轻依赖）。集成测试需 pandas，本地跑。

---

## 6. 安全基线（已落实）

- **ufw**：仅放行 22/80/443，5000 不公网直连。
- **fail2ban**：sshd 监狱，10 分钟失败 5 次封 1 小时。
- **HTTPS**：Let's Encrypt + certbot **自动续期**（每天检查，installer=nginx 自动 reload）。
- **应用层**：限流/登录防爆破/安全响应头(CSP/HSTS/X-Frame)/Cookie 硬化/Swagger 生产关。
- **防薅羊毛**：邀请制注册 + 同 IP 多开去重 + 一次性邮箱拦截 + AI 接口按用户配额。
- **降权**：应用以 `quant` 非 root 运行；`data/` 权限 750。
- **找回密码**：后台审批重置（管理后台「系统管理→找回申请」一键生成临时密码）。

---

## 7. 日志 / 监控

- 应用日志 `/opt/quant/logs/`：`service.log`(stdout) / `service-err.log`(stderr，含 INFO) /
  `access.log` / `error.log`，已配 **logrotate**（日轮转、保 7 天、压缩）。
- **健康告警**：`deploy/health_monitor.py` 经 cron 每 4 分钟检查 gunicorn 响应 + worker 内存，
  掉线/内存超标推送到通知渠道（需在管理后台「通知设置」启用至少一个：企业微信/飞书/钉钉/Telegram）。
  cron：`*/4 * * * * /opt/quant/venv/bin/python3 /opt/quant/deploy/health_monitor.py >> /opt/quant/logs/health_monitor.log 2>&1`

---

## 8. 常见排障

| 症状 | 原因 / 处理 |
|---|---|
| 改后端不生效、新路由 404 | 没重启（云端 `systemctl restart`；本地走第 3 节管理员重启） |
| 云端选股选不出来 | 单 worker 进程态坏死，`access.log` 全 200/50字节 → `systemctl restart quant-platform` |
| 全市场回测把服务搞挂 | OOM。`.env` 设 `BACKTEST_MAX_CODES=400` 重启 |
| HTTPS 突然挂 | 证书问题。`certbot renew --dry-run` 自查；现已自动续期，正常不会发生 |
| 服务器查库 | 没装 sqlite3 CLI，用 `/opt/quant/venv/bin/python3` |
