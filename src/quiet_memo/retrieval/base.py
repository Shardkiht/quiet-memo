from __future__ import annotations

from collections.abc import Sequence
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


class Embedder(Protocol):
    """文本向量化接口；具体模型在后续实现中接入。"""

    model_id: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class VectorRetriever(Retriever, Protocol):
    """向量检索接口；实现需要先同步索引，具体实现后续接入。"""

    def sync(self) -> None: ...
