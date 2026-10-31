from __future__ import annotations

import pytest

pytestmark = pytest.mark.agent

from quiet_memo.agent.context import ContextManager
from quiet_memo.agent.session import SessionStore, new_message_id
from quiet_memo.infra.config import ChatConfig
from quiet_memo.infra.tokens import estimate_messages_tokens


class FakeSummarizer:
    def __init__(self):
        self.calls = []

    def summarize(self, previous, transcript, max_chars=1200):
        self.calls.append((previous, transcript))
        return f"摘要-{len(self.calls)}"


def build_session(store, repeat=3):
    session = store.create()
    for index in range(6):
        store.append(
            session,
            {
                "type": "message",
                "id": new_message_id(),
                "role": "user",
                "content": f"第 {index} 轮用户消息：" + "这是一个比较长的偏好描述。" * repeat,
            },
        )
        store.append(
            session,
            {
                "type": "assistant",
                "id": new_message_id(),
                "content": f"第 {index} 轮助手回复：" + "已记录。" * 5,
            },
        )
    return session


def test_prepare_under_budget_does_not_compact(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    store.append(session, {"type": "message", "id": "m1", "role": "user", "content": "你好"})
    manager = ContextManager(
        ChatConfig(context_window=32000, reserve_output_tokens=1000),
        FakeSummarizer(),
        store,
    )

    plan = manager.prepare(session, "系统提示", "继续")

    assert not plan.compacted
    assert plan.summary == ""
    assert plan.messages[-1] == {"role": "user", "content": "继续"}


def test_force_compact_summarizes_and_flushes(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store)
    current_id = new_message_id()
    store.append(session, {"type": "message", "id": current_id, "role": "user", "content": "当前问题"})
    flushed = []

    def flush(texts, session):
        flushed.append(texts)
        return {"admitted": len(texts), "queued": 0}

    summarizer = FakeSummarizer()
    manager = ContextManager(
        ChatConfig(
            context_window=1000,
            reserve_output_tokens=100,
            keep_recent_tokens=100,
            compact_threshold=0.7,
        ),
        summarizer,
        store,
        flush=flush,
    )

    plan = manager.prepare(session, "系统提示", "当前问题", current_id=current_id, force=True)

    assert plan.compacted
    assert plan.evicted_records > 0
    assert plan.flushed["admitted"] > 0
    assert flushed and flushed[0]
    assert plan.summary.startswith("摘要-")
    assert any(message["content"].startswith("<session_summary>") for message in plan.messages)
    assert sum(1 for message in plan.messages if message["content"] == "当前问题") == 1

    loaded = store.load(session.id)
    assert loaded.latest_summary() is not None
    assert loaded.covered_records() > 0
    assert "当前问题" in loaded.user_texts_after_summary()


def test_resume_uses_persisted_summary(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store)
    manager = ContextManager(
        ChatConfig(context_window=800, reserve_output_tokens=100, keep_recent_tokens=100),
        FakeSummarizer(),
        store,
    )
    manager.prepare(session, "系统提示", "触发压缩", force=True)

    reloaded = store.load(session.id)
    plan = manager.prepare(reloaded, "系统提示", "下一轮")

    assert plan.summary
    assert any(message["content"].startswith("<session_summary>") for message in plan.messages)
    assert all("第 0 轮用户消息" not in str(message.get("content")) for message in plan.messages)


def test_undo_invalidates_later_summary_and_compacts_against_raw_positions(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    for index in range(1, 5):
        store.append(
            session,
            {
                "type": "message",
                "id": f"m{index}",
                "role": "user",
                "content": f"第 {index} 问" + "较长的对话内容" * 20,
            },
        )
        store.append(
            session,
            {
                "type": "assistant",
                "id": f"a{index}",
                "content": f"第 {index} 答" + "较长的回复内容" * 20,
            },
        )
    store.append(
        session,
        {"type": "summary", "id": "s1", "content": "旧摘要", "covered_records": 8},
    )
    store.append(session, {"type": "undo", "id": "u1", "message_id": "m3"})
    loaded = store.load(session.id)

    assert loaded.summary_text() == ""
    assert [text.split("较长", 1)[0] for text in loaded.user_texts()] == ["第 1 问", "第 2 问"]

    manager = ContextManager(
        ChatConfig(context_window=1000, reserve_output_tokens=100, keep_recent_tokens=100),
        FakeSummarizer(),
        store,
    )
    plan = manager.prepare(loaded, "系统提示", "下一问", force=True)

    assert loaded.covered_records() == 3  # meta + 第一条用户消息 + 第一条助手回复
    assert loaded.summary_text() == "摘要-1"
    contents = [str(message.get("content")) for message in plan.messages]
    assert any(content.startswith("第 2 问") for content in contents)
    assert all(not content.startswith("第 3 问") for content in contents)
    assert all("旧摘要" not in content for content in contents)


def test_no_compaction_with_only_current_message(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    store.append(session, {"type": "message", "id": "m1", "role": "user", "content": "唯一一句"})
    manager = ContextManager(ChatConfig(), FakeSummarizer(), store)

    plan = manager.prepare(session, "系统提示", "唯一一句", current_id="m1", force=True)

    assert not plan.compacted
    assert plan.summary == ""


@pytest.mark.parametrize("raise_error", [False, True])
def test_failed_flush_still_compacts_and_keeps_messages_for_retry(tmp_path, raise_error):
    store = SessionStore(tmp_path)
    session = build_session(store, repeat=80)
    original_texts = session.user_texts()
    store.append(session, {"type": "message", "id": "current", "role": "user", "content": "当前问题"})

    def failed_flush(texts, session):
        if raise_error:
            raise RuntimeError("synthetic timeout")
        return {"error": "synthetic timeout"}

    manager = ContextManager(
        ChatConfig(context_window=4096, reserve_output_tokens=512, keep_recent_tokens=1500),
        FakeSummarizer(), store, flush=failed_flush,
    )
    plan = manager.prepare(session, "系统提示", "当前问题", current_id="current")

    assert plan.compacted
    assert estimate_messages_tokens(plan.messages) <= manager.input_budget
    assert plan.flushed == {"error": "synthetic timeout"}
    assert any("distillation" in event and "failed" in event for event in plan.events)
    loaded = store.load(session.id)
    assert loaded.covered_records() > 0
    assert loaded.latest_summary()["distilled_ids"] == []
    assert loaded.user_texts_after_distilled() == original_texts + ["当前问题"]


def test_later_successful_compaction_does_not_cover_an_earlier_failed_flush(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store, repeat=80)
    attempts = []

    def flush(texts, session):
        attempts.append(texts)
        if len(attempts) == 1:
            raise RuntimeError("synthetic timeout")
        return {"admitted": len(texts), "queued": 0}

    manager = ContextManager(
        ChatConfig(context_window=4096, reserve_output_tokens=512, keep_recent_tokens=1500),
        FakeSummarizer(), store, flush=flush,
    )
    manager.prepare(session, "系统提示", "继续", force=True)
    failed_texts = list(attempts[0])
    loaded = store.load(session.id)
    for number in range(3):
        store.append(loaded, {
            "type": "message", "id": f"later-{number}", "role": "user",
            "content": f"后续合成陈述 {number}：" + "合成陈述" * 80,
        })
    plan = manager.prepare(loaded, "系统提示", "继续", force=True)

    assert plan.compacted
    assert len(attempts) >= 2
    pending = store.load(session.id).user_texts_after_distilled()
    assert all(text in pending for text in failed_texts)
    assert all(text not in pending for text in attempts[-1])


def test_compaction_without_flush_does_not_acknowledge_messages(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store)
    manager = ContextManager(
        ChatConfig(context_window=1000, reserve_output_tokens=100, keep_recent_tokens=100),
        FakeSummarizer(), store,
    )
    plan = manager.prepare(session, "系统提示", "继续", force=True)
    assert plan.compacted
    assert store.load(session.id).user_texts_after_distilled() == session.user_texts()


def test_compaction_does_not_flush_already_distilled_messages(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store)
    store.append(session, {"type": "distilled", "up_to": session.last_user_message_id()})
    flushed = []
    manager = ContextManager(
        ChatConfig(context_window=1000, reserve_output_tokens=100, keep_recent_tokens=100),
        FakeSummarizer(), store, flush=lambda texts, session: flushed.append(texts),
    )
    plan = manager.prepare(session, "系统提示", "继续", force=True)
    assert plan.compacted
    assert not flushed
    assert not store.load(session.id).needs_consolidation()
