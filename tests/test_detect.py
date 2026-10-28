from __future__ import annotations

import pytest

pytestmark = pytest.mark.memory

from quiet_memo.memory.detect import contains_secret, find_conflicts
from quiet_memo.memory.models import Candidate, Entry


def entry(entry_id: str, text: str, topic: str = "housing") -> Entry:
    return Entry(id=entry_id, topic=topic, text=text, timestamp="2026-10-22T12:00:00")


@pytest.mark.parametrize(
    "text",
    [
        "我的密码是 demo-123456",
        "API key: test-placeholder",
        "api_key demo-123456",
        "access_token demo-token-placeholder",
        "password=!Demo1234",
        "password=@@!!",
        "验证码 458712请勿转发",
        "验证码458712，请勿转发",
        "密码：合成测试口令",
        "密码是 合成测试口令",
        "私钥: -----BEGIN PRIVATE KEY-----",
    ],
)
def test_detects_secret_material(text):
    assert contains_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        "喜欢清淡风味",
        "密码忘记了怎么办",
        "验证码一直收不到",
        "这个软件的密码管理器好用",
        "我忘了密码",
        "验证码收不到时需要联系客服",
        "密码 忘记了怎么办",
        "验证码 一直收不到",
    ],
)
def test_allows_statements_about_credentials(text):
    assert not contains_secret(text)


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
