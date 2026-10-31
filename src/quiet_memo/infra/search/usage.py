"""轻量 API 调用额度限制：按厂商记录 Search/Fetch 请求次数并按周期重置。

只做计数、持久化与上限判断，达到上限即跳过该厂商并允许正常回退；
不做预留/结算/退款，也不是服务商账单上限——同一厂商的 Search 与
Fetch 共用一个计数池（按 provider 名）。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

# 状态文件名；放在 vault 的 .quiet-memo 下，属派生数据可删除重建。
USAGE_FILENAME = "usage.json"


def window_key(period: str, now: float) -> str:
    """计费周期标识：daily 用日期，monthly 用年月（本地时区）。"""
    moment = datetime.fromtimestamp(now)
    return moment.strftime("%Y-%m-%d") if period == "daily" else moment.strftime("%Y-%m")


class UsageLimiter:
    """内存计数 + JSON 原子落盘；limits 为空或 max_calls<=0 表示不限制。"""

    def __init__(
        self,
        limits: dict,
        path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._limits = {
            name: limit
            for name, limit in limits.items()
            if limit.period in ("daily", "monthly") and limit.max_calls > 0
        }
        self._path = path
        self._clock = clock
        self._counts: dict[str, dict] = {}
        self._mtime: float | None = None
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._mtime = self._path.stat().st_mtime
        except (OSError, ValueError):
            # 损坏的计数文件当作空计数重建，不阻断搜索。
            return
        if isinstance(data, dict):
            self._counts = {
                name: entry
                for name, entry in data.items()
                if isinstance(entry, dict) and isinstance(entry.get("count"), int)
            }

    def _reload_if_changed(self) -> None:
        """同一计数文件可能被 Search/Fetch 两个路由实例写，读前检查 mtime。"""
        if self._path is None:
            return
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return
        if mtime != self._mtime:
            self._load()

    def _save(self) -> None:
        if self._path is None:
            return
        # 先写临时文件再原子替换，避免中断截断计数。
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(
            json.dumps(self._counts, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, self._path)

    def _limit_for(self, provider: str) -> tuple[str, int] | None:
        limit = self._limits.get(provider)
        if limit is None:
            return None
        return limit.period, limit.max_calls

    def remaining(self, provider: str) -> int | None:
        """剩余可调用次数；None 表示该厂商未设置上限。"""
        self._reload_if_changed()
        limit = self._limit_for(provider)
        if limit is None:
            return None
        period, max_calls = limit
        entry = self._counts.get(provider)
        if not isinstance(entry, dict) or entry.get("window") != window_key(
            period, self._clock()
        ):
            return max_calls
        return max(0, max_calls - int(entry.get("count", 0)))

    def allows(self, provider: str) -> bool:
        remaining = self.remaining(provider)
        return remaining is None or remaining > 0

    def record(self, provider: str) -> None:
        """记一次实际请求；失败请求同样保守计数。"""
        self._reload_if_changed()
        limit = self._limit_for(provider)
        if limit is None:
            return
        period, _ = limit
        window = window_key(period, self._clock())
        entry = self._counts.get(provider)
        if not isinstance(entry, dict) or entry.get("window") != window:
            entry = {"window": window, "count": 0}
        entry["count"] = int(entry.get("count", 0)) + 1
        self._counts[provider] = entry
        self._save()
