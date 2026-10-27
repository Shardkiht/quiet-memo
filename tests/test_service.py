from __future__ import annotations

import pytest

pytestmark = pytest.mark.memory

from quiet_memo.infra.config import Config, LLMConfig
from quiet_memo.memory.extractor import StaticExtractor
from quiet_memo.memory.models import Candidate, ExtractionResult, PendingItem
from quiet_memo.memory.service import MemoryService


def make_service(tmp_path, candidates):
    config = Config(vault=tmp_path / "vault", llm=LLMConfig())
    extractor = StaticExtractor(ExtractionResult(memories=candidates))
    service = MemoryService(config, extractor=extractor)
    service.init_vault()
    return service


def test_init_creates_vault_git_and_readme(tmp_path):
    service = make_service(tmp_path, [])
    vault = tmp_path / "vault"
    assert (vault / "README.md").exists()
    assert (vault / ".git").is_dir()
    assert service.history(limit=5)[0]["subject"] == "memory: 初始化记忆库"


def test_memorize_admits_and_queues(tmp_path):
    service = make_service(
        tmp_path,
        [
            Candidate(text="偏好清淡风味", topic="preferences", confidence=0.95),
            Candidate(text="可能喜欢浅烘", topic="preferences", confidence=0.5),
            Candidate(text="最近在吃药", topic="health", confidence=0.99, sensitive=True),
            Candidate(text="预算改为 5000", topic="housing", confidence=0.99, kind="conflict"),
        ],
    )

    result = service.memorize("一段对话", source="chat")

    assert [entry.text for entry in result.admitted] == ["偏好清淡风味"]
    assert {item.text for item in result.queued} == {"可能喜欢浅烘", "最近在吃药", "预算改为 5000"}
    assert service.status()["pending"] == 3


def test_memorize_skips_duplicates(tmp_path):
    service = make_service(tmp_path, [Candidate(text="偏好清淡风味", confidence=0.95)])
    service.memorize("第一次")
    result = service.memorize("第二次")
    assert result.admitted == []
    assert result.duplicates == ["偏好清淡风味"]
    assert len(service.store.entries()) == 1


def test_approve_writes_entry_and_clears_pending(tmp_path):
    service = make_service(
        tmp_path, [Candidate(text="可能喜欢浅烘", topic="preferences", confidence=0.5)]
    )
    item = service.memorize("对话").queued[0]

    entry = service.approve(item.id)

    assert entry.text == "可能喜欢浅烘"
    assert entry.metadata["source"] == "chat+approved"
    assert service.pending() == []
    assert "人工确认" in (tmp_path / "vault" / "memories" / "preferences.md").read_text(encoding="utf-8")


def test_reject_clears_pending(tmp_path):
    service = make_service(tmp_path, [Candidate(text="不确定", confidence=0.4)])
    item = service.memorize("对话").queued[0]

    assert service.reject(item.id).id == item.id
    assert service.pending() == []
    assert service.store.entries() == []


def test_note_rejects_duplicate(tmp_path):
    service = make_service(tmp_path, [])
    service.note("使用 手冲滤纸", topic="devices")
    with pytest.raises(ValueError):
        service.note("使用 手冲滤纸", topic="devices")


def test_commit_generates_fallback_summary_and_history(tmp_path):
    service = make_service(tmp_path, [])
    service.note("使用 手冲滤纸", topic="devices")

    result = service.commit(push=False)

    assert result["committed"]
    assert result["summary"].startswith("memory:")
    assert result["hash"]
    assert service.history(1)[0]["hash"] == result["hash"]

    again = service.commit(push=False)
    assert not again["committed"]


def test_push_without_remote_reports_error_but_keeps_commit(tmp_path):
    # 回归：push 失败不再抛异常掩盖已完成的本地提交。
    service = make_service(tmp_path, [])
    service.note("一条记录", topic="misc")
    service.commit(push=False)
    service.note("另一条记录", topic="misc")

    result = service.commit(push=True)

    assert result["committed"] and result["hash"]
    assert result["pushed"] is False
    assert "remote" in result["push_error"]


