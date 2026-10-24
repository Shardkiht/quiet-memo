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


def test_append_flattens_multiline_text(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("preferences", "标题行\n正文第一行\n正文第二行")

    entries = store.entries()
    assert len(entries) == 1
    assert "\n" not in entries[0].text
    assert "标题行" in entries[0].text and "正文第二行" in entries[0].text


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


def test_entry_uses_trailing_code_tags(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append(
        "housing",
        "在找市区的房子",
        source="chat",
        confidence=0.91,
        extra={"session": "s-1", "message": "m-2"},
        when=datetime(2026, 10, 22, 18, 0, 0),
    )

    raw = (tmp_path / "memories" / "housing.md").read_text(encoding="utf-8")
    line = next(item for item in raw.splitlines() if item.startswith("- "))
    assert line == (
        "- 2026-10-22T18:00:00 — 在找市区的房子  "
        f"`id={entry.id}` `status=active` `source=chat` `confidence=0.91` "
        "`session=s-1` `message=m-2`"
    )


def test_set_status_rewrites_tag(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("housing", "预算 4000")
    assert store.set_status(entry.id, "superseded")

    raw = (tmp_path / "memories" / "housing.md").read_text(encoding="utf-8")
    assert "`status=superseded`" in raw
    assert store.entries("housing") == []
    assert store.find(entry.id).status == "superseded"


def test_inline_code_in_text_is_kept(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("tools", "用 `uv sync` 安装")

    entry = store.entries("tools")[0]
    assert entry.text == "用 `uv sync` 安装"


def test_legacy_comment_line_still_parses(tmp_path):
    path = tmp_path / "memories" / "old.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\ntopic: old\ntitle: 旧\n---\n\n# 旧\n\n"
        "- <!-- id=mem-old; status=active --> 2026-10-01T09:00:00 — 旧事实"
        "  (source: chat, confidence: 0.80)\n",
        encoding="utf-8",
    )
    store = MemoryStore(tmp_path)

    entry = store.find("mem-old")
    assert entry is not None
    assert entry.text == "旧事实"
    assert entry.metadata["confidence"] == "0.80"

    assert store.set_status("mem-old", "deleted")
    assert "status=deleted -->" in path.read_text(encoding="utf-8")


def test_append_retries_duplicate_entry_id(tmp_path, monkeypatch):
    from quiet_memo.memory import store as store_module

    store = MemoryStore(tmp_path)
    stamp = datetime(2026, 10, 24, 12, 53, 48)
    tokens = iter(["175a", "175a", "beef"])
    monkeypatch.setattr(store_module.secrets, "token_hex", lambda _: next(tokens))

    first = store.append("a", "第一条", when=stamp)
    second = store.append("b", "第二条", when=stamp)

    assert first.id.endswith("175a")
    assert second.id.endswith("beef")
    assert len({entry.id for entry in store.entries()}) == 2
