from __future__ import annotations

import typer
from prompt_toolkit.styles import Style
from rich import box
from rich.console import Console
from rich.padding import Padding
from rich.table import Table

from ..infra.layout import widths

console = Console()

ACCENT = "#d0d0d0"
MUTED = "#808080"
SUCCESS = "#d0d0d0"
WARNING = "#b8b8b8"


def layout_widths(cols: int | None = None) -> tuple[int, int, int]:
    # 统一以 Rich 实际渲染宽度为准，避免终端检测不一致导致内容偏右。
    return widths(cols or console.width)


def indent(cols: int | None = None) -> str:
    return " " * layout_widths(cols)[0]


def centered(renderable, cols: int | None = None):
    left, _, right = layout_widths(cols)
    return Padding(renderable, (0, right, 0, left))

MENU_STYLE = Style.from_dict(
    {
        "qmark": f"bold {ACCENT}",
        "question": "bold #e0e0e0",
        "pointer": f"bold {ACCENT}",
        "highlighted": "bg:#3a3a3a #ffffff",
        "selected": SUCCESS,
        "separator": MUTED,
        "instruction": MUTED,
        "text": "#d8d8d8",
    }
)

PROMPT_STYLE = Style.from_dict(
    {
        "prompt": f"bold {ACCENT}",
        "bottom-toolbar": MUTED,
        "completion-menu.completion": "bg:#262626 #d8d8d8",
        "completion-menu.completion.current": "bg:#3a3a3a #ffffff",
        "completion-menu.meta.completion": "bg:#262626 #808080",
        "completion-menu.meta.completion.current": "bg:#3a3a3a #d0d0d0",
    }
)

SLASH_COMMANDS = [
    ("new", "/new", "新建会话"),
    ("sessions", "/sessions", "会话管理（选择、搜索、删除）"),
    ("resume", "/resume [id|query]", "恢复会话，无参数打开选择器"),
    ("compact", "/compact", "立即压缩上下文"),
    ("summary", "/summary", "查看当前会话摘要"),
    ("status", "/status", "会话与记忆状态"),
    ("models", "/models [refresh]", "切换模型，refresh 强制刷新列表"),
    ("setup", "/setup", "完整配置模型 / API Key"),
    ("rename", "/rename <title>", "重命名当前会话"),
    ("recall", "/recall <query>", "直接检索记忆"),
    ("remember", "/remember <text>", "直接写入记忆"),
    ("pending", "/pending", "查看待确认候选"),
    ("help", "/help", "查看命令帮助"),
    ("exit", "/exit", "退出"),
]


def styled_table(title: str) -> Table:
    return Table(
        title=title,
        box=box.ROUNDED,
        border_style=MUTED,
        title_style=f"bold {ACCENT}",
        header_style=f"bold {ACCENT}",
        expand=False,
    )


def print_hits(query: str, hits, center: bool = False) -> None:
    if not hits:
        console.print(centered("[dim]未找到相关记忆。[/dim]") if center else "[dim]未找到相关记忆。[/dim]")
        return
    table = styled_table(f"检索：{query}")
    table.add_column("ID")
    table.add_column("主题")
    table.add_column("分数")
    table.add_column("内容", overflow="fold")
    for hit in hits:
        table.add_row(hit.id, hit.topic, f"{hit.score:.2f}", hit.text)
    console.print(centered(table) if center else table)


def slash_completer():
    from prompt_toolkit.completion import Completer, Completion

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/") or " " in text:
                return
            prefix = text[1:].lower()
            for command, usage, description in SLASH_COMMANDS:
                if command.startswith(prefix):
                    yield Completion(
                        f"/{command}",
                        start_position=-len(text),
                        display=f"/{command}",
                        display_meta=description,
                    )

    return SlashCompleter()


def fail(message: str, center: bool = False) -> None:
    console.print(centered(f"[red]{message}[/red]") if center else f"[red]{message}[/red]")
    raise typer.Exit(code=1)
