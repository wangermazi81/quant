import multiprocessing

# 仅监听本地回环：nginx 在同机通过 127.0.0.1 反代，外部一律走 nginx(80/443)。
# 即便 ufw 失效，5000 也不会暴露到公网（防御纵深）。
bind = "127.0.0.1:5000"
# ⚠️ 必须单 worker：运行态(user_states/SSE 订阅)与扫描调度器都在进程内内存，
#    多 worker 会各持一份互不相通的状态。等状态外置到 Redis/DB 后才能调大。
workers = 1
worker_class = "gthread"
threads = 8                    # SSE 长连接不能独占唯一请求通道；仍保持单进程共享内存状态
timeout = 300                  # AI 分析最多等 5 分钟
keepalive = 5
# ⚠️ 必须关闭预加载：后台调度线程在 main 模块导入时启动，
#    若 preload 则线程只活在 master，fork 后 worker 里不存在 → 自动扫描失效。
preload_app = False

# 日志
accesslog = "logs/access.log"
errorlog  = "logs/error.log"
loglevel  = "info"
