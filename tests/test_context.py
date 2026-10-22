from __future__ import annotations

from quiet_memo.agent.context import ContextManager
from quiet_memo.agent.session import SessionStore, new_message_id
from quiet_memo.infra.config import ChatConfig


class FakeSummarizer:
    def __init__(self):
        self.calls = []

    def summarize(self, previous, transcript, max_chars=1200):
        self.calls.append((previous, transcript))
        return f"摘要-{len(self.calls)}"


def build_session(store):
    session = store.create()
    for index in range(6):
        store.append(
            session,
            {
                "type": "message",
                "id": new_message_id(),
                "role": "user",
                "content": f"第 {index} 轮用户消息：" + "这是一个比较长的偏好描述。" * 3,
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

    def flush(texts):
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


def test_no_compaction_with_only_current_message(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    store.append(session, {"type": "message", "id": "m1", "role": "user", "content": "唯一一句"})
    manager = ContextManager(ChatConfig(), FakeSummarizer(), store)

    plan = manager.prepare(session, "系统提示", "唯一一句", current_id="m1", force=True)

    assert not plan.compacted
    assert plan.summary == ""
