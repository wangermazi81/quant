#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""见微 Alpha 服务健康监控 + 掉线/高内存告警（cron 每 4 分钟运行一次）。

检查：
  1. gunicorn 是否响应（HTTP 健康检查）；
  2. gunicorn worker 内存（RSS），逼近总内存时提前告警，预防 OOM。
异常时通过平台已配置的通知渠道（企业微信/飞书/钉钉/Telegram，data/notify_config.json）
推送告警；**仅在状态变化时**发送（避免每轮刷屏），服务恢复时发恢复通知。

安装（服务器 root crontab）：
  */4 * * * * /opt/quant/venv/bin/python3 /opt/quant/deploy/health_monitor.py >> /opt/quant/logs/health_monitor.log 2>&1

可调环境变量：
  HEALTH_URL              健康检查地址（默认 http://127.0.0.1:5000/api/auth/me）
  HEALTH_MEM_ALERT_MB     worker 内存告警阈值 MB（默认 1200，总内存约 1613）
"""
import os
import sys
import json
import time
import subprocess
import urllib.request

APP_DIR  = os.environ.get("APP_DIR", "/opt/quant")
SERVICE  = "quant-platform"
HOST     = os.environ.get("HEALTH_HOST", "jwalpha.top")
HEALTH_URL   = os.environ.get("HEALTH_URL", "http://127.0.0.1:5000/api/auth/me")
MEM_ALERT_MB = int(os.environ.get("HEALTH_MEM_ALERT_MB", "1200"))
STATE_FILE   = os.path.join(APP_DIR, "data", ".health_state.json")

# notification_manager 在 core/，仅依赖标准库
sys.path.insert(0, os.path.join(APP_DIR, "core"))


def _http_ok() -> bool:
    try:
        req = urllib.request.Request(HEALTH_URL, headers={"User-Agent": "health-monitor"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= getattr(r, "status", r.getcode()) < 500
    except Exception:
        return False


def _worker_rss_mb() -> int:
    """gunicorn 所有 worker 的 RSS 之和（MB）。取不到返回 0。"""
    try:
        master = subprocess.check_output(
            ["systemctl", "show", "-p", "MainPID", "--value", SERVICE],
            timeout=10).decode().strip()
        if not master or master == "0":
            return 0
        workers = subprocess.check_output(["pgrep", "-P", master], timeout=10).decode().split()
        total = 0
        for w in workers:
            try:
                with open(f"/proc/{w}/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            total += int(line.split()[1]) // 1024
                            break
            except Exception:
                pass
        return total
    except Exception:
        return 0


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"status": "ok"}


def _save_state(s: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
    except Exception:
        pass


def _notify(title: str, content: str) -> dict:
    try:
        import notification_manager as nm
        return nm.send_notification(title, content)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main():
    ok = _http_ok()
    if not ok:                      # 二次确认，避免瞬时抖动误报
        time.sleep(5)
        ok = _http_ok()
    rss = _worker_rss_mb()

    if not ok:
        status = "down"
    elif rss >= MEM_ALERT_MB:
        status = "highmem"
    else:
        status = "ok"

    prev = _load_state().get("status", "ok")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    if status != prev:
        if status == "down":
            _notify("🔴 见微 Alpha 掉线",
                    f"时间：{ts}\nhttps://{HOST} 健康检查连续失败（gunicorn 无响应）。"
                    f"\nsystemd 通常会自动重启（Restart=always），请尽快查看。")
        elif status == "highmem":
            _notify("🟠 见微 Alpha 内存偏高",
                    f"时间：{ts}\nworker 内存 {rss}MB ≥ 告警阈值 {MEM_ALERT_MB}MB（总内存约 1613MB），"
                    f"有 OOM 风险。\n若正在跑全市场回测，可在 .env 设 BACKTEST_MAX_CODES=400 降内存后重启。")
        elif status == "ok" and prev in ("down", "highmem"):
            _notify("🟢 见微 Alpha 已恢复", f"时间：{ts}\n服务健康，worker 内存 {rss}MB。")

    _save_state({"status": status, "rss": rss, "ok": ok, "ts": time.time(),
                 "checked_at": ts})
    print(f"[{ts}] status={status} http_ok={ok} worker_rss={rss}MB (prev={prev})")


if __name__ == "__main__":
    main()
