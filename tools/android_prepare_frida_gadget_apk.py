# -*- coding: utf-8 -*-

import argparse
import json
import lzma
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APK = PROJECT_ROOT / "logs" / "mobile_capture" / "apk" / "ths-11.32.03-base.apk"
DEFAULT_OUTPUT = PROJECT_ROOT / "logs" / "mobile_capture" / "apk" / "ths-11.32.03-frida-gadget-unsigned.apk"
DEFAULT_GADGET_DIR = PROJECT_ROOT / "logs" / "mobile_capture" / "frida-gadget"
DEFAULT_PATCH_LIB = "libweituo.so"
PYDEPS = PROJECT_ROOT / "logs" / "mobile_capture" / "pydeps"
ABI_TO_FRIDA = {
    "arm64-v8a": "android-arm64",
    "armeabi-v7a": "android-arm",
    "x86": "android-x86",
    "x86_64": "android-x86_64",
}


def _ensure_local_pydeps():
    if PYDEPS.exists():
        path = str(PYDEPS)
        if path not in sys.path:
            sys.path.insert(0, path)


def gadget_asset_url(version, abi):
    frida_abi = ABI_TO_FRIDA[abi]
    name = f"frida-gadget-{version}-{frida_abi}.so.xz"
    return f"https://github.com/frida/frida/releases/download/{version}/{name}"


def gadget_config(address="127.0.0.1", port=27042, on_load="wait"):
    return json.dumps({
        "interaction": {
            "type": "listen",
            "address": address,
            "port": int(port),
            "on_load": on_load,
        }
    }, ensure_ascii=False, indent=2).encode("utf-8")


def _download(url, output, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as response, Path(output).open("wb") as stream:
        shutil.copyfileobj(response, stream)


def ensure_gadget(version, abi, output_dir=DEFAULT_GADGET_DIR, gadget_path=None, download_timeout=60):
    if gadget_path:
        return Path(gadget_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    gadget = output_dir / f"libfrida-gadget-{version}-{abi}.so"
    if gadget.exists():
        return gadget
    archive = gadget.with_suffix(gadget.suffix + ".xz")
    _download(gadget_asset_url(version, abi), archive, timeout=download_timeout)
    gadget.write_bytes(lzma.decompress(archive.read_bytes()))
    return gadget


def patch_needed_library(input_so, output_so, needed="libfrida-gadget.so"):
    _ensure_local_pydeps()
    import lief

    binary = lief.parse(str(input_so))
    if needed not in list(binary.libraries):
        binary.add_library(needed)
    binary.write(str(output_so))


def _write_entry(zout, name, data, source_info=None):
    if source_info is None:
        info = zipfile.ZipInfo(name)
        info.compress_type = zipfile.ZIP_DEFLATED
    else:
        info = zipfile.ZipInfo(name, source_info.date_time)
        info.compress_type = source_info.compress_type
        info.external_attr = source_info.external_attr
        info.comment = source_info.comment
        info.extra = source_info.extra
    zout.writestr(info, data)


def build_patched_apk(
    apk_path,
    output_path,
    gadget_so,
    abi="arm64-v8a",
    patch_lib=DEFAULT_PATCH_LIB,
    config_bytes=None,
):
    apk_path = Path(apk_path)
    output_path = Path(output_path)
    gadget_so = Path(gadget_so)
    target_lib = f"lib/{abi}/{patch_lib}"
    gadget_name = f"lib/{abi}/libfrida-gadget.so"
    config_name = f"lib/{abi}/libfrida-gadget.config.so"
    config_bytes = config_bytes if config_bytes is not None else gadget_config()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ths_gadget_") as tmpdir:
        tmpdir = Path(tmpdir)
        original_so = tmpdir / patch_lib
        patched_so = tmpdir / f"patched-{patch_lib}"
        with zipfile.ZipFile(apk_path, "r") as zin:
            original_so.write_bytes(zin.read(target_lib))
        patch_needed_library(original_so, patched_so)
        patched_data = patched_so.read_bytes()

        with zipfile.ZipFile(apk_path, "r") as zin, zipfile.ZipFile(output_path, "w") as zout:
            names = set()
            for info in zin.infolist():
                name = info.filename
                if name.startswith("META-INF/"):
                    continue
                if name in {gadget_name, config_name}:
                    continue
                data = patched_data if name == target_lib else zin.read(info)
                _write_entry(zout, name, data, info)
                names.add(name)
            _write_entry(zout, gadget_name, gadget_so.read_bytes())
            _write_entry(zout, config_name, config_bytes)

    return {
        "ok": True,
        "apk": str(apk_path),
        "output": str(output_path),
        "abi": abi,
        "patch_lib": target_lib,
        "gadget": gadget_name,
        "config": config_name,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare THS APK with Frida Gadget")
    parser.add_argument("--apk", type=Path, default=DEFAULT_APK)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--abi", default="arm64-v8a", choices=sorted(ABI_TO_FRIDA))
    parser.add_argument("--patch-lib", default=DEFAULT_PATCH_LIB)
    parser.add_argument("--frida-version", default="17.16.3")
    parser.add_argument("--gadget", type=Path, help="existing libfrida-gadget.so")
    parser.add_argument("--download-timeout", type=float, default=60, help="gadget download timeout seconds")
    parser.add_argument("--listen", default="127.0.0.1:27042", help="gadget listen address:port")
    args = parser.parse_args(argv)

    address, port_text = args.listen.rsplit(":", 1)
    gadget = ensure_gadget(
        args.frida_version,
        args.abi,
        gadget_path=args.gadget,
        download_timeout=args.download_timeout,
    )
    report = build_patched_apk(
        args.apk,
        args.output,
        gadget,
        abi=args.abi,
        patch_lib=args.patch_lib,
        config_bytes=gadget_config(address=address, port=int(port_text)),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())