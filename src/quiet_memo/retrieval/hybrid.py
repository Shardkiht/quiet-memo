from __future__ import annotations

from .base import Retriever, SearchHit

RRF_K = 60


class HybridRetriever:
    """BM25 + 向量 RRF 融合；向量路失败或为空时退化为纯 BM25。"""

    def __init__(self, keyword: Retriever, vector: Retriever):
        self.keyword = keyword
        self.vector = vector

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        deep = max(limit * 3, 12)
        keyword_hits = self.keyword.search(query, deep)
        vector_hits = self.vector.search(query, deep)
        if not vector_hits:
            return keyword_hits[:limit]
        if not keyword_hits:
            return vector_hits[:limit]

        scores: dict[str, float] = {}
        for hits in (keyword_hits, vector_hits):
            for rank, hit in enumerate(hits):
                scores[hit.id] = scores.get(hit.id, 0.0) + 1.0 / (RRF_K + rank)
        by_id: dict[str, SearchHit] = {}
        for hits in (keyword_hits, vector_hits):
            for hit in hits:
                by_id.setdefault(hit.id, hit)
        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
        return [by_id[entry_id] for entry_id, _ in ordered]
