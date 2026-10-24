from __future__ import annotations

import sys
from pathlib import Path

import typer

from ..agent.session import SessionStore
from ..infra.config import (
    Config,
    default_config_path,
    default_vault_path,
    load_config,
    write_example_config,
)
from ..infra.gitops import GitError
from ..memory.extractor import ExtractorError, split_chunks
from ..memory.service import MemoryService, get_service
from .session_ui import print_sessions
from .tui import run_chat
from .ui import centered, console, fail, print_hits, styled_table

app = typer.Typer(
    help="quiet-memo — 安静的个人记忆库：默认无感写入本地 Markdown，Git 可回滚，MCP 可调用。",
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
)

CONFIG_OPTION = typer.Option(None, "--config", "-c", help="配置文件路径")
VAULT_OPTION = typer.Option(None, "--vault", help="覆盖配置中的记忆库路径")


@app.callback()
def _root(ctx: typer.Context) -> None:
    """无子命令时直接进入对话；非交互环境回退到帮助。"""
    if ctx.invoked_subcommand is not None:
        return
    if not sys.stdin.isatty():
        console.print(ctx.get_help())
        return
    service = _service(None, None)
    if not service.config.llm.api_key():
        console.print(centered("[#b8b8b8]未配置 API Key，先运行 qm setup。[/]"))
        return
    try:
        run_chat(service)
    except Exception as exc:
        console.print(centered(f"[#b8b8b8]无法启动对话界面：{exc}[/]"))
        console.print(ctx.get_help())


def _service(config: Path | None, vault: Path | None) -> MemoryService:
    return get_service(config, vault)


@app.command()
def init(
    vault: Path = typer.Option(default_vault_path(), "--vault", help="记忆库目录"),
    config: Path | None = CONFIG_OPTION,
    force: bool = typer.Option(False, "--force", help="目标目录非空且不是记忆库时仍然继续"),
) -> None:
    """初始化记忆库与 Git 仓库。"""
    vault = vault.expanduser().resolve()
    if vault.exists() and any(vault.iterdir()) and not (vault / ".git").exists() and not force:
        fail(f"{vault} 非空且不是 quiet-memo 记忆库；确认无误后加 --force。")
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
        fail("已取消。")
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
            f"[bold]API Key 已保存到系统凭据[/]（环境变量 {result['api_key_env']} 优先）"
        )
    elif result["keyring_error"]:
        console.print(f"[#b8b8b8]{result['keyring_error']}[/]")
    if result["checked"]:
        console.print(f"[bold]连接测试：{result['check_detail']}[/]")
    elif result["base_url"]:
        console.print(f"[dim]{result['check_detail']}[/dim]")
    if result["initialized"]:
        console.print(f"[bold]记忆库已初始化：{result['vault']}[/]")


@app.command()
def chat(
    resume: str | None = typer.Option(None, "--resume", "-r", help="恢复指定会话 ID"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """启动交互式记忆 Agent。支持 /new /sessions /compact 等命令。"""
    run_chat(_service(config, vault), resume)


@app.command("sessions")
def list_sessions(
    limit: int = typer.Option(20, "--limit", "-n"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """列出历史会话。"""
    service = _service(config, vault)
    print_sessions(SessionStore(service.config.vault).list(limit))


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
        fail(str(exc))
        return
    for entry in result.admitted:
        console.print(f"[bold]已写入[/] [{entry.topic}] {entry.text}")
    for item in result.queued:
        console.print(f"[#b8b8b8]待确认[/] {item.id} [{item.topic}] {item.text}  ({item.reason})")
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
        fail(str(exc))
        return
    console.print(f"[bold]已记录[/] [{entry.topic}] {entry.text}")


@app.command("import")
def import_memories(
    path: Path = typer.Argument(..., help="要导入的 Markdown 文件或目录", exists=True),
    apply: bool = typer.Option(False, "--apply", help="实际写入；默认只打印计划"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """导入已有 Markdown：AI 整理成条目，程序裁决写入；默认 dry-run。"""
    files = sorted(path.glob("*.md")) if path.is_dir() else [path]
    if not files:
        fail("没有找到 Markdown 文件。")
        return
    service = _service(config, vault)
    totals = {"admitted": 0, "queued": 0, "duplicates": 0, "blocked": 0}
    failed_chunks = 0
    total_chunks = 0
    for file in files:
        chunks = split_chunks(file.read_text(encoding="utf-8"))
        file_failed = 0
        for chunk in chunks:
            total_chunks += 1
            try:
                result = service.import_text(
                    chunk, source=f"import:{file.name}", dry_run=not apply
                )
            except ExtractorError as exc:
                file_failed += 1
                console.print(f"[#b8b8b8]块抽取失败：{exc}[/]")
                continue
            totals["admitted"] += len(result.admitted)
            totals["queued"] += len(result.queued)
            totals["duplicates"] += len(result.duplicates)
            totals["blocked"] += len(result.blocked)
        failed_chunks += file_failed
        note = f"，失败 {file_failed} 块" if file_failed else ""
        console.print(f"[dim]{file.name}：{len(chunks)} 块{note}[/dim]")
    if total_chunks and failed_chunks == total_chunks:
        fail("所有块抽取失败，未写入任何内容。")
        return
    mode = "已写入" if apply else "计划写入（dry-run，确认无误后加 --apply 落盘）"
    console.print(
        f"[bold]{mode}[/]：直写 {totals['admitted']} · 待确认 {totals['queued']} · "
        f"重复 {totals['duplicates']} · 拦截 {totals['blocked']}"
    )
    if apply and totals["queued"]:
        console.print("运行 qm pending 查看待确认条目。")


@app.command()
def recall(
    query: str = typer.Argument(..., help="检索关键词"),
    limit: int = typer.Option(10, "--limit", "-n", help="最多返回条数"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """检索记忆。"""
    hits = _service(config, vault).recall(query, limit)
    print_hits(query, hits)


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
    table = styled_table("待确认")
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
        fail(str(exc))
        return
    console.print(f"[bold]已写入[/] [{entry.topic}] {entry.text}")


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
        fail(str(exc))
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
    table = styled_table("记忆主题")
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
    table = styled_table("提交历史")
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
        fail(str(exc))
        return
    if not result["committed"]:
        console.print(f"[dim]{result['summary']}[/dim]")
        return
    console.print("[bold]本次提交总结[/bold]")
    console.print(result["summary"])
    console.print(f"提交：{result['hash']}")
    if result["pushed"]:
        console.print("[bold]已推送[/]")


@app.command()
def status(
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """查看记忆库状态。"""
    info = _service(config, vault).status()
    console.print(f"记忆库：{info['vault']}")
    console.print(f"主题 {info['topics']} · 条目 {info['entries']} · 待确认 {info['pending']} · 未提交 {info['dirty']}")
    console.print(f"检索 {info['retrieval']}")


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
