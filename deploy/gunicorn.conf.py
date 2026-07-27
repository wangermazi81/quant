
# 仅监听本地回环：nginx 在同机通过 127.0.0.1 反代，外部一律走 nginx(80/443)。
# 即便 ufw 失效，5000 也不会暴露到公网（防御纵深）。
bind = "127.0.0.1:5000"
# ⚠️ 必须单 worker：运行态(user_states/SSE 订阅)与交易组调度器（扫描/Vibe）
#    仍在进程内内存，多 worker 会各持一份互不相通的状态。
#    工单5 后数据组调度（新闻/复盘/概念/AI因子挖掘）可移到独立进程
#    quant-scheduler.service（Web 服务加 Environment="THS_EMBED_SCHEDULER=0"），
#    进一步等状态外置到 Redis/DB 后才能调大 workers。
workers = 1
worker_class = "gthread"
# ⚠️ 8 个线程曾是生产事故的直接原因：两条 SSE 流（/api/paper/stream、
#    /api/ai_trader/stream）在 gthread 下**每个连接永久占一个线程**，一个浏览器
#    标签就吃 2 个；再叠加同步全市场选股/同步回测，3-4 个标签就能把线程池占满 →
#    后续所有请求（含 /api/health）排队到 300s 超时，外部表现为
#    access.log 全 200/50 字节、「选股返回 0」、必须重启进程。
#    现在三管齐下：① 线程数提到 32；② SSE 限额 + 最长存活（web/_concurrency.py，
#    到期主动收摊由浏览器自动重连）；③ 同步重路由限并发、超量自动转异步。
#    线程本身几乎不占内存，systemd 的 MemoryMax=512M 仍是主要约束。
threads = 32
timeout = 300                  # AI 分析最多等 5 分钟
keepalive = 5
# ⚠️ 必须关闭预加载：后台调度线程在 main 模块导入时启动，
#    若 preload 则线程只活在 master，fork 后 worker 里不存在 → 自动扫描失效。
preload_app = False

# Gunicorn 26 默认创建控制套接字；本服务未使用该接口，关闭后保留 systemd ProtectHome 隔离。
control_socket_disable = True

# 日志
accesslog = "logs/access.log"
errorlog  = "logs/error.log"
loglevel  = "info"
