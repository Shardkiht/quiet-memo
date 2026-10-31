"""TUI 小组件：命令面板、选择/输入模态与侧栏条目和按钮。

组件一律通过 self.app 鸭子类型回调 ChatApp，不反向 import，避免界面层内部循环依赖。
"""
from __future__ import annotations

from rich.markup import escape
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.content import Content
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.style import Style
from textual.strip import Strip
from textual.visual import RenderOptions, RichVisual
from textual.widgets import Input, Label, ListView, ListItem, Static

from ..agent.session import Session
from .command_catalog import SLASH_COMMANDS


class _SelectableMarkdown(RichVisual):
    """Rich Markdown 本身不提供文本坐标，转成 Content 后才能选择和高亮。"""

    def __init__(self, widget: Static, markdown: str) -> None:
        super().__init__(widget, RichMarkdown(markdown))
        self._content_cache: tuple[int, Content] | None = None

    def selectable_content(self, width: int) -> Content:
        width = max(1, width)
        if self._content_cache is None or self._content_cache[0] != width:
            console = self._widget.app.console
            options = self._widget.app.console_options.update(width=width, highlight=False)
            text = Text.assemble(*(
                (segment.text, segment.style)
                for segment in console.render(self._renderable, options)
                if not segment.control
            ))
            text = text[:len(text.plain.rstrip("\n"))]
            self._content_cache = (width, Content.from_rich_text(text))
        return self._content_cache[1]

    def render_strips(
        self, width: int, height: int | None, style: Style, options: RenderOptions
    ) -> list[Strip]:
        return self.selectable_content(width).render_strips(width, height, style, options)


class ChatReply(Static):
    """整条回复用一个可缓存控件绘制，避免每个 Markdown 块再注册一组子控件。"""

    def __init__(self, content: str, **kwargs) -> None:
        self._markdown = content
        super().__init__(**kwargs)
        super().update(_SelectableMarkdown(self, content))

    def update(self, content: str) -> None:
        self._markdown = content
        super().update(_SelectableMarkdown(self, content))

    def get_selection(self, selection: Selection) -> tuple[str, str]:
        content = self.visual.selectable_content(self.content_size.width)
        return selection.extract(str(content)), "\n"


class HistoryMore(Static):
    can_focus = True
    BINDINGS = [("enter", "load", "Load history"), ("space", "load", "Load history")]

    def on_click(self) -> None:
        self.action_load()

    def action_load(self) -> None:
        self.parent.load_earlier()


class TranscriptView(VerticalScroll):
    PAGE_SIZE = 10

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._turns: list[dict] = []
        self._tools: dict[str, dict] = {}
        self._start = 0
        self._more: HistoryMore | None = None

    def clear_transcript(self) -> None:
        self._turns = []
        self._tools = {}
        self._start = 0
        self._more = None
        self.remove_children()

    def show_session(self, session: Session) -> None:
        self._turns = []
        self._tools = {}
        for record in session.records_after_summary():
            kind = record.get("type")
            if kind == "tool":
                self._tools[str(record.get("tool_call_id"))] = record
            elif kind == "message" and record.get("role") == "user" and str(record.get("content") or "").strip():
                self._turns.append(record)
            elif kind == "assistant" and (str(record.get("content") or "").strip() or record.get("tool_calls")):
                self._turns.append(record)
        widgets = []
        if session.damaged_tail_line is not None:
            widgets.append(Static("Incomplete session tail: recovered complete records; original file preserved.", classes="card"))
        summary = session.summary_text()
        if summary:
            widgets.extend([Static("Earlier summary", classes="who-ai"), ChatReply(summary, classes="reply")])
        self._start = max(0, len(self._turns) - self.PAGE_SIZE)
        if self._start:
            self._more = HistoryMore(f"Load earlier records ({self._start} remaining)", classes="card")
            widgets.append(self._more)
        widgets.extend(self._widgets(self._turns[self._start:]))
        if widgets:
            self.mount(*widgets)

    def _widgets(self, turns: list[dict]) -> list:
        widgets = []
        for record in turns:
            content = str(record.get("content") or "")
            stamp = str(record.get("at") or "")[11:16]
            if record.get("type") == "message":
                if stamp:
                    widgets.append(Static(f"You · {stamp}", classes="who-user"))
                widgets.append(Vertical(Static(content, classes="bubble"), classes="user-turn"))
                continue
            if content.strip():
                widgets.append(Static(f"qmo · {stamp}" if stamp else "qmo", classes="who-ai"))
                widgets.append(ChatReply(content, classes="reply"))
            for call in record.get("tool_calls") or []:
                result = self._tools.get(str(call.get("id")))
                if result is not None:
                    event = self.app._tool_event_label(call, result)
                    memo = event.startswith(("Saved", "Corrected", "Deleted"))
                    widgets.append(Static(f"✓ {event}" if memo else event,
                                          classes="event-card memo" if memo else "event-card"))
        return widgets

    def load_earlier(self) -> None:
        if not self._start or self._more is None:
            return
        more = self._more
        start = max(0, self._start - self.PAGE_SIZE)
        self.mount(*self._widgets(self._turns[start:self._start]), after=more)
        self._start = start
        if start:
            more.update(f"Load earlier records ({start} remaining)")
        else:
            more.remove()
            self._more = None
        self.call_after_refresh(self.scroll_home, animate=False)


