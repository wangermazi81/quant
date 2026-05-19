# -*- coding: utf-8 -*-
"""
一键启动脚本
双击运行或配置为桌面快捷方式目标:
  D:\Anaconda\python.exe  D:\同花顺量化交易\ths_auto_trader\launcher.py
"""
import subprocess, sys, time, os, webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.normpath(BASE_DIR)
PYTHON   = sys.executable
MAIN     = os.path.join(BASE_DIR, "main.py")
URL      = "http://127.0.0.1:5001"
PORT     = 5001

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
    time.sleep(1)

    print(f"[launcher] 启动服务: {PYTHON} {MAIN}")
    proc = subprocess.Popen(
        [PYTHON, MAIN],
        cwd=BASE_DIR,
        creationflags=subprocess.CREATE_NO_WINDOW,  # 不显示黑框
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
