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
import time

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


def _supervise_factor_scheduler(
    factor_scheduler,
    *,
    data_runtime=None,
    stop_event=_stop,
    interval_seconds: int = 5,
    grace_seconds: int = 120,
    max_factor_progress_age_seconds: int = 3600,
    max_factor_job_age_seconds: int = 21600,
    max_data_progress_age_seconds: int = 900,
    monotonic=time.monotonic,
) -> int:
    not_ready_since: float | None = None
    while not stop_event.wait(max(0, interval_seconds)):
        runtime = factor_scheduler.refresh_runtime_heartbeat()
        now = monotonic()
        if runtime.get("thread_alive") is not True:
            logger.error("AI 因子调度线程已退出，终止主进程以触发重启")
            return 1
        if runtime.get("job_running") is True:
            factor_job_age = runtime.get("job_age_seconds")
            if (
                not isinstance(factor_job_age, (int, float))
                or isinstance(factor_job_age, bool)
                or factor_job_age
                > max(1, max_factor_job_age_seconds)
            ):
                logger.error(
                    "AI 因子任务运行超时或缺少起始时间，"
                    "终止主进程以触发重启"
                )
                return 1
        else:
            factor_progress_age = runtime.get("progress_age_seconds")
            if (
                isinstance(factor_progress_age, (int, float))
                and not isinstance(factor_progress_age, bool)
                and factor_progress_age
                > max(1, max_factor_progress_age_seconds)
            ):
                logger.error(
                    "AI 因子调度长时间无进展，终止主进程以触发重启"
                )
                return 1
        if data_runtime is not None:
            auxiliary = data_runtime()
            if not isinstance(auxiliary, dict) or not auxiliary:
                logger.error("数据调度线程注册表为空，终止主进程以触发重启")
                return 1
            for name, state in auxiliary.items():
                if (
                    not isinstance(state, dict)
                    or state.get("thread_alive") is not True
                ):
                    logger.error("数据调度线程 %s 已退出", name)
                    return 1
                progress_age = state.get("progress_age_seconds")
                if (
                    not isinstance(progress_age, (int, float))
                    or isinstance(progress_age, bool)
                    or progress_age
                    > max(1, max_data_progress_age_seconds)
                ):
                    logger.error("数据调度线程 %s 长时间无进展", name)
                    return 1
        if runtime.get("ready") is True:
            not_ready_since = None
            continue
        if not_ready_since is None:
            not_ready_since = now
            continue
        if now - not_ready_since >= max(1, grace_seconds):
            logger.error(
                "AI 因子调度持续未就绪 %s 秒，终止主进程以触发重启",
                max(1, grace_seconds),
            )
            return 1
    return 0


def main() -> int:
    import config  # noqa: F401  # 触发 .env 加载（代理清理/交易开关与 Web 同源）
    from core import db_migration
    from core.schedulers import (
        get_data_scheduler_runtime,
        start_data_schedulers,
    )
    from analytics import auto_factor_scheduler
    from pathlib import Path

    # PID 文件：launcher 重启时据此清掉上一个调度进程，避免数据组双跑
    pid_file = Path(__file__).parent / "data" / "scheduler.pid"
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:
        logger.warning(f"写 PID 文件失败（不阻断）: {e}")

    # Schema 不完整时因子任务和管理员配置都不可信，交给 systemd 重启恢复。
    db_migration.migrate_users_db()

    start_data_schedulers()
    logger.info("数据调度进程就绪（新闻/复盘/概念/AI因子挖掘），Ctrl+C 退出")

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    try:
        grace_seconds = int(
            os.environ.get(
                "SCHEDULER_NOT_READY_GRACE_SECONDS",
                "120",
            )
        )
    except ValueError:
        grace_seconds = 120
    try:
        factor_progress_age = int(
            os.environ.get(
                "SCHEDULER_FACTOR_MAX_PROGRESS_AGE_SECONDS",
                "3600",
            )
        )
    except ValueError:
        factor_progress_age = 3600
    try:
        factor_job_age = int(
            os.environ.get(
                "SCHEDULER_FACTOR_MAX_JOB_AGE_SECONDS",
                "21600",
            )
        )
    except ValueError:
        factor_job_age = 21600
    try:
        data_progress_age = int(
            os.environ.get(
                "SCHEDULER_DATA_MAX_PROGRESS_AGE_SECONDS",
                "900",
            )
        )
    except ValueError:
        data_progress_age = 900
    exit_code = _supervise_factor_scheduler(
        auto_factor_scheduler,
        data_runtime=get_data_scheduler_runtime,
        grace_seconds=max(1, grace_seconds),
        max_factor_progress_age_seconds=max(1, factor_progress_age),
        max_factor_job_age_seconds=max(1, factor_job_age),
        max_data_progress_age_seconds=max(1, data_progress_age),
    )
    try:
        pid_file.unlink(missing_ok=True)
    except Exception:
        pass
    return exit_code


if __name__ == "__main__":
    exit_code = main()
    logging.shutdown()
    # concurrent.futures 会在 atexit 阶段等待仍在拉取数据的线程，服务重启会因此卡满 90 秒。
    os._exit(exit_code)
