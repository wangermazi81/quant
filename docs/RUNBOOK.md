# 见微 Alpha 运维手册（RUNBOOK）

> 最后更新：2026-06-14。本机 `D:\同花顺量化交易\ths_auto_trader`（Windows 实盘），
> 云端 `/opt/quant`（阿里云 Ubuntu 24.04，1.6GB 内存，gunicorn + nginx）。

---

## 1. 关键坐标

| 项 | 值 |
|---|---|
| 域名 | `https://jwalpha.top`（规范入口，www 与 IP 均 301 跳此） |
| 服务器 | `root@47.100.109.173`（SSH 密钥登录，密钥 `C:\Users\张新春\.ssh\id_ed25519`） |
| 应用目录 | `/opt/quant`，Python venv `/opt/quant/venv` |
| systemd 服务 | `quant-platform`（gunicorn 单 worker 绑 `127.0.0.1:5000`，nginx 反代 443） |
| 运行用户 | `quant`（非 root），数据库/日志/代码均 `quant:quant` |
| Git 远端 | `origin`=gitee(SSH)、github 备份；服务器从 gitee pull |

---

## 2. 部署 / 更新（收盘后做，改后端需重启）

```bash
# 本地：提交并推送
git add <files>; git commit -m "..."; git push gitee main; git push origin main

# 服务器：拉取 + 重启
ssh root@47.100.109.173
cd /opt/quant
git pull --ff-only origin main      # 注意：用这条，别用 git fetch --all（历史遗留远端会中止）
chown -R quant:quant /opt/quant     # 拉取后归还属主（root pull 会改属主）
systemctl restart quant-platform
```

> PowerShell 5.1 给远程 bash 传脚本会破坏引号 → 用 `脚本 base64 → ssh host "echo <b64> | base64 -d | bash"`。
> 给 git 传多行提交信息：用文件 + `git commit -F file`（PowerShell here-string 含 `/` 会被沙箱误判）。

### 部署后验证（必查）
```bash
systemctl is-active quant-platform                                  # active
curl -s -o /dev/null -w "%{http_code}" https://jwalpha.top/ --resolve jwalpha.top:443:127.0.0.1   # 200
grep "后台调度已启动" /opt/quant/logs/service-err.log | tail -1      # 证明调度器启动(gunicorn INFO 走 stderr)
MASTER=$(systemctl show -p MainPID --value quant-platform); for W in $(pgrep -P $MASTER); do ls /proc/$W/task | wc -l; done  # worker 线程 >1 = 调度线程在跑
```

### 回滚
```bash
cd /opt/quant && git reset --hard <上一个好的 commit> && chown -R quant:quant /opt/quant && systemctl restart quant-platform
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