class OptionItem(ListItem):
    def __init__(self, label: str, value, marker: bool = False) -> None:
        self.label = label
        self.value = value
        super().__init__(Label(f"{label}  ← current" if marker else label))


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
    """单选模态：ESC 取消（回调收到 None），Enter/点击确认；可选用搜索框过滤。"""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("down", "focus_list", "Select"),
    ]

    def __init__(
        self, title: str, options: list[tuple], on_result, searchable: bool = False
    ) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        self._on_result = on_result
        self._searchable = searchable

    @staticmethod
    def _items(options: list[tuple]) -> list[OptionItem]:
        return [OptionItem(*option) for option in options]

    def compose(self) -> ComposeResult:
        with Vertical(id="pick-card"):
            yield Static(self._title, classes="pick-title")
            if self._searchable:
                yield Input(placeholder="Search models…", id="pick-search")
            yield ListView(*self._items(self._options), id="pick-list", classes="thin-scroll")
            hint = (
                "Type to filter · ↓ list · Enter confirm · ESC cancel"
                if self._searchable
                else "↑↓ select · Enter confirm · ESC cancel"
            )
            yield Static(hint, classes="pick-hint")

    def on_mount(self) -> None:
        target = "#pick-search" if self._searchable else "#pick-list"
        self.query_one(target).focus()

    def update_options(self, options: list[tuple]) -> None:
        self._options = list(options)
        value = self.query_one("#pick-search", Input).value if self._searchable else ""
        self._apply_filter(value)

    def _apply_filter(self, value: str) -> None:
        needle = value.strip().lower()
        options = [option for option in self._options if needle in option[0].lower()]
        listing = self.query_one("#pick-list", ListView)
        listing.clear()
        for item in self._items(options):
            listing.append(item)
        if options:
            listing.index = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._searchable:
            self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not self._searchable:
            return
        event.stop()
        item = self.query_one("#pick-list", ListView).highlighted_child
        if item is not None:
            self._finish(item.value)

    def action_focus_list(self) -> None:
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


class ModelPickerScreen(PickScreen):
    """模型列表只显示模型；管理入口使用独立快捷键，搜索框聚焦时也可用。"""

    BINDINGS = [
        Binding("ctrl+p", "providers", "Manage providers", priority=True),
        Binding("ctrl+r", "refresh_models", "Refresh models", priority=True),
        Binding("ctrl+n", "custom_model", "Custom model", priority=True),
    ]

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one(".pick-hint", Static).update(
            "Type to search · Down to select · Enter to confirm · Esc to cancel\n"
            "Ctrl+P providers · Ctrl+R refresh · Ctrl+N custom model"
        )

    def action_providers(self) -> None:
        self._finish(("__providers__", ""))

    def action_refresh_models(self) -> None:
        self._finish(("__refresh__", ""))

    def action_custom_model(self) -> None:
        self._finish(("__custom__", ""))


class NewSessionButton(Static):
    """＋ 新会话：Button 默认 hover 样式（tall 边框 + 整块背景）会盖过虚线框，改 Static 自绘。"""

    def on_click(self) -> None:
        self.app._new_session()


class SidebarToggle(Static):
    """«/» 折叠切换按钮：Button 的标签渲染会溢出窄按钮，Static 按内容框精确渲染。"""

    def on_click(self) -> None:
        self.app.action_toggle_sidebar()
        self.app.query_one("#input", Input).focus()


class CommandRow(Static):
    """命令面板的一行：命令用法 + 说明；点击即执行。"""

    def __init__(self, command: str, usage: str, description: str) -> None:
        self.command = command
        usage_width = max(len(item[1]) for item in SLASH_COMMANDS)
        # 用法里的 [refresh] 等方括号会被当成 Rich 标记吞掉，须转义后再补空格
        super().__init__(
            f"[cmd-usage]{escape(f'{usage:<{usage_width}}')}[/] [cmd-desc]{escape(description)}[/]"
        )
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


