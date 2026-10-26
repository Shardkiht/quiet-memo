"""Textual 全屏聊天界面：左侧会话列表 + 中部对话流 + 底部输入与状态栏。

界面层只做展示与选择收集；模型/思考/上下文的判定与落盘在 actions.py，
会话与记忆操作走 AgentRuntime / MemoryService。
"""
from __future__ import annotations

import time
from functools import partial

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Label, ListView, ListItem, Markdown, Static

from ..agent.runtime import AgentRuntime
from ..agent.session import SessionStore
from ..infra.search.daemon import ensure_search_daemon
from ..infra.tokens import format_tokens
from ..llm import ChatModelError
from ..memory.service import MemoryService
from .chat_commands import ChatCommandsMixin
from .chat_input import BarInput
from .chat_widgets import (
    CommandPalette,
    NewSessionButton,
    PickScreen,
    SessionItem,
    SessionPickerScreen,
    SidebarToggle,
)
from .session_ui import visible_sessions


class ChatApp(ChatCommandsMixin, App):
    TITLE = "quiet-memo — 个人记忆"

    CSS = """
    $accent: #8FAECB;
    $dim: #46586B;
    $muted: #8a8f9c;
    $faint: #5a5f6c;
    $panel: #1c1e26;
    $green: #9CBF9A;

    #root { width: 1fr; height: 1fr; }
    #sidebar { width: 30; background: #181a21; border-right: solid #262933; }
    #sidebar-head { height: 3; align: left middle; }
    #brand { color: $accent; text-style: bold; padding: 0 1; width: 1fr; }
    /* 折叠按钮：朴素方框、不贴分割线（右侧留 1 格），避免凸出感 */
    #hide-sidebar { width: 3; height: 3; min-width: 3; padding: 0; background: #181a21; color: $accent; border: solid #4a4f5a; margin-right: 1; content-align: center middle; }
    #hide-sidebar:hover { border: solid $accent; }
    /* 侧栏隐藏后：对话区左上角的展开按钮，同款方框 */
    #show-sidebar { layer: overlay; dock: top; width: 3; height: 3; min-width: 3; padding: 0; background: #181a21; color: $accent; border: solid #4a4f5a; content-align: center middle; display: none; }
    #show-sidebar:hover { border: solid $accent; }
    #new-session { width: 1fr; height: 3; margin: 0 1 1 1; background: #232630; color: $muted; border: dashed #333947; content-align: center middle; }
    #new-session:hover { background: #2b303c; color: #d6dae2; }
    #sessions { height: 1fr; }
    /* ListItem 默认高度跟随子容器（Vertical 是 1fr 会撑满列表），必须钉死 auto */
    #sessions > ListItem { padding: 0 1; height: auto; }
    #sessions > ListItem > Vertical { height: auto; }
    .session-title { color: #c8ccd6; width: 1fr; overflow-x: hidden; text-overflow: ellipsis; }
    .session-meta { color: $faint; }
    /* 覆写主题默认的整行亮色高亮：用克制的主题色，避免空白区域像误选 */
    #sessions > ListItem.-highlight { background: #2b3444; }
    #sessions > ListItem.current { background: #232c3a; border-left: heavy $accent; }
    #sessions > ListItem.current.-highlight { background: #2b3444; border-left: heavy $accent; }
    .group-label { color: $faint; padding: 1 1 0 1; }

    #main { width: 1fr; layers: base overlay; }
    #chat { width: 1fr; height: 1fr; padding: 0 2; }
    .who-user { content-align: right top; color: $faint; width: 1fr; margin-top: 1; }
    .user-turn { width: 1fr; height: auto; align: right top; }
    .bubble { background: #232c3a; color: #dbe6f2; padding: 0 2; width: auto; max-width: 62%; height: auto; }
    .who-ai { color: $accent; text-style: bold; margin-top: 1; width: 1fr; }
    .reply { margin: 0 0 0 1; height: auto; }
    .event-card { border-left: heavy $dim; color: $muted; padding: 0 1; width: 1fr; }
    .event-card.memo { border-left: heavy $green; color: $green; }
    .meta { color: $faint; width: 1fr; }
    .divider { border-top: solid #3a4150; height: 1; margin: 1 0; }
    .card { border: round #262933; background: $panel; padding: 0 2; margin: 1 0; color: #c8ccd6; width: 1fr; }

    #input { margin: 0 2; }
    /* 光标由 BarInput 渲染成竖线；这里只提供前景色，避免默认整格反色块。 */
    #input > .input--cursor {
        background: transparent;
        color: #d6dae2;
    }
    #status { width: 1fr; height: 1; background: #181a21; color: $muted; padding: 0 2; }

    /* 命令面板：浮在输入框上方（覆盖对话区），OpenCode 风格竖排列表 */
    #cmd-palette { layer: overlay; dock: bottom; offset-y: -4; width: 64; height: auto; max-height: 16; background: $panel; border: round $dim; padding: 0 1; display: none; }
    .cmd-row { width: 1fr; height: 1; padding: 0 1; color: #c8ccd6; }
    .cmd-row.cmd-active { background: #2b3444; }
    .cmd-usage { color: $accent; }
    .cmd-desc { color: $faint; }

    #pick-card { width: 64; height: auto; max-height: 70%; background: $panel; border: round $dim; padding: 1 2; }
    #picker-input { width: 1fr; margin-bottom: 1; }
    #picker-list { height: auto; max-height: 16; }
    .pick-title { color: $accent; text-style: bold; margin-bottom: 1; }
    #pick-list { height: auto; max-height: 18; }
    #pick-input { width: 1fr; }
    .pick-hint { color: $faint; margin-top: 1; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("ctrl+b", "toggle_sidebar", "折叠/展开会话栏"),
    ]

    def __init__(
        self,
        runtime: AgentRuntime,
        service: MemoryService,
        sessions: SessionStore,
    ) -> None:
        super().__init__()
        self.runtime = runtime
        self.service = service
        self.sessions = sessions
        self.busy = False
        self.pending_count = 0
        self._pending_switch: str | None = None
        self._pending_new = False
        self._stream_widget: Markdown | None = None
        self._stream_buffer = ""
        self._stream_flush_scheduled = False

    # ---------- 布局 ----------

    def compose(self) -> ComposeResult:
        with Horizontal(id="root"):
            with Vertical(id="sidebar"):
                with Horizontal(id="sidebar-head"):
                    yield Static("quiet-memo", id="brand")
                    yield SidebarToggle("«", id="hide-sidebar")
                yield NewSessionButton("＋ 新会话", id="new-session")
                yield ListView(id="sessions")
            with Vertical(id="main"):
                yield SidebarToggle("»", id="show-sidebar")
                yield VerticalScroll(id="chat")
                yield CommandPalette()
                yield BarInput(placeholder="发送消息，/ 命令，Ctrl+B 侧栏", id="input")
                yield Static("", id="status")

    def on_mount(self) -> None:
        self.refresh_pending()
        self.refresh_sessions()
        self.update_status()
        if self.runtime.has_session:
            self.build_transcript()
        else:
            self.add_welcome()
        self.query_one("#input", Input).focus()
        self._run_catchup()
        self._warm_search()

    def _warm_search(self) -> None:
        search = self.service.config.search
        if not search.enabled or not search.auto_start or search.transport != "http":
            return
        self.run_worker(
            self._warm_search_work, group="search", thread=True, exit_on_error=False
        )

    def _warm_search_work(self) -> None:
        try:
            result = ensure_search_daemon(self.service.config.search.base_url)
        except Exception as exc:
            self.call_from_thread(self.notify, f"联网搜索未启动：{exc}")
            return
        if result == "started":
            self.call_from_thread(self.notify, "联网搜索已启动")

    def _run_catchup(self) -> None:
        """启动补沉淀：扫描异常终止/抽取失败的会话并补记，同时提交未保存的 vault 变更。

        会话扫描很便宜；git 查询和提交都放后台，避免启动时阻塞 UI。
        """
        exclude = self.runtime.current.id if self.runtime.has_session else None
        pending = self.sessions.unconsolidated(limit=3, exclude=exclude)
        if pending:
            self._set_busy(True)
            self.run_worker(
                self._catchup_work, group="ask", thread=True, exclusive=True, exit_on_error=False
            )
            return
        self.run_worker(
            self._background_commit_work, group="startup", thread=True, exit_on_error=False
        )

    def _catchup_work(self) -> None:
        try:
            result = self.runtime.consolidate_pending()
            auto_commit = self.service.commit_pending_changes()
        except Exception as exc:
            self.call_from_thread(self._finish_error, f"启动补记检查失败：{exc}")
            return
        self.call_from_thread(self._after_catchup, result, auto_commit)

    def _background_commit_work(self) -> None:
        try:
            commit = self.service.commit_pending_changes()
        except Exception as exc:
            self.call_from_thread(self._finish_error, f"启动自动提交失败：{exc}")
            return
        if commit:
            self.call_from_thread(self._after_background_commit, commit)

    def _after_background_commit(self, commit: str) -> None:
        self.notify(f"已自动提交 vault 未保存变更：{commit}")
        self.refresh_sessions()

    def _after_catchup(self, result: dict, auto_commit: str | None) -> None:
        self._set_busy(False)
        if auto_commit:
            self.notify(f"已自动提交 vault 未保存变更：{auto_commit}")
            self.refresh_sessions()
        if result["consolidated"] or result["errors"]:
            parts = []
            if result["consolidated"]:
                parts.append(
                    f"补记 {result['consolidated']} 个未收尾会话："
                    f"写入 {result['admitted']} 条，待确认 {result['queued']} 条"
                )
            for error in result["errors"]:
                parts.append(f"补记失败 {error}")
            self.add_card("\n".join(parts), "启动检查")
            self.refresh_sessions()
            self.refresh_pending()
            self.update_status()
        self._drain_pending_intent()

    def action_toggle_sidebar(self) -> None:
        self._set_sidebar_visible(not self.query_one("#sidebar", Vertical).display)

    def _set_sidebar_visible(self, visible: bool) -> None:
        self.query_one("#sidebar", Vertical).display = visible
        self.query_one("#show-sidebar", SidebarToggle).display = not visible

    def show_sidebar(self) -> None:
        self._set_sidebar_visible(True)
        self.query_one("#sessions", ListView).focus()

    def run_command_from_palette(self, command: str) -> None:
        self.query_one("#cmd-palette", CommandPalette).close()
        self.query_one("#input", Input).value = ""
        self._command(f"/{command}")

    # ---------- 会话侧栏 ----------

    def refresh_sessions(self) -> None:
        view = self.query_one("#sessions", ListView)
        view.clear()
        current_id = self.runtime.current.id if self.runtime.has_session else None
        last_date = None
        for row in visible_sessions(self.sessions.list(100, with_tokens=False)):
            stamp = row.get("updated_at") or row.get("started_at") or ""
            date = stamp[:10] or "未知日期"
            if date != last_date:
                view.append(ListItem(Label(date, classes="group-label"), disabled=True))
                last_date = date
            time_text = stamp[11:16] if len(stamp) >= 16 else ""
            meta = f"{time_text} · {row['messages']} 条" if time_text else f"{row['messages']} 条"
            item = SessionItem({**row, "meta": meta})
            if row["id"] == current_id:
                item.add_class("current")
            view.append(item)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.item, SessionItem):
            return
        if self.busy:
            # 忙碌期间点击会话：记住意图，结束后自动切换，不静默丢弃。
            self._pending_switch = event.item.row["id"]
            self._pending_new = False
            return
        row = event.item.row
        if self.runtime.has_session and row["id"] == self.runtime.current.id:
            self.notify("已经是当前会话。")
            self.query_one("#input", Input).focus()
            return
        self._switch_session(row["id"])

    def on_key(self, event) -> None:
        palette = self.query_one("#cmd-palette", CommandPalette)
        if palette.display:
            if event.key == "up":
                event.prevent_default()
                palette.move(-1)
                return
            if event.key == "down":
                event.prevent_default()
                palette.move(1)
                return
            if event.key == "escape":
                event.prevent_default()
                palette.close()
                return
        # Del 删除侧栏高亮的会话；其他按键不受影响。
        if event.key != "delete":
            return
        view = self.query_one("#sessions", ListView)
        if not view.has_focus:
            return
        item = view.highlighted_child
        if isinstance(item, SessionItem):
            event.prevent_default()
            self.confirm_delete_session(item.row)

    def _new_session(self) -> None:
        if self.busy:
            self._pending_new = True
            self._pending_switch = None
            return
        self._set_busy(True)
        self.run_worker(
            partial(self._session_work, None),
            group="session",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _switch_session(self, session_id: str) -> None:
        if self.busy:
            self._pending_switch = session_id
            self._pending_new = False
            return
        self._set_busy(True)
        self.run_worker(
            partial(self._session_work, session_id),
            group="session",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _drain_pending_intent(self) -> None:
        """忙碌期间收到的会话意图在空闲后接续执行，最后一次操作优先。"""
        if self.busy:
            return
        if self._pending_new:
            self._pending_new = False
            self._new_session()
            return
        if self._pending_switch:
            session_id = self._pending_switch
            self._pending_switch = None
            self._switch_session(session_id)

    def _session_work(self, session_id: str | None) -> None:
        try:
            # 快速路径：切换只做记账（distill=False 不跑 LLM 抽取），UI 立即就绪。
            stats = self.runtime.close(distill=False)
            if session_id is None:
                self.runtime.start()
                label = None
            else:
                session = self.runtime.resume(session_id)
                label = f"{session.id} · {session.title()}"
        except Exception as exc:
            # 会话文件损坏/被删等异常不能让 busy 卡死：留在原会话并提示。
            self.call_from_thread(self._finish_error, f"会话切换失败：{exc}")
            return
        self.call_from_thread(self._after_session_switch, stats, label)
        # 沉淀延后到后台：刚切走的会话标记 distill_deferred，这里立即补；
        # 用户已开始新对话（busy）则不打断，留给下次启动补记。
        if self.busy:
            return
        try:
            # 切走时的后台补沉淀设阈值：小尾巴攒批留给启动补记，不烧小调用。
            deferred = self.runtime.consolidate_pending(min_new_messages=3)
        except Exception:
            return
        if deferred["consolidated"] or deferred["errors"]:
            self.call_from_thread(self._after_deferred_distill, deferred)

    def _after_deferred_distill(self, deferred: dict) -> None:
        self.refresh_pending()
        self.update_status()
        if deferred["consolidated"]:
            self.notify(
                f"上一会话已后台沉淀：写入 {deferred['admitted']} 条，待确认 {deferred['queued']} 条"
            )
        for error in deferred["errors"]:
            self.notify(f"后台沉淀失败：{error}")

    def _after_session_switch(self, stats: dict, label: str | None) -> None:
        self._clear_chat()
        if label is None:
            self.add_welcome()
            message = "新会话 · 发送第一条消息后创建"
        else:
            self.build_transcript()
            message = f"已恢复 {label}"
        if stats.get("admitted") or stats.get("queued"):
            message += f"（上一会话沉淀：写入 {stats['admitted']} 条，待确认 {stats['queued']} 条）"
        self.refresh_sessions()
        self.refresh_pending()
        self.update_status()
        self._set_busy(False)
        self.notify(message)
        self.query_one("#input", Input).focus()
        self._drain_pending_intent()

    def confirm_delete_session(self, row: dict) -> None:
        if self.runtime.has_session and row["id"] == self.runtime.current.id:
            self.notify("当前会话不能删除，先 /new 再处理。")
            return
        self.push_screen(
            PickScreen(
                f"删除会话「{row['title']}」？不可恢复",
                [("确认删除", True), ("取消", False)],
                partial(self._delete_session_result, row),
            )
        )

    def _delete_session_result(self, row: dict, confirmed) -> None:
        if confirmed is not True:
            self.query_one("#input", Input).focus()
            return
        # 用弹窗打开前捕获的 row，不读列表当前高亮：模态期间列表可能被刷新重建。
        if self.sessions.exists(row["id"]):
            self.sessions.delete(row["id"])
            self.refresh_sessions()
            self.notify("已删除会话。")
        else:
            self.notify("会话已不存在。")
        # 从会话选择器里删除时，同步刷新底层选择器的列表
        for screen in self.screen_stack:
            if isinstance(screen, SessionPickerScreen):
                screen.set_rows(visible_sessions(self.sessions.list(100, with_tokens=False)))
        self.query_one("#input", Input).focus()

    # ---------- 对话流 ----------

    def _chat(self) -> VerticalScroll:
        return self.query_one("#chat", VerticalScroll)

    def _clear_chat(self) -> None:
        for child in list(self._chat().children):
            child.remove()

    def _scroll_end(self) -> None:
        self.call_after_refresh(self._chat().scroll_end, animate=False)

    def add_welcome(self) -> None:
        total = self.service.config.chat.context_window
        self.add_card(
            "安静的个人记忆库 · 发送第一条消息后创建会话\n"
            f"◆ {self.runtime.model_label} · {self.service.retrieval_mode}"
            f" · 上下文 {format_tokens(total)}\n"
            "/help 命令 · /models 模型 · Ctrl+B 折叠侧栏 · Ctrl+Q 退出"
        )

    def build_transcript(self) -> None:
        session = self.runtime.current
        summary = session.summary_text()
        if summary:
            self.add_markdown_card(summary, title="此前摘要")
        records = [
            record
            for record in session.records_after_summary()
            if record.get("type") in ("message", "assistant")
            and str(record.get("content") or "").strip()
        ]
        if len(records) > 40:
            self.add_card(f"更早的 {len(records) - 40} 条记录已省略")
            records = records[-40:]
        for record in records:
            content = str(record.get("content"))
            at = str(record.get("at") or "")
            stamp = at[11:16] if len(at) >= 16 else None
            if record.get("type") == "message" and record.get("role") == "user":
                self.add_user_turn(content, stamp)
            else:
                self._chat().mount(Static(f"quiet-memo · {stamp}" if stamp else "quiet-memo", classes="who-ai"))
                self._chat().mount(Markdown(content, classes="reply"))
        self._scroll_end()

    def add_user_turn(self, content: str, stamp: str | None = None) -> None:
        chat = self._chat()
        if stamp:
            chat.mount(Static(f"我 · {stamp}", classes="who-user"))
        # 容器构造时直接带子组件，避免“先挂载容器再补挂子组件”的时序问题。
        chat.mount(Vertical(Static(content, classes="bubble"), classes="user-turn"))
        self._scroll_end()

    def add_assistant_turn(self, content: str, stamp: str, meta: str | None = None) -> None:
        chat = self._chat()
        chat.mount(Static(f"quiet-memo · {stamp}", classes="who-ai"))
        chat.mount(Markdown(content or "（空回复）", classes="reply"))
        if meta:
            chat.mount(Static(meta, classes="meta"))
        chat.mount(Static("", classes="divider"))
        self._scroll_end()

    def add_event(self, event: str) -> None:
        memo = event.startswith(("写入", "已更正", "已删除"))
        self._chat().mount(
            Static(f"✓ {event}" if memo else f"┃ {event}", classes="event-card memo" if memo else "event-card")
        )
        self._scroll_end()

    def add_card(self, text: str, title: str | None = None) -> None:
        body = f"{title}\n{text}" if title else text
        self._chat().mount(Static(body, classes="card"))
        self._scroll_end()

    def add_markdown_card(self, markdown: str, title: str | None = None) -> None:
        if title:
            self._chat().mount(Static(title, classes="who-ai"))
        self._chat().mount(Markdown(markdown, classes="reply"))
        self._scroll_end()

    # ---------- 状态栏与忙碌态 ----------

    def refresh_pending(self) -> None:
        self.pending_count = len(self.service.pending())

    def update_status(self) -> None:
        total = self.service.config.chat.context_window
        plan = self.runtime.last_plan
        used = plan.estimated_tokens if plan is not None else 0
        percent = used / max(total, 1)
        blocks = min(10, max(0, round(percent * 10)))
        # 用量条用制表横线（━ 已用 / ─ 剩余）：块/阴影字符在部分 CJK 字体会画成竖线；
        # 低占用时百分比恒为 0%，直接展示“已用/上限”数字。
        bar = f"[#8FAECB]{'━' * blocks}[/][#4a4f5a]{'─' * (10 - blocks)}[/]"
        used_text = format_tokens(used) if used else "0"
        text = (
            f"[#d0d0d0]◆ {self.runtime.model_label}[/] [#5a5f6c]·[/] [#8a8f9c]{self.service.retrieval_mode}[/]"
            f"   [#5a5f6c]上下文[/] {bar} [#8a8f9c]{used_text}/{format_tokens(total)}[/]"
        )
        if self.pending_count:
            text += f"   [#D9A66C]待确认 {self.pending_count}[/]"
        if percent >= self.runtime.config.compact_threshold:
            text += " [#D9A66C]⚠ 下轮压缩[/]"
        self.query_one("#status", Static).update(text)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        widget = self.query_one("#input", Input)
        widget.disabled = busy
        if not busy:
            widget.focus()

    # ---------- 输入与回合 ----------

    def on_input_changed(self, event: Input.Changed) -> None:
        palette = self.query_one("#cmd-palette", CommandPalette)
        value = event.value
        if value.startswith("/") and " " not in value:
            palette.open(value[1:])
        else:
            palette.close()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.busy:
            return
        line = event.value.strip()
        event.input.value = ""
        if not line:
            return
        if line.startswith("/"):
            palette = self.query_one("#cmd-palette", CommandPalette)
            if palette.display and palette.current and " " not in line:
                self.run_command_from_palette(palette.current)
                return
            palette.close()
            self._command(line)
            return
        self.query_one("#cmd-palette", CommandPalette).close()
        stamp = time.strftime("%H:%M")
        self.add_user_turn(line, stamp)
        self._set_busy(True)
        self.run_worker(
            partial(self._ask_work, line, stamp),
            group="ask",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _ask_work(self, line: str, stamp: str) -> None:
        started = time.monotonic()
        try:
            reply = self.runtime.ask(
                line,
                on_event=lambda event: self.call_from_thread(self.add_event, event),
                on_delta=lambda delta: self.call_from_thread(self._stream_append, stamp, delta),
            )
        except ChatModelError as exc:
            self.call_from_thread(self._finish_error, str(exc))
            return
        except Exception as exc:  # 界面不因单轮失败退出
            self.call_from_thread(self._finish_error, f"调用失败：{exc}")
            return
        elapsed = time.monotonic() - started
        meta = [f"{reply.steps} 步", f"{reply.estimated_tokens} tok"]
        if reply.usage.cost_usd:
            meta.append(f"${reply.usage.cost_usd:.4f}")
        meta.append(f"{elapsed:.1f}s")
        self.call_from_thread(self._finish_reply, reply.content, stamp, " · ".join(meta))

    # ---------- 流式渲染：帧合并更新，完成/中断后收尾 ----------

    def _stream_append(self, stamp: str, delta: str) -> None:
        chat = self._chat()
        if self._stream_widget is None:
            chat.mount(Static(f"quiet-memo · {stamp}", classes="who-ai"))
            self._stream_buffer = ""
            self._stream_widget = Markdown("", classes="reply")
            chat.mount(self._stream_widget)
        self._stream_buffer += delta
        if not self._stream_flush_scheduled:
            self._stream_flush_scheduled = True
            self.call_after_refresh(self._stream_flush)

    def _stream_flush(self) -> None:
        self._stream_flush_scheduled = False
        if self._stream_widget is not None:
            self._stream_widget.update(self._stream_buffer)
            self._scroll_end()

    def _stream_close(self, final_content: str) -> None:
        if self._stream_widget is not None:
            self._stream_widget.update(final_content)
            self._stream_widget = None
            self._stream_buffer = ""
        self._stream_flush_scheduled = False

    def _finish_reply(self, content: str, stamp: str, meta: str) -> None:
        if self._stream_widget is not None:
            # 流式已实时渲染正文：就地定稿并补 meta/分隔线，不再重复挂载。
            self._stream_close(content or "（空回复）")
            chat = self._chat()
            if meta:
                chat.mount(Static(meta, classes="meta"))
            chat.mount(Static("", classes="divider"))
            self._scroll_end()
        else:
            self.add_assistant_turn(content, stamp, meta)
        self.refresh_sessions()  # 首条消息会创建会话，侧栏需要随之出现
        self.refresh_pending()
        self.update_status()
        self._set_busy(False)
        self._drain_pending_intent()

    def _finish_error(self, message: str) -> None:
        if self._stream_widget is not None:
            # 中途断流：保留已生成的半截并显式标注，不静默吞掉。
            interrupted = self._stream_buffer + "\n\n*（生成中断）*"
            self._stream_close(interrupted)
            self._chat().mount(Static("", classes="divider"))
            self._scroll_end()
        self.add_card(message)
        self.refresh_sessions()
        self._set_busy(False)
        self._drain_pending_intent()
