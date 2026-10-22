from __future__ import annotations

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
