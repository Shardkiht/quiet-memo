from __future__ import annotations

import time

from rich import box
from rich.markdown import Markdown
from rich.panel import Panel

from ..agent.runtime import AgentRuntime
from ..agent.session import SessionStore
from ..infra.tokens import format_tokens
from ..llm import ChatModelError, LiteLLMChatModel
from ..memory.service import MemoryService
from .model_ui import switch_model
from .session_ui import pick_session, session_manager
from .ui import (
    ACCENT,
    MUTED,
    PROMPT_STYLE,
    SLASH_COMMANDS,
    WARNING,
    centered,
    console,
    fail,
    indent,
    print_hits,
    styled_table,
)


def run_chat(service: MemoryService, resume: str | None = None) -> None:
    if not service.config.llm.api_key():
        fail(
            f"未配置 LLM API Key，请设置环境变量 {service.config.llm.api_key_env}，"
            "或在聊天里输入 /setup 配置。",
            center=True,
        )
    if not (service.config.vault / ".git").exists():
        service.init_vault()
    sessions = SessionStore(service.config.vault)
    runtime = AgentRuntime(service, LiteLLMChatModel(service.config.llm), sessions)
    if resume:
        try:
            runtime.resume(resume)
        except KeyError as exc:
            fail(str(exc), center=True)
            return
        print_transcript(runtime.current)
    else:
        runtime.start()
    chat_loop(runtime, service, sessions)


def print_transcript(session, limit: int = 40) -> None:
    summary = session.summary_text()
    if summary:
        console.print(
            centered(
                Panel(
                    Markdown(summary),
                    title="此前摘要",
                    border_style=MUTED,
                    box=box.ROUNDED,
                    padding=(0, 2),
                )
            )
        )
    records = [
        record
        for record in session.records_after_summary()
        if record.get("type") in ("message", "assistant")
        and str(record.get("content") or "").strip()
    ]
    if not records:
        return
    if len(records) > limit:
        console.print(centered(f"[{MUTED}]更早的 {len(records) - limit} 条记录已省略[/]"))
        records = records[-limit:]
    for record in records:
        content = str(record.get("content"))
        if record.get("type") == "message" and record.get("role") == "user":
            console.print(centered(f"[bold]❯[/] {content}"))
        else:
            console.print(centered(Markdown(content)))
            console.print()


