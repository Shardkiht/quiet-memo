from __future__ import annotations

import pytest

from quiet_memo.memory.extractor import ExtractorError, parse_extraction


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
