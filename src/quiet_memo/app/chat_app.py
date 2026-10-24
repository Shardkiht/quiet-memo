"""Textual 全屏聊天界面：左侧会话列表 + 中部对话流 + 底部输入与状态栏。

界面层只做展示与选择收集；模型/思考/上下文的判定与落盘在 actions.py，
会话与记忆操作走 AgentRuntime / MemoryService。
"""
from __future__ import annotations

import time
from functools import partial

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListView, ListItem, Markdown, Static

from ..agent.runtime import AgentRuntime
from ..agent.session import SessionStore
from ..infra.tokens import format_tokens
from ..llm import ChatModelError
from ..memory.service import MemoryService
from . import actions
from .session_ui import visible_sessions

SLASH_COMMANDS = [
    ("new", "/new", "新建会话"),
    ("sessions", "/sessions", "跳到左侧会话列表"),
    ("resume", "/resume [id|query]", "恢复会话，无参数跳到会话列表"),
    ("compact", "/compact", "立即压缩上下文"),
    ("summary", "/summary", "查看当前会话摘要"),
    ("status", "/status", "会话与记忆状态"),
    ("models", "/models [refresh]", "切换模型，refresh 强制刷新列表"),
    ("think", "/think", "切换思考强度（low/medium/high，按模型支持）"),
    ("context", "/context", "切换上下文长度（档位按模型上限生成）"),
    ("setup", "/setup", "完整配置模型 / API Key"),
    ("rename", "/rename <title>", "重命名当前会话"),
    ("recall", "/recall <query>", "直接检索记忆"),
    ("remember", "/remember <text>", "直接写入记忆"),
    ("pending", "/pending", "查看待确认候选"),
    ("help", "/help", "查看命令帮助"),
    ("exit", "/exit", "退出"),
]


class OptionItem(ListItem):
    def __init__(self, label: str, value, marker: bool = False) -> None:
        self.value = value
        super().__init__(Label(f"{label}  ← 当前" if marker else label))


class SessionItem(ListItem):
    def __init__(self, row: dict) -> None:
        self.row = row
        super().__init__(
            Vertical(
                Label(row["title"], classes="session-title"),
                Label(row["meta"], classes="session-meta"),
            )
        )


