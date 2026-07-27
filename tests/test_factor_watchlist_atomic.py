import json
import threading
import time

from core import factor_watchlist as fw


def _candidate(code):
    return {
        "ts_code": code,
        "name": f"Stock-{code}",
        "trade_date": "2026-07-27",
        "candidate_rank": 1,
    }


def test_concurrent_imports_keep_both_candidates(monkeypatch, tmp_path):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    original_read = fw._read_json_file
    start = threading.Barrier(2)

    def slow_read(path, default):
        data = original_read(path, default)
        time.sleep(0.08)
        return data

    monkeypatch.setattr(fw, "_read_json_file", slow_read)
    results = []

    def run(code):
        start.wait()
        results.append(fw.import_factor_candidates_to_watchlist([_candidate(code)]))

    threads = [threading.Thread(target=run, args=(code,)) for code in ("000001.SZ", "600000.SH")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    items = fw.load_factor_watchlist()["items"]
    assert {item["ts_code"] for item in items} == {"000001.SZ", "600000.SH"}


def test_save_uses_atomic_replace(monkeypatch, tmp_path):
    import app_state

    monkeypatch.setattr(app_state, "BASE_DIR", tmp_path)
    calls = []
    original_replace = fw.os.replace

    def tracked_replace(source, target):
        calls.append((source, target))
        return original_replace(source, target)

    monkeypatch.setattr(fw.os, "replace", tracked_replace)
    fw.save_factor_watchlist({"items": [_candidate("000001.SZ")]})

    assert len(calls) == 1
    source, target = map(fw.Path, calls[0])
    assert source.parent == target.parent
    assert target == fw.factor_watchlist_path()
    assert json.loads(target.read_text(encoding="utf-8"))["items"][0]["ts_code"] == "000001.SZ"
