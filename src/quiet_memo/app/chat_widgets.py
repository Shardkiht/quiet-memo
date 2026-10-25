"""TUI 小组件：命令面板、选择/输入模态与侧栏条目和按钮。

组件一律通过 self.app 鸭子类型回调 ChatApp，不反向 import，避免界面层内部循环依赖。
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListView, ListItem, Static

SLASH_COMMANDS = [
    ("new", "/new", "新建会话"),
    ("sessions", "/sessions", "跳到左侧会话列表"),
    ("resume", "/resume [id|query]", "恢复会话，无参数跳到会话列表"),
    ("compact", "/compact", "立即压缩上下文"),
    ("summary", "/summary", "查看当前会话摘要"),
    ("status", "/status", "会话与记忆状态"),
    ("models", "/models [refresh]", "切换模型，refresh 强制刷新列表"),
    ("model", "/model", "同 /models"),
    ("think", "/think", "切换思考强度（low/medium/high，按模型支持）"),
    ("context", "/context", "切换上下文长度（档位按模型上限生成）"),
    ("setup", "/setup", "完整配置模型 / API Key"),
    ("rename", "/rename <title>", "重命名当前会话"),
    ("recall", "/recall <query>", "直接检索记忆"),
    ("remember", "/remember <text>", "直接写入记忆"),
    ("pending", "/pending", "查看待确认候选"),
    ("help", "/help", "查看命令帮助"),
    ("exit", "/exit", "退出"),
    ("quit", "/quit", "同 /exit"),
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


class NewSessionButton(Static):
    """＋ 新会话：Button 默认 hover 样式（tall 边框 + 整块背景）会盖过虚线框，改 Static 自绘。"""

    def on_click(self) -> None:
        if not self.app.busy:
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
