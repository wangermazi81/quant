#!/usr/bin/env python3
"""为量化平台的 SQLite 数据库创建一致性在线备份。"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

DATABASES = ("users.db", "paper_trading.db", "ai_research.db", "news.db")
CONFIG_FILES = (".env", "data/.secret_key", "data/ai_config.json", "data/datasource_config.json", "data/search_config.json")


def backup_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as src, sqlite3.connect(target) as dst:
        src.backup(dst, pages=256, sleep=0.02)
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError(f"备份完整性检查失败: {source}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(root: Path, output_root: Path, keep: int = 14) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_root.mkdir(parents=True, exist_ok=True)
    os.chmod(output_root, 0o700)
    temp = output_root / f".{stamp}.tmp"
    final = output_root / stamp
    temp.mkdir(mode=0o700)
    manifest = {"created_at": datetime.now().isoformat(timespec="seconds"), "files": {}}
    for name in DATABASES:
        source = root / "data" / name
        if source.exists():
            target = temp / name
            backup_database(source, target)
            manifest["files"][name] = {"size": target.stat().st_size, "sha256": sha256(target)}
    for relative in CONFIG_FILES:
        source = root / relative
        if source.exists():
            target = temp / "config" / relative.replace("/", "__")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            os.chmod(target, 0o600)
            manifest["files"][f"config/{relative}"] = {"size": target.stat().st_size, "sha256": sha256(target)}
    (temp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.rename(final)
    backups = sorted((p for p in output_root.iterdir() if p.is_dir() and not p.name.startswith(".")), reverse=True)
    for old in backups[max(1, keep):]:
        shutil.rmtree(old)
    return final


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent.parent / "backups" / "quant-data")
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()
    print(create_backup(args.root, args.output, max(1, args.keep)))


if __name__ == "__main__":
    main()
