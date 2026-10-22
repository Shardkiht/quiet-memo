from __future__ import annotations

import pytest

from quiet_memo.infra.config import Config, LLMConfig
from quiet_memo.infra.gitops import GitError
from quiet_memo.memory.extractor import StaticExtractor
from quiet_memo.memory.models import Candidate, ExtractionResult
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


def test_push_without_remote_raises(tmp_path):
    service = make_service(tmp_path, [])
    service.note("一条记录", topic="misc")
    service.commit(push=False)
    service.note("另一条记录", topic="misc")
    with pytest.raises(GitError):
        service.commit(push=True)


def test_recall_and_topics(tmp_path):
    service = make_service(tmp_path, [])
    service.note("喜欢清淡风味", topic="preferences", title="偏好")
    service.note("找后端工作", topic="career")

    assert service.recall("清淡")[0].topic == "preferences"
    topics = {item["topic"]: item for item in service.topics()}
    assert topics["preferences"]["title"] == "偏好"
    assert topics["preferences"]["entries"] == 1
