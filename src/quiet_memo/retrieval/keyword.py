from __future__ import annotations

import logging
from functools import lru_cache

from rank_bm25 import BM25Okapi

from ..memory.store import MemoryStore
from .base import SearchHit

try:
    import jieba

    jieba.setLogLevel(logging.ERROR)
except ImportError:  # pragma: no cover - 依赖缺失时退回字符二元组
    jieba = None


@lru_cache(maxsize=4096)
def tokenize(text: str) -> tuple[str, ...]:
    if jieba is not None:
        tokens = [token.strip().lower() for token in jieba.lcut_for_search(text)]
        return tuple(token for token in tokens if token)
    lowered = text.lower()
    return tuple(lowered[index : index + 2] for index in range(max(1, len(lowered) - 1)))


class KeywordRetriever:
    def __init__(self, store: MemoryStore, k1: float = 1.5, b: float = 0.75):
        self.store = store
        self.k1 = k1
        self.b = b

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        entries = self.store.entries()
        if not entries:
            return []

        corpus = [list(tokenize(entry.text)) for entry in entries]
        bm25 = BM25Okapi(corpus, k1=self.k1, b=self.b)
        scores = bm25.get_scores(list(tokenize(query)))
        lowered = query.lower()
        ranked = sorted(
            (
                (score + (2.0 if lowered in entry.text.lower() else 0.0), index)
                for index, (entry, score) in enumerate(zip(entries, scores))
            ),
            reverse=True,
        )
        return [
            SearchHit(
                id=entries[index].id,
                file=str(self.store.topic_path(entries[index].topic)),
                line=0,
                text=entries[index].text,
                topic=entries[index].topic,
                timestamp=entries[index].timestamp,
                score=round(max(float(score), 0.0), 4),
            )
            for score, index in ranked[:limit]
            if score > 0
        ]
