from __future__ import annotations

import logging
from functools import lru_cache

from rank_bm25 import BM25Okapi

from ..memory.models import Entry
from ..memory.store import MemoryStore
from .base import SearchHit

try:
    import jieba

    jieba.setLogLevel(logging.ERROR)
except ImportError:  # pragma: no cover - 依赖缺失时退回字符二元组
    jieba = None


def _is_word(token: str) -> bool:
    return any(char.isalnum() or "\u4e00" <= char <= "\u9fff" for char in token)


@lru_cache(maxsize=4096)
def tokenize(text: str) -> tuple[str, ...]:
    if jieba is not None:
        tokens = [token.strip().lower() for token in jieba.lcut_for_search(text)]
    else:
        lowered = text.lower()
        tokens = [lowered[index : index + 2] for index in range(max(1, len(lowered) - 1))]
    return tuple(token for token in tokens if token and _is_word(token))


class KeywordRetriever:
    def __init__(self, store: MemoryStore, k1: float = 1.5, b: float = 0.75):
        self.store = store
        self.k1 = k1
        self.b = b
        self._entries: list[Entry] = []
        self._bm25: BM25Okapi | None = None
        self._fingerprint: tuple[tuple[str, int, int], ...] | None = None

    def _file_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        if not self.store.root.exists():
            return ()
        items: list[tuple[str, int, int]] = []
        for path in sorted(self.store.root.glob("*.md")):
            stat = path.stat()
            items.append((path.name, stat.st_mtime_ns, stat.st_size))
        return tuple(items)

    def _index(self) -> tuple[list[Entry], BM25Okapi | None]:
        fingerprint = self._file_fingerprint()
        if self._bm25 is None or fingerprint != self._fingerprint:
            self._entries = self.store.entries()
            corpus = [list(tokenize(entry.text)) for entry in self._entries]
            self._bm25 = BM25Okapi(corpus, k1=self.k1, b=self.b) if corpus else None
            self._fingerprint = fingerprint
        return self._entries, self._bm25

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        entries, bm25 = self._index()
        if not entries or bm25 is None:
            return []
        query_tokens = list(tokenize(query))
        if not query_tokens:
            return []

        scores = bm25.get_scores(query_tokens)
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
