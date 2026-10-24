from __future__ import annotations

import typer
from prompt_toolkit.styles import Style
from rich import box
from rich.cells import cell_len
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from ..infra.layout import widths

# 主题色：石板蓝（与个人主页 slate-blue 同族）；警告用暖琥珀，区别于灰色与错误红。
ACCENT = "#8FAECB"
ACCENT_DIM = "#46586B"
MUTED = "#808080"
SUCCESS = "#d0d0d0"
WARNING = "#D9A66C"
MEMO = "#9CBF9A"  # 记忆写入成功的确认绿
BUBBLE_BG = "#232c3a"  # 用户气泡底色
BUBBLE_FG = "#dbe6f2"

console = Console(
    theme=Theme(
        {
            "markdown.h1": f"bold {ACCENT}",
            "markdown.h1.border": ACCENT_DIM,
            "markdown.h2": "bold #d0d0d0",
            "markdown.h2.border": ACCENT_DIM,
            "markdown.code": "#C9D4E0",
        }
    )
)


def layout_widths(cols: int | None = None) -> tuple[int, int, int]:
    # 统一以 Rich 实际渲染宽度为准，避免终端检测不一致导致内容偏右。
    return widths(cols or console.width)


def indent(cols: int | None = None) -> str:
    return " " * layout_widths(cols)[0]


def padded(renderable, cols: int | None = None):
    left, _, right = layout_widths(cols)
    return Padding(renderable, (0, right, 0, left))


def print_right(markup: str, cols: int | None = None) -> None:
    """在对话列内右对齐打印一行（用户侧说话人标头）。"""
    left, width, _ = layout_widths(cols)
    text = Text.from_markup(markup)
    text.pad_left(max(0, width - text.cell_len) + left)
    console.print(text)


def bubble_lines(content: str, bubble_width: int) -> list[Text]:
    """用户消息按气泡宽度折行；首尾各留一列空、右侧补齐成等宽块，整块带底色。"""
    style = f"{BUBBLE_FG} on {BUBBLE_BG}"
    lines = Text(content, style=style).wrap(console, max(10, bubble_width - 2))
    longest = max((line.cell_len for line in lines), default=0)
    blocks = []
    for line in lines:
        block = Text(" ", style=f"on {BUBBLE_BG}")
        block += line
        block.append(" " * (longest - line.cell_len + 1), style=f"on {BUBBLE_BG}")
        blocks.append(block)
    return blocks


def print_user_turn(content: str, stamp: str | None = None, cols: int | None = None) -> None:
    """用户回合：右对齐说话人标头 + 深色气泡块，约占列宽 58%。"""
    left, width, _ = layout_widths(cols)
    if stamp:
        print_right(f"[dim]我 · {stamp}[/]", cols)
    for line in bubble_lines(content, max(20, round(width * 0.58))):
        line.pad_left(max(0, width - line.cell_len) + left)
        console.print(line)


def print_event(event: str) -> None:
    """过程事件卡片：记忆写入类用绿色对勾强调，其余以左竖线弱化展示。"""
    if event.startswith(("写入", "已更正", "已删除")):
        console.print(padded(f"[{MEMO}]✓ {event}[/]"))
    else:
        console.print(padded(f"[{ACCENT_DIM}]┃[/] [{MUTED}]{event}[/]"))


def turn_divider(cols: int | None = None) -> None:
    _, width, _ = layout_widths(cols)
    console.print(padded(f"[{ACCENT_DIM}]{'─' * width}[/]", cols))


def input_echo_rows(prompt_cells: int, line: str, cols: int | None = None) -> int:
    """估算 prompt 输入回显占用的终端行数（含折行），用于擦除后改绘为气泡。"""
    width = console.width if cols is None else cols
    cells = prompt_cells + cell_len(line)
    return max(1, -(-cells // width))


def erase_input_rows(rows: int) -> None:
    """上移并清除 prompt_toolkit 留下的回显行，用户消息交由气泡渲染。"""
    console.file.write("\x1b[2K\x1b[1A" * rows + "\x1b[2K")
    console.file.flush()

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
        # 底部状态栏的分段配色：模型亮、检索模式弱、进度条点亮、待确认与预警琥珀。
        "sb_model": "#d0d0d0",
        "sb_dim": MUTED,
        "sb_mode": MUTED,
        "sb_label": MUTED,
        "sb_on": f"bold {ACCENT}",
        "sb_off": "#4a4f5a",
        "sb_inbox": WARNING,
        "sb_warn": WARNING,
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
        console.print(padded("[dim]未找到相关记忆。[/dim]") if center else "[dim]未找到相关记忆。[/dim]")
        return
    table = styled_table(f"检索：{query}")
    table.add_column("ID")
    table.add_column("主题")
    table.add_column("分数")
    table.add_column("内容", overflow="fold")
    for hit in hits:
        table.add_row(hit.id, hit.topic, f"{hit.score:.2f}", hit.text)
    console.print(padded(table) if center else table)


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
    console.print(padded(f"[red]{message}[/red]") if center else f"[red]{message}[/red]")
    raise typer.Exit(code=1)
