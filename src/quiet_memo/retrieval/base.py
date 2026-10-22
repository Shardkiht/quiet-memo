from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class SearchHit(BaseModel):
    id: str = ""
    file: str = ""
    line: int = 0
    text: str
    topic: str
    timestamp: str = ""
    score: float = 0.0


class Retriever(Protocol):
    """检索抽象：当前实现关键词 BM25，后续可替换为向量/混合检索。"""

    def search(self, query: str, limit: int = 10) -> list[SearchHit]: ...