class PickScreen(ModalScreen):
    """单选模态：ESC 取消（回调收到 None），Enter/点击确认。"""

    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, title: str, options: list[tuple], on_result) -> None:
        super().__init__()
        self._title = title
        self._options = options
        self._on_result = on_result

    def compose(self) -> ComposeResult:
        with Vertical(id="pick-card"):
            yield Static(self._title, classes="pick-title")
            yield ListView(
                *[OptionItem(*option) for option in self._options],
                id="pick-list",
            )
            yield Static("↑↓ 选择 · Enter 确认 · ESC 取消", classes="pick-hint")

    def on_mount(self) -> None:
        self.query_one("#pick-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        value = getattr(event.item, "value", None) if event.item is not None else None
        self._finish(value)

    def action_cancel(self) -> None:
        self._finish(None)

    def _finish(self, value) -> None:
        on_result = self._on_result
        self.dismiss()
        # dismiss 尚未完成时直接 push 下一层会撞屏，延迟到下一帧再回调。
        self.app.call_later(on_result, value)


class CommandRow(Static):
    """命令面板的一行：命令用法 + 说明；点击即执行。"""

    def __init__(self, command: str, usage: str, description: str) -> None:
        self.command = command
        super().__init__(f"[cmd-usage]{usage:<20}[/] [cmd-desc]{description}[/]")
        self.add_class("cmd-row")

    def on_click(self) -> None:
        self.app.run_command_from_palette(self.command)


class CommandPalette(Vertical):
    """竖排命令面板：跟随输入前缀过滤，↑↓ 移动高亮，Enter 执行。"""

    def __init__(self) -> None:
        super().__init__(id="cmd-palette")
        self.matches: list[tuple[str, str, str]] = []
        self.index = 0

    def open(self, prefix: str) -> None:
        needle = prefix.lower()
        self.matches = [
            (command, usage, description)
            for command, usage, description in SLASH_COMMANDS
            if command.startswith(needle)
        ]
        self.index = 0
        self.rebuild()
        self.display = bool(self.matches)

    def close(self) -> None:
        self.matches = []
        self.display = False

    def move(self, delta: int) -> None:
        if not self.matches:
            return
        self.index = (self.index + delta) % len(self.matches)
        self.refresh_rows()

    @property
    def current(self) -> str | None:
        if 0 <= self.index < len(self.matches):
            return self.matches[self.index][0]
        return None

    def rebuild(self) -> None:
        self.remove_children()
        if self.matches:
            self.mount(*[CommandRow(*match) for match in self.matches])
        self.refresh_rows()

    def refresh_rows(self) -> None:
        for position, row in enumerate(self.children):
            row.set_class(position == self.index, "cmd-active")


class TextInputScreen(ModalScreen):
    """单行输入模态：ESC 取消（None），Enter 提交。"""

    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, title: str, default: str = "", on_result=None) -> None:
        super().__init__()
        self._title = title
        self._default = default
        self._on_result = on_result

    def compose(self) -> ComposeResult:
        with Vertical(id="pick-card"):
            yield Static(self._title, classes="pick-title")
            yield Input(value=self._default, id="pick-input")
            yield Static("Enter 确认 · ESC 取消", classes="pick-hint")

    def on_mount(self) -> None:
        self.query_one("#pick-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._finish(event.value.strip())

    def action_cancel(self) -> None:
        self._finish(None)

    def _finish(self, value) -> None:
        on_result = self._on_result
        self.dismiss()
        self.app.call_later(on_result, value or None)


class ChatApp(App):
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
    #hide-sidebar { width: 3; height: 3; min-width: 3; background: #181a21; color: $accent; border: solid #4a4f5a; margin-right: 1; }
    #hide-sidebar:hover { border: solid $accent; }
    /* 侧栏隐藏后：对话区左上角的展开按钮，同款方框 */
    #show-sidebar { layer: overlay; dock: top; width: 3; height: 3; min-width: 3; background: #181a21; color: $accent; border: solid #4a4f5a; display: none; }
    #show-sidebar:hover { border: solid $accent; }
    #new-session { width: 1fr; margin: 0 1 1 1; background: #232630; color: $muted; border: dashed #333947; }
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
    #status { width: 1fr; height: 1; background: #181a21; color: $muted; padding: 0 2; }

    /* 命令面板：浮在输入框上方（覆盖对话区），OpenCode 风格竖排列表 */
    #cmd-palette { layer: overlay; dock: bottom; offset-y: -4; width: 64; height: auto; max-height: 16; background: $panel; border: round $dim; padding: 0 1; display: none; }
    .cmd-row { width: 1fr; height: 1; padding: 0 1; color: #c8ccd6; }
    .cmd-row.cmd-active { background: #2b3444; }
    .cmd-usage { color: $accent; }
    .cmd-desc { color: $faint; }

    #pick-card { width: 64; height: auto; max-height: 70%; background: $panel; border: round $dim; padding: 1 2; }
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

    # ---------- 布局 ----------

    def compose(self) -> ComposeResult:
        with Horizontal(id="root"):
            with Vertical(id="sidebar"):
                with Horizontal(id="sidebar-head"):
                    yield Static("quiet-memo", id="brand")
                    yield Button("«", id="hide-sidebar")
                yield Button("＋ 新会话", id="new-session")
                yield ListView(id="sessions")
            with Vertical(id="main"):
                yield Button("»", id="show-sidebar")
                yield VerticalScroll(id="chat")
                yield CommandPalette()
                yield Input(placeholder="发送消息，/ 命令，Ctrl+B 侧栏", id="input")
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

    def action_toggle_sidebar(self) -> None:
        self._set_sidebar_visible(not self.query_one("#sidebar", Vertical).display)

    def _set_sidebar_visible(self, visible: bool) -> None:
        self.query_one("#sidebar", Vertical).display = visible
        self.query_one("#show-sidebar", Button).display = not visible

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
        for row in visible_sessions(self.sessions.list(100)):
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
        if not isinstance(event.item, SessionItem) or self.busy:
            return
        row = event.item.row
        if self.runtime.has_session and row["id"] == self.runtime.current.id:
            self.notify("已经是当前会话。")
            self.query_one("#input", Input).focus()
            return
        self._switch_session(row["id"])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-session" and not self.busy:
            self._new_session()
        elif event.button.id in ("hide-sidebar", "show-sidebar"):
            self.action_toggle_sidebar()
            self.query_one("#input", Input).focus()

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
        self._set_busy(True)
        self.run_worker(
            partial(self._session_work, None),
            group="session",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _switch_session(self, session_id: str) -> None:
        self._set_busy(True)
        self.run_worker(
            partial(self._session_work, session_id),
            group="session",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _session_work(self, session_id: str | None) -> None:
        stats = self.runtime.close()
        if session_id is None:
            self.runtime.start()
            label = None
        else:
            session = self.runtime.resume(session_id)
            label = f"{session.id} · {session.title()}"
        self.call_from_thread(self._after_session_switch, stats, label)

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

    def confirm_delete_session(self, row: dict) -> None:
        if self.runtime.has_session and row["id"] == self.runtime.current.id:
            self.notify("当前会话不能删除，先 /new 再处理。")
            return
        self.push_screen(
            PickScreen(
                f"删除会话「{row['title']}」？不可恢复",
                [("确认删除", True), ("取消", False)],
                self._delete_session_result,
            )
        )

    def _delete_session_result(self, confirmed) -> None:
        if confirmed is not True:
            self.query_one("#input", Input).focus()
            return
        view = self.query_one("#sessions", ListView)
        item = view.highlighted_child
        if isinstance(item, SessionItem):
            self.sessions.delete(item.row["id"])
            self.refresh_sessions()
            self.notify("已删除会话。")
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
                line, on_event=lambda event: self.call_from_thread(self.add_event, event)
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

    def _finish_reply(self, content: str, stamp: str, meta: str) -> None:
        self.add_assistant_turn(content, stamp, meta)
        self.refresh_sessions()  # 首条消息会创建会话，侧栏需要随之出现
        self.refresh_pending()
        self.update_status()
        self._set_busy(False)

    def _finish_error(self, message: str) -> None:
        self.add_card(message)
        self.refresh_sessions()
        self._set_busy(False)

    # ---------- 斜杠命令 ----------

    def _command(self, line: str) -> None:
        command, _, argument = line.partition(" ")
        command = command.lower()
        argument = argument.strip()

        if command in ("/exit", "/quit"):
            self.exit()
        elif command == "/help":
            self.add_card("\n".join(f"{usage:<24}{description}" for _, usage, description in SLASH_COMMANDS), "命令")
        elif command in ("/models", "/model"):
            self._open_models(argument.lower() in ("refresh", "--refresh", "-r"))
        elif command == "/think":
            self._pick_reasoning()
        elif command == "/context":
            self._pick_context()
        elif command == "/setup":
            self.add_card("完整配置需在终端运行 qm setup，当前界面内无法启动配置向导。")
        elif command == "/new":
            if not self.busy:
                self._new_session()
        elif command in ("/sessions", "/resume"):
            if argument and command == "/resume":
                self._resume_by_query(argument)
            else:
                self.show_sidebar()
                self.notify("↑↓ 选择 · Enter 恢复 · Del 删除")
        elif command == "/compact":
            self._run_compact()
        elif command == "/summary":
            if not self.runtime.has_session:
                self.add_card("新会话还没有摘要。")
            else:
                summary = self.runtime.summary_text()
                self.add_markdown_card(summary, "会话摘要") if summary else self.add_card("当前会话还没有摘要。")
        elif command == "/status":
            self._show_status()
        elif command == "/rename":
            if not argument:
                self.add_card("用法：/rename <title>")
            elif not self.runtime.has_session:
                self.add_card("新会话尚未创建，发送第一条消息后再重命名。")
            else:
                self.runtime.rename(argument)
                self.refresh_sessions()
                self.add_card(f"已重命名为：{argument}")
        elif command == "/recall":
            if not argument:
                self.add_card("用法：/recall <query>")
            else:
                hits = self.service.recall(argument, 10)
                self.add_card(
                    "\n".join(f"[{hit.topic}] {hit.score:.2f} · {hit.text}" for hit in hits)
                    if hits
                    else "未找到相关记忆。"
                )
        elif command == "/remember":
            if not argument:
                self.add_card("用法：/remember <text>")
            else:
                self._run_remember(argument)
        elif command == "/pending":
            items = self.service.pending()
            self.add_card(
                "\n".join(f"{item.id} · [{item.topic}] {item.reason} — {item.text}" for item in items)
                if items
                else "没有待确认项。"
            )
        else:
            self.add_card(f"未知命令：{command}，输入 /help 查看帮助。")

    def _resume_by_query(self, query: str) -> None:
        if self.busy:
            return
        rows = visible_sessions(self.sessions.list(100))
        needle = query.lower()
        matched = next(
            (row for row in rows if row["id"] == query or needle in row["title"].lower()),
            None,
        )
        if matched is None:
            self.notify("没有匹配的会话。")
            return
        if self.runtime.has_session and matched["id"] == self.runtime.current.id:
            self.notify("已经是当前会话。")
            return
        self._switch_session(matched["id"])

    def _show_status(self) -> None:
        if not self.runtime.has_session:
            self.add_card("新会话尚未创建，发送第一条消息后生成。")
            return
        info = self.runtime.status()
        percent = info["estimated_tokens"] / max(info["context_window"], 1)
        self.add_card(
            f"会话：{info['session_id']} · {info['title']}\n"
            f"模型：{info['model']}\n"
            f"检索：{info['retrieval']}\n"
            f"上下文：{format_tokens(info['estimated_tokens'])}/{format_tokens(info['context_window'])}（{percent:.0%}）\n"
            f"摘要：{info['summaries']} 段 / {info['summary_chars']} 字 · 已覆盖 {info['covered_records']} 条记录\n"
            f"待确认：{info['pending']} 条",
            "状态",
        )

    def _run_compact(self) -> None:
        self._set_busy(True)
        self.run_worker(
            self._compact_work, group="ask", thread=True, exclusive=True, exit_on_error=False
        )

    def _compact_work(self) -> None:
        events = self.runtime.force_compact()
        self.call_from_thread(self._after_compact, events)

    def _after_compact(self, events: list[str]) -> None:
        for event in events:
            self.add_event(event)
        self.update_status()
        self._set_busy(False)

    def _run_remember(self, text: str) -> None:
        self._set_busy(True)
        self.run_worker(
            partial(self._remember_work, text),
            group="ask",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _remember_work(self, text: str) -> None:
        source = f"session:{self.runtime.current.id}:manual" if self.runtime.has_session else "manual"
        try:
            result = self.service.memorize(text, source=source)
            message = (
                f"写入 {len(result.admitted)} 条，待确认 {len(result.queued)} 条，"
                f"重复 {len(result.duplicates)} 条，拦截 {len(result.blocked)} 条"
            )
        except Exception as exc:
            message = f"写入失败：{exc}"
        self.call_from_thread(self._after_remember, message)

    def _after_remember(self, message: str) -> None:
        self.add_card(message)
        self.refresh_pending()
        self.update_status()
        self._set_busy(False)

    # ---------- 设置类选择器（逻辑在 actions.py） ----------

    def _open_models(self, refresh: bool) -> None:
        self._set_busy(True)
        self.run_worker(
            partial(self._models_work, refresh),
            group="ask",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _models_work(self, refresh: bool) -> None:
        try:
            options = actions.model_options(self.service, refresh=refresh)
        except Exception as exc:
            self.call_from_thread(self._finish_error, f"获取模型列表失败：{exc}")
            return
        entries = [(option.label, (option.provider, option.model), option.current) for option in options]
        entries.append(("↻ 刷新模型列表", ("__refresh__", ""), False))
        entries.append(("自定义模型…", ("__custom__", ""), False))
        self.call_from_thread(self._show_model_picker, entries)

    def _show_model_picker(self, entries: list[tuple]) -> None:
        self._set_busy(False)
        self.push_screen(
            PickScreen(f"当前模型：{self.runtime.model_label}", entries, self._model_picked)
        )

    def _model_picked(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        provider, model = value
        if provider == "__refresh__":
            self._open_models(True)
            return
        if provider == "__custom__":
            current = self.service.config.llm
            self.push_screen(
                TextInputScreen("模型名称", default=current.model, on_result=self._model_custom)
            )
            return
        message = actions.apply_model(self.service, self.runtime, provider, model)
        self.add_card(message)
        self.update_status()
        self.query_one("#input", Input).focus()

    def _model_custom(self, name) -> None:
        if not name:
            self.query_one("#input", Input).focus()
            return
        current = self.service.config.llm
        message = actions.apply_model(self.service, self.runtime, current.provider, name)
        self.add_card(message)
        self.update_status()
        self.query_one("#input", Input).focus()

    def _pick_reasoning(self) -> None:
        options = actions.reasoning_options(self.service)
        if not options:
            self.add_card(f"{self.runtime.model_label} 不支持思考强度参数，可用 /models 换支持 reasoning 的模型。")
            return
        current = self.service.config.llm.reasoning_effort or ""
        entries = [(label, value, value == current) for value, label in options]
        self.push_screen(PickScreen(f"当前思考强度：{current or '默认'}", entries, self._reasoning_picked))

    def _reasoning_picked(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        self.add_card(actions.apply_reasoning(self.service, value))
        self.query_one("#input", Input).focus()

    def _pick_context(self) -> None:
        current = self.service.config.chat.context_window
        entries = [(format_tokens(size), size, size == current) for size in actions.context_options(self.service)]
        entries.append(("自定义…", 0, False))
        self.push_screen(PickScreen(f"当前上下文：{format_tokens(current)}", entries, self._context_picked))

    def _context_picked(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        if value == 0:
            current = self.service.config.chat.context_window
            self.push_screen(
                TextInputScreen("上下文长度（token）", default=str(current), on_result=self._context_custom)
            )
            return
        self.add_card(actions.apply_context(self.service, value))
        self.update_status()
        self.query_one("#input", Input).focus()

    def _context_custom(self, value) -> None:
        if not value or not value.isdigit():
            self.query_one("#input", Input).focus()
            return
        self.add_card(actions.apply_context(self.service, int(value)))
        self.update_status()
        self.query_one("#input", Input).focus()
