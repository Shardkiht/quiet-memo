from __future__ import annotations

import sys
import time
from pathlib import Path

import questionary
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from ..agent.runtime import AgentRuntime
from ..agent.session import SessionStore
from ..infra.config import (
    Config,
    default_config_path,
    default_vault_path,
    load_config,
    write_example_config,
)
from ..infra.gitops import GitError
from ..llm import ChatModelError, LiteLLMChatModel
from ..memory.extractor import ExtractorError
from ..memory.service import MemoryService, get_service

app = typer.Typer(
    help="quiet-memo — 安静的个人记忆库：默认无感写入本地 Markdown，Git 可回滚，MCP 可调用。",
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
)
console = Console()

CONFIG_OPTION = typer.Option(None, "--config", "-c", help="配置文件路径")
VAULT_OPTION = typer.Option(None, "--vault", help="覆盖配置中的记忆库路径")


@app.callback()
def _root(ctx: typer.Context) -> None:
    """无子命令时进入主操作界面。"""
    if ctx.invoked_subcommand is not None:
        return
    if not sys.stdin.isatty():
        console.print(ctx.get_help())
        return
    try:
        _main_menu()
    except Exception as exc:
        console.print(f"[yellow]无法启动交互菜单：{exc}[/yellow]")
        console.print(ctx.get_help())


def _service(config: Path | None, vault: Path | None) -> MemoryService:
    return get_service(config, vault)


def _fail(message: str) -> None:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


@app.command()
def init(
    vault: Path = typer.Option(default_vault_path(), "--vault", help="记忆库目录"),
    config: Path | None = CONFIG_OPTION,
    force: bool = typer.Option(False, "--force", help="目标目录非空且不是记忆库时仍然继续"),
) -> None:
    """初始化记忆库与 Git 仓库。"""
    vault = vault.expanduser().resolve()
    if vault.exists() and any(vault.iterdir()) and not (vault / ".git").exists() and not force:
        _fail(f"{vault} 非空且不是 quiet-memo 记忆库；确认无误后加 --force。")
    config_path = config.expanduser() if config else default_config_path()
    cfg = load_config(config_path) if config_path.exists() else Config(vault=vault)
    cfg.vault = vault
    result = MemoryService(cfg).init_vault()
    if not config_path.exists():
        write_example_config(config_path, vault)
    console.print(f"记忆库已就绪：{result['vault']}")
    console.print(f"配置文件：{config_path}")
    if result["commit"]:
        console.print(f"初始提交：{result['commit']}")


