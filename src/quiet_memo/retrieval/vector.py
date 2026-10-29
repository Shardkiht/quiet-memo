from __future__ import annotations

import hashlib
import importlib.util
import os
import time
from collections.abc import Callable

import numpy as np

from ..memory.models import Entry
from ..memory.store import MemoryStore, normalize_text
from .base import SearchHit

MIN_SCORE = 0.05
CACHE_FILENAME = "vectors.npz"
RETRY_COOLDOWN_SECONDS = 60.0


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

    def __init__(
        self,
        store: MemoryStore,
        embedder,
        batch_size: int = 32,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.embedder = embedder
        self.batch_size = batch_size
        self.cache_path = store.vault / ".quiet-memo" / CACHE_FILENAME
        self._entries: list[Entry] = []
        self._matrix: np.ndarray | None = None
        self._fingerprint: tuple[tuple[str, int, int], ...] | None = None
        self._degraded = False
        self._clock = clock
        self._retry_at = 0.0

    @property
    def degraded(self) -> bool:
        """向量后端是否降级；冷却到期后的成功重试会清除此状态。"""
        return self._degraded

    def _mark_degraded(self) -> None:
        self._degraded = True
        self._retry_at = self._clock() + RETRY_COOLDOWN_SECONDS

    def sync(self) -> None:
        """与记忆条目对齐：复用未变更向量，只为新增/变更条目计算 embedding。"""
        if self._degraded and self._clock() < self._retry_at:
            return
        fingerprint = self.store.fingerprint()
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
                if vectors.ndim != 2 or vectors.shape[0] != len(batch):
                    raise ValueError("embedding 返回形状或数量与输入不一致")
                if vectors.shape[1] == 0 or not np.isfinite(vectors).all():
                    raise ValueError("embedding 返回空维度或非有限数值")
                if rows and vectors.shape[1] != rows[0].shape[1]:
                    raise ValueError("embedding 返回维度与已有向量不一致")
                rows.append(_unit(vectors))
            matrix = np.vstack(rows) if rows else None
        except Exception:
            # 冷却内停用整条向量路；不覆盖成功缓存，恢复后仍可增量复用。
            self._mark_degraded()
            return

        kept_ids = {ids[index] for index in keep}
        self._entries = [entry for entry in entries if entry.id in kept_ids] + fresh
        self._matrix = matrix
        self._fingerprint = fingerprint
        self._degraded = False
        if fresh or len(keep) != len(ids):
            self._save_cache(self._entries, matrix)

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        if self._degraded and self._clock() < self._retry_at:
            return []
        if self.store.fingerprint() != self._fingerprint or self._degraded:
            self.sync()
        if self._degraded or self._matrix is None or not self._entries:
            return []
        try:
            vector = np.array(self.embedder.embed_query(query), dtype=np.float32)
            if vector.ndim != 1 or vector.shape[0] != self._matrix.shape[1]:
                raise ValueError("查询 embedding 返回维度与条目向量不一致")
            if not np.isfinite(vector).all():
                raise ValueError("查询 embedding 返回非有限数值")
            vector = _unit(vector[None, :])[0]
            scores = self._matrix @ vector
        except Exception:
            self._mark_degraded()
            return []
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
            if (
                not isinstance(ids, list)
                or not isinstance(hashes, list)
                or any(not isinstance(value, str) for value in ids + hashes)
                or len(set(ids)) != len(ids)
                or matrix.ndim != 2
                or matrix.shape[1] == 0
                or len(ids) != len(hashes)
                or len(ids) != matrix.shape[0]
                or not np.isfinite(matrix).all()
            ):
                return [], [], None
        except Exception:
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
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
