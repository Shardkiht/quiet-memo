from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Callable

from ..infra.tokens import truncate_to_tokens
from ..memory.service import MemoryService
from .session import Session


@dataclass
class ToolOutcome:
    ok: bool
    content: str
    display: str
    duration_ms: int = 0
    data: dict | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], ToolOutcome]
    timeout: float = 60.0


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}
        self._executor = ThreadPoolExecutor(max_workers=4)

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]

    def execute(self, name: str, arguments: dict) -> ToolOutcome:
        spec = self._tools.get(name)
        if spec is None:
            return ToolOutcome(
                ok=False,
                content=json.dumps({"error": f"未知工具：{name}"}, ensure_ascii=False),
                display=f"未知工具 {name}",
            )
        started = time.monotonic()
        future = self._executor.submit(spec.handler, arguments)
        try:
            outcome = future.result(timeout=spec.timeout)
        except FutureTimeout:
            outcome = ToolOutcome(
                ok=False,
                content=json.dumps({"error": f"{name} 执行超时"}, ensure_ascii=False),
                display=f"{name} 超时（{spec.timeout:.0f}s）",
            )
        except Exception as exc:
            outcome = ToolOutcome(
                ok=False,
                content=json.dumps({"error": str(exc)}, ensure_ascii=False),
                display=f"{name} 失败：{exc}",
            )
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        return outcome


