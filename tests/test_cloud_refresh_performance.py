from analytics import data_gateway as dg
from web import bridge as bridge_web
from web import market as market_web


def test_display_quote_cache_coalesces_equivalent_requests(monkeypatch):
    calls = []

    def fake_quotes(codes):
        calls.append(list(codes))
        return [{"code": code, "price": 10.0} for code in codes]

    monkeypatch.setattr(dg, "get_realtime_quotes", fake_quotes)
    dg.clear_display_quote_cache()

    first = dg.get_display_quotes(["000002", "000001"])
    first[0]["price"] = 99.0
    second = dg.get_display_quotes(["000001", "000002"])

    assert calls == [["000001", "000002"]]
    assert [row["code"] for row in second] == ["000001", "000002"]
    assert second[0]["price"] == 10.0


def test_display_quote_cache_does_not_serialize_different_keys(monkeypatch):
    barrier = threading.Barrier(2)
    errors = []

    def fake_quotes(codes):
        try:
            barrier.wait(timeout=1)
        except Exception as exc:
            errors.append(exc)
        return [{"code": code, "price": 10.0} for code in codes]

    monkeypatch.setattr(dg, "get_realtime_quotes", fake_quotes)
    dg.clear_display_quote_cache()
    threads = [
        threading.Thread(target=dg.get_display_quotes, args=([code],))
        for code in ("000001", "000002")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert not errors


def test_bridge_process_status_uses_short_cache(monkeypatch):
    calls = []

    def fake_processes():
        calls.append(True)
        return [{"pid": 123, "command": "ths_bridge.py"}]

    monkeypatch.setattr(bridge_web, "_bridge_processes", fake_processes)
    bridge_web.clear_bridge_process_status_cache()

    first = bridge_web.bridge_process_status_payload()
    second = bridge_web.bridge_process_status_payload()
    refreshed = bridge_web.bridge_process_status_payload(force_refresh=True)

    assert first == second
    assert first["pid"] == 123
    assert len(calls) == 2
    assert refreshed["running"] is True


def test_bridge_process_status_can_cache_confirmed_transition(monkeypatch):
    monkeypatch.setattr(
        bridge_web,
        "_bridge_processes",
        lambda: (_ for _ in ()).throw(AssertionError("confirmed transition must not rescan")),
    )
    bridge_web.cache_bridge_process_status(
        {"running": True, "pid": 456, "pids": [456], "processes": []}
    )
    assert bridge_web.bridge_process_status_payload()["pid"] == 456


def test_market_context_force_refresh_returns_cache_and_updates_in_background(monkeypatch):
    calls = []
    started = []

    def fake_regime(force_refresh=False):
        calls.append(force_refresh)
        return {
            "trend": "震荡",
            "strength": 50,
            "indices": {"上证指数": {"price": 3200, "change": 0.2}},
        }

    class FakeThread:
        def __init__(self, *, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            started.append(self.target)

    monkeypatch.setattr(market_web.mctx, "get_market_regime", fake_regime)
    monkeypatch.setattr(
        market_web.dg,
        "get_index_quotes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("market route must not fetch the same index quotes twice")
        ),
    )
    monkeypatch.setattr(market_web.threading, "Thread", FakeThread)
    market_web.clear_market_context_cache()

    initial = market_web.get_market_context_payload()
    forced = market_web.get_market_context_payload(force_refresh=True)

    assert initial["trend"] == "震荡"
    assert forced["trend"] == "震荡"
    assert calls == [False]
    assert len(started) == 1
    started[0]()
    assert market_web.get_market_context_payload()["trend"] == "震荡"


def test_market_context_cold_cache_coalesces_concurrent_requests(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fake_regime(force_refresh=False):
        calls.append(force_refresh)
        entered.set()
        release.wait(timeout=2)
        return {"trend": "震荡", "strength": 50, "indices": {}}

    monkeypatch.setattr(market_web.mctx, "get_market_regime", fake_regime)
    market_web.clear_market_context_cache()
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(market_web.get_market_context_payload()))
        for _ in range(2)
    ]
    threads[0].start()
    assert entered.wait(timeout=1)
    threads[1].start()
    time.sleep(0.05)
    assert len(calls) == 1
    release.set()
    for thread in threads:
        thread.join(timeout=2)
    assert len(results) == 2
import threading
import time
