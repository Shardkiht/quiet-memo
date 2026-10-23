from __future__ import annotations

import questionary
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from ..agent.session import SessionStore
from ..infra.prompts import ask, pointer_control
from .ui import MENU_STYLE, SUCCESS, console, styled_table

DELETE = "__delete__"


def visible_sessions(rows: list[dict]) -> list[dict]:
    return [row for row in rows if int(row.get("messages", 0) or 0) > 0]


def session_choices(rows: list[dict], current_id: str | None = None):
    choices = []
    last_date = None
    for row in rows:
        stamp = row.get("updated_at") or row.get("started_at") or ""
        date = stamp[:10]
        if date != last_date:
            choices.append(questionary.Separator(date or "未知日期"))
            last_date = date
        time_text = stamp[11:16] if len(stamp) >= 16 else ""
        label = f"{time_text}  {row['title']}"
        label += f"  · {row['messages']} 条"
        if row.get("summaries"):
            label += f" · 摘要 {row['summaries']}"
        if row.get("id") == current_id:
            label += "  ← 当前"
        choices.append(questionary.Choice(label, value=row["id"]))
    return choices


def session_manager(sessions: SessionStore, current_id: str | None = None) -> str | None:
    query = ""
    while True:
        rows = visible_sessions(sessions.list(100))
        if query:
            needle = query.lower()
            rows = [
                row
                for row in rows
                if needle in row["title"].lower() or needle in row["id"].lower()
            ]
        if not rows:
            console.print("[dim]没有匹配的会话。[/dim]")
        choices = [
            questionary.Choice(
                f"搜索…（{query}）" if query else "搜索…",
                value="__search__",
            )
        ]
        choices.extend(session_choices(rows, current_id))
        choices.append(questionary.Choice("返回", value="__back__"))

        selected = _session_select(
            questionary.select(
                "会话管理",
                choices=choices,
                style=MENU_STYLE,
                qmark="›",
                pointer="❯",
                instruction="↑↓ 选择 · Enter 恢复 · Delete 删除",
            )
        )
        if selected is None or selected == "__back__":
            return None
        if selected == "__search__":
            value = ask(
                questionary.text("搜索标题或 ID（留空显示全部）", default=query)
            )
            if value is not None:
                query = value.strip()
            continue
        if isinstance(selected, tuple):
            target = selected[1]
            if target in ("__search__", "__back__"):
                continue
            if target == current_id:
                console.print("[dim]当前会话不能删除，先 /new 再处理。[/dim]")
                continue
            title = sessions.load(target).title()
            confirmed = ask(
                questionary.confirm(
                    f"删除会话「{title}」？不可恢复",
                    default=False,
                )
            )
            if confirmed:
                sessions.delete(target)
                console.print(f"[{SUCCESS}]已删除会话 {target}[/]")
            continue

        target = selected
        if target == current_id:
            console.print("[dim]已经是当前会话。[/dim]")
            continue
        return target


def _session_select(question):
    control = pointer_control(question)
    bindings = KeyBindings()

    @bindings.add(Keys.Delete, eager=True)
    def _delete(event):
        event.app.exit(result=(DELETE, control.get_pointed_at().value))

    return ask(question, extra=bindings)


def pick_session(
    sessions: SessionStore,
    query: str = "",
    current_id: str | None = None,
    *,
    include_empty: bool = False,
    exclude_current: bool = False,
) -> str | None:
    rows = sessions.list(100) if include_empty else visible_sessions(sessions.list(100))
    if exclude_current and current_id:
        rows = [row for row in rows if row["id"] != current_id]
    if query:
        needle = query.lower()
        rows = [
            row
            for row in rows
            if needle in row["title"].lower() or needle in row["id"].lower()
        ]
    if not rows:
        console.print("[dim]没有可选择的会话。[/dim]")
        return None
    choices = session_choices(rows, current_id)
    return ask(
        questionary.select(
            "选择会话（↑↓ 选择，Enter 确认）",
            choices=choices,
            style=MENU_STYLE,
            qmark="›",
            pointer="❯",
            instruction="↑↓ 选择 · Enter 确认",
        )
    )


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
