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
    status: str = "active"
    context: dict[str, str] | None = None


class Retriever(Protocol):
    """检索抽象：默认 hybrid；关键字与向量实现共用同一个 search 接口。"""

    def search(self, query: str, limit: int = 10) -> list[SearchHit]: ...


class Embedder(Protocol):
    """文本向量化接口；embed 用于条目入库，embed_query 用于查询侧。"""

    model_id: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...
