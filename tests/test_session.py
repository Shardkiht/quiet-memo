from __future__ import annotations

import pytest

pytestmark = pytest.mark.agent

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


def test_session_redacts_credentials_in_nested_records(tmp_path):
    store, session = make_store(tmp_path)
    secret = "SYNTHETIC-ONLY-12345"
    record = store.append(session, {
        "type": "assistant", "content": f"密码是 {secret}",
        "tool_calls": [{"arguments": {"text": f"API key: {secret}"}}],
    })
    store.rename(session, f"密码是 {secret}")
    assert secret not in session.path.read_text(encoding="utf-8")
    assert secret not in str(record)
    assert secret not in str(session.records)
    assert secret not in str(store.load(session.id).records)
    assert "敏感信息已隐藏" in record["content"]


@pytest.mark.parametrize("ending", [b'', b'\n', "合".encode("utf-8")[:2]])
def test_session_recovers_torn_tail_without_rewriting_original(tmp_path, ending):
    store, session = make_store(tmp_path)
    store.append(session, {"type": "message", "id": "m1", "role": "user", "content": "合成陈述"})
    with session.path.open("ab") as handle:
        handle.write(b'{"type":"assistant","content":"' + ending)
    original = session.path.read_bytes()
    loaded = store.load(session.id)
    assert loaded.user_texts() == ["合成陈述"]
    assert loaded.damaged_tail_line is not None
    assert store.list()[0]["id"] == session.id
    assert store.unconsolidated()[0].id == session.id
    assert session.path.read_bytes() == original
    store.append(loaded, {"type": "message", "id": "m2", "role": "user", "content": "接续陈述"})
    assert session.path.read_bytes().startswith(original)
    reloaded = store.load(session.id)
    assert reloaded.user_texts() == ["合成陈述", "接续陈述"]
    assert any(record.get("type") == "recovery" for record in reloaded.records)


def test_session_appends_after_complete_record_without_newline(tmp_path):
    store, session = make_store(tmp_path)
    session.path.write_bytes(session.path.read_bytes().rstrip(b"\n"))
    original = session.path.read_bytes()
    loaded = store.load(session.id)
    store.append(loaded, {"type": "message", "id": "m1", "role": "user", "content": "合成陈述"})
    assert session.path.read_bytes().startswith(original + b"\n")
    assert store.load(session.id).user_texts() == ["合成陈述"]


def test_session_does_not_silently_ignore_unmarked_middle_corruption(tmp_path):
    import json

    store, session = make_store(tmp_path)
    with session.path.open("ab") as handle:
        handle.write(b'broken\n{"type":"message","content":"synthetic"}\n')
    with pytest.raises(json.JSONDecodeError):
        store.load(session.id)


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


def test_unconsolidated_filters_threshold_before_limit(tmp_path):
    store = SessionStore(tmp_path)
    sessions = []
    for index, count in enumerate((5, 1, 2, 1)):
        session = store.create()
        sessions.append(session)
        for number in range(count):
            store.append(session, {
                "type": "message", "id": f"m{index}-{number}", "role": "user",
                "content": f"合成陈述 {index}-{number}",
                "at": f"2026-01-0{index + 1}T12:00:00",
            })

    assert [s.id for s in store.unconsolidated(3, min_new_messages=3)] == [sessions[0].id]
    assert [s.id for s in store.unconsolidated(3)] == [s.id for s in reversed(sessions[1:])]
    assert store.unconsolidated(3, exclude=sessions[0].id, min_new_messages=3) == []


def test_distillation_tracks_ids_separately_from_new_summary_boundaries(tmp_path):
    store, session = make_store(tmp_path)
    store.append(session, {"type": "message", "id": "m1", "role": "user", "content": "旧版已沉淀"})
    store.append(session, {"type": "summary", "content": "旧版摘要", "covered_records": 2})
    store.append(session, {"type": "message", "id": "m2", "role": "user", "content": "首次抽取失败"})
    store.append(session, {
        "type": "summary", "content": "失败后仍压缩", "covered_records": 4, "distilled_ids": [],
    })
    store.append(session, {"type": "message", "id": "m3", "role": "user", "content": "后续抽取成功"})
    store.append(session, {
        "type": "summary", "content": "后续摘要", "covered_records": 6, "distilled_ids": ["m3"],
    })
    store.append(session, {"type": "message", "id": "m4", "role": "user", "content": "新消息"})

    loaded = store.load(session.id)
    assert loaded.user_texts_after_summary() == ["新消息"]
    assert loaded.user_texts_after_distilled() == ["首次抽取失败", "新消息"]
    store.append(loaded, {"type": "distilled", "up_to": "m4"})
    assert store.load(session.id).user_texts_after_distilled() == []


@pytest.mark.parametrize("succeeded", [False, True])
def test_undo_preserves_distillation_receipts_but_excludes_undone_messages(tmp_path, succeeded):
    store, session = make_store(tmp_path)
    store.append(session, {"type": "message", "id": "m1", "role": "user", "content": "保留陈述"})
    store.append(session, {"type": "message", "id": "m2", "role": "user", "content": "撤销陈述"})
    store.append(session, {
        "type": "summary", "content": "撤销后失效的摘要", "covered_records": 2,
        "distilled_ids": ["m1"] if succeeded else [],
    })
    store.append(session, {"type": "undo", "message_id": "m2"})

    loaded = store.load(session.id)
    assert loaded.summary_text() == ""
    assert loaded.user_texts() == ["保留陈述"]
    assert loaded.user_texts_after_distilled() == ([] if succeeded else ["保留陈述"])


def test_list_reuses_cached_summary_until_file_changes(tmp_path, monkeypatch):
    store, session = make_store(tmp_path)
    store.append(session, {
        "type": "message", "id": new_message_id(), "role": "user", "content": "第一条",
    })
    calls = 0
    original = store.load

    def counting(session_id):
        nonlocal calls
        calls += 1
        return original(session_id)

    monkeypatch.setattr(store, "load", counting)

    first = store.list()
    second = store.list()
    assert calls == 1
    assert first == second

    store.append(session, {
        "type": "message", "id": new_message_id(), "role": "user", "content": "第二条",
    })
    third = store.list()
    assert calls == 2
    assert third[0]["messages"] == 2


def test_unconsolidated_skips_unchanged_consolidated_files(tmp_path, monkeypatch):
    store, session = make_store(tmp_path)
    message_id = new_message_id()
    store.append(session, {
        "type": "message", "id": message_id, "role": "user", "content": "已经沉淀",
    })
    store.append(session, {"type": "distilled", "up_to": message_id})
    calls = 0
    original = store.load

    def counting(session_id):
        nonlocal calls
        calls += 1
        return original(session_id)

    monkeypatch.setattr(store, "load", counting)

    assert store.unconsolidated(3) == []
    assert store.unconsolidated(3, min_new_messages=3) == []
    assert calls == 1


def test_distilled_high_water_mark_does_not_move_backwards(tmp_path):
    store, session = make_store(tmp_path)
    for number in range(3):
        store.append(session, {
            "type": "message", "id": f"m{number}", "role": "user", "content": f"陈述{number}",
        })
    store.append(session, {"type": "distilled", "up_to": "m1"})
    store.append(session, {"type": "distilled", "up_to": "m0"})
    assert store.load(session.id).user_texts_after_distilled() == ["陈述2"]
