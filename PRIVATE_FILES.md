# 隐私文件清单

本文档列出本项目所有**敏感 / 私有 / 不应公开提交**的文件，便于：
- 上传到云端服务器前确认覆盖范围
- 排查泄漏风险
- 配置 `.gitignore` 规则

> 所有这些文件都已经在 [.gitignore](.gitignore) 中排除，不会进入 git 历史。

---

## 一、敏感配置（绝对私密）

| 文件 | 用途 | 风险等级 |
| --- | --- | --- |
| `.env` | Flask SECRET_KEY、AI API Key 等环境变量（如已创建） | 🔴 极高 |
| `data/ai_config.json` | AI 服务商配置 + API Key（明文） | 🔴 极高 |
| `secrets.json` | 通用密钥配置（如有） | 🔴 极高 |
| `api_keys.json` | 第三方 API Key（如有） | 🔴 极高 |

## 二、用户与账户数据库

| 文件 | 用途 | 风险等级 |
| --- | --- | --- |
| `data/users.db` | 用户账户表（密码哈希、积分、注册信息） | 🔴 极高 |
| `data/users.db-wal` | SQLite WAL 日志 | 🔴 极高 |
| `data/users.db-shm` | SQLite 共享内存 | 🔴 极高 |

## 三、模拟盘运行状态

| 文件 | 用途 | 风险等级 |
| --- | --- | --- |
| `data/paper_trading.db` | 模拟盘订单、持仓、资金状态 | 🟡 中（含交易记录） |
| `data/paper_trading.db-wal` | SQLite WAL 日志 | 🟡 中 |
| `data/paper_trading.db-shm` | SQLite 共享内存 | 🟡 中 |

## 四、个性化业务配置

| 文件 | 用途 | 风险等级 |
| --- | --- | --- |
| `data/search_config.json` | 搜索引擎 / 资讯接口配置（可能含 token） | 🟡 中 |
| `data/notify_config.json` | 企业微信 / 飞书 / 钉钉 webhook（如有） | 🟡 中 |

## 五、行情缓存（大文件，但非机密）

| 文件 | 用途 | 大小 |
| --- | --- | --- |
| `data/daily/*.csv` | 历史日线缓存 | 视股票数量而定 |
| `data/stock_names.json` | 全市场代码-名称映射 | ~150 KB |
| `data/metadata.json` | 数据缓存元信息 | ~1 KB |

> 这些文件不机密，但体积大、容易重新生成，**不建议**上传到公网仓库；上云备份时建议附带，避免冷启动重新爬取。

## 六、归档（不再使用）

| 文件 | 状态 |
| --- | --- |
| `_archive/users_merged.db` | 历史数据库合并临时文件 |
| `_archive/users_server.db` | 服务器导回的旧用户库 |
| `_archive/deploy.zip` | 旧部署打包文件 |

> 这些文件**不参与运行**，仅供回溯。如确定不需要可手动删除。

---

## 一键打包敏感文件

执行根目录脚本：

```powershell
.\bundle_private.ps1
```

会在项目根目录生成 `_private_bundle_YYYYMMDD-HHmm.zip`，包含上述 1-4 类所有文件。
直接把这个 zip 上传到云服务器，解压到 `ths_auto_trader/` 即可恢复完整隐私状态。
