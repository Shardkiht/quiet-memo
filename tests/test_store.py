from __future__ import annotations

from datetime import datetime

from quiet_memo.memory.store import MemoryStore, slugify


def test_append_creates_frontmatter_and_entry(tmp_path):
    store = MemoryStore(tmp_path)
    store.ensure()

    entry = store.append("preferences", "喜欢清淡、像茶的风味", source="chat", confidence=0.93)

    assert entry.topic == "preferences"
    path = tmp_path / "memories" / "preferences.md"
    raw = path.read_text(encoding="utf-8")
    assert "topic: preferences" in raw
    assert "喜欢清淡、像茶的风味" in raw

    entries = store.entries()
    assert len(entries) == 1
    assert entries[0].text == "喜欢清淡、像茶的风味"
    assert entries[0].metadata["source"] == "chat"
    assert entries[0].metadata["confidence"] == "0.93"


def test_append_appends_and_updates_frontmatter(tmp_path):
    store = MemoryStore(tmp_path)
    first = store.append("devices", "使用 手冲滤纸", when=datetime(2026, 10, 1, 9, 0, 0))
    second = store.append("devices", "买了一个 7L 挎包", when=datetime(2026, 10, 2, 10, 0, 0))

    assert first.timestamp == "2026-10-01T09:00:00"
    assert second.timestamp == "2026-10-02T10:00:00"
    raw = (tmp_path / "memories" / "devices.md").read_text(encoding="utf-8")
    assert "updated: 2026-10-02" in raw
    assert len(store.entries("devices")) == 2


def test_title_is_preserved(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("preferences", "第一条", title="偏好")
    store.append("preferences", "第二条")

    topic = store.topics()[0]
    assert topic.title == "偏好"
    assert topic.entries == 2


def test_has_text_normalizes_whitespace(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("preferences", "喜欢 清淡 风味")
    assert store.has_text("喜欢清淡风味")
    assert not store.has_text("喜欢浓郁风味")


def test_recent_context_is_limited(tmp_path):
    store = MemoryStore(tmp_path)
    for index in range(5):
        store.append("devices", f"记录 {index}", when=datetime(2026, 10, 1, 9, index, 0))

    context = store.recent_context(limit=3)
    assert context.count("\n") == 2
    assert "记录 4" in context
    assert "记录 2" in context
    assert "记录 0" not in context


def test_slugify():
    assert slugify("Preferences") == "preferences"
    assert slugify("food & drink") == "food-drink"
    assert slugify("  ") == "uncategorized"
    assert slugify("学习计划") == "学习计划"
