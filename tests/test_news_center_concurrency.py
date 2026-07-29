import threading

import news_center as nc


def _reset_news_db(monkeypatch, tmp_path):
    db_path = tmp_path / "news.db"
    monkeypatch.setattr(nc, "DB_PATH", db_path)
    if hasattr(nc, "_schema_lock"):
        with nc._schema_lock:
            nc._schema_ready_paths.discard(str(db_path.resolve()))
    return db_path


def test_query_does_not_wait_for_writer_lock(monkeypatch, tmp_path):
    _reset_news_db(monkeypatch, tmp_path)
    assert nc.query() == []

    finished = threading.Event()
    result = []

    nc._db_lock.acquire()
    try:
        thread = threading.Thread(target=lambda: (result.extend(nc.query()), finished.set()))
        thread.start()
        assert finished.wait(0.5)
    finally:
        nc._db_lock.release()
        thread.join(timeout=1)

    assert result == []


def test_fetch_quick_batches_all_sources_into_one_insert(monkeypatch):
    items = [
        nc._mk_item("来源A", "2026-07-29 09:30:00", "第一条测试新闻"),
        nc._mk_item("来源B", "2026-07-29 09:31:00", "第二条测试新闻"),
    ]
    monkeypatch.setattr(
        nc,
        "_QUICK_SOURCES",
        (("来源A", lambda: [items[0]]), ("来源B", lambda: [items[1]])),
    )
    batches = []
    monkeypatch.setattr(nc, "_insert", lambda batch: batches.append(batch) or len(batch))

    assert nc.fetch_quick() == 2
    assert batches == [items]


def test_insert_reports_only_new_rows(monkeypatch, tmp_path):
    _reset_news_db(monkeypatch, tmp_path)
    item = nc._mk_item("测试源", "2026-07-29 09:30:00", "用于去重的测试新闻")

    assert nc._insert([item, item]) == 1
    assert nc._insert([item]) == 0
