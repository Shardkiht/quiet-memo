from __future__ import annotations

import pytest

pytestmark = pytest.mark.mcp

from fastmcp import Client

from quiet_memo.app.server import build_server
from quiet_memo.infra.config import Config, LLMConfig
from quiet_memo.memory.extractor import StaticExtractor
from quiet_memo.memory.models import Candidate, ExtractionResult
from quiet_memo.memory.service import MemoryService


def make_server(tmp_path, candidates):
    config = Config(vault=tmp_path / "vault", llm=LLMConfig())
    service = MemoryService(
        config, extractor=StaticExtractor(ExtractionResult(memories=candidates))
    )
    service.init_vault()
    return build_server(service=service)


async def test_mcp_memorize_recall_pending(tmp_path):
    server = make_server(
        tmp_path,
        [
            Candidate(text="偏好清淡风味", topic="preferences", confidence=0.95),
            Candidate(text="可能喜欢浅烘", topic="preferences", confidence=0.5),
        ],
    )

    async with Client(server) as client:
        memorized = await client.call_tool("memorize", {"text": "一段对话"})
        assert [entry["text"] for entry in memorized.data["admitted"]] == ["偏好清淡风味"]
        assert len(memorized.data["queued"]) == 1

        recalled = await client.call_tool("recall", {"query": "清淡"})
        assert recalled.data[0]["topic"] == "preferences"

        pending = await client.call_tool("review_pending", {})
        assert len(pending.data) == 1
        item_id = pending.data[0]["id"]

        approved = await client.call_tool("approve", {"item_id": item_id})
        assert approved.data["text"] == "可能喜欢浅烘"

        pending_after = await client.call_tool("review_pending", {})
        assert pending_after.data == []


async def test_mcp_topics_and_status(tmp_path):
    server = make_server(tmp_path, [Candidate(text="用 手冲滤纸", topic="devices", confidence=0.95)])

    async with Client(server) as client:
        await client.call_tool("memorize", {"text": "对话"})

        topics = await client.call_tool("list_topics", {})
        assert topics.data[0]["topic"] == "devices"

        topic = await client.call_tool("read_topic", {"topic": "devices"})
        assert topic.data["content"] == "- 用 手冲滤纸"
        assert topic.data["entries"] == 1

        status = await client.call_tool("status", {})
        assert status.data["entries"] == 1
