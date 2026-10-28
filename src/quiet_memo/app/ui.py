from __future__ import annotations

import typer
from rich import box
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.theme import Theme

from ..infra.layout import widths

# 主题色：石板蓝（与个人主页 slate-blue 同族）；警告用暖琥珀，区别于灰色与错误红。
ACCENT = "#8FAECB"
ACCENT_DIM = "#46586B"
MUTED = "#808080"
SUCCESS = "#d0d0d0"

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


def padded(renderable, cols: int | None = None):
    # 不向右铺满终端（expand=False 并少补 2 列），避免整行恰好占满带来的折行问题。
    left, _, right = layout_widths(cols)
    return Padding(renderable, (0, max(0, right - 2), 0, left), expand=False)


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


def fail(message: str, center: bool = False) -> None:
    console.print(padded(f"[red]{message}[/red]") if center else f"[red]{message}[/red]")
    raise typer.Exit(code=1)
