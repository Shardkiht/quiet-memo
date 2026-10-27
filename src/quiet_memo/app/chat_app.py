"""Textual 全屏聊天界面：左侧会话列表 + 中部对话流 + 底部输入与状态栏。

界面层只做展示与选择收集；模型/思考/上下文的判定与落盘在 actions.py，
会话与记忆操作走 AgentRuntime / MemoryService。
"""
from __future__ import annotations

import json
import time
from functools import partial
from urllib.parse import urlsplit

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

ASSISTANT_NAME = "qmo"


class ChatApp(ChatCommandsMixin, App):
    TITLE = "quiet-memo — Personal Memory"

    CSS = """
    $accent: #d2d3d7;
    $dim: #3a3b42;
    $muted: #96979c;
    $faint: #63646b;
    $panel: #17181c;
    $bg: #131417;
    $scroll-track: #131417;
    $scroll-thumb: #45464e;
    $scroll-thumb-hover: #63646b;
    $scroll-thumb-active: #83848c;

    Screen { background: $bg; }
    /* 统一窄滚动条；新的可滚动组件加 thin-scroll 类即可复用。 */
    .thin-scroll {
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
        scrollbar-background: $scroll-track;
        scrollbar-color: $scroll-thumb;
        scrollbar-color-hover: $scroll-thumb-hover;
        scrollbar-color-active: $scroll-thumb-active;
    }
    #root { width: 1fr; height: 1fr; }
    #sidebar { width: 28; background: #101114; border-right: solid #202127; }
    #sidebar-head { height: 3; align: left middle; }
    #brand { color: $accent; text-style: bold; padding: 0 1; width: 1fr; }
    /* 折叠按钮只留符号，减少侧栏头部的方框噪音 */
    #hide-sidebar { width: 3; height: 3; min-width: 3; padding: 0; background: #101114; color: $faint; border: none; margin-right: 1; content-align: center middle; }
    #hide-sidebar:hover { color: $accent; }
    #show-sidebar { layer: overlay; dock: top; width: 3; height: 3; min-width: 3; padding: 0; background: $bg; color: $faint; border: none; content-align: center middle; display: none; }
    #show-sidebar:hover { color: $accent; }
    #new-session { width: 1fr; height: 3; margin: 0 1 1 1; background: #17181c; color: $muted; border: none; content-align: left middle; padding: 0 1; }
    #new-session:hover { background: #202127; color: $accent; }
    /* 列表与 New session 用同一左右外边距；隐藏滚动条，避免出现滚动条时行宽缩 2 格错位 */
    #sessions { width: 1fr; height: 1fr; margin: 0 1; scrollbar-size-vertical: 0; }
    /* ListItem 默认高度跟随子容器（Vertical 是 1fr 会撑满列表），必须钉死 auto */
    #sessions > ListItem { padding: 0 1; height: auto; }
    #sessions > ListItem > Vertical { height: auto; }
    .session-title { color: #c2c3c7; width: 1fr; overflow-x: hidden; text-overflow: ellipsis; }
    .session-meta { color: #777980; }
    /* 当前会话只用一条左线和轻微底色，避免整行亮色像误选 */
    #sessions > ListItem.-highlight { background: #1c1d22; }
    #sessions > ListItem.current { background: #1b1c21; border-left: heavy $muted; }
    #sessions > ListItem.current.-highlight { background: #22242a; border-left: heavy $accent; }
    .group-label { color: #777980; padding: 1 1 0 1; }

    #main { width: 1fr; layers: base overlay; }
    #chat { width: 100%; height: 1fr; padding: 1 0 1 3; }
    /* 右侧留白改由子元素承担，滚动条才能贴在窗口最右侧 */
    #chat > * { margin-right: 2; }
    .who-user { content-align: right top; color: #7c7d84; width: 1fr; margin-top: 1; }
    .user-turn { width: 1fr; height: auto; align: right top; }
    .bubble { background: #1e2026; color: #d5d6da; padding: 0 2; width: auto; max-width: 68%; height: auto; }
    .who-ai { color: $accent; text-style: bold; margin-top: 1; width: 1fr; }
    .reply { color: #d0d1d5; margin: 0; height: auto; }
    /* 工具事件用薄边线和弱底色分组，不与正式回复争夺注意力 */
    .event-card { border-left: solid $dim; background: #15161a; color: $muted; padding: 0 1; width: 1fr; }
    .event-card.memo { border-left: solid $muted; color: #a5a6ac; }
    .meta { color: $faint; width: 1fr; }
    .divider { border-top: solid #24262c; height: 1; margin: 1 0; }
    .card { border-left: solid $dim; background: $panel; padding: 0 2; margin: 1 0; color: #c2c3c7; width: 1fr; }
    /* 空会话欢迎区：无边框，靠字号和留白建立层级 */
    .welcome-brand { color: #d5d6da; text-style: bold; margin: 2 0 0 0; }
    .welcome-line { color: #9a9b9f; }
    .welcome-meta { color: #777980; margin-top: 1; }
    .welcome-hint { color: #777980; margin-top: 2; }

    #input { margin: 0 3 1 3; background: #17181c; border: round #303138; border-title-color: #83848c; border-title-align: left; }
    #input:focus { background: #191a1f; border: round #56575f; border-title-color: #b0b1b6; }
    /* 光标由 BarInput 渲染成竖线；这里只提供前景色，避免默认整格反色块。 */
    #input > .input--cursor {
        background: transparent;
        color: #d5d6da;
    }
    /* 1 行正文 + 1 行上边框；height:1 会被边框吃掉整个内容区。 */
    #status { width: 1fr; height: 2; background: $bg; color: $faint; padding: 0 3; border-top: solid #1d1e23; }

    /* 命令面板：浮在输入框上方，只显示命令、参数和一句短语 */
    #cmd-palette { layer: overlay; dock: bottom; offset-y: -4; width: 64; height: auto; max-height: 16; background: $panel; border: round #383a42; padding: 0 1; display: none; }
    .cmd-row { width: 1fr; height: 1; padding: 0 1; color: #b9babd; }
    .cmd-row.cmd-active { background: #292b32; }
    .cmd-usage { color: #96979c; }
    .cmd-desc { color: #7d7e85; }

    PickScreen, SessionPickerScreen, TextInputScreen { align: center middle; background: #0d0e11 75%; }
    #pick-card { width: 64; height: auto; max-height: 70%; background: $panel; border: round #383a42; padding: 1 2; }
    #picker-input, #pick-search, #pick-input { width: 1fr; background: #131417; color: #d5d6da; border: round #303138; }
    #picker-input:focus, #pick-search:focus, #pick-input:focus { background: #191a1f; border: round #56575f; }
    #picker-input, #pick-search { margin-bottom: 1; }
    #picker-list { height: auto; max-height: 16; }
    /* 与侧栏一致：SessionItem 内层 Vertical 默认 1fr 会把整行撑满 */
    #picker-list > ListItem { padding: 0 1; height: auto; border-left: heavy transparent; }
    #picker-list > ListItem > Vertical { height: auto; }
    #picker-list > ListItem.-highlight { background: #22242a; border-left: heavy $accent; }
    #picker-list > ListItem.current { background: #1b1c21; border-left: heavy $muted; }
    #picker-list > ListItem.current.-highlight { background: #22242a; border-left: heavy $accent; }
    .pick-title { color: $accent; text-style: bold; margin-bottom: 1; }
    #pick-list { height: auto; max-height: 18; }
    #pick-list > ListItem { height: auto; }
    #pick-list > ListItem.-highlight { background: #292b32; }
    .pick-hint { color: #83848c; margin-top: 1; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+b", "toggle_sidebar", "Toggle sidebar"),
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
                yield NewSessionButton("+ New session", id="new-session")
                yield ListView(id="sessions")
            with Vertical(id="main"):
                yield SidebarToggle("»", id="show-sidebar")
                yield VerticalScroll(id="chat", classes="thin-scroll")
                yield CommandPalette()
                yield BarInput(placeholder="Message or /command", id="input")
                yield Static("", id="status")

    def on_mount(self) -> None:
        self.refresh_pending()
        self.refresh_sessions()
        self.update_status()
        if self.runtime.has_session:
            self.build_transcript()
        else:
            self.add_welcome()
        # 主界面只有聊天输入框接受焦点；侧栏和对话区仍可点击/滚动，但不抢键盘焦点。
        self.query_one("#sessions", ListView).can_focus = False
        self.query_one("#chat", VerticalScroll).can_focus = False
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
            self.call_from_thread(self.notify, f"Web search did not start: {exc}")
            return
        if result == "started":
            self.call_from_thread(self.notify, "Web search started")

    def _run_catchup(self) -> None:
        """启动补沉淀：扫描异常终止/抽取失败的会话并补记，同时提交未保存的 vault 变更。

        会话扫描很便宜；git 查询和提交都放后台，避免启动时阻塞 UI。
        """
        exclude = self.runtime.current.id if self.runtime.has_session else None
        pending = self.sessions.unconsolidated(limit=3, exclude=exclude)
        if pending and self.runtime.config.extract_on_exit:
            # 补沉淀是后台维护，不占用交互 busy；切换会话和提问都不等它。
            self.notify(f"Catching up on {len(pending)} unfinished session(s) in the background")
            self.run_worker(
                self._catchup_work, group="catchup", thread=True, exit_on_error=False
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
            self.call_from_thread(self._after_catchup_error, str(exc))
            return
        self.call_from_thread(self._after_catchup, result, auto_commit)

    def _after_catchup_error(self, message: str) -> None:
        # 后台补记失败只提示，不动 busy：此时用户可能正在提问或切换会话。
        self.add_card(f"Startup catch-up failed: {message}")
        self.refresh_pending()
        self.update_status()

    def _background_commit_work(self) -> None:
        try:
            commit = self.service.commit_pending_changes()
        except Exception as exc:
            self.call_from_thread(self._finish_error, f"Startup auto-commit failed: {exc}")
            return
        if commit:
            self.call_from_thread(self._after_background_commit, commit)

    def _after_background_commit(self, commit: str) -> None:
        self.notify(f"Auto-committed uncommitted vault changes: {commit}")
        self.refresh_sessions()

    def _after_catchup(self, result: dict, auto_commit: str | None) -> None:
        if auto_commit:
            self.notify(f"Auto-committed uncommitted vault changes: {auto_commit}")
            self.refresh_sessions()
        if result["consolidated"] or result["errors"]:
            parts = []
            if result["consolidated"]:
                parts.append(
                    f"Consolidated {result['consolidated']} unfinished session(s): "
                    f"{result['admitted']} written, {result['queued']} pending"
                )
            for error in result["errors"]:
                parts.append(f"Catch-up failed: {error}")
            self.add_card("\n".join(parts), "Startup check")
            self.refresh_sessions()
            self.refresh_pending()
            self.update_status()

    def action_toggle_sidebar(self) -> None:
        self._set_sidebar_visible(not self.query_one("#sidebar", Vertical).display)

    def _set_sidebar_visible(self, visible: bool) -> None:
        self.query_one("#sidebar", Vertical).display = visible
        self.query_one("#show-sidebar", SidebarToggle).display = not visible

    def show_sidebar(self) -> None:
        self._set_sidebar_visible(True)
        self.query_one("#input", Input).focus()

    def _focus_active_input(self) -> None:
        if len(self.screen_stack) > 1:
            for widget in self.screen.query(Input):
                if widget.can_focus and widget.display and not widget.disabled:
                    widget.focus()
                    return
            return
        self.query_one("#input", Input).focus()

    def action_focus_next(self) -> None:
        self._focus_active_input()

    def action_focus_previous(self) -> None:
        self._focus_active_input()

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
            date = stamp[:10] or "Unknown date"
            if date != last_date:
                view.append(ListItem(Label(date, classes="group-label"), disabled=True))
                last_date = date
            time_text = stamp[11:16] if len(stamp) >= 16 else ""
            meta = f"{time_text} · {row['messages']} msgs" if time_text else f"{row['messages']} msgs"
            item = SessionItem({**row, "meta": meta})
            if row["id"] == current_id:
                item.add_class("current")
            view.append(item)

    def _sync_current_session(self) -> None:
        """切换会话只移动当前标记；侧栏时间/顺序留到下一次常规刷新。"""
        current_id = self.runtime.current.id if self.runtime.has_session else None
        found = False
        for item in self.query(SessionItem):
            is_current = item.row["id"] == current_id
            item.set_class(is_current, "current")
            found = found or is_current
        # /resume 可能指向尚未出现在当前列表里的会话，这时才退回全量刷新。
        if current_id and not found:
            self.refresh_sessions()

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
            self.notify("Already the current session.")
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
            self.call_from_thread(self._finish_error, f"Session switch failed: {exc}")
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
                f"Previous session distilled in background: {deferred['admitted']} written, {deferred['queued']} pending"
            )
        for error in deferred["errors"]:
            self.notify(f"Background distillation failed: {error}")

    def _after_session_switch(self, stats: dict, label: str | None) -> None:
        self._clear_chat()
        if label is None:
            self.add_welcome()
            message = "New session · created after your first message"
        else:
            self.build_transcript()
            message = f"Resumed {label}"
        if stats.get("admitted") or stats.get("queued"):
            message += f" (previous session distilled: {stats['admitted']} written, {stats['queued']} pending)"
        self._sync_current_session()
        self.refresh_pending()
        self.update_status()
        self._set_busy(False)
        self.notify(message)
        self.query_one("#input", Input).focus()
        self._drain_pending_intent()

    def confirm_delete_session(self, row: dict) -> None:
        if self.runtime.has_session and row["id"] == self.runtime.current.id:
            self.notify("Cannot delete the current session; use /new first.")
            return
        self.push_screen(
            PickScreen(
                f"Delete session \"{row['title']}\"? This cannot be undone",
                [("Delete", True), ("Cancel", False)],
                partial(self._delete_session_result, row),
            )
        )

    def _delete_session_result(self, row: dict, confirmed) -> None:
        if confirmed is not True:
            self.query_one("#input", Input).focus()
            return
        # 弹窗期间可能已排队切换到目标会话：确认时复查，不能删掉当前活跃会话。
        if self.runtime.has_session and row["id"] == self.runtime.current.id:
            self.notify("Cannot delete the current session; use /new first.")
            self.query_one("#input", Input).focus()
            return
        # 用弹窗打开前捕获的 row，不读列表当前高亮：模态期间列表可能被刷新重建。
        if self.sessions.exists(row["id"]):
            self.sessions.delete(row["id"])
            self.refresh_sessions()
            self.notify("Session deleted.")
        else:
            self.notify("Session no longer exists.")
        # 从会话选择器里删除时，同步刷新底层选择器的列表
        for screen in self.screen_stack:
            if isinstance(screen, SessionPickerScreen):
                screen.set_rows(visible_sessions(self.sessions.list(100, with_tokens=False)))
        self.query_one("#input", Input).focus()

    # ---------- 对话流 ----------

    def _chat(self) -> VerticalScroll:
        return self.query_one("#chat", VerticalScroll)

    def _clear_chat(self) -> None:
        self._chat().remove_children()

    def _scroll_end(self) -> None:
        self.call_after_refresh(self._chat().scroll_end, animate=False)

    def add_welcome(self) -> None:
        total = self.service.config.chat.context_window
        chat = self._chat()
        chat.mount(Static("quiet-memo", classes="welcome-brand"))
        chat.mount(Static("A quiet personal memory vault", classes="welcome-line"))
        chat.mount(
            Static(
                f"{self.runtime.model_label} · {self.service.retrieval_mode}"
                f" · {format_tokens(total)} context",
                classes="welcome-meta",
            )
        )
        chat.mount(
            Static(
                "/help commands · Ctrl+B sidebar · Ctrl+Q quit",
                classes="welcome-hint",
            )
        )

    def build_transcript(self) -> None:
        session = self.runtime.current
        widgets = []
        summary = session.summary_text()
        if summary:
            widgets.append(Static("Earlier summary", classes="who-ai"))
            widgets.append(Markdown(summary, classes="reply"))
        # 先按回合收集，工具结果按 call_id 挂回对应 assistant，回放时顺序与当时一致。
        turns: list[dict] = []
        tool_results: dict[str, dict] = {}
        for record in session.records_after_summary():
            record_type = record.get("type")
            if record_type == "tool":
                tool_results[str(record.get("tool_call_id"))] = record
                continue
            if record_type == "message":
                if record.get("role") == "user" and str(record.get("content") or "").strip():
                    turns.append(record)
                continue
            if record_type != "assistant":
                continue
            content = str(record.get("content") or "")
            if content.strip() or record.get("tool_calls"):
                turns.append(record)
        if len(turns) > 40:
            widgets.append(Static(f"{len(turns) - 40} earlier records omitted", classes="card"))
            turns = turns[-40:]
        for record in turns:
            content = str(record.get("content") or "")
            at = str(record.get("at") or "")
            stamp = at[11:16] if len(at) >= 16 else None
            if record.get("type") == "message":
                if stamp:
                    widgets.append(Static(f"You · {stamp}", classes="who-user"))
                widgets.append(Vertical(Static(content, classes="bubble"), classes="user-turn"))
                continue
            if content.strip():
                label = f"{ASSISTANT_NAME} · {stamp}" if stamp else ASSISTANT_NAME
                widgets.append(Static(label, classes="who-ai"))
                widgets.append(Markdown(content, classes="reply"))
            for call in record.get("tool_calls") or []:
                result = tool_results.get(str(call.get("id")))
                if result is None:
                    continue
                event = self._tool_event_label(call, result)
                memo = event.startswith(("Saved", "Corrected", "Deleted"))
                widgets.append(
                    Static(
                        f"✓ {event}" if memo else event,
                        classes="event-card memo" if memo else "event-card",
                    )
                )
        # 一次挂载整段历史，避免逐条 mount 在长会话里反复触发布局。
        if widgets:
            self._chat().mount(*widgets)
        self._scroll_end()

    @staticmethod
    def _tool_event_label(call: dict, result: dict) -> str:
        """从历史 tool 记录重建事件行；结果 JSON 被截断时退化为成功/失败，不展示原文。"""
        name = str(call.get("name") or result.get("name") or "tool")
        arguments = call.get("arguments") or {}
        ok = bool(result.get("ok"))
        content = str(result.get("content") or "")
        payload = None
        if "\n" in content:
            try:
                payload = json.loads(content.split("\n", 1)[1])
            except (TypeError, ValueError):
                payload = None
        if name == "search":
            query = str(arguments.get("query") or "")[:20]
            if ok and isinstance(payload, dict) and isinstance(payload.get("results"), list):
                provider = payload.get("provider") or "open_websearch"
                return f'Search "{query}": {len(payload["results"])} results ({provider})'
            return f'Search "{query}"' + ("" if ok else " failed")
        if name == "fetch_web":
            url = str(arguments.get("url") or "")
            if ok:
                final_url = str((payload or {}).get("final_url") or url)
                return f"Fetched {urlsplit(final_url).netloc or url}"
            detail = str((payload or {}).get("error") or "")
            return f"Fetch failed: {detail}" if detail else "Fetch failed"
        return f"{name} {'ok' if ok else 'failed'}"

    def add_user_turn(self, content: str, stamp: str | None = None) -> None:
        chat = self._chat()
        if stamp:
            chat.mount(Static(f"You · {stamp}", classes="who-user"))
        # 容器构造时直接带子组件，避免“先挂载容器再补挂子组件”的时序问题。
        chat.mount(Vertical(Static(content, classes="bubble"), classes="user-turn"))
        self._scroll_end()

    def add_assistant_turn(self, content: str, stamp: str, meta: str | None = None) -> None:
        chat = self._chat()
        chat.mount(Static(f"{ASSISTANT_NAME} · {stamp}", classes="who-ai"))
        chat.mount(Markdown(content or "(empty reply)", classes="reply"))
        if meta:
            chat.mount(Static(meta, classes="meta"))
        chat.mount(Static("", classes="divider"))
        self._scroll_end()

    def add_event(self, event: str) -> None:
        if self._stream_widget is not None and self._stream_buffer:
            # 工具事件在两轮生成之间：先就地定稿当前段，后续 delta 另起一块，
            # 否则最终回复会覆盖工具前的正文，事件顺序也会颠倒。
            self._stream_close(self._stream_buffer)
        memo = event.startswith(("Saved", "Corrected", "Deleted"))
        self._chat().mount(
            Static(f"✓ {event}" if memo else event, classes="event-card memo" if memo else "event-card")
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
        bar = f"[#8a8b90]{'━' * blocks}[/][#2f3035]{'─' * (10 - blocks)}[/]"
        used_text = format_tokens(used) if used else "0"
        text = (
            f"[#c2c3c7]◆ {self.runtime.model_label}[/] [#5f6068]·[/] "
            f"[#8a8b90]Context[/] {bar} [#8a8b90]{used_text}/{format_tokens(total)}[/]"
        )
        if self.pending_count:
            text += f"   [#b08a6a]Pending {self.pending_count}[/]"
        if percent >= self.runtime.config.compact_threshold:
            text += " [#b08a6a]⚠ compact next turn[/]"
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
            self.call_from_thread(self._finish_error, f"Request failed: {exc}")
            return
        elapsed = time.monotonic() - started
        meta = [f"{reply.steps} steps", f"{reply.estimated_tokens} tok"]
        if reply.usage.cost_usd:
            meta.append(f"${reply.usage.cost_usd:.4f}")
        meta.append(f"{elapsed:.1f}s")
        self.call_from_thread(self._finish_reply, reply.content, stamp, " · ".join(meta))

    # ---------- 流式渲染：帧合并更新，完成/中断后收尾 ----------

    def _stream_append(self, stamp: str, delta: str) -> None:
        chat = self._chat()
        if self._stream_widget is None:
            chat.mount(Static(f"{ASSISTANT_NAME} · {stamp}", classes="who-ai"))
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
            self._stream_close(content or "(empty reply)")
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
            interrupted = self._stream_buffer + "\n\n*(generation interrupted)*"
            self._stream_close(interrupted)
            self._chat().mount(Static("", classes="divider"))
            self._scroll_end()
        self.add_card(message)
        self.refresh_sessions()
        self._set_busy(False)
        self._drain_pending_intent()
