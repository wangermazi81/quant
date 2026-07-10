# -*- coding: utf-8 -*-
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics import ai_analyst


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

    monkeypatch.setattr(ai_analyst, "_urlopen_smart", lambda *args, **kwargs: _FakeResp(payload))

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

    monkeypatch.setattr(ai_analyst, "_urlopen_smart", lambda *args, **kwargs: _FakeResp(payload))

    result = ai_analyst._call_llm(
        {"provider": "deepseek", "base_url": "https://example.test/v1", "api_key": "sk-test", "model": "deepseek-chat"},
        [{"role": "user", "content": "ping"}],
    )

    assert result == "legacy text"
