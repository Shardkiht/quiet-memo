from __future__ import annotations

import pytest

pytestmark = pytest.mark.memory

from quiet_memo.memory.models import PendingItem
from quiet_memo.memory.queue import PendingQueue


def make_item(item_id: str = "p-1") -> PendingItem:
    return PendingItem(
        id=item_id,
        created_at="2026-10-22T12:00:00",
        source="chat",
        text="候选事实",
        topic="preferences",
        confidence=0.5,
        kind="add",
        reason="low confidence 0.50",
    )


def test_add_and_load(tmp_path):
    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    assert [item.id for item in queue.all()] == ["p-1"]
    assert queue.get("p-1").text == "候选事实"


def test_resolve_removes_and_logs(tmp_path):
    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    queue.add(make_item("p-2"))

    resolved = queue.resolve("p-1", "approved")

    assert resolved.id == "p-1"
    assert [item.id for item in queue.all()] == ["p-2"]
    log = (tmp_path / ".quiet-memo" / "resolved.jsonl").read_text(encoding="utf-8")
    assert '"status": "approved"' in log
    assert "p-1" in log


def test_resolve_missing_returns_none(tmp_path):
    assert PendingQueue(tmp_path).resolve("nope", "rejected") is None


def test_save_failure_leaves_pending_queue_intact(tmp_path, monkeypatch):
    from quiet_memo.memory import queue as queue_module

    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    before = queue.pending_path.read_text(encoding="utf-8")

    def fail_replace(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(queue_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        queue.add(make_item("p-2"))

    assert queue.pending_path.read_text(encoding="utf-8") == before
    assert [item.id for item in queue.all()] == ["p-1"]
    assert not [item for item in queue.dir.iterdir() if item.name.endswith(".tmp")]


def test_resolve_only_removes_the_targeted_duplicate(tmp_path):
    queue = PendingQueue(tmp_path)
    queue.add(make_item("p-dup"))
    queue.add(make_item("p-dup"))

    queue.resolve("p-dup", "approved")

    # 历史数据若出现同 ID，只处理目标那一条，不能把另一条一并抹掉。
    assert len(queue.all()) == 1


def test_resolve_log_failure_keeps_candidate(tmp_path):
    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    queue.resolved_path.mkdir()
    with pytest.raises(OSError):
        queue.resolve("p-1", "rejected")
    assert queue.get("p-1") is not None
