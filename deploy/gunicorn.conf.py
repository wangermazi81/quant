import multiprocessing

bind = "0.0.0.0:5000"
# ⚠️ 必须单 worker：运行态(user_states/SSE 订阅)与扫描调度器都在进程内内存，
#    多 worker 会各持一份互不相通的状态。等状态外置到 Redis/DB 后才能调大。
workers = 1
worker_class = "sync"
timeout = 300                  # AI 分析最多等 5 分钟
keepalive = 5
# ⚠️ 必须关闭预加载：后台调度线程在 main 模块导入时启动，
#    若 preload 则线程只活在 master，fork 后 worker 里不存在 → 自动扫描失效。
preload_app = False

# 日志
accesslog = "logs/access.log"
errorlog  = "logs/error.log"
loglevel  = "info"