def chat_loop(runtime: AgentRuntime, service: MemoryService, sessions: SessionStore) -> None:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.shortcuts import CompleteStyle

    from .ui import slash_completer

    history_path = service.config.vault / ".quiet-memo" / "chat_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    def status_bar() -> str:
        # 底部状态栏：模型 + 本轮实际占用的上下文，随每轮刷新。
        total = service.config.chat.context_window
        plan = runtime.last_plan
        used = plan.estimated_tokens if plan is not None else 0
        percent = used / max(total, 1)
        return (
            f"{indent()} {runtime.model_label} · "
            f"上下文 {format_tokens(used)}/{format_tokens(total)} · {percent:.0%}"
        )

    try:
        prompt = PromptSession(
            history=FileHistory(str(history_path)),
            completer=slash_completer(),
            complete_while_typing=True,
            complete_style=CompleteStyle.COLUMN,
            reserve_space_for_menu=10,
            style=PROMPT_STYLE,
            bottom_toolbar=status_bar,
        )
    except Exception as exc:
        console.print(
            f"[#b8b8b8]当前终端不支持交互输入：{exc}[/]\n"
            "请在 Windows Terminal / PowerShell 中运行，Git Bash 可尝试 winpty。"
        )
        return
    if runtime.has_session:
        session_label = f"[{MUTED}]{runtime.current.id[:2]}…[/]"
        session_title = runtime.current.title()
    else:
        session_label = f"[{MUTED}]新会话[/]"
        session_title = "发送第一条消息后创建"
    console.print(
        centered(
            Panel(
                f"[bold {ACCENT}]quiet-memo[/]  {session_label}\n"
                f"[bold]{session_title}[/]\n"
                f"[{MUTED}]模型[/] {runtime.model_label}  [{MUTED}]·[/]  "
                f"[{MUTED}]上下文[/] {format_tokens(service.config.chat.context_window)}\n"
                f"[{MUTED}]/help 命令 · /models 模型 · /exit 退出[/]",
                border_style=ACCENT,
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )
    )

    try:
        while True:
            prompt_text = [("class:prompt", f"{indent()}❯ ")]
            try:
                line = prompt.prompt(prompt_text)
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if handle_command(runtime, service, sessions, line):
                    continue
                break
            started = time.monotonic()
            try:
                with console.status(f"{indent()}思考中…", spinner="dots"):
                    reply = runtime.ask(line)
            except KeyboardInterrupt:
                console.print(centered(f"[{WARNING}]已中断当前请求。[/]"))
                continue
            except ChatModelError as exc:
                console.print(centered(f"[red]{exc}[/red]"))
                continue
            elapsed = time.monotonic() - started
            for event in reply.events:
                console.print(centered(f"[{MUTED}]  ↳ {event}[/]"))
            console.print(centered(Markdown(reply.content or "(空回复)")))
            meta = [f"{reply.steps} 步", f"{reply.estimated_tokens} tok"]
            percent = reply.estimated_tokens / max(service.config.chat.context_window, 1)
            if percent >= 0.5:
                meta.append(f"上下文 {percent:.0%}")
            if reply.usage.cost_usd:
                meta.append(f"${reply.usage.cost_usd:.4f}")
            meta.append(f"{elapsed:.1f}s")
            console.print(centered(f"[{MUTED}]  {' · '.join(meta)}[/]"))
    finally:
        stats = runtime.close()
        if runtime.has_session:
            console.print(centered(f"[dim]会话已保存：{runtime.current.id}[/dim]"))
        else:
            console.print(centered("[dim]未发送消息，没有创建会话。[/dim]"))
        if stats.get("error"):
            console.print(centered(f"[dim]退出抽取失败：{stats['error']}[/dim]"))
        elif any(stats.get(key) for key in ("admitted", "queued", "blocked")):
            console.print(
                centered(
                    f"[dim]退出时沉淀记忆：写入 {stats['admitted']} 条，"
                    f"待确认 {stats['queued']} 条，拦截 {stats['blocked']} 条[/dim]"
                )
            )


def handle_command(
    runtime: AgentRuntime,
    service: MemoryService,
    sessions: SessionStore,
    line: str,
) -> bool:
    """返回 False 表示退出聊天。"""
    command, _, argument = line.partition(" ")
    command = command.lower()
    argument = argument.strip()

    if command in ("/exit", "/quit"):
        return False
    if command == "/help":
        console.print(
            centered(
                Panel(
                    "\n".join(
                        f"{usage:<22} {description}" for _, usage, description in SLASH_COMMANDS
                    ),
                    title="命令",
                    border_style=ACCENT,
                    box=box.ROUNDED,
                )
            )
        )
        return True
    if command in ("/models", "/model"):
        switch_model(
            runtime,
            service,
            refresh=argument.lower() in ("refresh", "--refresh", "-r"),
        )
        return True
    if command == "/setup":
        from ..infra.providers import run_setup

        try:
            run_setup()
        except KeyboardInterrupt:
            console.print("[#b8b8b8]已取消配置。[/]")
            return True
        runtime.reload_llm()
        console.print(f"[bold]模型已切换：{runtime.model_label}[/]")
        return True
    if command == "/new":
        stats = runtime.close()
        runtime.start()
        console.print("[bold]新会话[/] [dim]发送第一条消息后创建[/]")
        if stats.get("admitted"):
            console.print(centered(f"[dim]上一会话退出时写入 {stats['admitted']} 条记忆[/dim]"))
        return True
    if command == "/sessions":
        current_id = runtime.current.id if runtime.has_session else None
        target = session_manager(sessions, current_id)
        if target is None or target == current_id:
            return True
        runtime.close()
        try:
            runtime.resume(target)
        except KeyError as exc:
            console.print(f"[red]{exc}[/red]")
            return True
        console.print(centered(f"[bold]已恢复 {runtime.current.id} · {runtime.current.title()}[/]"))
        print_transcript(runtime.current)
        return True
    if command == "/resume":
        current_id = runtime.current.id if runtime.has_session else None
        target = None
        if argument:
            try:
                if sessions.exists(argument):
                    target = argument
            except KeyError:
                pass
        if target is None:
            target = pick_session(sessions, argument, current_id)
        if target is None:
            return True
        if target == current_id:
            console.print("[dim]已经是当前会话。[/dim]")
            return True
        runtime.close()
        try:
            runtime.resume(target)
        except KeyError as exc:
            console.print(f"[red]{exc}[/red]")
            return True
        console.print(centered(f"[bold]已恢复 {runtime.current.id} · {runtime.current.title()}[/]"))
        print_transcript(runtime.current)
        return True
    if command == "/compact":
        for event in runtime.force_compact():
            console.print(centered(f"[dim]· {event}[/dim]"))
        return True
    if command == "/summary":
        if not runtime.has_session:
            console.print(centered("[dim]新会话还没有摘要。[/dim]"))
            return True
        summary = runtime.summary_text()
        console.print(
            centered(Markdown(summary))
            if summary
            else centered("[dim]当前会话还没有摘要。[/dim]")
        )
        return True
    if command == "/status":
        if not runtime.has_session:
            console.print(centered("[dim]新会话尚未创建，发送第一条消息后生成。[/dim]"))
            return True
        info = runtime.status()
        percent = info["estimated_tokens"] / max(info["context_window"], 1)
        console.print(
            centered(
                Panel(
                    f"会话：{info['session_id']} · {info['title']}\n"
                    f"模型：{info['model']}\n"
                    f"上下文：{format_tokens(info['estimated_tokens'])}/{format_tokens(info['context_window'])}（{percent:.0%}）\n"
                    f"摘要：{info['summaries']} 段 / {info['summary_chars']} 字 · "
                    f"已覆盖 {info['covered_records']} 条记录\n"
                    f"待确认：{info['pending']} 条",
                    title="状态",
                    border_style=ACCENT,
                    box=box.ROUNDED,
                )
            )
        )
        return True
    if command == "/rename":
        if not argument:
            console.print("[#b8b8b8]用法：/rename <title>[/]")
            return True
        if not runtime.has_session:
            console.print("[dim]新会话尚未创建，发送第一条消息后再重命名。[/dim]")
            return True
        runtime.rename(argument)
        console.print(centered(f"[bold]已重命名为：{argument}[/]"))
        return True
    if command == "/recall":
        if not argument:
            console.print(centered("[#b8b8b8]用法：/recall <query>[/]"))
            return True
        print_hits(argument, service.recall(argument, 10), center=True)
        return True
    if command == "/remember":
        if not argument:
            console.print(centered("[#b8b8b8]用法：/remember <text>[/]"))
            return True
        source = f"session:{runtime.current.id}:manual" if runtime.has_session else "manual"
        result = service.memorize(argument, source=source)
        console.print(
            centered(
                f"写入 {len(result.admitted)} 条，待确认 {len(result.queued)} 条，"
                f"重复 {len(result.duplicates)} 条，拦截 {len(result.blocked)} 条"
            )
        )
        return True
    if command == "/pending":
        items = service.pending()
        if not items:
            console.print(centered("[dim]没有待确认项。[/dim]"))
            return True
        table = styled_table("待确认")
        table.add_column("ID")
        table.add_column("主题")
        table.add_column("原因")
        table.add_column("内容", overflow="fold")
        for item in items:
            table.add_row(item.id, item.topic, item.reason, item.text)
        console.print(centered(table))
        return True
    console.print(centered(f"[#b8b8b8]未知命令：{command}，输入 /help 查看帮助。[/]"))
    return True
