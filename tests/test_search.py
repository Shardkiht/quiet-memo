from __future__ import annotations

import pytest

pytestmark = pytest.mark.retrieval

from quiet_memo.memory.store import MemoryStore
from quiet_memo.retrieval import KeywordRetriever, tokenize


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


def test_search_sees_new_entries_without_recreating_retriever(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("preferences", "喜欢浓咖啡")
    retriever = KeywordRetriever(store)
    assert retriever.search("浓咖啡")

    store.append("preferences", "喜欢清淡风味")
    hits = retriever.search("清淡")

    assert hits and "清淡" in hits[0].text


def test_tokenize_drops_punctuation():
    tokens = tokenize("apartment, budget.")

    assert "apartment" in tokens
    assert "budget" in tokens
    assert "," not in tokens
    assert "." not in tokens


def test_bigram_coverage_distinguishes_hit_from_noise():
    from quiet_memo.memory.detect import bigram_coverage

    assert bigram_coverage("法压壶", "咖啡器具包括法压壶、分享壶和电子秤") == 1.0
    # 虚匹配：查询与干扰项只有零星 bigram 重叠
    assert bigram_coverage("投递了多少份申请", "申请里的经历的措辞改了很多遍") < 0.3
    assert bigram_coverage("coffee", "纯中文条目") == 0.0


def test_hybrid_gates_literal_mismatch():
    # 回归（issue #3）：keyword 虚匹配（干扰项 BM25 高名次）不得拖累 RRF 融合。
    from quiet_memo.retrieval import HybridRetriever
    from quiet_memo.retrieval.base import SearchHit

    class Stub:
        def __init__(self, hits):
            self.hits = hits

        def search(self, query, limit=10):
            return self.hits[:limit]

    query = "投递了多少份申请"
    distractor = SearchHit(text="申请里的经历的措辞改了很多遍", topic="career")
    target = SearchHit(text="上月在活动平台提交申请约 12 场，大多数没有回执", topic="career")
    hybrid = HybridRetriever(Stub([distractor]), Stub([target]))

    hits = hybrid.search(query, 5)
    assert hits[0].text == target.text


def test_hybrid_keeps_keyword_hits_for_non_cjk_query():
    # 非 CJK 查询没有 bigram 信号，字面闸不应把精确命中过滤掉。
    from quiet_memo.retrieval import HybridRetriever
    from quiet_memo.retrieval.base import SearchHit

    class Stub:
        def __init__(self, hits):
            self.hits = hits

        def search(self, query, limit=10):
            return self.hits[:limit]

    target = SearchHit(text="Uses Neovim as the daily editor", topic="devices")
    hybrid = HybridRetriever(Stub([target]), Stub([]))

    hits = hybrid.search("Neovim", 5)
    assert [hit.text for hit in hits] == [target.text]
