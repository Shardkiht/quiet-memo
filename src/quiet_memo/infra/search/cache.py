from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .base import SearchResult

CACHE_VERSION = 1


def _expires(entry: dict) -> float:
    try:
        return float(entry.get("expires", 0))
    except (TypeError, ValueError):
        return 0.0


class SearchCache:
    """搜索结果的文件缓存；只是派生数据，损坏或过期都可直接丢弃。"""

    def __init__(
        self,
        path: Path,
        ttl: int,
        clock: Callable[[], float] = time.time,
    ):
        self.path = path
        self.ttl = ttl
        self.clock = clock

    def get(self, key: str) -> tuple[list[SearchResult], str] | None:
        if self.ttl <= 0:
            return None
        entry = self._load().get("entries", {}).get(key)
        if not isinstance(entry, dict):
            return None
        if _expires(entry) <= self.clock():
            return None
        try:
            results = [
                SearchResult(**item)
                for item in entry.get("results", [])
                if isinstance(item, dict)
            ]
        except (TypeError, ValueError):
            # 派生缓存字段损坏时按未命中处理，不能让搜索整体失败。
            return None
        return results, str(entry.get("provider", ""))

    def set(self, key: str, results: list[SearchResult], provider: str, ttl: float | None = None) -> None:
        """ttl 为空时用实例默认值；空结果等短命条目传更短的 ttl。"""
        if self.ttl <= 0:
            return
        data = self._load()
        now = self.clock()
        entries = {
            item_key: item
            for item_key, item in data.get("entries", {}).items()
            if isinstance(item, dict) and _expires(item) > now
        }
        entries[key] = {
            "expires": now + (self.ttl if ttl is None else min(ttl, self.ttl)),
            "provider": provider,
            "results": [asdict(result) for result in results],
        }
        data = {"version": CACHE_VERSION, "entries": entries}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            # 缓存写失败不影响搜索结果；下次直接重新请求。
            return

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": CACHE_VERSION, "entries": {}}
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return {"version": CACHE_VERSION, "entries": {}}
        if not isinstance(data.get("entries"), dict):
            return {"version": CACHE_VERSION, "entries": {}}
        return data
