# -*- coding: utf-8 -*-

import argparse
import base64
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE = "com.hexin.plat.android"
DEFAULT_SCRIPT = PROJECT_ROOT / "tools" / "frida_ths_capture.js"
DEFAULT_OUTPUT = PROJECT_ROOT / "logs" / "mobile_capture" / "hook" / "ths-jni.jsonl"
PYDEPS = PROJECT_ROOT / "logs" / "mobile_capture" / "pydeps"


def _ensure_local_pydeps():
    if PYDEPS.exists():
        path = str(PYDEPS)
        if path not in sys.path:
            sys.path.insert(0, path)


def _local_ts(now=None):
    now = now or datetime.now(timezone.utc).astimezone()
    return now.isoformat(timespec="milliseconds")


def _ascii_preview(data, limit=96):
    preview = []
    for value in data[:limit]:
        preview.append(chr(value) if 32 <= value <= 126 else ".")
    return "".join(preview)


def normalize_frida_message(message, data=None, now=None):
    record = {
        "recv_at": _local_ts(now),
        "frida_type": message.get("type", "unknown"),
    }
    payload = message.get("payload")
    if isinstance(payload, dict):
        record.update(payload)
    elif payload is not None:
        record["payload"] = payload
    if message.get("type") == "error":
        record["description"] = message.get("description", "")
        record["stack"] = message.get("stack", "")
    if data:
        record["data_base64"] = base64.b64encode(data).decode("ascii")
        record["data_len"] = len(data)

    sample_b64 = record.get("sample_base64")
    if sample_b64:
        sample = base64.b64decode(sample_b64)
        record["sample_sha256_16"] = hashlib.sha256(sample).hexdigest()[:16]
        record["prefix_hex"] = sample[:32].hex(" ")
        record["ascii_preview"] = _ascii_preview(sample)
    return record


def load_hook_script(path=DEFAULT_SCRIPT):
    return Path(path).read_text(encoding="utf-8")


def _select_device(frida, serial=None, remote=None, timeout=10):
    if remote:
        return frida.get_device_manager().add_remote_device(remote)
    if serial:
        return frida.get_device_manager().get_device_matching(
            lambda device: device.id == serial or device.name == serial,
            timeout=timeout,
        )
    return frida.get_usb_device(timeout=timeout)


def run_capture(
    package=DEFAULT_PACKAGE,
    output=DEFAULT_OUTPUT,
    script_path=DEFAULT_SCRIPT,
    serial=None,
    remote=None,
    spawn=False,
    duration_sec=0,
    device_timeout=10,
):
    _ensure_local_pydeps()
    import frida

    device = _select_device(frida, serial=serial, remote=remote, timeout=device_timeout)
    pid = None
    if spawn:
        pid = device.spawn([package])
        session = device.attach(pid)
    else:
        session = device.attach(package)

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    source = load_hook_script(script_path)

    with output.open("a", encoding="utf-8") as stream:
        def on_message(message, data):
            record = normalize_frida_message(message, data=data)
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            label = record.get("hook") or record.get("stage") or record.get("frida_type")
            length = record.get("length", "")
            print(f"{record['recv_at']} {label} len={length}")

        script = session.create_script(source)
        script.on("message", on_message)
        script.load()
        if pid is not None:
            device.resume(pid)
        print(f"hook 闁诲海鎳撻幉陇銇愰崘顔肩畾閹兼番鍔岀粈澶嬨亜椤撶喎绗х紒鎰叞ackage={package} output={output}")
        started = time.time()
        try:
            while True:
                time.sleep(0.2)
                if duration_sec and time.time() - started >= duration_sec:
                    break
        finally:
            session.detach()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Capture Android THS Java/native byte boundaries")
    parser.add_argument("--package", default=DEFAULT_PACKAGE, help="target Android package")
    parser.add_argument("--serial", help="Frida USB device serial")
    parser.add_argument("--remote", help="remote Frida device, e.g. 127.0.0.1:27042")
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT, help="Frida JS hook script")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="JSONL output path")
    parser.add_argument("--spawn", action="store_true", help="spawn package; default attaches to running process")
    parser.add_argument("--duration-sec", type=float, default=0, help="capture duration; 0 keeps running")
    parser.add_argument("--device-timeout", type=float, default=10, help="USB device timeout seconds")
    args = parser.parse_args(argv)
    run_capture(
        package=args.package,
        output=args.output,
        script_path=args.script,
        serial=args.serial,
        remote=args.remote,
        spawn=args.spawn,
        duration_sec=args.duration_sec,
        device_timeout=args.device_timeout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())