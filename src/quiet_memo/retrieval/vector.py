from __future__ import annotations

import hashlib
import importlib.util
import os

import numpy as np

from ..memory.models import Entry
from ..memory.store import MemoryStore, normalize_text
from .base import SearchHit

MIN_SCORE = 0.05
CACHE_FILENAME = "vectors.npz"


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:16]


def _unit(matrix: np.ndarray) -> np.ndarray:
    """L2 归一化；零向量兜底，归一化后点积即余弦。"""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-12)


class LocalEmbedder:
    """fastembed 本地推理；模型懒加载，未安装 extra 时 available() 为 False。"""

    def __init__(self, model: str):
        self.model_id = model
        self._model = None

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("fastembed") is not None

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_id)
        return self._model

    def embed(self, texts) -> list[list[float]]:
        return [vector.tolist() for vector in self._load().embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._load().query_embed([text]))).tolist()


class VectorRetriever:
    """numpy 暴力余弦检索；vectors.npz 是派生缓存，删除后下次检索自动重建。"""

    def __init__(self, store: MemoryStore, embedder, batch_size: int = 32):
        self.store = store
        self.embedder = embedder
        self.batch_size = batch_size
        self.cache_path = store.vault / ".quiet-memo" / CACHE_FILENAME
        self._entries: list[Entry] = []
        self._matrix: np.ndarray | None = None
        self._fingerprint: tuple | None = None
        self._degraded = False

    def sync(self) -> None:
        """与记忆条目对齐：复用未变更向量，只为新增/变更条目计算 embedding。"""
        entries = self.store.entries()
        # 同 ID 条目只保留首个：历史数据若存在重复 ID，去重避免矩阵行错位。
        unique: dict[str, Entry] = {}
        for entry in entries:
            unique.setdefault(entry.id, entry)
        entries = list(unique.values())
        ids, hashes, cached = self._load_cache()
        known = {entry_id: index for index, entry_id in enumerate(ids)}
        keep: list[int] = []
        fresh: list[Entry] = []
        for entry in entries:
            index = known.get(entry.id)
            if index is not None and hashes[index] == text_hash(entry.text):
                keep.append(index)
            else:
                fresh.append(entry)

        if cached is None:
            cached = np.zeros((0, 0), dtype=np.float32)
        rows = [cached[keep]] if keep else []
        try:
            for start in range(0, len(fresh), self.batch_size):
                batch = fresh[start : start + self.batch_size]
                vectors = np.array(
                    self.embedder.embed([entry.text for entry in batch]), dtype=np.float32
                )
                if len(vectors) != len(batch):
                    raise ValueError("embedding 返回数量与输入不一致")
                rows.append(_unit(vectors))
        except Exception:
            # embedding 失败：保留已缓存子集，缺的条目本轮检索不到，下次检索重试同步。
            self._degraded = True
            fresh = []
            rows = [cached[keep]] if keep else []
        else:
            self._degraded = False

        matrix = np.vstack(rows) if rows else cached[keep] if keep else None
        kept_ids = {ids[index] for index in keep}
        self._entries = [entry for entry in entries if entry.id in kept_ids] + fresh
        self._matrix = matrix
        if fresh or len(keep) != len(ids):
            self._save_cache(self._entries, matrix)

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        fingerprint = self._file_fingerprint()
        if fingerprint != self._fingerprint or self._degraded:
            self.sync()
            self._fingerprint = fingerprint
        if self._matrix is None or not self._entries:
            return []
        try:
            vector = _unit(np.array([self.embedder.embed_query(query)], dtype=np.float32))[0]
        except Exception:
            return []
        scores = self._matrix @ vector
        hits: list[SearchHit] = []
        for index in np.argsort(-scores)[:limit]:
            score = float(scores[index])
            if score <= MIN_SCORE:
                continue
            entry = self._entries[index]
            hits.append(
                SearchHit(
                    id=entry.id,
                    file=str(self.store.topic_path(entry.topic)),
                    text=entry.text,
                    topic=entry.topic,
                    timestamp=entry.timestamp,
                    score=round(score, 4),
                )
            )
        return hits

    def _file_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        if not self.store.root.exists():
            return ()
        items = []
        for path in sorted(self.store.root.glob("*.md")):
            stat = path.stat()
            items.append((path.name, stat.st_mtime_ns, stat.st_size))
        return tuple(items)

    def _load_cache(self) -> tuple[list[str], list[str], np.ndarray | None]:
        if not self.cache_path.exists():
            return [], [], None
        try:
            with np.load(self.cache_path, allow_pickle=False) as data:
                if str(data["model"]) != self.embedder.model_id:
                    return [], [], None
                ids = data["ids"].tolist()
                hashes = data["hashes"].tolist()
                matrix = np.asarray(data["vectors"], dtype=np.float32)
        except Exception:
            return [], [], None
        if len(ids) != matrix.shape[0]:
            return [], [], None
        return ids, hashes, matrix

    def _save_cache(self, entries: list[Entry], matrix: np.ndarray | None) -> None:
        if matrix is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
        try:
            with open(tmp, "wb") as handle:
                np.savez_compressed(
                    handle,
                    model=np.array(self.embedder.model_id),
                    ids=np.array([entry.id for entry in entries]),
                    hashes=np.array([text_hash(entry.text) for entry in entries]),
                    vectors=matrix,
                )
            os.replace(tmp, self.cache_path)
        except OSError:
            # 写盘失败不影响本次检索；下次进程启动会重新计算缺失向量。
            tmp.unlink(missing_ok=True)
