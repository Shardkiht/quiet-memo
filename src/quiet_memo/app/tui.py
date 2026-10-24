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
from .model_ui import switch_context, switch_model, switch_reasoning
from .session_ui import pick_session, session_manager
from .ui import (
    ACCENT,
    ACCENT_DIM,
    MUTED,
    PROMPT_STYLE,
    SLASH_COMMANDS,
    WARNING,
    console,
    erase_input_rows,
    fail,
    indent,
    input_echo_rows,
    padded,
    print_event,
    print_hits,
    print_user_turn,
    styled_table,
    turn_divider,
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
            padded(
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
        console.print(padded(f"[{MUTED}]更早的 {len(records) - limit} 条记录已省略[/]"))
        records = records[-limit:]
    for record in records:
        content = str(record.get("content"))
        at = str(record.get("at") or "")
        stamp = at[11:16] if len(at) >= 16 else None
        if record.get("type") == "message" and record.get("role") == "user":
            print_user_turn(content, stamp)
        else:
            if stamp:
                console.print(padded(f"[bold {ACCENT}]quiet-memo[/] [dim]· {stamp}[/]"))
            console.print(padded(Markdown(content)))
            console.print()


def chat_loop(runtime: AgentRuntime, service: MemoryService, sessions: SessionStore) -> None:
    from prompt_toolkit import HTML, PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.shortcuts import CompleteStyle

    from .ui import slash_completer

    history_path = service.config.vault / ".quiet-memo" / "chat_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    # 待确认数量只在回合边界刷新，避免状态栏每次重绘都读 pending.jsonl。
    live = {"pending": 0}

    def refresh_pending() -> None:
        live["pending"] = len(service.pending())

    def status_bar():
        # 底部状态栏三段：模型+检索模式 ｜ 上下文进度条 ｜ 待确认；超压缩线加琥珀警示。
        total = service.config.chat.context_window
        plan = runtime.last_plan
        used = plan.estimated_tokens if plan is not None else 0
        percent = used / max(total, 1)
        blocks = min(10, max(0, round(percent * 10)))
        bar_on = "▊" * blocks
        bar_off = "▏" * (10 - blocks)
        html = (
            f"{indent()}<sb_model>◆ {runtime.model_label}</sb_model>"
            f"<sb_dim> · </sb_dim><sb_mode>{service.retrieval_mode}</sb_mode>"
            f"   <sb_label>上下文 </sb_label>"
            f"<sb_on>{bar_on}</sb_on><sb_off>{bar_off}</sb_off>"
            f"<sb_label> {percent:.0%}</sb_label>"
        )
        if live["pending"]:
            html += f"   <sb_inbox>待确认 {live['pending']}</sb_inbox>"
        if percent >= runtime.config.compact_threshold:
            html += " <sb_warn>⚠ 下轮压缩</sb_warn>"
        return HTML(html)

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
    refresh_pending()
    if runtime.has_session:
        session_label = f"[{MUTED}]{runtime.current.id[:2]}…[/]"
        session_title = runtime.current.title()
    else:
        session_label = f"[{MUTED}]新会话[/]"
        session_title = "发送第一条消息后创建"
    # 入口标头：扁平左对齐，与对话列同宽，替代旧的圆角面板盒；分隔线隔开会话区。
    console.print(
        padded(
            f"[bold {ACCENT}]quiet-memo[/]  [{MUTED}]安静的个人记忆库[/]\n"
            f"[bold]{session_title}[/]  {session_label}\n"
            f"[{ACCENT}]◆[/] {runtime.model_label} · {service.retrieval_mode}"
            f"  [{MUTED}]·[/]  [{MUTED}]上下文[/] {format_tokens(service.config.chat.context_window)}\n"
            f"[{MUTED}]/help 命令 · /models 模型 · /sessions 会话 · /exit 退出[/]"
        )
    )
    turn_divider()

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
                    refresh_pending()
                    continue
                break
            started = time.monotonic()
            stamp = time.strftime("%H:%M")
            # 拉伸窗口后 rich 探测的宽度可能与 PT 实际渲染宽度不一致，
            # 擦除行数以 PT 的渲染宽度为准，避免擦多吞掉上一轮或擦少留残行。
            echo_cols = prompt.output.get_size().columns
            erase_input_rows(input_echo_rows(len(indent()) + 2, line, echo_cols))
            print_user_turn(line, stamp)
            try:
                with console.status(f"{indent()}思考中…", spinner="dots"):
                    # 工具与检索事件实时打印，不再等整轮回复结束。
                    reply = runtime.ask(line, on_event=print_event)
            except KeyboardInterrupt:
                console.print(padded(f"[{WARNING}]已中断当前请求。[/]"))
                continue
            except ChatModelError as exc:
                console.print(padded(f"[red]{exc}[/red]"))
                continue
            refresh_pending()
            elapsed = time.monotonic() - started
            console.print(padded(f"[bold {ACCENT}]quiet-memo[/] [dim]· {stamp}[/]"))
            console.print(padded(Markdown(reply.content or "(空回复)")))
            meta = [f"{reply.steps} 步", f"{reply.estimated_tokens} tok"]
            percent = reply.estimated_tokens / max(service.config.chat.context_window, 1)
            if percent >= 0.5:
                meta.append(f"上下文 {percent:.0%}")
            if reply.usage.cost_usd:
                meta.append(f"${reply.usage.cost_usd:.4f}")
            meta.append(f"{elapsed:.1f}s")
            console.print(padded(f"[{MUTED}]{' · '.join(meta)}[/]"))
            # 回合分隔线：把每轮问答划成独立区块。
            turn_divider()
    finally:
        if runtime.has_session and runtime.config.extract_on_exit:
            console.print(padded(f"[{MUTED}]正在沉淀本轮记忆…[/]"))
        stats = runtime.close()
        if runtime.has_session:
            console.print(padded(f"[dim]会话已保存：{runtime.current.id}[/dim]"))
        else:
            console.print(padded("[dim]未发送消息，没有创建会话。[/dim]"))
        if stats.get("admitted") or stats.get("queued"):
            console.print(
                padded(
                    f"[dim]退出时沉淀：写入 {stats['admitted']} 条，待确认 {stats['queued']} 条[/dim]"
                )
            )
        if stats.get("error"):
            console.print(padded(f"[dim]退出抽取失败：{stats['error']}[/dim]"))
        elif any(stats.get(key) for key in ("admitted", "queued", "blocked")):
            console.print(
                padded(
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
            padded(
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
    if command == "/think":
        switch_reasoning(runtime, service)
        return True
    if command == "/context":
        switch_context(runtime, service)
        return True
    if command == "/setup":
        from ..infra.providers import run_setup

        try:
            run_setup()
        except KeyboardInterrupt:
            console.print(padded("[#b8b8b8]已取消配置。[/]"))
            return True
        runtime.reload_llm()
        console.print(padded(f"[bold]模型已切换：{runtime.model_label}[/]"))
        return True
    if command == "/new":
        stats = runtime.close()
        runtime.start()
        console.print(padded("[bold]新会话[/] [dim]发送第一条消息后创建[/]"))
        if stats.get("admitted"):
            console.print(padded(f"[dim]上一会话退出时写入 {stats['admitted']} 条记忆[/dim]"))
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
            console.print(padded(f"[red]{exc}[/red]"))
            return True
        console.print(padded(f"[bold]已恢复 {runtime.current.id} · {runtime.current.title()}[/]"))
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
            console.print(padded("[dim]已经是当前会话。[/dim]"))
            return True
        runtime.close()
        try:
            runtime.resume(target)
        except KeyError as exc:
            console.print(padded(f"[red]{exc}[/red]"))
            return True
        console.print(padded(f"[bold]已恢复 {runtime.current.id} · {runtime.current.title()}[/]"))
        print_transcript(runtime.current)
        return True
    if command == "/compact":
        for event in runtime.force_compact():
            console.print(padded(f"[dim]· {event}[/dim]"))
        return True
    if command == "/summary":
        if not runtime.has_session:
            console.print(padded("[dim]新会话还没有摘要。[/dim]"))
            return True
        summary = runtime.summary_text()
        console.print(
            padded(Markdown(summary))
            if summary
            else padded("[dim]当前会话还没有摘要。[/dim]")
        )
        return True
    if command == "/status":
        if not runtime.has_session:
            console.print(padded("[dim]新会话尚未创建，发送第一条消息后生成。[/dim]"))
            return True
        info = runtime.status()
        percent = info["estimated_tokens"] / max(info["context_window"], 1)
        console.print(
            padded(
                Panel(
                    f"会话：{info['session_id']} · {info['title']}\n"
                    f"模型：{info['model']}\n"
                    f"检索：{info['retrieval']}\n"
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
            console.print(padded("[#b8b8b8]用法：/rename <title>[/]"))
            return True
        if not runtime.has_session:
            console.print(padded("[dim]新会话尚未创建，发送第一条消息后再重命名。[/dim]"))
            return True
        runtime.rename(argument)
        console.print(padded(f"[bold]已重命名为：{argument}[/]"))
        return True
    if command == "/recall":
        if not argument:
            console.print(padded("[#b8b8b8]用法：/recall <query>[/]"))
            return True
        print_hits(argument, service.recall(argument, 10), center=True)
        return True
    if command == "/remember":
        if not argument:
            console.print(padded("[#b8b8b8]用法：/remember <text>[/]"))
            return True
        source = f"session:{runtime.current.id}:manual" if runtime.has_session else "manual"
        result = service.memorize(argument, source=source)
        console.print(
            padded(
                f"写入 {len(result.admitted)} 条，待确认 {len(result.queued)} 条，"
                f"重复 {len(result.duplicates)} 条，拦截 {len(result.blocked)} 条"
            )
        )
        return True
    if command == "/pending":
        items = service.pending()
        if not items:
            console.print(padded("[dim]没有待确认项。[/dim]"))
            return True
        table = styled_table("待确认")
        table.add_column("ID")
        table.add_column("主题")
        table.add_column("原因")
        table.add_column("内容", overflow="fold")
        for item in items:
            table.add_row(item.id, item.topic, item.reason, item.text)
        console.print(padded(table))
        return True
    console.print(padded(f"[#b8b8b8]未知命令：{command}，输入 /help 查看帮助。[/]"))
    return True
