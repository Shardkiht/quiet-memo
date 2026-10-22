from __future__ import annotations

from quiet_memo.memory.store import MemoryStore
from quiet_memo.retrieval import KeywordRetriever


def seed(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("preferences", "喜欢清淡、像茶的风味")
    store.append("career", "目前在找后端和 Agent 方向的工作")
    store.append("devices", "使用手冲滤纸和磨豆机")
    return store


def test_search_finds_keyword(tmp_path):
    store = seed(tmp_path)
    hits = KeywordRetriever(store).search("清淡")
    assert len(hits) == 1
    assert hits[0].topic == "preferences"
    assert "清淡" in hits[0].text


def test_search_limit(tmp_path):
    store = MemoryStore(tmp_path)
    for index in range(5):
        store.append("devices", f"设备编号 {index} 可用")
    hits = KeywordRetriever(store).search("设备编号", limit=2)
    assert len(hits) == 2


def test_search_ignores_inactive_entries(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("preferences", "喜欢浓咖啡")
    store.set_status(entry.id, "deleted")
    assert KeywordRetriever(store).search("浓咖啡") == []


def test_empty_query_returns_nothing(tmp_path):
    store = seed(tmp_path)
    assert KeywordRetriever(store).search("  ") == []
