# -*- coding: utf-8 -*-
r"""
一键启动脚本（web + 数据调度 双进程，工单5）
双击运行或配置为桌面快捷方式目标:
  D:\Anaconda\python.exe  D:\同花顺量化交易\ths_auto_trader\launcher.py
"""
import subprocess, sys, time, os, webbrowser

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
BASE_DIR  = os.path.normpath(BASE_DIR)
PYTHON    = sys.executable
MAIN      = os.path.join(BASE_DIR, "main.py")
SCHEDULER = os.path.join(BASE_DIR, "scheduler_main.py")
SCHED_PID = os.path.join(BASE_DIR, "data", "scheduler.pid")
URL       = "http://127.0.0.1:5001"
PORT      = 5001


def kill_stale_scheduler():
    """按 PID 文件清掉上一个数据调度进程（防止新闻/复盘等数据组双跑）。"""
    try:
        if not os.path.exists(SCHED_PID):
            return
        pid = open(SCHED_PID, encoding="utf-8").read().strip()
        if not pid.isdigit():
            return
        # 防 PID 复用误杀：确认该 PID 是 python 进程才下手
        chk = subprocess.run(f'tasklist /FI "PID eq {pid}" /FO CSV /NH',
                             shell=True, capture_output=True, text=True)
        if "python" in (chk.stdout or "").lower():
            subprocess.run(f"taskkill /PID {pid} /F", shell=True, capture_output=True)
            print(f"[launcher] 已终止旧数据调度进程 PID={pid}")
    except Exception:
        pass


def kill_port(port):
    """释放占用指定端口的进程"""
    try:
        result = subprocess.run(
            f'netstat -ano | findstr ":{port} "',
            shell=True, capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5:
                pid = parts[-1]
                if pid.isdigit() and pid != "0":
                    subprocess.run(f"taskkill /PID {pid} /F", shell=True,
                                   capture_output=True)
    except Exception:
        pass

def server_ready(timeout=60):
    """等待服务器就绪，返回是否成功"""
    import urllib.request
    for _ in range(timeout):
        try:
            with urllib.request.urlopen(f"{URL}/api/auth/me", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False

if __name__ == "__main__":
    print("🔬 见微 Alpha — 正在启动...")
    kill_port(PORT)
    kill_stale_scheduler()
    time.sleep(1)

    # 拆分模式（工单5）：Web 只跑交易组调度，数据组（新闻/复盘/概念/AI因子挖掘）
    # 由 scheduler_main.py 独立进程承担 → 重计算不再占用 Web 进程。
    env = {**os.environ, "THS_EMBED_SCHEDULER": "0"}
    print(f"[launcher] 启动 web 服务: {PYTHON} {MAIN}")
    proc = subprocess.Popen(
        [PYTHON, MAIN],
        cwd=BASE_DIR,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,  # 不显示黑框
    )
    print(f"[launcher] 启动数据调度进程: {PYTHON} {SCHEDULER}")
    subprocess.Popen(
        [PYTHON, SCHEDULER],
        cwd=BASE_DIR,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    print("[launcher] 等待服务就绪（最多60秒）...")
    if server_ready(60):
        print(f"[launcher] 见微 Alpha 已就绪，打开浏览器: {URL}")
        webbrowser.open(URL)
    else:
        print("[launcher] 启动超时！请检查 main.py 是否有错误")
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            "见微 Alpha 启动超时，请手动运行 main.py 查看错误信息",
            "见微 Alpha — 启动失败",
            0x10  # MB_ICONERROR
        )
