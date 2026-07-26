# -*- coding: utf-8 -*-
"""K线元数据缓存 / 原子落盘回归。

背景：metadata.json 实测 858KB，而 merge_kline_cache() 对**每只股票**都整份读+整份重写。
一轮全市场扫描 ≈ 数 GB 无谓 I/O + 4000 次 json 解析（GIL 内串行，抢占唯一 worker）；
write_text 又是非原子截断写且该路径无锁，并发扫描会把文件写残
（生产日志里"元数据文件损坏，回退空"即由此而来）。

这些用例锁住修复后的语义：读走进程内缓存、热路径只标脏、落盘原子替换。
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

from analytics import data_manager as dm


@pytest.fixture
def meta_file(tmp_path, monkeypatch):
    """把元数据指向临时文件并清空进程内缓存，绝不碰真实 data/metadata.json。"""
    path = tmp_path / "metadata.json"
    monkeypatch.setattr(dm, "META_FILE", path)
    monkeypatch.setattr(dm, "_META_CACHE", None)
    monkeypatch.setattr(dm, "_META_STAMP", None)
    monkeypatch.setattr(dm, "_META_DIRTY", False)
    monkeypatch.setattr(dm, "_META_LAST_FLUSH", 0.0)
    return path


def test_missing_file_returns_empty(meta_file):
    assert dm._load_meta() == {}


def test_immediate_save_writes_through(meta_file):
    dm._save_meta({"000001": {"name": "平安银行", "rows": 100}})
    assert json.loads(meta_file.read_text(encoding="utf-8"))["000001"]["rows"] == 100


def test_hot_path_save_does_not_rewrite_file(meta_file):
    """immediate=False（逐只股票的热路径）只标脏，不触碰磁盘"""
    dm._save_meta({"000001": {"rows": 1}})
    mtime_before = meta_file.stat().st_mtime_ns

    meta = dm._load_meta()
    meta["000002"] = {"rows": 2}
    dm._save_meta(meta, immediate=False)

    assert meta_file.stat().st_mtime_ns == mtime_before
    assert "000002" not in json.loads(meta_file.read_text(encoding="utf-8"))


def test_dirty_state_reads_from_memory(meta_file):
    """未落盘时 _load_meta 必须返回内存版，否则热路径会读到过期数据"""
    dm._save_meta({"000001": {"rows": 1}})
    meta = dm._load_meta()
    meta["000002"] = {"rows": 2}
    dm._save_meta(meta, immediate=False)

    assert dm._load_meta()["000002"]["rows"] == 2


def test_flush_meta_persists_pending_changes(meta_file):
    dm._save_meta({"000001": {"rows": 1}})
    meta = dm._load_meta()
    meta["000002"] = {"rows": 2}
    dm._save_meta(meta, immediate=False)

    dm.flush_meta()

    assert json.loads(meta_file.read_text(encoding="utf-8"))["000002"]["rows"] == 2


def test_external_change_invalidates_cache(meta_file):
    """别的进程改了文件（如独立调度进程），本进程必须重新读盘"""
    dm._save_meta({"000001": {"name": "平安银行"}})
    time.sleep(0.01)
    meta_file.write_text(json.dumps({"600519": {"name": "贵州茅台"}}), encoding="utf-8")

    reloaded = dm._load_meta()

    assert "600519" in reloaded
    assert "000001" not in reloaded


def test_save_is_atomic_and_leaves_no_temp_file(meta_file):
    dm._save_meta({"000001": {"rows": 1}})
    dm._save_meta({"000001": {"rows": 2}})

    assert [p.name for p in meta_file.parent.iterdir() if ".tmp" in p.name] == []
    assert json.loads(meta_file.read_text(encoding="utf-8"))["000001"]["rows"] == 2


def test_corrupt_file_falls_back_to_empty(meta_file):
    meta_file.write_text("{not json", encoding="utf-8")
    assert dm._load_meta() == {}


# ── P-1 后半：本地缓存命中时不得回写 ─────────────────────────────
def _fake_kline(rows=30):
    import pandas as pd

    dates = pd.bdate_range("2026-01-02", periods=rows)
    return pd.DataFrame({
        "date": dates,
        "open": [10.0] * rows,
        "close": [10.5] * rows,
        "high": [10.8] * rows,
        "low": [9.9] * rows,
        "volume": [100000] * rows,
    })


def test_local_cache_hit_skips_merge_writeback(meta_file, monkeypatch):
    """★数据原样来自本地 CSV 时必须跳过 merge_kline_cache。

    否则每只股票都白做一次「读 CSV + concat + 重写 CSV + 更新元数据」——
    一轮 5000 只的全市场扫描就是 5000 次无谓读写放大，全在 GIL 内串行。
    """
    merges = []
    monkeypatch.setattr(dm, "merge_kline_cache",
                        lambda code, df, **k: merges.append(code) or df)
    monkeypatch.setattr(dm.datasource_config, "get_internal_config",
                        lambda: {"provider": "current", "fallback_enabled": False,
                                 "tushare_mode": "replay"})
    monkeypatch.setattr(dm, "_fetch_current_kline",
                        lambda code, days, min_rows, prefer_local=True:
                        dm._mark_from_local(_fake_kline()))

    r = dm.get_kline_by_config("000001", days=20, min_rows=10, update_cache=True)

    assert r["ok"] is True
    assert merges == [], "本地缓存命中却仍然回写了缓存"


def test_fresh_remote_data_still_merges(meta_file, monkeypatch):
    """未打标记（= 来自远程/TDX 的新数据）时照常回写，不能把正常缓存更新也跳掉。"""
    merges = []
    monkeypatch.setattr(dm, "merge_kline_cache",
                        lambda code, df, **k: merges.append(code) or df)
    monkeypatch.setattr(dm.datasource_config, "get_internal_config",
                        lambda: {"provider": "current", "fallback_enabled": False,
                                 "tushare_mode": "replay"})
    monkeypatch.setattr(dm, "_fetch_current_kline",
                        lambda code, days, min_rows, prefer_local=True: _fake_kline())

    r = dm.get_kline_by_config("000001", days=20, min_rows=10, update_cache=True)

    assert r["ok"] is True
    assert merges == ["000001"]