class SessionPickerScreen(ModalScreen):
    """居中会话选择器：搜索过滤、Enter 恢复、Ctrl+R 重命名、Del 删除（带确认）、ESC 取消。"""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("down", "focus_list", "Select"),
        ("ctrl+r", "rename_session", "Rename"),
    ]

    def __init__(self, rows: list[dict], current_id: str | None, on_result) -> None:
        super().__init__()
        self._rows = rows
        self._current_id = current_id
        self._on_result = on_result
        self._filtered = rows

    def compose(self) -> ComposeResult:
        with Vertical(id="pick-card"):
            yield Input(placeholder="Search session title or ID…", id="picker-input")
            yield ListView(id="picker-list", classes="thin-scroll")
            yield Static("↓ list · Enter resume · Ctrl+R rename · Del delete · ESC", classes="pick-hint")

    def on_mount(self) -> None:
        self._rebuild()
        self.query_one("#picker-input", Input).focus()

    def set_rows(self, rows: list[dict]) -> None:
        self._rows = rows
        # 外部刷新（如删除会话）也要重算过滤结果，否则已删条目仍显示并可被选中。
        needle = self.query_one("#picker-input", Input).value.strip().lower()
        self._filtered = [
            row
            for row in rows
            if needle in row["title"].lower() or needle in row["id"].lower()
        ] if needle else rows
        self._rebuild()

    def on_input_changed(self, event: Input.Changed) -> None:
        needle = event.value.strip().lower()
        if needle:
            self._filtered = [
                row
                for row in self._rows
                if needle in row["title"].lower() or needle in row["id"].lower()
            ]
        else:
            self._filtered = self._rows
        self._rebuild()

    def _rebuild(self) -> None:
        listing = self.query_one("#picker-list", ListView)
        listing.clear()
        for row in self._filtered:
            time_text = (row.get("updated_at") or "")[11:16]
            meta = f"{time_text} · {row['messages']} msgs".lstrip("· ")
            if row["id"] == self._current_id:
                meta += " · ← current"
            item = SessionItem({**row, "meta": meta})
            if row["id"] == self._current_id:
                item.add_class("current")
            listing.append(item)
        # 清空重建后 index 为 None（无高亮），Enter 会落空——显式高亮第一项。
        if self._filtered:
            listing.index = 0

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # 焦点在搜索框时 Enter：确认当前高亮的会话。
        event.stop()
        item = self.query_one("#picker-list", ListView).highlighted_child
        if isinstance(item, SessionItem):
            self._finish(item.row["id"])

    def action_focus_list(self) -> None:
        self.query_one("#picker-list", ListView).focus()

    def action_rename_session(self) -> None:
        item = self.query_one("#picker-list", ListView).highlighted_child
        if isinstance(item, SessionItem):
            self.app.rename_session(item.row)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        event.prevent_default()
        row = getattr(event.item, "row", None)
        if row is not None:
            self._finish(row["id"])

    def on_key(self, event) -> None:
        if event.key != "delete":
            return
        view = self.query_one("#picker-list", ListView)
        item = view.highlighted_child
        if isinstance(item, SessionItem):
            event.stop()
            event.prevent_default()
            self.app.confirm_delete_session(item.row)

    def action_cancel(self) -> None:
        self._finish(None)

    def _finish(self, value) -> None:
        on_result = self._on_result
        self.dismiss()
        self.app.call_later(on_result, value)


class TextInputScreen(ModalScreen):
    """单行输入模态：ESC 取消（None），Enter 提交。"""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, default: str = "", on_result=None, password: bool = False) -> None:
        super().__init__()
        self._title = title
        self._default = default
        self._on_result = on_result
        self._password = password

    def compose(self) -> ComposeResult:
        with Vertical(id="pick-card"):
            yield Static(self._title, classes="pick-title")
            yield Input(value=self._default, id="pick-input", password=self._password)
            yield Static("Enter confirm · ESC cancel", classes="pick-hint")

    def on_mount(self) -> None:
        self.query_one("#pick-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # 模态输入必须在此终止；否则同一个 Enter 还会冒泡到主输入框被当成聊天消息。
        event.stop()
        self._finish(event.value.strip())

    def action_cancel(self) -> None:
        self._finish(None)

    def _finish(self, value) -> None:
        on_result = self._on_result
        self.dismiss()
        self.app.call_later(on_result, value or None)
