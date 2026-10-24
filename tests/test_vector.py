from __future__ import annotations

import numpy as np

from quiet_memo.infra.config import Config, EmbeddingConfig, RetrievalConfig
from quiet_memo.memory import service as service_module
from quiet_memo.memory.store import MemoryStore
from quiet_memo.retrieval import (
    HybridRetriever,
    KeywordRetriever,
    SearchHit,
    VectorRetriever,
)

# 同义词组：组内任一词命中即贡献该维度方向，模拟语义相近。
GROUPS = {
    "housing": ["房", "住", "租"],
    "coffee": ["咖啡", "清淡", "茶"],
    "work": ["工作", "后端"],
}


class FakeEmbedder:
    """确定性假 Embedder；记录条目/查询侧调用，fail=True 模拟后端不可用。"""

    def __init__(self, groups: dict[str, list[str]], model_id: str = "fake", fail: bool = False):
        self.model_id = model_id
        self.groups = groups
        self.fail = fail
        self.passages: list[str] = []
        self.queries: list[str] = []

    def _vector(self, text: str) -> list[float]:
        if self.fail:
            raise RuntimeError("embedding 不可用")
        vector = np.zeros(len(self.groups), dtype=np.float32)
        for index, words in enumerate(self.groups.values()):
            if any(word in text for word in words):
                vector[index] = 1.0
        return vector.tolist()

    def embed(self, texts) -> list[list[float]]:
        self.passages.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vector(text)


def seeded_store(tmp_path) -> MemoryStore:
    store = MemoryStore(tmp_path)
    store.append("housing", "在找市区的房子，预算 4000 以内")
    store.append("preferences", "咖啡偏好清淡、像茶的风味")
    return store


def test_vector_search_finds_paraphrase(tmp_path):
    store = seeded_store(tmp_path)
    retriever = VectorRetriever(store, FakeEmbedder(GROUPS))

    hits = retriever.search("住的地方", limit=3)

    assert hits and hits[0].topic == "housing"


def test_incremental_sync_embeds_only_new_entries(tmp_path):
    store = seeded_store(tmp_path)
    embedder = FakeEmbedder(GROUPS)
    retriever = VectorRetriever(store, embedder)

    retriever.search("房子", 3)
    assert len(embedder.passages) == 2
    retriever.search("房子", 3)
    assert len(embedder.passages) == 2

    store.append("career", "在找后端工作")
    retriever.search("工作", 3)
    assert len(embedder.passages) == 3


def test_cache_persists_across_instances(tmp_path):
    store = seeded_store(tmp_path)
    VectorRetriever(store, FakeEmbedder(GROUPS)).search("房子", 3)

    embedder = FakeEmbedder(GROUPS)
    retriever = VectorRetriever(store, embedder)
    assert retriever.search("房子", 3)
    assert embedder.passages == []


def test_model_change_rebuilds_cache(tmp_path):
    store = seeded_store(tmp_path)
    VectorRetriever(store, FakeEmbedder(GROUPS, model_id="fake-a")).search("房子", 3)

    embedder = FakeEmbedder(GROUPS, model_id="fake-b")
    retriever = VectorRetriever(store, embedder)
    assert retriever.search("房子", 3)
    assert len(embedder.passages) == 2


def test_vector_search_degrades_to_empty_when_embedder_fails(tmp_path):
    store = seeded_store(tmp_path)
    retriever = VectorRetriever(store, FakeEmbedder(GROUPS, fail=True))

    assert retriever.search("房子", 3) == []


def test_search_ignores_deleted_entries(tmp_path):
    store = seeded_store(tmp_path)
    entry = store.append("career", "在找后端工作")
    retriever = VectorRetriever(store, FakeEmbedder(GROUPS))
    assert retriever.search("后端", 3)

    store.set_status(entry.id, "deleted")
    assert all(hit.id != entry.id for hit in retriever.search("后端", 3))


class StubRetriever:
    def __init__(self, ids: list[str]):
        self.ids = ids

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        return [SearchHit(id=i, text=f"text-{i}", topic="t") for i in self.ids[:limit]]


def test_hybrid_ranks_hits_in_both_paths_first():
    keyword = StubRetriever(["a", "b", "c"])
    vector = StubRetriever(["b", "d"])
    hits = HybridRetriever(keyword, vector).search("q", limit=3)

    assert hits[0].id == "b"
    assert len(hits) == 3


def test_hybrid_breaks_rrf_ties_toward_vector_rank():
    # BM25 噪声把 a 排第一、c 排第三；向量把 c 排第一、a 排第三 → RRF 完全平局，
    # 决胜应信任向量名次（c 第一）。
    keyword = StubRetriever(["a", "b", "c"])
    vector = StubRetriever(["c", "b", "a"])
    hits = HybridRetriever(keyword, vector).search("q", limit=3)

    assert [hit.id for hit in hits[:2]] == ["c", "a"]


def test_hybrid_falls_back_to_keyword_when_vector_fails(tmp_path):
    store = seeded_store(tmp_path)
    hybrid = HybridRetriever(
        KeywordRetriever(store),
        VectorRetriever(store, FakeEmbedder(GROUPS, fail=True)),
    )

    hits = hybrid.search("咖啡", limit=3)

    assert hits and hits[0].topic == "preferences"


def test_build_retriever_falls_back_without_fastembed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        service_module.LocalEmbedder, "available", staticmethod(lambda: False)
    )
    config = Config(vault=tmp_path, retrieval=RetrievalConfig(method="hybrid"))

    retriever, mode = service_module.build_retriever(config, MemoryStore(tmp_path))

    assert isinstance(retriever, KeywordRetriever)
    assert mode == "hybrid-fallback-keyword"


def test_build_retriever_hybrid_when_local_available(tmp_path, monkeypatch):
    monkeypatch.setattr(
        service_module.LocalEmbedder, "available", staticmethod(lambda: True)
    )
    config = Config(vault=tmp_path, retrieval=RetrievalConfig(method="hybrid"))

    retriever, mode = service_module.build_retriever(config, MemoryStore(tmp_path))

    assert isinstance(retriever, HybridRetriever)
    assert mode == "hybrid"
    assert isinstance(retriever.vector.embedder.model_id, str)


def test_build_retriever_api_backend(tmp_path, monkeypatch):
    config = Config(
        vault=tmp_path,
        retrieval=RetrievalConfig(
            method="hybrid",
            embedding=EmbeddingConfig(
                backend="api", model="BAAI/bge-m3", api_key_env="TEST_EMBED_KEY"
            ),
        ),
    )
    monkeypatch.setenv("TEST_EMBED_KEY", "test-key")
    retriever, mode = service_module.build_retriever(config, MemoryStore(tmp_path))
    assert mode == "hybrid"

    monkeypatch.delenv("TEST_EMBED_KEY")
    retriever, mode = service_module.build_retriever(config, MemoryStore(tmp_path))
    assert isinstance(retriever, KeywordRetriever)
    assert mode == "hybrid-fallback-keyword"


def test_init_vault_gitignores_vector_cache(tmp_path):
    from quiet_memo.memory.service import MemoryService

    config = Config(vault=tmp_path / "vault", retrieval=RetrievalConfig(method="keyword"))
    service = MemoryService(config)
    service.init_vault()

    assert "vectors.npz" in (tmp_path / "vault" / ".gitignore").read_text(encoding="utf-8")
