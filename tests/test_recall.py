from __future__ import annotations

import pytest

pytestmark = pytest.mark.agent

from types import SimpleNamespace

from quiet_memo.agent.recall import RecallQueryBuilder
from quiet_memo.infra.config import LLMConfig


def response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def builder(**kwargs) -> RecallQueryBuilder:
    return RecallQueryBuilder(LLMConfig(), rewrite=False, **kwargs)


def test_followup_uses_recent_context():
    query = builder().build("那附近便宜吗", ["我在看滨江的房子"])

    assert "滨江" in query.text
    assert "那附近便宜吗" in query.text
    assert query.cue is False


def test_memory_cue_marks_cue_and_uses_context():
    query = builder().build("你还记得我的咖啡偏好吗", ["我之前喜欢清淡风味"])

    assert query.cue is True
    assert "清淡" in query.text


def test_plain_question_uses_current_text_only():
    query = builder().build("今天适合跑步吗", ["我在看滨江的房子"])

    assert query.text == "今天适合跑步吗"


def test_greeting_ignores_recent_context():
    query = builder().build("你好", ["我在看滨江的房子"])

    assert query.text == "你好"


def test_rewrite_uses_llm(monkeypatch):
    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "test-key")
    calls = []

    def fake_complete(config, messages, **kwargs):
        calls.append((messages, kwargs))
        return response("滨江 租房 房源 预算")

    query = RecallQueryBuilder(
        LLMConfig(), rewrite=True, completer=fake_complete
    ).build("那附近便宜吗", ["我在看滨江的房子"])

    assert query.text == "滨江 租房 房源 预算"
    assert query.rewritten is True
    assert calls
    assert "同义词" in calls[0][0][0]["content"]


def test_rewrite_is_skipped_without_api_key():
    calls = []

    def should_not_run(*args, **kwargs):
        calls.append(True)
        return response("should not run")

    query = RecallQueryBuilder(
        LLMConfig(), rewrite=True, completer=should_not_run
    ).build("那附近便宜吗", ["我在看滨江的房子"])

    assert calls == []
    assert "滨江" in query.text


def test_rewrite_falls_back_to_combined_text(monkeypatch):
    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "test-key")

    def fail(*args, **kwargs):
        raise RuntimeError("offline")

    query = RecallQueryBuilder(
        LLMConfig(), rewrite=True, completer=fail
    ).build("那附近便宜吗", ["我在看滨江的房子"])

    assert "滨江" in query.text
    assert query.rewritten is False


@pytest.mark.parametrize("text,recent,expected", [
    ("你好", [], False), ("帮我解释 Python", [], False), ("帮我翻译这句话", [], False),
    ("搜索 https://example.com", [], False), ("HTTP 是什么", [], False),
    ("我喜欢什么饮料", [], True), ("你还记得上次的计划吗", [], True),
    ("那应该怎么选", ["我的预算有限"], True),
    ("那怎么用", ["解释 Python"], False),
])
def test_automatic_recall_gate(text, recent, expected):
    from quiet_memo.agent.recall import should_auto_recall
    assert should_auto_recall(text, recent) is expected
