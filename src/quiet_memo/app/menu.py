from __future__ import annotations

import questionary
from rich import box
from rich.panel import Panel

from ..agent.session import SessionStore
from ..memory.service import get_service
from .session_ui import print_sessions
from .tui import run_chat
from .ui import ACCENT, MENU_STYLE, MUTED, SUCCESS, WARNING, console, print_hits


def main_menu() -> None:
    while True:
        service = get_service(None, None)
        if not (service.config.vault / ".git").exists():
            service.init_vault()
        has_key = bool(service.config.llm.api_key())
        model = f"{service.config.llm.provider}/{service.config.llm.model}"
        choices = [
            questionary.Choice("开始对话", value="chat"),
            questionary.Choice("配置模型 / API Key", value="setup"),
            questionary.Choice("查看会话", value="sessions"),
            questionary.Choice("检索记忆", value="recall"),
            questionary.Choice("退出", value="exit"),
        ]
        if not has_key:
            choices.insert(0, choices.pop(1))
        key_status = "已配置 API Key" if has_key else "未配置 API Key"
        key_color = SUCCESS if has_key else WARNING
        console.print(
            Panel.fit(
                f"[bold {ACCENT}]quiet-memo[/]  [{MUTED}]personal memory agent[/]\n"
                f"[{MUTED}]{model} · {service.config.vault}[/]\n"
                f"[{key_color}]{key_status}[/]",
                border_style=ACCENT,
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )
        choice = questionary.select(
            "选择操作",
            choices=choices,
            style=MENU_STYLE,
            qmark="›",
            pointer="❯",
            instruction="↑↓ 选择 · Enter 确认",
        ).ask()
        if choice in (None, "exit"):
            return
        if choice == "setup":
            from ..infra.providers import run_setup

            try:
                run_setup()
            except KeyboardInterrupt:
                console.print("[#b8b8b8]已取消配置。[/]")
            continue
        if choice == "sessions":
            print_sessions(SessionStore(service.config.vault).list(20))
            continue
        if choice == "recall":
            query = questionary.text("检索关键词").ask()
            if query:
                print_hits(query, service.recall(query, 10))
            continue
        if choice == "chat":
            if not has_key:
                console.print("[#b8b8b8]先配置 API Key，再开始对话。[/]")
                continue
            run_chat(service)
