from __future__ import annotations

from .ui import console, styled_table


def visible_sessions(rows: list[dict]) -> list[dict]:
    return [row for row in rows if int(row.get("messages", 0) or 0) > 0]


def print_sessions(rows: list[dict]) -> None:
    rows = visible_sessions(rows)
    if not rows:
        console.print("[dim]暂无会话。[/dim]")
        return
    table = styled_table("会话")
    table.add_column("ID")
    table.add_column("更新时间")
    table.add_column("消息")
    table.add_column("tokens")
    table.add_column("摘要")
    table.add_column("标题", overflow="fold")
    for row in rows:
        table.add_row(
            row["id"],
            row["updated_at"].replace("T", " "),
            str(row["messages"]),
            str(row["tokens"]),
            str(row["summaries"]),
            row["title"],
        )
    console.print(table)
