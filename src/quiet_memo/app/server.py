from __future__ import annotations

from fastmcp import FastMCP

from ..infra.config import Config, load_config
from ..memory.service import MemoryService


def build_server(config: Config | None = None, service: MemoryService | None = None) -> FastMCP:
    svc = service or MemoryService(config or load_config())
    mcp = FastMCP(
        "quiet-memo",
        instructions=(
            "个人记忆库。默认静默：用户陈述值得长期保留的事实时，直接调用 memorize 写入本地 Markdown，"
            "不要逐条向用户确认。memorize 返回的 queued 表示低置信、冲突或敏感内容，已进入待确认队列，"
            "必要时简要提示用户运行 review_pending 或 qm pending。"
            "回答用户问题前，如果涉及用户个人信息、偏好、经历、计划，先调用 recall 检索。"
        ),
    )

    @mcp.tool
    def memorize(text: str, source: str = "chat") -> dict:
        """从一段对话中抽取用户本人的事实、偏好或计划并静默写入。低置信/冲突/敏感项返回在 queued 中，不直接打断用户。"""
        return svc.memorize(text, source).to_dict()

    @mcp.tool
    def note(text: str, topic: str = "uncategorized", title: str | None = None) -> dict:
        """明确记录一条用户给定的事实，不经过 LLM 抽取。"""
        entry = svc.note(text, topic=topic, title=title)
        return entry.model_dump()

    @mcp.tool
    def recall(query: str, limit: int = 10) -> list[dict]:
        """按关键词检索本地记忆，返回命中的文件、行号和内容。"""
        return [hit.model_dump() for hit in svc.recall(query, limit)]

    @mcp.tool
    def review_pending() -> list[dict]:
        """列出等待人工确认的记忆候选。"""
        return [item.model_dump() for item in svc.pending()]

    @mcp.tool
    def approve(item_id: str) -> dict:
        """把一条待确认候选写入记忆库。"""
        try:
            return svc.approve(item_id).model_dump()
        except KeyError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool
    def reject(item_id: str) -> dict:
        """拒绝一条待确认候选，保留处理记录。"""
        try:
            return svc.reject(item_id).model_dump()
        except KeyError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool
    def list_topics() -> list[dict]:
        """列出记忆主题及条目数。"""
        return svc.topics()

    @mcp.tool
    def history(limit: int = 10) -> list[dict]:
        """查看记忆库最近的 Git 提交历史。"""
        return svc.history(limit)

    @mcp.tool
    def commit(push: bool = False) -> dict:
        """生成总结并提交当前变更；push=true 时推送到 remote。默认不推送。"""
        return svc.commit(push=push)

    @mcp.tool
    def status() -> dict:
        """查看记忆库状态：主题数、条目数、待确认数、未提交变更。"""
        return svc.status()

    return mcp
