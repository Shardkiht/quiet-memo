from __future__ import annotations

from quiet_memo.memory.detect import contains_secret, find_conflicts
from quiet_memo.memory.models import Candidate, Entry


def entry(entry_id: str, text: str, topic: str = "housing") -> Entry:
    return Entry(id=entry_id, topic=topic, text=text, timestamp="2026-10-22T12:00:00")


def test_detects_secret_material():
    assert contains_secret("我的密码是 abc12345")
    assert contains_secret("API key: test-placeholder")
    assert contains_secret("验证码 889900")
    assert not contains_secret("喜欢清淡风味")


def test_numeric_conflict():
    conflicts = find_conflicts(
        Candidate(text="我的租房预算改成 5000", topic="housing"),
        [entry("m1", "我的租房预算 4000")],
    )
    assert [item.id for item in conflicts] == ["m1"]


def test_negation_conflict():
    conflicts = find_conflicts(
        Candidate(text="我不喜欢喝浓咖啡", topic="preferences"),
        [entry("m1", "我喜欢喝浓咖啡", topic="preferences")],
    )
    assert [item.id for item in conflicts] == ["m1"]


def test_near_duplicate_is_conflict():
    conflicts = find_conflicts(
        Candidate(text="使用 手冲滤纸冲咖啡", topic="devices"),
        [entry("m1", "使用 手冲滤纸冲咖啡", topic="devices")],
    )
    assert [item.id for item in conflicts] == ["m1"]


def test_unrelated_fact_has_no_conflict():
    conflicts = find_conflicts(
        Candidate(text="喜欢清淡风味", topic="preferences"),
        [entry("m1", "目前住在市区", topic="housing")],
    )
    assert conflicts == []
