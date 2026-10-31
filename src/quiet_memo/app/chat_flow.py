from __future__ import annotations

import time
from dataclasses import dataclass
from functools import partial

from textual.widgets import Static

from ..llm import ChatModelError
from .chat_widgets import ASSISTANT_NAME, ChatReply


@dataclass
class _QueuedLine:
    """等待期提交的全局 FIFO 项；执行时按 session_id 从磁盘新鲜加载会话。"""

    session_id: str
    line: str


class ChatFlowMixin:
    """LLM 请求调度与流式渲染：忙碌态解耦、全局 FIFO 排队、切走切回的流式续播。

    依赖 ChatApp 的 runtime/sessions/service、busy 三态字段与 `_begin_ask`/`_end_ask`
    等生命周期方法；提交路由规则见 `_submit_during_ask`。
    """

    # 等待 LLM 期间可直接执行的导航/只读命令；其余提交（消息与命令）进全局 FIFO。
    SAFE_COMMANDS = frozenset({
        "/new", "/sessions", "/exit", "/quit", "/help", "/model",
        "/status", "/topics", "/summary", "/pending",
    })

    # ---------- 提交与调度 ----------

    def _ask_in_current_view(self) -> bool:
        """请求的会话是否就是当前正在查看的会话（尚未绑定时按当前视图处理）。"""
        if self._ask_in_flight is None:
            return True
        return self.runtime.has_session and self.runtime.current.id == self._ask_in_flight

    def _bind_ask_session(self, session_id: str) -> None:
        # on_session 先于一切 delta/事件回调投递，这里补齐首条消息的会话绑定。
        if self._ask_active and self._ask_in_flight is None:
            self._ask_in_flight = session_id

    def _submit_during_ask(self, line: str) -> None:
        # 等待期提交：导航/只读命令立即执行；其余按提交顺序进全局 FIFO，
        # 完成后逐条执行——意图排队接续，绝不静默丢弃，也不并发第二个请求。
        if line.startswith("/") and line.lower().split(" ", 1)[0] in self.SAFE_COMMANDS:
            self._command(line)
            return
        if not self.runtime.has_session:
            # /new 后尚未发过消息：先落一个会话文件，排队任务才有绑定目标。
            self.runtime.ensure_session()
        session_id = self.runtime.session.id
        self._queue.append(_QueuedLine(session_id=session_id, line=line))
        self.notify(f"Queued ({len(self._queue)} waiting)")

    def _start_ask(self, line: str, session_id: str | None = None) -> None:
        stamp = time.strftime("%H:%M")
        # 用户气泡只渲染进它所属会话的视图；后台任务不串入当前视图。
        if session_id is None or not self.runtime.has_session or session_id == self.runtime.current.id:
            self.add_user_turn(line, stamp)
        else:
            self.notify("Running queued task in background session")
        self._begin_ask(session_id)
        self._stream_stamp = stamp
        self.run_worker(
            partial(self._ask_work, line, stamp, session_id),
            group="ask",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _dispatch_queue(self) -> None:
        if self.busy or not self._queue:
            return
        item = self._queue.pop(0)
        if item.line.startswith("/"):
            # 排队的命令按命令分发，不能当聊天消息发给模型。
            self._command(item.line)
            self._drain_pending_intent()
            return
        self._start_ask(item.line, item.session_id)

    def _ui(self, fn, *args) -> None:
        try:
            self.call_from_thread(fn, *args)
        except RuntimeError:
            pass  # 界面已退出：丢弃收尾回调，避免淹没退出流程。

    def _ask_work(self, line: str, stamp: str, session_id: str | None) -> None:
        started = time.monotonic()
        try:
            session = None
            if session_id and not (
                self.runtime.has_session and self.runtime.session.id == session_id
            ):
                # 后台任务执行时从磁盘新鲜加载，不跨切换复用内存对象（对象会过期）。
                session = self.runtime.sessions.load(session_id)
            reply = self.runtime.ask(
                line,
                session=session,
                on_session=lambda s: self.call_from_thread(self._bind_ask_session, s.id),
                on_event=lambda event: self.call_from_thread(self.add_event, event),
                on_delta=lambda delta: self.call_from_thread(self._stream_append, stamp, delta),
                on_progress=lambda phase: self.call_from_thread(self._progress_update, phase),
            )
        except ChatModelError as exc:
            self._ui(self._finish_error, str(exc), session_id)
            return
        except Exception as exc:  # 界面不因单轮失败退出
            self._ui(self._finish_error, f"Request failed: {exc}", session_id)
            return
        elapsed = time.monotonic() - started
        meta = [f"{reply.steps} steps", f"{reply.estimated_tokens} tok"]
        if reply.usage.cost_usd:
            meta.append(f"${reply.usage.cost_usd:.4f}")
        meta.append(f"{elapsed:.1f}s")
        self._ui(self._finish_reply, reply.content, stamp, " · ".join(meta), session_id)

    # ---------- 流式渲染：帧合并更新，完成/中断后收尾 ----------

    def _stream_append(self, stamp: str, delta: str) -> None:
        self._progress_phase = "Streaming"
        self._stream_buffer += delta
        if not self._ask_in_current_view():
            return  # 请求在后台会话：内容留在 buffer，切回时重放。
        chat = self._chat()
        if self._stream_widget is None:
            chat.mount(Static(f"{ASSISTANT_NAME} · {stamp}", classes="who-ai"))
            self._stream_widget = ChatReply("", classes="reply")
            chat.mount(self._stream_widget)
        if not self._stream_flush_scheduled:
            self._stream_flush_scheduled = True
            self.call_after_refresh(self._stream_flush)

    def _stream_flush(self) -> None:
        self._stream_flush_scheduled = False
        if self._stream_widget is not None:
            self._stream_widget.update(self._stream_buffer)
            self._scroll_end()

    def _finalize_stream_segment(self, content: str) -> None:
        """就地定稿当前可见的活跃段；后台会话没有挂载控件，无需处理。"""
        if self._stream_widget is not None:
            self._stream_widget.update(content)
            self._stream_widget = None
        self._stream_flush_scheduled = False

    def _replay_stream(self) -> None:
        """切回生成中的会话：磁盘重建负责已完成轮次，这里只补挂尚未落盘的活跃段。"""
        if not self._stream_buffer:
            return
        chat = self._chat()
        chat.mount(Static(f"{ASSISTANT_NAME} · {self._stream_stamp}", classes="who-ai"))
        self._stream_widget = ChatReply(self._stream_buffer, classes="reply")
        chat.mount(self._stream_widget)
        self._scroll_end()

    def add_event(self, event: str) -> None:
        if self._stream_widget is not None and self._stream_buffer:
            # 工具事件在两轮生成之间：先就地定稿当前段（已完成段随 assistant 记录落盘），
            # 后续 delta 另起一段，否则最终回复会覆盖工具前的正文，事件顺序也会颠倒。
            self._finalize_stream_segment(self._stream_buffer)
            self._stream_buffer = ""
        memo = event.startswith(("Saved", "Corrected", "Deleted"))
        # 事件卡只进当前视图；后台请求的事件由磁盘 tool 记录在切回时重建。
        if self._ask_in_current_view():
            self._chat().mount(
                Static(f"✓ {event}" if memo else event, classes="event-card memo" if memo else "event-card")
            )
            self._scroll_end()

    # ---------- 回合收尾 ----------

    def _heal_current_session(self, session_id: str | None) -> None:
        """后台写入期间用户可能切走再切回，内存对象会落后于磁盘：完成后从磁盘刷新。"""
        if not session_id or not self.runtime.has_session or self.runtime.current.id != session_id:
            return
        try:
            self.runtime.session = self.runtime.sessions.load(session_id)
        except Exception:
            pass  # 会话文件异常时保留内存对象，后续操作各自报错。

    def _finish_reply(self, content: str, stamp: str, meta: str, session_id: str | None) -> None:
        sid = session_id or self._ask_in_flight
        in_view = sid is None or (
            self.runtime.has_session and self.runtime.current.id == sid
        )
        if in_view:
            if self._stream_widget is not None:
                # 流式已实时渲染正文：就地定稿并补 meta/分隔线，不再重复挂载。
                self._finalize_stream_segment(content or "(empty reply)")
                chat = self._chat()
                if meta:
                    chat.mount(Static(meta, classes="meta"))
                chat.mount(Static("", classes="divider"))
                self._scroll_end()
            else:
                self.add_assistant_turn(content, stamp, meta)
        else:
            # 后台完成：不动当前视图；回复已落盘，切回时从磁盘重建。
            self.notify("Reply ready in background session")
        self._heal_current_session(sid)
        self.refresh_sessions()  # 首条消息会创建会话，侧栏需要随之出现
        self.refresh_pending()
        self.update_status()
        self._end_ask()
        self._drain_pending_intent()

    def _finish_error(self, message: str, session_id: str | None = None) -> None:
        sid = session_id or self._ask_in_flight
        in_view = sid is None or (
            self.runtime.has_session and self.runtime.current.id == sid
        )
        if in_view:
            if self._stream_widget is not None:
                # 中途断流：保留已生成的半截并显式标注，不静默吞掉。
                interrupted = self._stream_buffer + "\n\n*(generation interrupted)*"
                self._finalize_stream_segment(interrupted)
                self._chat().mount(Static("", classes="divider"))
                self._scroll_end()
            self.add_card(message)
        else:
            self.notify(f"Background request failed: {message}")
        self._heal_current_session(sid)
        self.refresh_sessions()
        self._end_ask()
        # 通用失败出口：压缩/导入/切换失败走这里时还要释放导航忙。
        self._end_navigation()
        self._drain_pending_intent()

    def _plan_is_current(self) -> bool:
        """last_plan 属于别的会话时不得污染当前视图的上下文用量显示。"""
        runtime = self.runtime
        if runtime.last_plan_session is None:
            return True
        return runtime.has_session and runtime.last_plan_session == runtime.current.id
