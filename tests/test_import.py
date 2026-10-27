from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.memory

from quiet_memo.infra.config import Config, LLMConfig, RetrievalConfig
from quiet_memo.memory.extractor import (
    ExtractorError,
    ImportExtractor,
    parse_import,
    split_chunks,
)
from quiet_memo.memory.models import Candidate, ExtractionResult
from quiet_memo.memory.service import MemoryService


def response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def service(tmp_path) -> MemoryService:
    config = Config(
        vault=tmp_path / "vault", llm=LLMConfig(), retrieval=RetrievalConfig(method="keyword")
    )
    return MemoryService(config)


def test_parse_import_accepts_valid_payload():
    result = parse_import(
        '{"memories":[{"text":"偏好清淡风味","topic":"preferences","confidence":0.9}]}'
    )

    assert len(result.memories) == 1
    assert result.memories[0].topic == "preferences"


def test_parse_import_rejects_invalid_item():
    with pytest.raises(ExtractorError, match="第 1 条"):
        parse_import('{"memories":[{"text":"x","confidence":1.5}]}')


def test_parse_import_rejects_missing_memories():
    with pytest.raises(ExtractorError, match="memories"):
        parse_import('{"items":[]}')


def test_import_extractor_retries_with_feedback(monkeypatch):
    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "test-key")
    outputs = [
        response("不是 JSON"),
        response('{"memories":[{"text":"瑞幸偏拿铁","topic":"preferences","confidence":0.9}]}'),
    ]
    calls = []

    def fake_complete(config, messages, **kwargs):
        calls.append(messages)
        return outputs[len(calls) - 1]

    result = ImportExtractor(LLMConfig(), completer=fake_complete).extract("档案内容")

    assert result.memories[0].text == "瑞幸偏拿铁"
    assert len(calls) == 2
    assert "上一轮输出被拒绝" in calls[1][-1]["content"]


def test_import_extractor_gives_up_after_three_attempts(monkeypatch):
    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "test-key")
    calls = []

    def fake_complete(config, messages, **kwargs):
        calls.append(1)
        return response("还是不是 JSON")

    with pytest.raises(ExtractorError, match="连续 3 次"):
        ImportExtractor(LLMConfig(), completer=fake_complete).extract("档案内容")

    assert len(calls) == 3


def test_import_extractor_requires_api_key():
    with pytest.raises(ExtractorError, match="API Key"):
        ImportExtractor(LLMConfig()).extract("档案内容")


def test_import_text_adjudicates_candidates(tmp_path):
    svc = service(tmp_path)
    svc.store.append("preferences", "已经存在的记忆", source="seed")
    extractor = _StaticResult(
        [
            Candidate(text="咖啡偏好清淡风味", topic="preferences", confidence=0.95),
            Candidate(text="当时记录未验证：曾询问茶叶保存", topic="learning", confidence=0.5),
            Candidate(text="密码是 abcd1234", topic="identity", confidence=0.9),
            Candidate(text="已经存在的记忆", topic="preferences", confidence=0.9),
        ]
    )

    result = svc.import_text("档案内容", source="import:test", extractor=extractor)

    assert len(result.admitted) == 1
    assert len(result.queued) == 1
    assert len(result.duplicates) == 1
    assert len(result.blocked) == 1
    assert svc.store.entries()[0].metadata["source"] == "seed"
    assert "咖啡偏好清淡" in svc.store.entries()[-1].text
    assert svc.pending()[0].reason.startswith("low confidence")


def test_import_text_dry_run_writes_nothing(tmp_path):
    svc = service(tmp_path)
    extractor = _StaticResult(
        [Candidate(text="干跑条目", topic="preferences", confidence=0.95)]
    )

    result = svc.import_text("档案内容", dry_run=True, extractor=extractor)

    assert result.admitted[0].id == "(dry-run)"
    assert svc.store.entries() == []
    assert svc.pending() == []


def test_import_text_detects_conflicts(tmp_path):
    svc = service(tmp_path)
    svc.store.append("housing", "在找市区的房子，预算 4000 以内", source="seed")
    extractor = _StaticResult(
        [Candidate(text="在找市区的房子，预算 5000 以内", topic="housing", confidence=0.95)]
    )

    result = svc.import_text("档案内容", extractor=extractor)

    assert result.admitted == []
    assert result.queued[0].conflict_with is not None


def test_split_chunks_respects_boundaries():
    paragraphs = ["段落一" * 10, "段落二" * 10, "段落三" * 4000]
    chunks = split_chunks("\n\n".join(paragraphs), max_chars=6000)

    assert all(len(chunk) <= 6000 for chunk in chunks)
    assert "".join(chunk.replace("\n\n", "") for chunk in chunks).startswith("段落一")


class _StaticResult:
    def __init__(self, memories: list[Candidate]):
        from quiet_memo.memory.models import ExtractionResult

        self.result = ExtractionResult(memories=memories)

    def extract(self, text: str) -> object:
        return self.result


def test_import_extractor_injects_existing_topics(monkeypatch):
    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "test-key")
    captured = {}

    def fake_complete(config, messages, **kwargs):
        captured["user"] = messages[-1]["content"]

        class _Msg:
            content = '{"memories":[]}'

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()

    ImportExtractor(
        LLMConfig(), completer=fake_complete, topics=["preferences", "08-coffee-food-routine"]
    ).extract("内容")
    assert "08-coffee-food-routine" in captured["user"]
    assert "优先归入" in captured["user"]

    ImportExtractor(LLMConfig(), completer=fake_complete).extract("内容")
    assert "已有主题" not in captured["user"]


def test_import_text_passes_store_topics_to_extractor(tmp_path, monkeypatch):
    # 回归：service 注入路径要把 store.topics() 的主题名传给 ImportExtractor。
    import quiet_memo.memory.service as service_module
    from quiet_memo.memory.extractor import StaticExtractor

    captured = {}

    class FakeImportExtractor:
        def __init__(self, config, topics=None):
            captured["topics"] = topics or []

        def extract(self, text):
            return ExtractionResult(memories=[])

    monkeypatch.setattr(service_module, "ImportExtractor", FakeImportExtractor)
    config = Config(vault=tmp_path / "vault")
    service = MemoryService(config, extractor=StaticExtractor(ExtractionResult(memories=[])))
    service.init_vault()
    service.note("既有条目", topic="preferences")

    service.import_text("新内容")

    assert captured["topics"] == ["preferences"]
