# -*- coding: utf-8 -*-
import json
import sys
import builtins
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics import ai_analyst
from analytics import llm_client


class _FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_call_llm_can_return_content_with_openai_usage(monkeypatch):
    payload = {
        "choices": [{"message": {"content": "OK"}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }

    # A-6：_call_llm 的实现已搬到 analytics/llm_client，内部查找的是那边的
    # _urlopen_smart，所以打桩要打到 llm_client 上（打 ai_analyst 已经拦不住）。
    monkeypatch.setattr(llm_client, "_urlopen_smart", lambda *args, **kwargs: _FakeResp(payload))

    result = ai_analyst._call_llm(
        {"provider": "deepseek", "base_url": "https://example.test/v1", "api_key": "sk-test", "model": "deepseek-chat"},
        [{"role": "user", "content": "ping"}],
        return_usage=True,
    )

    assert result == {
        "content": "OK",
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        "provider": "deepseek",
        "model": "deepseek-chat",
        "estimated": False,
    }


def test_call_llm_keeps_text_return_by_default(monkeypatch):
    payload = {
        "choices": [{"message": {"content": "legacy text"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }

    # A-6：_call_llm 的实现已搬到 analytics/llm_client，内部查找的是那边的
    # _urlopen_smart，所以打桩要打到 llm_client 上（打 ai_analyst 已经拦不住）。
    monkeypatch.setattr(llm_client, "_urlopen_smart", lambda *args, **kwargs: _FakeResp(payload))

    result = ai_analyst._call_llm(
        {"provider": "deepseek", "base_url": "https://example.test/v1", "api_key": "sk-test", "model": "deepseek-chat"},
        [{"role": "user", "content": "ping"}],
    )

    assert result == "legacy text"


def test_fundamentals_skips_legacy_sources_when_gateway_fallback_disabled(monkeypatch):
    imported_legacy = []
    real_import = builtins.__import__

    monkeypatch.setattr(
        ai_analyst.dg,
        "get_active_config",
        lambda: {"provider": "tushare", "fallback_enabled": False},
    )
    monkeypatch.setattr(
        ai_analyst.dg,
        "get_finance_info",
        lambda code: {"mktcap": 120.0, "pe_ttm": 12.0, "pb": 1.2, "bps": 3.4, "holders": 1000, "ipo_date": "2020-01-01"},
    )

    def guard_import(name, *args, **kwargs):
        if name in {"akshare", "efinance"}:
            imported_legacy.append(name)
            raise AssertionError(f"legacy datasource imported: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard_import)

    result = ai_analyst._get_fundamentals("000001")

    assert result["text"]
    assert imported_legacy == []
