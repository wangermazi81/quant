# -*- coding: utf-8 -*-
"""Android USB 设备探测和页面诊断产物导出。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_ADB = PROJECT_ROOT / ".android-sdk" / "platform-tools" / "adb.exe"
Runner = Callable[..., str | bytes]


def find_adb() -> Path:
    if LOCAL_ADB.exists():
        return LOCAL_ADB
    system_adb = shutil.which("adb")
    if system_adb:
        return Path(system_adb)
    raise RuntimeError("未找到 ADB，请先运行 tools/install_android_tools.ps1")


def run_adb(
    args: list[str],
    serial: str | None = None,
    timeout: int = 15,
    binary: bool = False,
) -> str | bytes:
    command = [str(find_adb())]
    if serial:
        command.extend(["-s", serial])
    command.extend(args)
    completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(error or f"ADB 命令失败，退出码 {completed.returncode}")
    if binary:
        return completed.stdout
    return completed.stdout.decode("utf-8", errors="replace").strip()


def parse_adb_devices(text: str) -> list[dict]:
    devices = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        metadata = dict(item.split(":", 1) for item in parts[2:] if ":" in item)
        devices.append({
            "serial": parts[0],
            "state": parts[1],
            "model": metadata.get("model", ""),
            "product": metadata.get("product", ""),
            "device": metadata.get("device", ""),
        })
    return devices


def select_device(devices: list[dict], serial: str | None = None) -> dict:
    if serial:
        for device in devices:
            if device["serial"] == serial:
                return device
        raise RuntimeError(f"未找到已授权设备 {serial}")
    if not devices:
        raise RuntimeError("未检测到已授权 Android 设备")
    if len(devices) != 1:
        raise RuntimeError(f"检测到 {len(devices)} 台已授权设备，请指定序列号")
    return devices[0]


def parse_foreground_activity(text: str) -> dict:
    match = re.search(r"u\d+\s+([^/\s]+)/([^\s}]+)", text)
    if not match:
        return {"package": "", "activity": ""}
    return {"package": match.group(1), "activity": match.group(2)}


def probe_device(serial: str | None = None, runner: Runner | None = None) -> dict:
    runner = runner or run_adb
    devices = parse_adb_devices(str(runner(["devices", "-l"])))
    device = select_device(devices, serial=serial)
    selected = device["serial"]
    android_version = str(runner(["shell", "getprop", "ro.build.version.release"], serial=selected)).strip()
    size_output = str(runner(["shell", "wm", "size"], serial=selected))
    size_match = re.search(r"(\d+x\d+)", size_output)
    activity_output = str(runner(["shell", "dumpsys", "activity", "activities"], serial=selected))
    foreground = parse_foreground_activity(activity_output)
    return {
        "ok": True,
        "serial": selected,
        "android_version": android_version,
        "screen_size": size_match.group(1) if size_match else "",
        "foreground_package": foreground["package"],
        "foreground_activity": foreground["activity"],
    }


def dump_artifacts(output_dir: str | Path, device_info: dict, runner: Runner | None = None) -> dict:
    runner = runner or run_adb
    if not device_info.get("ok") or not device_info.get("serial"):
        raise RuntimeError("设备信息无效，先执行 probe_device()")
    serial = device_info["serial"]
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    screenshot = runner(["exec-out", "screencap", "-p"], serial=serial, binary=True)
    runner(["shell", "uiautomator", "dump", "/sdcard/window.xml"], serial=serial)
    window_xml = runner(["exec-out", "cat", "/sdcard/window.xml"], serial=serial, binary=True)
    (output_path / "screen.png").write_bytes(bytes(screenshot))
    (output_path / "window.xml").write_bytes(bytes(window_xml))
    (output_path / "device.json").write_text(
        json.dumps(device_info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "ok": True,
        "output_dir": str(output_path),
        "screen": str(output_path / "screen.png"),
        "window_xml": str(output_path / "window.xml"),
        "device": str(output_path / "device.json"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="探测 Android USB 设备")
    parser.add_argument("--serial", help="指定 ADB 设备序列号")
    parser.add_argument("--dump", type=Path, help="导出截图、UI XML 和设备信息")
    args = parser.parse_args()
    info = probe_device(serial=args.serial)
    result = dump_artifacts(args.dump, info) if args.dump else info
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
