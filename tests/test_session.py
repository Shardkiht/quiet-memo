from __future__ import annotations

from quiet_memo.agent.session import SessionStore, new_message_id


def make_store(tmp_path):
    return SessionStore(tmp_path), SessionStore(tmp_path).create()


def test_create_append_and_reload(tmp_path):
    store, session = make_store(tmp_path)
    store.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "你好"},
    )
    store.append(
        session,
        {"type": "assistant", "id": new_message_id(), "content": "你好，有什么可以帮你？"},
    )

    loaded = store.load(session.id)
    assert loaded.id == session.id
    assert [message["role"] for message in loaded.messages] == ["user"]
    assert loaded.messages[0]["content"] == "你好"
    assert loaded.title() == "你好"


def test_rename_and_list(tmp_path):
    store, session = make_store(tmp_path)
    store.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "一段很长的对话内容"},
    )
    store.rename(session, "口味偏好")

    rows = store.list()
    assert rows[0]["id"] == session.id
    assert rows[0]["title"] == "口味偏好"
    assert rows[0]["messages"] == 1


def test_summary_covers_records(tmp_path):
    store, session = make_store(tmp_path)
    store.append(session, {"type": "message", "id": "m1", "role": "user", "content": "第一句"})
    store.append(session, {"type": "tool", "id": "t1", "name": "recall", "content": "结果"})
    store.append(session, {"type": "message", "id": "m2", "role": "user", "content": "第二句"})
    store.append(
        session,
        {
            "type": "summary",
            "id": "s1",
            "content": "摘要",
            "covered_records": 3,
        },
    )

    loaded = store.load(session.id)
    assert loaded.summary_text() == "摘要"
    assert loaded.covered_records() == 3
    assert [record["id"] for record in loaded.records_after_summary()] == ["m2"]
    assert loaded.user_texts_after_summary() == ["第二句"]


def test_undo_persists_and_allows_reedited_message(tmp_path):
    store, session = make_store(tmp_path)
    for message_id, text in (("m1", "第一问"), ("m2", "第二问")):
        store.append(session, {"type": "message", "id": message_id, "role": "user", "content": text})
        store.append(
            session,
            {"type": "assistant", "id": f"a{message_id[1:]}", "content": f"{text}的回答"},
        )
    raw_records = list(session.records)

    store.append(session, {"type": "undo", "id": "u1", "message_id": "m2"})
    loaded = store.load(session.id)

    assert loaded.user_texts() == ["第一问"]
    assert loaded.last_user_message_id() == "m1"
    assert [record["id"] for record in loaded.records_after_summary()] == ["m1", "a1"]
    assert loaded.user_texts_after_distilled() == ["第一问"]
    assert loaded.records[: len(raw_records)] == raw_records

    store.append(loaded, {"type": "message", "id": "m3", "role": "user", "content": "编辑后"})
    store.append(loaded, {"type": "assistant", "id": "a3", "content": "新回答"})
    assert loaded.user_texts() == ["第一问", "编辑后"]

    store.append(loaded, {"type": "undo", "id": "u2", "message_id": "m3"})
    assert store.load(session.id).user_texts() == ["第一问"]


def test_invalid_session_id_is_rejected(tmp_path):
    store = SessionStore(tmp_path)
    try:
        store.load("../../etc/passwd")
    except KeyError as exc:
        assert "非法会话" in str(exc)
    else:
        raise AssertionError("应当拒绝非法会话 ID")


def test_delete_session_file(tmp_path):
    store, session = make_store(tmp_path)
    store.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "待删除"},
    )

    assert store.delete(session.id) is True
    assert not session.path.exists()
    assert store.list() == []
    assert store.delete(session.id) is False


def test_create_retries_id_collision_without_overwrite(tmp_path, monkeypatch):
    from datetime import datetime

    from quiet_memo.agent import session as session_module

    class FixedDateTime:
        @staticmethod
        def now():
            return datetime(2026, 1, 1, 0, 0, 0)

    monkeypatch.setattr(session_module, "datetime", FixedDateTime)
    tokens = iter(["aaaa", "aaaa", "ffff"])
    monkeypatch.setattr(session_module.secrets, "token_hex", lambda _: next(tokens))
    store = SessionStore(tmp_path)

    first = store.create()
    second = store.create()

    assert first.id == "s-20260101-000000-aaaa"
    assert second.id == "s-20260101-000000-ffff"
    assert store.load(first.id).records[0]["id"] == first.id
