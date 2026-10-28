from __future__ import annotations

import json
from datetime import datetime

import pytest
from pydantic import ValidationError

from quiet_memo.memory.service import MemoryService

pytestmark = pytest.mark.memory


def test_inference_is_separate_from_facts_and_recall(service):
    source = service.note("喜欢在安静环境阅读", topic="preferences")
    before = service.store.topic_path("preferences").read_bytes()
    item = service.propose_inference("似乎偏好安静的活动", [source.id])
    assert item.status == "hypothesis"
    assert item.kind == "profile"
    assert item.source_ids == [source.id]
    assert datetime.fromisoformat(item.created_at).utcoffset() is not None
    assert item.updated_at == item.created_at
    assert service.store.topic_path("preferences").read_bytes() == before
    assert service.store.find(item.id) is None
    assert service.recall("似乎偏好安静的活动")[0].id == source.id
    assert all(hit.id != item.id for hit in service.recall("安静"))
    assert service.pending() == []
    fresh = MemoryService(service.config)
    assert fresh.inferences.all() == [item]


def test_repeated_evidence_is_idempotent_without_confidence_inflation(service):
    first = service.note("偏好纸质阅读", topic="preferences")
    second = service.note("周末常去合成书屋", topic="events")
    item = service.propose_inference("似乎喜欢阅读", [second.id, first.id, second.id])
    before = service.inferences.path.read_bytes()
    again = service.propose_inference("  似乎喜欢阅读  ", [first.id, second.id])
    assert again == item
    assert item.source_ids == sorted([first.id, second.id])
    assert service.inferences.path.read_bytes() == before
    assert "confidence" not in item.model_dump()


def test_changed_evidence_creates_distinct_hypothesis(service):
    first = service.note("偏好纸质阅读", topic="preferences")
    second = service.note("常看合成读书笔记", topic="events")
    one = service.propose_inference("似乎喜欢阅读", [first.id])
    before = service.inferences.path.read_bytes()
    two = service.propose_inference("似乎喜欢阅读", [first.id, second.id])
    assert one.id != two.id
    assert service.inferences.path.read_bytes().startswith(before)
    assert len(service.inferences.all()) == 2


@pytest.mark.parametrize("kind", ["profile", "emotion"])
def test_inference_kind_preserves_uncertainty(service, kind):
    source = service.note("最近常提起合成展览", topic="events")
    assert service.propose_inference("可能期待下次展览", [source.id], kind).status == "hypothesis"


@pytest.mark.parametrize("source_state", ["missing", "deleted", "superseded", "inference"])
def test_inference_rejects_non_fact_or_inactive_sources(service, source_state):
    source = service.note("偏好纸质阅读", topic="preferences")
    source_id = source.id
    if source_state == "missing":
        source_id = "mem-missing"
    elif source_state == "deleted":
        service.forget(source.id)
    elif source_state == "superseded":
        service.correct(source.id, "偏好电子阅读")
    else:
        source_id = service.propose_inference("似乎喜欢阅读", [source.id]).id
    before = service.inferences.path.read_bytes() if service.inferences.path.exists() else b""
    with pytest.raises(ValueError, match="当前有效的事实条目"):
        service.propose_inference("可能适合阅读活动", [source_id])
    assert (service.inferences.path.read_bytes() if service.inferences.path.exists() else b"") == before


@pytest.mark.parametrize("ids", [[], [""], ["   "]])
def test_inference_requires_nonempty_evidence(service, ids):
    with pytest.raises(ValidationError):
        service.propose_inference("合成假设", ids)
    assert not service.inferences.path.exists()


@pytest.mark.parametrize("text", [" ", "密码是 synthetic-only"])
def test_inference_rejects_empty_text_and_credentials(service, text):
    source = service.note("合成事实")
    with pytest.raises(ValueError):
        service.propose_inference(text, [source.id])
    assert not service.inferences.path.exists()


def test_inference_damaged_file_fails_without_overwriting(service):
    source = service.note("合成事实")
    item = service.propose_inference("合成假设", [source.id])
    with service.inferences.path.open("ab") as handle:
        handle.write(b'{"damaged":')
    before = service.inferences.path.read_bytes()
    with pytest.raises(ValidationError):
        service.propose_inference("另一条合成假设", [source.id])
    assert service.inferences.path.read_bytes() == before
    assert json.loads(before.splitlines()[0])["id"] == item.id