@app.command()
def setup(
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """交互式选择模型供应商并安全保存 API Key。"""
    from ..infra.providers import run_setup

    try:
        result = run_setup(config, vault)
    except KeyboardInterrupt:
        _fail("已取消。")
        return
    console.print(f"配置已保存：{result['config_path']}")
    console.print(
        f"供应商：{result['base_url']} · 模型：{result['model']} · provider：{result['provider']}"
    )
    console.print(
        f"上下文：{result['context_window']} · 思考强度：{result['reasoning_effort'] or '默认'}"
    )
    if result["key_saved"]:
        console.print(
            f"[green]API Key 已保存到系统凭据[/green]（环境变量 {result['api_key_env']} 优先）"
        )
    elif result["keyring_error"]:
        console.print(f"[yellow]{result['keyring_error']}[/yellow]")
    if result["checked"]:
        console.print(f"[green]连接测试：{result['check_detail']}[/green]")
    elif result["base_url"]:
        console.print(f"[dim]{result['check_detail']}[/dim]")
    if result["initialized"]:
        console.print(f"[green]记忆库已初始化：{result['vault']}[/green]")


@app.command()
def chat(
    resume: str | None = typer.Option(None, "--resume", "-r", help="恢复指定会话 ID"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """启动交互式记忆 Agent。支持 /new /sessions /compact 等命令。"""
    _run_chat(_service(config, vault), resume)


def _main_menu() -> None:
    from ..infra.providers import run_setup

    while True:
        service = _service(None, None)
        if not (service.config.vault / ".git").exists():
            service.init_vault()
        has_key = bool(service.config.llm.api_key())
        model = f"{service.config.llm.provider}/{service.config.llm.model}"
        message = f"quiet-memo\n{model} · {service.config.vault}"
        if not has_key:
            message += "\n未配置 API Key"
        choices = [
            questionary.Choice("开始对话", value="chat"),
            questionary.Choice("配置模型 / API Key", value="setup"),
            questionary.Choice("查看会话", value="sessions"),
            questionary.Choice("检索记忆", value="recall"),
            questionary.Choice("退出", value="exit"),
        ]
        choice = questionary.select(
            message,
            choices=choices,
            default="chat" if has_key else "setup",
        ).ask()
        if choice in (None, "exit"):
            return
        if choice == "setup":
            try:
                run_setup()
            except KeyboardInterrupt:
                console.print("[yellow]已取消配置。[/yellow]")
            continue
        if choice == "sessions":
            _print_sessions(SessionStore(service.config.vault).list(20))
            continue
        if choice == "recall":
            query = questionary.text("检索关键词").ask()
            if query:
                _print_hits(query, service.recall(query, 10))
            continue
        if choice == "chat":
            if not has_key:
                console.print("[yellow]先配置 API Key，再开始对话。[/yellow]")
                continue
            _run_chat(service)


def _run_chat(service: MemoryService, resume: str | None = None) -> None:
    if not service.config.llm.api_key():
        _fail(
            f"未配置 LLM API Key，请设置环境变量 {service.config.llm.api_key_env}，"
            "或在聊天里输入 /setup 配置。"
        )
    if not (service.config.vault / ".git").exists():
        service.init_vault()
    sessions = SessionStore(service.config.vault)
    runtime = AgentRuntime(service, LiteLLMChatModel(service.config.llm), sessions)
    try:
        runtime.resume(resume) if resume else runtime.start()
    except KeyError as exc:
        _fail(str(exc))
        return
    _chat_loop(runtime, service, sessions)


def _chat_loop(runtime: AgentRuntime, service: MemoryService, sessions: SessionStore) -> None:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory

    history_path = service.config.vault / ".quiet-memo" / "chat_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prompt = PromptSession(history=FileHistory(str(history_path)))
    except Exception as exc:
        console.print(
            f"[yellow]当前终端不支持交互输入：{exc}[/yellow]\n"
            "请在 Windows Terminal / PowerShell 中运行，Git Bash 可尝试 winpty。"
        )
        return
    console.print(
        Panel(
            f"会话 {runtime.current.id} · {runtime.current.title()}\n"
            f"模型 {runtime.model_label} · 上下文 {service.config.chat.context_window}\n"
            "/help 查看命令 · /setup 修改模型 · /exit 退出",
            title="quiet-memo",
            border_style="cyan",
        )
    )

    try:
        while True:
            status = runtime.status()
            prompt_text = f"你 [{status['session_id'][-4:]}] › "
            try:
                line = prompt.prompt(prompt_text)
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if _handle_command(runtime, service, sessions, line):
                    continue
                break
            started = time.monotonic()
            try:
                with console.status("[dim]思考中…[/dim]", spinner="dots"):
                    reply = runtime.ask(line)
            except KeyboardInterrupt:
                console.print("[yellow]已中断当前请求。[/yellow]")
                continue
            except ChatModelError as exc:
                console.print(f"[red]{exc}[/red]")
                continue
            elapsed = time.monotonic() - started
            for event in reply.events:
                console.print(f"[dim]· {event}[/dim]")
            console.print(Markdown(reply.content or "(空回复)"))
            meta = [f"{reply.steps} 步", f"{reply.estimated_tokens} tok"]
            percent = reply.estimated_tokens / max(service.config.chat.context_window, 1)
            if percent >= 0.5:
                meta.append(f"上下文 {percent:.0%}")
            if reply.usage.cost_usd:
                meta.append(f"${reply.usage.cost_usd:.4f}")
            meta.append(f"{elapsed:.1f}s")
            console.print(f"[dim]{' · '.join(meta)}[/dim]")
    finally:
        stats = runtime.close()
        console.print(f"[dim]会话已保存：{runtime.current.id}[/dim]")
        if stats.get("error"):
            console.print(f"[dim]退出抽取失败：{stats['error']}[/dim]")
        elif any(stats.get(key) for key in ("admitted", "queued", "blocked")):
            console.print(
                f"[dim]退出时沉淀记忆：写入 {stats['admitted']} 条，"
                f"待确认 {stats['queued']} 条，拦截 {stats['blocked']} 条[/dim]"
            )


def _print_hits(query: str, hits) -> None:
    if not hits:
        console.print("[dim]未找到相关记忆。[/dim]")
        return
    table = Table(title=f"检索：{query}")
    table.add_column("ID")
    table.add_column("主题")
    table.add_column("分数")
    table.add_column("内容", overflow="fold")
    for hit in hits:
        table.add_row(hit.id, hit.topic, f"{hit.score:.2f}", hit.text)
    console.print(table)


def _handle_command(
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
            Panel(
                "\n".join(
                    [
                        "/new              新建会话",
                        "/sessions         列出历史会话",
                        "/resume <id>      恢复会话",
                        "/compact          立即压缩上下文",
                        "/summary          查看当前会话摘要",
                        "/status           会话与记忆状态",
                        "/setup            配置模型 / API Key",
                        "/rename <title>   重命名当前会话",
                        "/recall <query>   直接检索记忆",
                        "/remember <text>  直接写入记忆",
                        "/pending          查看待确认候选",
                        "/exit             退出",
                    ]
                ),
                title="命令",
            )
        )
        return True
    if command in ("/setup", "/model"):
        from ..infra.providers import run_setup

        try:
            run_setup()
        except KeyboardInterrupt:
            console.print("[yellow]已取消配置。[/yellow]")
            return True
        runtime.reload_llm()
        console.print(f"[green]模型已切换：{runtime.model_label}[/green]")
        return True
    if command == "/new":
        stats = runtime.close()
        runtime.start()
        console.print(f"[green]新会话：{runtime.current.id}[/green]")
        if stats.get("admitted"):
            console.print(f"[dim]上一会话退出时写入 {stats['admitted']} 条记忆[/dim]")
        return True
    if command == "/sessions":
        rows = sessions.list(20)
        _print_sessions(rows)
        return True
    if command == "/resume":
        if not argument:
            console.print("[yellow]用法：/resume <session-id>[/yellow]")
            return True
        runtime.close()
        try:
            runtime.resume(argument)
        except KeyError as exc:
            console.print(f"[red]{exc}[/red]")
            return True
        console.print(f"[green]已恢复 {runtime.current.id} · {runtime.current.title()}[/green]")
        return True
    if command == "/compact":
        for event in runtime.force_compact():
            console.print(f"[dim]· {event}[/dim]")
        return True
    if command == "/summary":
        summary = runtime.summary_text()
        console.print(Markdown(summary) if summary else "[dim]当前会话还没有摘要。[/dim]")
        return True
    if command == "/status":
        info = runtime.status()
        percent = info["estimated_tokens"] / max(info["context_window"], 1)
        console.print(
            Panel(
                f"会话：{info['session_id']} · {info['title']}\n"
                f"模型：{info['model']}\n"
                f"上下文：{info['estimated_tokens']}/{info['context_window']}（{percent:.0%}）\n"
                f"摘要：{info['summaries']} 段 / {info['summary_chars']} 字 · "
                f"已覆盖 {info['covered_records']} 条记录\n"
                f"待确认：{info['pending']} 条",
                title="状态",
                border_style="cyan",
            )
        )
        return True
    if command == "/rename":
        if not argument:
            console.print("[yellow]用法：/rename <title>[/yellow]")
            return True
        runtime.rename(argument)
        console.print(f"[green]已重命名为：{argument}[/green]")
        return True
    if command == "/recall":
        if not argument:
            console.print("[yellow]用法：/recall <query>[/yellow]")
            return True
        _print_hits(argument, service.recall(argument, 10))
        return True
    if command == "/remember":
        if not argument:
            console.print("[yellow]用法：/remember <text>[/yellow]")
            return True
        result = service.memorize(argument, source=f"session:{runtime.current.id}:manual")
        console.print(
            f"写入 {len(result.admitted)} 条，待确认 {len(result.queued)} 条，"
            f"重复 {len(result.duplicates)} 条，拦截 {len(result.blocked)} 条"
        )
        return True
    if command == "/pending":
        items = service.pending()
        if not items:
            console.print("[dim]没有待确认项。[/dim]")
            return True
        table = Table(title="待确认")
        table.add_column("ID")
        table.add_column("主题")
        table.add_column("原因")
        table.add_column("内容", overflow="fold")
        for item in items:
            table.add_row(item.id, item.topic, item.reason, item.text)
        console.print(table)
        return True
    console.print(f"[yellow]未知命令：{command}，输入 /help 查看帮助。[/yellow]")
    return True


def _print_sessions(rows: list[dict]) -> None:
    if not rows:
        console.print("[dim]暂无会话。[/dim]")
        return
    table = Table(title="会话")
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


@app.command("sessions")
def list_sessions(
    limit: int = typer.Option(20, "--limit", "-n"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """列出历史会话。"""
    service = _service(config, vault)
    _print_sessions(SessionStore(service.config.vault).list(limit))


@app.command()
def memorize(
    text: str = typer.Argument(..., help="一段对话或陈述"),
    source: str = typer.Option("chat", "--source", help="来源标记"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """从对话中抽取本人事实，静默写入；不确定项进入待确认队列。"""
    service = _service(config, vault)
    try:
        result = service.memorize(text, source)
    except ExtractorError as exc:
        _fail(str(exc))
        return
    for entry in result.admitted:
        console.print(f"[green]已写入[/green] [{entry.topic}] {entry.text}")
    for item in result.queued:
        console.print(f"[yellow]待确认[/yellow] {item.id} [{item.topic}] {item.text}  ({item.reason})")
    for text_ in result.duplicates:
        console.print(f"[dim]已存在，跳过：{text_}[/dim]")
    if not result.admitted and not result.queued:
        console.print("[dim]没有可记录的新信息。[/dim]")


@app.command()
def note(
    text: str = typer.Argument(..., help="要记录的事实"),
    topic: str = typer.Option("uncategorized", "--topic", "-t", help="主题 slug"),
    title: str | None = typer.Option(None, "--title", help="主题标题"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """直接记录一条事实，不调用 LLM。"""
    service = _service(config, vault)
    try:
        entry = service.note(text, topic=topic, title=title)
    except ValueError as exc:
        _fail(str(exc))
        return
    console.print(f"[green]已记录[/green] [{entry.topic}] {entry.text}")


@app.command()
def recall(
    query: str = typer.Argument(..., help="检索关键词"),
    limit: int = typer.Option(10, "--limit", "-n", help="最多返回条数"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """检索记忆。"""
    hits = _service(config, vault).recall(query, limit)
    if not hits:
        console.print("[dim]未找到相关记忆。[/dim]")
        return
    table = Table(title=f"检索：{query}")
    table.add_column("ID")
    table.add_column("主题")
    table.add_column("分数")
    table.add_column("内容", overflow="fold")
    for hit in hits:
        table.add_row(hit.id, hit.topic, f"{hit.score:.2f}", hit.text)
    console.print(table)


@app.command()
def pending(
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """列出待确认候选。"""
    items = _service(config, vault).pending()
    if not items:
        console.print("[dim]没有待确认项。[/dim]")
        return
    table = Table(title="待确认")
    table.add_column("ID")
    table.add_column("主题")
    table.add_column("置信度")
    table.add_column("原因")
    table.add_column("内容", overflow="fold")
    for item in items:
        table.add_row(item.id, item.topic, f"{item.confidence:.2f}", item.reason, item.text)
    console.print(table)


@app.command()
def approve(
    item_id: str = typer.Argument(..., help="待确认项 ID"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """确认一条候选并写入记忆库。"""
    try:
        entry = _service(config, vault).approve(item_id)
    except KeyError as exc:
        _fail(str(exc))
        return
    console.print(f"[green]已写入[/green] [{entry.topic}] {entry.text}")


@app.command()
def reject(
    item_id: str = typer.Argument(..., help="待确认项 ID"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """拒绝一条候选，保留处理记录。"""
    try:
        item = _service(config, vault).reject(item_id)
    except KeyError as exc:
        _fail(str(exc))
        return
    console.print(f"[dim]已拒绝[/dim] {item.id} {item.text}")


@app.command("topics")
def list_topics(
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """列出记忆主题。"""
    topics = _service(config, vault).topics()
    if not topics:
        console.print("[dim]记忆库为空。[/dim]")
        return
    table = Table(title="记忆主题")
    table.add_column("主题")
    table.add_column("标题")
    table.add_column("条目")
    table.add_column("更新")
    for topic in topics:
        table.add_row(topic["topic"], topic["title"], str(topic["entries"]), topic["updated"])
    console.print(table)


@app.command()
def history(
    limit: int = typer.Option(10, "--limit", "-n"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """查看提交历史。"""
    commits = _service(config, vault).history(limit)
    if not commits:
        console.print("[dim]暂无提交。[/dim]")
        return
    table = Table(title="提交历史")
    table.add_column("提交")
    table.add_column("时间")
    table.add_column("摘要", overflow="fold")
    for commit in commits:
        table.add_row(commit["hash"], commit["date"], commit["subject"])
    console.print(table)


@app.command()
def commit(
    push: bool | None = typer.Option(None, "--push/--no-push", help="是否推送到 remote；默认读取 auto_push"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """生成总结并提交变更，供本人审阅。"""
    service = _service(config, vault)
    try:
        result = service.commit(push=push)
    except GitError as exc:
        _fail(str(exc))
        return
    if not result["committed"]:
        console.print(f"[dim]{result['summary']}[/dim]")
        return
    console.print("[bold]本次提交总结[/bold]")
    console.print(result["summary"])
    console.print(f"提交：{result['hash']}")
    if result["pushed"]:
        console.print("[green]已推送[/green]")


@app.command()
def status(
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """查看记忆库状态。"""
    info = _service(config, vault).status()
    console.print(f"记忆库：{info['vault']}")
    console.print(f"主题 {info['topics']} · 条目 {info['entries']} · 待确认 {info['pending']} · 未提交 {info['dirty']}")


@app.command()
def serve(
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """以 stdio 启动 MCP server。"""
    service = _service(config, vault)
    from .server import build_server

    build_server(service=service).run()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
