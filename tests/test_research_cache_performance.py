import threading
import time

import research_center as rc


def _reset_research_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(rc, "CACHE_DIR", tmp_path)
    with rc._cache_lock:
        rc._cache.clear()
    if hasattr(rc, "_refresh_lock"):
        with rc._refresh_lock:
            rc._refreshing.clear()


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_concepts_return_disk_cache_before_background_refresh(monkeypatch, tmp_path):
    _reset_research_cache(monkeypatch, tmp_path)
    stale = [{"name": "人工智能", "code": "BK0800", "chg": 1.2, "up": 8, "down": 2}]
    fresh = [{"name": "机器人", "code": "BK0700", "chg": 2.3, "up": 10, "down": 1}]
    rc._disk_put("concepts", stale)

    started = threading.Event()
    release = threading.Event()

    def slow_fetch():
        started.set()
        release.wait(1)
        return fresh

    monkeypatch.setattr(rc, "_em_direct_concepts", slow_fetch)

    began = time.monotonic()
    result = rc.get_concepts()
    elapsed = time.monotonic() - began

    assert result == stale
    assert elapsed < 0.2
    assert started.wait(0.5)

    release.set()
    assert _wait_until(lambda: rc._cached("concepts", 600) == fresh)


def test_concept_stocks_return_disk_cache_before_background_refresh(monkeypatch, tmp_path):
    _reset_research_cache(monkeypatch, tmp_path)
    key = "cons:人工智能"
    stale = {
        "ok": True,
        "name": "人工智能",
        "stocks": [{"code": "000001", "name": "平安银行", "price": 10, "chg": 1}],
    }
    fresh_stocks = [{"code": "000002", "name": "万科A", "price": 8, "chg": 2}]
    rc._disk_put(key, stale)

    started = threading.Event()
    release = threading.Event()

    def slow_fetch(_name):
        started.set()
        release.wait(1)
        return fresh_stocks

    monkeypatch.setattr(rc, "_em_direct_cons", slow_fetch)

    began = time.monotonic()
    result = rc.get_concept_stocks("人工智能")
    elapsed = time.monotonic() - began

    assert result["stocks"][0]["code"] == "000001"
    assert result["stale"] is True
    assert elapsed < 0.2
    assert started.wait(0.5)

    release.set()
    assert _wait_until(
        lambda: (rc._cached(key, 600) or {}).get("stocks", [{}])[0].get("code") == "000002"
    )


def test_reports_return_disk_cache_before_background_refresh(monkeypatch, tmp_path):
    _reset_research_cache(monkeypatch, tmp_path)
    key = "latest_reports:0:"
    stale = [{
        "title": "人工智能行业深度研究",
        "org": "测试证券",
        "rating": "买入",
        "date": "2026-07-28",
        "stock": "",
        "code": "",
        "industry": "人工智能",
        "url": "",
    }]
    rc._disk_put(key, stale)

    started = threading.Event()
    release = threading.Event()

    def slow_fetch(*_args, **_kwargs):
        started.set()
        release.wait(1)
        return []

    monkeypatch.setattr(rc, "_fetch_reportapi", slow_fetch)

    began = time.monotonic()
    result = rc.get_latest_reports()
    elapsed = time.monotonic() - began

    assert result == stale
    assert elapsed < 0.2
    assert started.wait(0.5)
    release.set()
    assert _wait_until(lambda: not hasattr(rc, "_refreshing") or key not in rc._refreshing)


def test_reports_cold_start_uses_one_page_and_skips_ths_without_code(monkeypatch, tmp_path):
    _reset_research_cache(monkeypatch, tmp_path)
    pages = []
    ths_calls = []

    def fetch_page(_qtype, _code, page_size=80, page_no=1):
        pages.append((page_size, page_no))
        return [{
            "title": "人工智能行业深度研究",
            "orgSName": "测试证券",
            "publishDate": "2026-07-28",
            "infoCode": "abc",
        }]

    monkeypatch.setattr(rc, "_fetch_reportapi", fetch_page)
    monkeypatch.setattr(rc, "_fetch_ths_reports", lambda *a, **k: ths_calls.append((a, k)) or [])

    result = rc.get_latest_reports(qtype=0, code="", limit=60)

    assert len(result) == 1
    assert pages == [(80, 1)]
    assert ths_calls == []


def test_chain_cache_path_stays_inside_chain_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(rc, "CHAIN_DIR", tmp_path)

    path = rc._chain_path("DRG/DIP")
    hostile_path = rc._chain_path("../../outside")

    assert path.parent == tmp_path
    assert hostile_path.parent == tmp_path
    assert "/" not in path.name
    assert "\\" not in path.name