def test_recall_and_topics(tmp_path):
    service = make_service(tmp_path, [])
    service.note("喜欢清淡风味", topic="preferences", title="偏好")
    service.note("找后端工作", topic="career")

    assert service.recall("清淡")[0].topic == "preferences"
    topics = {item["topic"]: item for item in service.topics()}
    assert topics["preferences"]["title"] == "偏好"
    assert topics["preferences"]["entries"] == 1


def test_read_topic_returns_only_active_entries_for_known_topic(tmp_path):
    service = make_service(tmp_path, [])
    service.note("保留的有效条目", topic="preferences", title="偏好")
    removed = service.note("已删除的旧条目", topic="preferences")
    service.forget(removed.id)

    result = service.read_topic("偏好")

    assert result["topic"] == "preferences"
    assert result["content"] == "- 保留的有效条目"
    assert result["entries"] == 1
    assert result["truncated"] is False
    with pytest.raises(KeyError):
        service.read_topic("../preferences")


def test_read_topic_truncates_to_token_budget(tmp_path):
    from quiet_memo.infra.tokens import estimate_tokens

    service = make_service(tmp_path, [])
    service.note("很长的主题内容" * 100, topic="documents")

    result = service.read_topic("documents", max_tokens=30)

    assert result["truncated"] is True
    assert estimate_tokens(result["content"]) <= 30


def test_commit_push_failure_does_not_hide_commit(tmp_path):
    # auto_push 但无 remote：本地提交必须成功返回 hash，push 失败只记录在 push_error。
    config = Config(vault=tmp_path / "vault", llm=LLMConfig(), auto_push=True)
    service = MemoryService(
        config, extractor=StaticExtractor(ExtractionResult(memories=[]))
    )
    service.init_vault()
    service.note("一条记录", topic="misc")

    result = service.commit()

    assert result["committed"] and result["hash"]
    assert result["pushed"] is False
    assert "remote" in result["push_error"]


def test_load_persona_falls_back_to_gbk(tmp_path):
    # 记事本 ANSI（GBK）保存的 SOUL.md 不应炸掉整轮对话。
    from quiet_memo.memory.service import load_persona

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "SOUL.md").write_bytes("你是阿澈，语气简洁。".encode("gbk"))

    assert "阿澈" in load_persona(vault)


def test_load_persona_missing_or_empty(tmp_path):
    from quiet_memo.memory.service import load_persona

    vault = tmp_path / "vault"
    vault.mkdir()
    assert load_persona(vault) == ""
    (vault / "SOUL.md").write_text("   \n", encoding="utf-8")
    assert load_persona(vault) == ""


def test_commit_pending_changes_auto_commits_without_llm(tmp_path):
    # 启动检查：有未提交变更时自动提交（不 push、不调 LLM），无变更时返回 None。
    service = make_service(tmp_path, [])
    assert service.commit_pending_changes() is None  # 干净库

    service.note("启动前未提交的一条", topic="misc")
    commit = service.commit_pending_changes()
    assert commit
    assert not service.repo.has_changes()
    assert any("自动提交" in item["subject"] for item in service.history(3))


def test_note_and_correct_reject_secrets(tmp_path):
    service = make_service(tmp_path, [])
    with pytest.raises(ValueError):
        service.note("api_key=abcd1234")
    assert service.store.entries() == []

    entry = service.note("普通偏好", topic="preferences")
    with pytest.raises(ValueError):
        service.correct(entry.id, "password=abcd1234")
    assert service.store.find(entry.id).status == "active"
    assert [item.text for item in service.store.entries()] == ["普通偏好"]


def test_approve_rejects_secret_and_keeps_pending(tmp_path):
    service = make_service(tmp_path, [])
    item = PendingItem.from_candidate(
        Candidate(text="api_key=abcd1234", topic="misc", confidence=0.5), "chat", "low confidence"
    )
    service.queue.add(item)

    with pytest.raises(ValueError):
        service.approve(item.id)
    assert service.store.entries() == []
    assert [pending.id for pending in service.pending()] == [item.id]
