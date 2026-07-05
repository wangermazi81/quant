import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

import review_engine


def test_collect_turnover_uses_requested_compact_date(monkeypatch):
    calls = []

    class FakeMarketStats:
        @staticmethod
        def get_market_turnover(date_text=None):
            calls.append(date_text)
            return {"today": 123.0, "trend": [], "source": "test"}

    monkeypatch.setitem(sys.modules, "market_stats", FakeMarketStats)

    turnover = review_engine._collect_turnover("20260703")

    assert turnover["today"] == 123.0
    assert calls == ["20260703"]
