from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..infra.config import ChatConfig, Config
from ..infra.tokens import truncate_to_tokens
from ..llm import (
    AssistantTurn,
    ChatModel,
    ChatModelError,
    OpenAISummarizer,
    Summarizer,
    Usage,
)
from ..memory.service import MemoryService, load_persona
from .context import ContextManager
from .prompts import build_system_prompt
from .recall import RecallQueryBuilder
from .session import Session, SessionStore, new_message_id
from .tools import build_tools, compact_tool_outputs


@dataclass
class AgentReply:
    content: str
    events: list[str] = field(default_factory=list)
    steps: int = 0
    compacted: bool = False
    usage: Usage = field(default_factory=Usage)
    estimated_tokens: int = 0


class AgentRuntime:
    def __init__(
        self,
        service: MemoryService,
        model: ChatModel,
        sessions: SessionStore,
        *,
        summarizer: Summarizer | None = None,
        config: ChatConfig | None = None,
    ):
        self.service = service
        self.model = model
        self.sessions = sessions
        self.config = config or service.config.chat
        self.recall_queries = RecallQueryBuilder(
            service.config.llm,
            rewrite=self.config.recall_rewrite,
            context_turns=self.config.recall_context_turns,
        )
        self.summarizer = summarizer or OpenAISummarizer(service.config.llm)
        self.context = ContextManager(
            self.config,
            self.summarizer,
            sessions,
            flush=self._flush_memories,
        )
        self.session: Session | None = None
        self.last_plan = None
        self._closed = False

    def start(self) -> None:
        """进入新对话。会话文件在第一条消息发送时才创建。"""
        self.session = None
        self._closed = False

    def ensure_session(self) -> Session:
        if self.session is None:
            self.session = self.sessions.create()
            self._closed = False
        return self.session

    @property
    def has_session(self) -> bool:
        return self.session is not None

    def resume(self, session_id: str) -> Session:
        self.session = self.sessions.load(session_id)
        self._closed = False
        return self.session

    def reload_llm(self, config: "Config | None" = None) -> None:
        """配置变更后热更新模型、抽取器和摘要器，不重启会话。

        默认从 service 记住的配置路径重读；调用方持有新配置对象时直接传入，
        避免落盘与重读之间的竞态把切换回滚。
        """
        from ..llm import LLMSummarizer, LiteLLMChatModel
        from ..memory.extractor import LLMExtractor

        if config is None:
            from ..infra.config import load_config

            path = self.service.config_path
            config = load_config(path) if path else load_config()
        self.service.config.llm = config.llm
        self.service.config.chat = config.chat
        self.service.extractor = LLMExtractor(config.llm)
        self.model = LiteLLMChatModel(config.llm)
        self.summarizer = LLMSummarizer(config.llm)
        self.config = config.chat
        self.recall_queries = RecallQueryBuilder(
            config.llm,
            rewrite=config.chat.recall_rewrite,
            context_turns=config.chat.recall_context_turns,
        )
        self.context.config = config.chat
        self.context.summarizer = self.summarizer

    @property
    def model_label(self) -> str:
        return f"{self.service.config.llm.provider}/{self.service.config.llm.model}"

    @property
    def current(self) -> Session:
        if self.session is None:
            raise RuntimeError("会话尚未开始")
        return self.session

    def ask(
        self,
        text: str,
        on_event: Callable[[str], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> AgentReply:
        text = text.strip()
        if not text:
            raise ValueError("消息为空。")
        session = self.ensure_session()
        message_id = new_message_id()
        self.sessions.append(
            session,
            {"type": "message", "id": message_id, "role": "user", "content": text},
        )

        memory_context = self._auto_recall(text)
        plan = self.context.prepare(
            session,
            self._system_prompt(),
            text,
            current_id=message_id,
            memory_context=memory_context,
        )
        self.last_plan = plan
        events: list[str] = []

        def emit(event: str) -> None:
            # 事件实时通知界面，同时保留在 reply.events 里供测试与调用方复用。
            events.append(event)
            if on_event is not None:
                on_event(event)

        for event in plan.events:
            emit(event)
        if plan.flushed:
            flushed = plan.flushed
            if flushed.get("error"):
                emit(f"记忆沉淀失败：{flushed['error']}")
            elif flushed.get("admitted") or flushed.get("queued"):
                emit(
                    f"压缩前沉淀记忆：写入 {flushed.get('admitted', 0)} 条，"
                    f"待确认 {flushed.get('queued', 0)} 条"
                )

        tools = build_tools(self.service, session, timeout=self.config.tool_timeout)
        messages = plan.messages
        usage = Usage()
        steps = 0
        final = ""
        completed = False

        while steps < self.config.max_tool_steps:
            turn = self._chat_with_retry(messages, tools.schemas(), on_delta=on_delta)
            usage.add(turn.usage)
            steps += 1
            record = self._append_assistant(session, turn)
            messages.append(self._message_from_record(record))
            if not turn.tool_calls:
                final = turn.content
                completed = True
                break
            for call in turn.tool_calls:
                outcome = tools.execute(call.name, call.arguments)
                content = truncate_to_tokens(outcome.content, 2000)
                self.sessions.append(
                    session,
                    {
                        "type": "tool",
                        "id": new_message_id(),
                        "tool_call_id": call.id,
                        "name": call.name,
                        "ok": outcome.ok,
                        "duration_ms": outcome.duration_ms,
                        "content": content,
                    },
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": content}
                )
                emit(outcome.display)
            compact_tool_outputs(messages)

        if not completed:
            messages.append(
                {
                    "role": "user",
                    "content": "已达到工具调用上限。请基于以上信息直接给出最终回答，不要再调用工具。",
                }
            )
            turn = self._chat_with_retry(messages, [], on_delta=on_delta)
            usage.add(turn.usage)
            steps += 1
            record = self._append_assistant(session, turn)
            final = turn.content or "（未能生成回答）"
            emit("达到工具调用上限，已强制收敛")

        return AgentReply(
            content=final,
            events=events,
            steps=steps,
            compacted=plan.compacted,
            usage=usage,
            estimated_tokens=plan.estimated_tokens,
        )

    def force_compact(self) -> list[str]:
        if self.session is None:
            return ["新会话还没有记录，无需压缩。"]
        plan = self.context.prepare(
            self.current,
            self._system_prompt(),
            "",
            force=True,
        )
        events = list(plan.events)
        if plan.flushed and plan.flushed.get("admitted") is not None:
            events.append(
                f"压缩前沉淀记忆：写入 {plan.flushed.get('admitted', 0)} 条，"
                f"待确认 {plan.flushed.get('queued', 0)} 条"
            )
        if not events:
            events.append("没有可压缩的早期记录。")
        return events

    def close(self, distill: bool = True) -> dict:
        empty = {"admitted": 0, "queued": 0, "duplicates": 0, "blocked": 0, "error": None}
        if self.session is None or self._closed:
            return empty
        session = self.session
        self._closed = True
        stats = dict(empty)
        # 游标感知：只抽取上次成功沉淀之后的新内容，反复切换不重复抽取。
        user_texts = session.user_texts_after_distilled()
        if not distill:
            # 切换会话走快速路径：只做记账，沉淀延后（标记供补沉淀拾取）。
            if user_texts:
                self.sessions.append(session, {"type": "distill_deferred"})
            self.sessions.append(session, {"type": "closed"})
            return stats
        if self.config.extract_on_exit and user_texts:
            joined = "\n---\n".join(user_texts)[-12000:]
            try:
                result = self.service.memorize(
                    joined,
                    source=f"session:{session.id}:exit",
                    session_id=session.id,
                    message_id=session.last_user_message_id(),
                )
                stats.update(
                    admitted=len(result.admitted),
                    queued=len(result.queued),
                    duplicates=len(result.duplicates),
                    blocked=len(result.blocked),
                )
                self.sessions.append(
                    session,
                    {"type": "distilled", "up_to": session.last_user_message_id()},
                )
            except Exception as exc:
                stats["error"] = str(exc)
                # 留下失败标记：启动补沉淀据此识别"有 closed 但没沉淀成"的会话。
                self.sessions.append(
                    session, {"type": "extraction_error", "error": str(exc)[:200]}
                )
        self.sessions.append(session, {"type": "closed"})
        return stats

    def consolidate_pending(self, limit: int = 3, min_new_messages: int = 1) -> dict:
        """补沉淀：抽取异常终止、退出抽取失败或切换时延后的会话。

        游标之后新用户消息不足 min_new_messages 的会话跳过（标记保留，零成本
        攒批），避免频繁切换触发大量小抽取。当前活跃会话不参与。
        """
        result = {"scanned": 0, "consolidated": 0, "admitted": 0, "queued": 0, "errors": []}
        if not self.config.extract_on_exit:
            return result
        exclude = self.session.id if self.session is not None else None
        for session in self.sessions.unconsolidated(limit, exclude=exclude):
            result["scanned"] += 1
            texts = session.user_texts_after_distilled()
            if not texts:
                # 空会话没有可抽取内容，直接标记收尾，避免每次启动都重复扫描。
                self.sessions.append(session, {"type": "consolidated"})
                continue
            if len(texts) < min_new_messages:
                continue
            if texts:
                joined = "\n---\n".join(texts)[-12000:]
                try:
                    extracted = self.service.memorize(
                        joined,
                        source=f"session:{session.id}:catchup",
                        session_id=session.id,
                        message_id=session.last_user_message_id(),
                    )
                except Exception as exc:
                    result["errors"].append(f"{session.id}: {exc}")
                    self.sessions.append(
                        session, {"type": "extraction_error", "error": str(exc)[:200]}
                    )
                    continue
                result["admitted"] += len(extracted.admitted)
                result["queued"] += len(extracted.queued)
                self.sessions.append(
                    session,
                    {"type": "distilled", "up_to": session.last_user_message_id()},
                )
            self.sessions.append(session, {"type": "consolidated"})
            result["consolidated"] += 1
        return result

    def status(self) -> dict:
        session = self.current
        summary = session.latest_summary() or {}
        return {
            "session_id": session.id,
            "title": session.title(),
            "model": self.model_label,
            "retrieval": self.service.retrieval_mode,
            "messages": len(session.messages),
            "records": len(session.records),
            "summaries": sum(1 for record in session.records if record.get("type") == "summary"),
            "summary_chars": len(session.summary_text()),
            "covered_records": session.covered_records(),
            "estimated_tokens": session.estimated_tokens(),
            "context_window": self.config.context_window,
            "pending": len(self.service.pending()),
            "last_summary_at": summary.get("at", ""),
        }

    def summary_text(self) -> str:
        return self.current.summary_text()

    def rename(self, title: str) -> None:
        self.sessions.rename(self.current, title)

    def _system_prompt(self) -> str:
        """每轮读 vault/SOUL.md，人设改动下一轮即生效；拼接规则见 prompts.py。"""
        return build_system_prompt(load_persona(self.service.config.vault))

    def _auto_recall(self, text: str) -> str:
        if self.config.auto_recall <= 0:
            return ""
        recall = self.recall_queries.build(text, self.current.user_texts()[:-1])
        limit = self.config.auto_recall
        if recall.cue:
            limit = max(limit, 8)
        hits = self.service.recall(recall.text, limit)
        return "\n".join(f"- [{hit.topic}] ({hit.timestamp}) {hit.text}" for hit in hits)

    def _flush_memories(self, texts: list[str]) -> dict:
        joined = "\n---\n".join(texts)[-12000:]
        result = self.service.memorize(
            joined,
            source=f"session:{self.current.id}:compact",
            session_id=self.current.id,
            message_id=self.current.last_user_message_id(),
        )
        return {
            "admitted": len(result.admitted),
            "queued": len(result.queued),
            "duplicates": len(result.duplicates),
            "blocked": len(result.blocked),
        }

    def _append_assistant(self, session: Session, turn: AssistantTurn) -> dict:
        return self.sessions.append(
            session,
            {
                "type": "assistant",
                "id": new_message_id(),
                "content": turn.content,
                "tool_calls": [
                    {"id": call.id, "name": call.name, "arguments": call.arguments}
                    for call in turn.tool_calls
                ],
            },
        )

    @staticmethod
    def _message_from_record(record: dict) -> dict:
        from .context import record_to_message

        return record_to_message(record) or {"role": "assistant", "content": ""}

    def _chat_with_retry(
        self,
        messages: list[dict],
        tools: list[dict],
        on_delta: Callable[[str], None] | None = None,
    ) -> AssistantTurn:
        if on_delta is not None:
            # 流式不做循环级重试：中断重发会让界面收到重复 delta。
            try:
                return self.model.chat(messages, tools, on_delta=on_delta)
            except ChatModelError:
                raise
            except Exception as exc:
                raise ChatModelError(f"LLM 调用失败：{exc}") from exc
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return self.model.chat(messages, tools)
            except ChatModelError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        raise ChatModelError(f"LLM 调用失败：{last_error}")
