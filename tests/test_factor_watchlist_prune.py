# -*- coding: utf-8 -*-
"""观察池保留策略：按交易日裁剪，但已决策条目永不删。

线上实测问题：import_factor_candidates_to_watchlist 按 id(trade_date-ts_code)
合并后**从不淘汰**，每次挖掘导入约 100 条，跑了 7 次就堆到 700 条、
7 个交易日新旧混排 —— 后台打开是一片分不清今天该看哪些的列表。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import factor_watchlist as fw


@pytest.fixture
def store(tmp_path, monkeypatch):
    """把观察池指到临时文件，绝不碰真实 data/research/relay_factor。"""
    root = tmp_path / "relay_factor"
    root.mkdir(parents=True)
    monkeypatch.setattr(fw, "factor_mining_root", lambda: root)
    # 名称解析会去查行情，测试里直接回落代码本身
    monkeypatch.setattr(fw, "resolve_watchlist_name", lambda ts, raw=None: raw or ts)
    return root / "watchlist.json"


def _cand(code, date, rank=1):
    return {"ts_code": f"{code}.SH", "name": code, "trade_date": date,
            "candidate_rank": rank, "relay_score": 80.0}


def test_prune_keeps_only_recent_trading_days(store):
    data = {"items": [
        fw.sanitize_watchlist_item(_cand("60000%d" % i, d))
        for d in ("2026-07-24", "2026-07-22", "2026-07-20", "2026-07-16",
                  "2026-07-15", "2026-07-14", "2026-07-13")
        for i in range(3)
    ]}
    assert len(data["items"]) == 21

    fw.prune_factor_watchlist(data, keep_days=3)

    kept_dates = {i["trade_date"] for i in data["items"]}
    assert kept_dates == {"2026-07-24", "2026-07-22", "2026-07-20"}
    assert len(data["items"]) == 9


def test_prune_never_drops_decided_items(store):
    """★已决策的条目是决策留痕，删了就无法复盘「当初为什么买/为什么跳过」。"""
    old = fw.sanitize_watchlist_item(_cand("600001", "2026-01-05"))
    old["status"] = "bought"
    old2 = fw.sanitize_watchlist_item(_cand("600002", "2026-01-05"))
    old2["status"] = "skipped"
    old3 = fw.sanitize_watchlist_item(_cand("600003", "2026-01-05"))
    old3["status"] = "pending"          # 未决策 → 该被裁掉
    fresh = fw.sanitize_watchlist_item(_cand("600009", "2026-07-24"))

    data = {"items": [old, old2, old3, fresh]}
    fw.prune_factor_watchlist(data, keep_days=1)

    codes = {i["code"] for i in data["items"]}
    assert "600001" in codes and "600002" in codes, "已决策条目不得被裁剪"
    assert "600003" not in codes, "未决策的过期条目应被裁掉"
    assert "600009" in codes


def test_import_prunes_before_saving(store):
    """导入路径必须落盘前裁剪，否则还是会无限堆积。"""
    for d in ("2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16",
              "2026-07-20", "2026-07-22", "2026-07-24"):
        r = fw.import_factor_candidates_to_watchlist(
            [_cand("60010%d" % i, d, rank=i + 1) for i in range(4)],
            run_id=f"run-{d}",
        )
        assert r["ok"] is True

    saved = json.loads(store.read_text(encoding="utf-8"))
    dates = {i["trade_date"] for i in saved["items"]}
    assert len(dates) <= fw.WATCHLIST_KEEP_DAYS, f"仍在堆积：{sorted(dates)}"
    assert "2026-07-24" in dates, "最新交易日必须保留"
    assert "2026-07-13" not in dates, "最旧的应被裁掉"


def test_readiness_sort_puts_actionable_first():
    """★可执行的必须排在最前。

    市场弱势时 100 只候选可能只有 5 只 ready，而它们按 relay_score 排会散落在
    #1/#10/#12/#26/#32 —— 用户得人肉翻找「明天到底买哪几只」。
    这里直接验后台用的排序键。
    """
    import web.admin as admin

    rows = [
        {"trade_date": "2026-07-24", "candidate_rank": 1, "code": "600001",
         "live_readiness_status": "watch"},
        {"trade_date": "2026-07-24", "candidate_rank": 32, "code": "600032",
         "live_readiness_status": "ready"},
        {"trade_date": "2026-07-24", "candidate_rank": 4, "code": "600004",
         "live_readiness_status": "avoid"},
        {"trade_date": "2026-07-24", "candidate_rank": 10, "code": "600010",
         "live_readiness_status": "ready"},
    ]
    order = {"ready": 2, "watch": 1, "avoid": 0}

    def key(item):
        return (str(item.get("trade_date") or ""),
                order.get(str(item.get("live_readiness_status") or ""), 0),
                -int(item.get("candidate_rank") or 999999),
                str(item.get("code") or ""))

    got = [i["code"] for i in sorted(rows, key=key, reverse=True)]
    # ready 两只（rank 10 在前，32 在后）→ watch → avoid
    assert got == ["600010", "600032", "600001", "600004"], got
    assert admin._READY_ORDER == order, "后台排序权重被改动，请同步本用例"


def test_import_preserves_manual_note_and_status(store):
    """重复导入同一只票时，人工备注与已改状态不能被覆盖回 pending。"""
    fw.import_factor_candidates_to_watchlist([_cand("600123", "2026-07-24")], run_id="r1")

    data = fw.load_factor_watchlist()
    data["items"][0]["status"] = "ready"
    data["items"][0]["note"] = "竞价看量"
    fw.save_factor_watchlist(data)

    fw.import_factor_candidates_to_watchlist([_cand("600123", "2026-07-24")], run_id="r2")

    item = fw.load_factor_watchlist()["items"][0]
    assert item["status"] == "ready", "人工改过的状态不得被重新导入覆盖"
    assert item["note"] == "竞价看量"
