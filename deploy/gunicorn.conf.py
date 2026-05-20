import multiprocessing

bind = "0.0.0.0:5000"
workers = 2                    # 轻量服务器用 2 个 worker 即可
worker_class = "sync"
timeout = 300                  # AI 分析最多等 5 分钟
keepalive = 5
preload_app = True

# 日志
accesslog = "logs/access.log"
errorlog  = "logs/error.log"
loglevel  = "info"