def build_tools(service: MemoryService, session: Session, timeout: float = 60.0) -> ToolRegistry:
    registry = ToolRegistry()

    def recall(arguments: dict) -> ToolOutcome:
        query = str(arguments.get("query", "")).strip()
        limit = max(1, min(int(arguments.get("limit", 8)), 20))
        hits = service.recall(query, limit)
        results = [
            {
                "id": hit.id,
                "topic": hit.topic,
                "text": hit.text,
                "timestamp": hit.timestamp,
                "score": hit.score,
            }
            for hit in hits
        ]
        content = json.dumps({"results": results}, ensure_ascii=False)
        return ToolOutcome(
            ok=True,
            content=content,
            display=f"检索「{query}」：{len(hits)} 条",
            data={"results": results},
        )

    def memorize(arguments: dict) -> ToolOutcome:
        text = str(arguments.get("text", "")).strip()
        if not text:
            return ToolOutcome(
                ok=False,
                content=json.dumps({"error": "text 为空"}, ensure_ascii=False),
                display="记忆写入失败：空文本",
            )
        result = service.memorize(
            text,
            source=f"session:{session.id}",
            session_id=session.id,
            message_id=session.last_user_message_id(),
        )
        payload = {
            "admitted": [
                {"id": entry.id, "topic": entry.topic, "text": entry.text}
                for entry in result.admitted
            ],
            "queued": [
                {"id": item.id, "topic": item.topic, "text": item.text, "reason": item.reason}
                for item in result.queued
            ],
            "duplicates": result.duplicates,
            "blocked": result.blocked,
        }
        display = f"写入 {len(result.admitted)} 条"
        if result.queued:
            display += f"，待确认 {len(result.queued)} 条"
        if result.blocked:
            display += f"，拦截敏感内容 {len(result.blocked)} 条"
        return ToolOutcome(
            ok=True,
            content=json.dumps(payload, ensure_ascii=False),
            display=display,
            data=payload,
        )

    def correct(arguments: dict) -> ToolOutcome:
        entry_id = str(arguments.get("entry_id", "")).strip()
        text = str(arguments.get("text", "")).strip()
        if not entry_id or not text:
            return ToolOutcome(
                ok=False,
                content=json.dumps({"error": "entry_id 和 text 必填"}, ensure_ascii=False),
                display="更正失败：参数缺失",
            )
        entry = service.correct(entry_id, text, reason=str(arguments.get("reason", "")))
        payload = {"id": entry.id, "topic": entry.topic, "text": entry.text, "supersedes": entry.supersedes}
        return ToolOutcome(
            ok=True,
            content=json.dumps(payload, ensure_ascii=False),
            display=f"已更正 {entry_id} → {entry.id}",
            data=payload,
        )

    def forget(arguments: dict) -> ToolOutcome:
        entry_id = str(arguments.get("entry_id", "")).strip()
        entry = service.forget(entry_id, reason=str(arguments.get("reason", "")))
        payload = {"id": entry.id, "status": entry.status, "text": entry.text}
        return ToolOutcome(
            ok=True,
            content=json.dumps(payload, ensure_ascii=False),
            display=f"已删除记忆 {entry.id}（软删除，可审计）",
            data=payload,
        )

    def review_pending(_: dict) -> ToolOutcome:
        items = service.pending()
        payload = [
            {
                "id": item.id,
                "topic": item.topic,
                "text": item.text,
                "reason": item.reason,
                "conflict_with": item.conflict_with,
            }
            for item in items
        ]
        return ToolOutcome(
            ok=True,
            content=json.dumps({"pending": payload}, ensure_ascii=False),
            display=f"待确认 {len(items)} 条",
            data={"pending": payload},
        )

    def list_topics(_: dict) -> ToolOutcome:
        topics = service.topics()
        return ToolOutcome(
            ok=True,
            content=json.dumps({"topics": topics}, ensure_ascii=False),
            display=f"主题 {len(topics)} 个",
            data={"topics": topics},
        )

    registry.register(
        ToolSpec(
            name="recall",
            description="检索用户长期记忆。回答涉及用户个人信息的问题前先调用。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                },
                "required": ["query"],
            },
            handler=recall,
            timeout=timeout,
        )
    )
    registry.register(
        ToolSpec(
            name="memorize",
            description="把用户明确陈述的个人事实、偏好或计划静默写入长期记忆。用户说“记住”时优先调用。",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "用户陈述的原文或连续片段"},
                },
                "required": ["text"],
            },
            handler=memorize,
            timeout=timeout,
        )
    )
    registry.register(
        ToolSpec(
            name="correct",
            description="更正一条已有记忆。仅在用户明确指出旧记录错误、或确认新的表述应取代旧记录时调用。",
            parameters={
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string", "description": "旧记忆 ID"},
                    "text": {"type": "string", "description": "更正后的完整表述"},
                    "reason": {"type": "string", "description": "更正原因"},
                },
                "required": ["entry_id", "text"],
            },
            handler=correct,
            timeout=timeout,
        )
    )
    registry.register(
        ToolSpec(
            name="forget",
            description="软删除一条记忆。只在用户明确要求忘记时调用；删除会保留审计记录。",
            parameters={
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string", "description": "要删除的记忆 ID"},
                    "reason": {"type": "string", "description": "删除原因"},
                },
                "required": ["entry_id"],
            },
            handler=forget,
            timeout=timeout,
        )
    )
    registry.register(
        ToolSpec(
            name="review_pending",
            description="查看等待人工确认的记忆候选。用户问“有什么待确认”时调用。",
            parameters={"type": "object", "properties": {}},
            handler=review_pending,
            timeout=timeout,
        )
    )
    registry.register(
        ToolSpec(
            name="list_topics",
            description="列出记忆主题和条目数，用于了解记忆库结构。",
            parameters={"type": "object", "properties": {}},
            handler=list_topics,
            timeout=timeout,
        )
    )
    return registry


def compact_tool_outputs(messages: list[dict], max_tokens: int = 8000) -> int:
    """超出预算时，把较早的工具结果替换为占位符，保留最近两条完整结果。"""
    from ..infra.tokens import estimate_messages_tokens

    if estimate_messages_tokens(messages) <= max_tokens:
        return 0
    tool_indexes = [
        index for index, message in enumerate(messages) if message.get("role") == "tool"
    ]
    removed = 0
    for index in tool_indexes[:-2]:
        content = str(messages[index].get("content") or "")
        if len(content) > 200:
            messages[index]["content"] = truncate_to_tokens(content, 200) + "\n（较早工具结果已压缩）"
            removed += 1
    return removed
