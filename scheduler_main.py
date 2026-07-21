# -*- coding: utf-8 -*-
"""数据调度独立进程入口（工单5）。

跑「数据组」四个后台调度：新闻聚合 / 每日复盘 / 概念日更 / AI 因子挖掘。
它们只写各自的 DB/文件（news.db、data/review/、data/chains/、ai_research.db），
与 Web 进程零 user_states 耦合，Web 路由读 DB 即可看到最新数据。

与 Web 的配合（拆分模式）：
    Web 进程设 THS_EMBED_SCHEDULER=0 → main.py 只启动交易组（扫描/Vibe）；
    本进程承担数据组。两进程都不设 → 退回单进程全启动（本进程勿重复启动）。

启动方式：
    本机   ：launcher.py 自动拉起（web + scheduler 两进程）
    云端   ：systemd quant-scheduler.service（见 deploy/）
    手动   ：python scheduler_main.py

停止：Ctrl+C / SIGTERM，优雅退出（调度线程均为 daemon，主线程退出即收拢）。
"""
import logging
import os
import signal
import threading

# 与 main.py 一致：清系统代理，防止 akshare 等国内源被本机代理掐断
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy", "SOCKS_PROXY", "socks_proxy"):
    os.environ.pop(_k, None)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [scheduler] %(message)s")
logger = logging.getLogger("scheduler_main")

_stop = threading.Event()


def _on_signal(signum, frame):
    logger.info(f"收到信号 {signum}，退出数据调度进程")
    _stop.set()


def main() -> int:
    import config  # noqa: F401  # 触发 .env 加载（代理清理/交易开关与 Web 同源）
    from core import db_migration
    from core.schedulers import start_data_schedulers
    from pathlib import Path

    # PID 文件：launcher 重启时据此清掉上一个调度进程，避免数据组双跑
    pid_file = Path(__file__).parent / "data" / "scheduler.pid"
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:
        logger.warning(f"写 PID 文件失败（不阻断）: {e}")

    # 幂等建表/迁移（news/review 等模块自建各自的表；users.db 迁移防表缺失）
    try:
        db_migration.migrate_users_db()
    except Exception as e:
        logger.warning(f"启动迁移失败（不阻断）: {e}")

    start_data_schedulers()
    logger.info("数据调度进程就绪（新闻/复盘/概念/AI因子挖掘），Ctrl+C 退出")

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    while not _stop.wait(timeout=5):
        pass
    try:
        pid_file.unlink(missing_ok=True)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    exit_code = main()
    logging.shutdown()
    # concurrent.futures 会在 atexit 阶段等待仍在拉取数据的线程，服务重启会因此卡满 90 秒。
    os._exit(exit_code)
