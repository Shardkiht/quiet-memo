from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.memory

from quiet_memo.memory.extractor import ExtractorError, parse_extraction
from quiet_memo.memory.models import MemoryContext


@pytest.mark.parametrize("value", ["2024", "2024-02", "2024-02-29"])
def test_context_preserves_date_precision(value):
    payload = {"memories": [{"text": "合成事件", "context": {"event_time": value}}]}
    candidate = parse_extraction(json.dumps(payload)).memories[0]
    assert candidate.context.event_time == value
    assert candidate.context.valid_to is None


@pytest.mark.parametrize("value", ["0000", "2023-02-29", "2024-13", "2024-1", "昨天", "2024-02-29T12:00:00Z"])
def test_context_rejects_invalid_or_invented_date_formats(value):
    with pytest.raises(ValidationError):
        MemoryContext(event_time=value)


def test_context_relative_time_without_anchor_stays_unknown():
    candidate = parse_extraction(
        '{"memories":[{"text":"去年去过合成展馆","context":{"time_text":"去年"}}]}'
    ).memories[0]
    assert candidate.context.time_text == "去年"
    assert candidate.context.event_time is None
    assert candidate.context.valid_from is None


def test_missing_context_does_not_add_dates_or_emotions():
    assert parse_extraction('{"memories":[{"text":"喜欢热饮"}]}').memories[0].context is None


@pytest.mark.parametrize("start,end", [("2025", "2024"), ("2024-03", "2024-02"), ("2024-02-29", "2024-02-28")])
def test_context_rejects_reversed_validity(start, end):
    with pytest.raises(ValidationError, match="结束时间"):
        MemoryContext(valid_from=start, valid_to=end)


def test_partial_validity_dates_are_not_fabricated():
    context = MemoryContext(valid_from="2024-06-15", valid_to="2024")
    assert context.valid_from == "2024-06-15"
    assert context.valid_to == "2024"


@pytest.mark.parametrize("kind", ["event", "residence"])
def test_context_distinguishes_location_kind(kind):
    assert MemoryContext(location="合成地点", location_kind=kind).location_kind == kind


@pytest.mark.parametrize("context", [
    {"location": "合成地点"}, {"location_kind": "event"},
    {"emotion": "开心", "emotion_source": "inferred"}, {"emotion": "可能不开心"},
    {"emotion_source": "explicit"}, {"emotion": "  ", "emotion_source": "explicit"},
])
def test_context_rejects_ambiguous_or_inferred_labels(context):
    with pytest.raises(ValidationError):
        MemoryContext.model_validate(context)


def test_emotion_defaults_to_present_expression():
    context = MemoryContext(emotion="如今想起很开心", emotion_source="explicit")
    assert context.emotion_at == "expression"
    then = MemoryContext(emotion="当时很开心", emotion_source="explicit", emotion_at="event")
    assert then.emotion_at == "event"


def test_llm_extractor_requests_optional_context_without_current_date(monkeypatch):
    from quiet_memo.infra.config import LLMConfig
    from quiet_memo.memory import extractor

    captured = []
    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "synthetic-key")

    def fake_complete(config, messages, **kwargs):
        captured.extend(messages)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"memories":[{"text":"去年参加合成展览","context":{"time_text":"去年"}}]}'
        ))])

    monkeypatch.setattr(extractor, "complete", fake_complete)
    candidate = extractor.LLMExtractor(LLMConfig()).extract("去年参加合成展览").memories[0]
    assert "禁止用抽取或写入当天" in captured[0]["content"]
    assert "emotion_source" in captured[0]["content"]
    assert candidate.context.event_time is None


def test_parse_plain_json():
    result = parse_extraction(
        '{"memories":[{"text":"喜欢清淡风味","topic":"preferences","confidence":0.92}]}'
    )
    assert len(result.memories) == 1
    assert result.memories[0].topic == "preferences"
    assert result.memories[0].confidence == 0.92


def test_parse_fenced_json():
    content = '```json\n{"memories":[{"text":"使用 手冲滤纸","topic":"devices"}]}\n```'
    result = parse_extraction(content)
    assert result.memories[0].text == "使用 手冲滤纸"


def test_parse_list_payload():
    result = parse_extraction('[{"text":"事实","topic":"misc"}]')
    assert len(result.memories) == 1


def test_parse_skips_empty_text():
    result = parse_extraction('{"memories":[{"text":"  "},{"text":"有效"}]}')
    assert [item.text for item in result.memories] == ["有效"]


def test_parse_rejects_non_json():
    with pytest.raises(ExtractorError):
        parse_extraction("模型拒绝回答")
