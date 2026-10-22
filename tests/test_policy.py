from __future__ import annotations

from quiet_memo.memory.models import Candidate
from quiet_memo.memory.policy import decide


def test_high_confidence_is_admitted():
    result = decide(Candidate(text="事实", confidence=0.9), threshold=0.8)
    assert result.admit
    assert result.reason == "auto"


def test_low_confidence_is_queued():
    result = decide(Candidate(text="事实", confidence=0.5), threshold=0.8)
    assert not result.admit
    assert "low confidence" in result.reason


def test_sensitive_is_queued_even_with_high_confidence():
    result = decide(Candidate(text="健康情况", confidence=0.99, sensitive=True))
    assert not result.admit
    assert result.reason == "sensitive"


def test_conflict_and_update_are_queued():
    assert not decide(Candidate(text="a", kind="conflict", confidence=0.99)).admit
    assert not decide(Candidate(text="b", kind="update", confidence=0.99)).admit
