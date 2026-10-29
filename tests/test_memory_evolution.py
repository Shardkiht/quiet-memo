from __future__ import annotations

import pytest
from typer.testing import CliRunner

from quiet_memo.agent.session import dated_message, now_iso
from quiet_memo.app.cli import app
from quiet_memo.memory.extractor import StaticExtractor
from quiet_memo.memory.models import Candidate, ExtractionResult, MemoryContext
from quiet_memo.memory.temporal import matches_context, resolve_relative, validate_range

pytestmark = pytest.mark.memory


def test_inference_review_correction_and_source_invalidation(service):
    source = service.note("偏好合成阅读活动")
    item = service.propose_inference("可能喜欢安静活动", [source.id])
    prefix = service.inferences.path.read_bytes()
    confirmed = service.review_inference(item.id, "confirm")
    assert confirmed.status == "confirmed"
    before = service.inferences.path.read_bytes()
    assert service.review_inference(item.id, "confirm") == confirmed
    assert service.inferences.path.read_bytes() == before
    replacement = service.review_inference(item.id, "correct", "可能喜欢室内活动")
    assert replacement.id != item.id
    assert replacement.status == "hypothesis"
    assert service.inferences.path.read_bytes().startswith(prefix)
    assert len(service.store.entries()) == 1
    service.forget(source.id)
    assert all(item.status == "superseded" for item in service.list_inferences())
    with pytest.raises(ValueError):
        service.review_inference(replacement.id, "confirm")


def test_rejected_inference_cannot_be_revived(service):
    source = service.note("合成事实")
    item = service.propose_inference("合成假设", [source.id])
    service.review_inference(item.id, "reject")
    assert service.propose_inference(item.text, item.source_ids).status == "rejected"
    with pytest.raises(ValueError):
        service.review_inference(item.id, "confirm")


def test_external_source_change_invalidates_on_read(service):
    source = service.note("合成事实")
    item = service.propose_inference("合成假设", [source.id])
    service.store.set_status(source.id, "deleted")
    assert service.list_inferences()[0].status == "superseded"


@pytest.mark.parametrize("text,anchor,expected", [
    ("昨天", "2024-03-01T00:10:00+08:00", "2024-02-29"),
    ("昨天", "2024-03-01T23:10:00-08:00", "2024-02-29"),
    ("去年", "2024-03-01T00:10:00+08:00", "2023"),
    ("上个月", "2024-01-01T00:10:00+08:00", "2023-12"),
    ("最近", "2024-01-01T00:10:00+08:00", None),
    ("昨天", "2024-01-01T00:10:00", None),
    ("昨天", None, None),
])
def test_relative_dates(text, anchor, expected):
    assert resolve_relative(text, anchor) == expected


def test_memorize_resolves_original_anchor(service):
    service.extractor = StaticExtractor(ExtractionResult(memories=[Candidate(
        text="昨天参加合成展览", confidence=1, context=MemoryContext(time_text="昨天"))]))
    result = service.memorize("昨天参加合成展览", observed_at="2024-03-01T00:10:00+08:00")
    assert result.admitted[0].context.event_time == "2024-02-29"


def test_message_anchor_preserves_legacy_unknown():
    assert dated_message({"content": "昨天", "at": "2024-03-01T00:00:00"}) == "昨天"
    assert "+08:00" in dated_message({"content": "昨天", "at": "2024-03-01T00:00:00+08:00"})
    from datetime import datetime
    assert datetime.fromisoformat(now_iso()).utcoffset() is not None


def test_context_range_and_missing_metadata():
    context = MemoryContext(event_time="2024", location="合成展馆", location_kind="event")
    assert matches_context(context, "2024-06", "2024-06", "展馆", None)
    assert not matches_context(context, "2025", None, None, None)
    assert not matches_context(None, "2024", None, None, None)
    with pytest.raises(ValueError):
        validate_range("2025", "2024")


def test_filtered_recall_filters_before_limit(service):
    from quiet_memo.retrieval.base import SearchHit
    first = service.note("合成展览旧记录")
    service.extractor = StaticExtractor(ExtractionResult(memories=[Candidate(
        text="合成展览新记录", confidence=1, context=MemoryContext(event_time="2024-06"))]))
    second = service.memorize("合成展览新记录").admitted[0]
    class Ranked:
        def search(self, query, limit=10):
            return [SearchHit(id=e.id, text=e.text, topic=e.topic) for e in (first, second)][:limit]
    service.retriever = Ranked()
    assert [h.id for h in service.recall("展览", 1, date_from="2024")] == [second.id]
    assert [h.id for h in service.recall("展览", 1)] == [first.id]


def test_keyword_context_retrieval_keeps_old_memories_without_filters(service):
    from datetime import datetime

    old = service.store.append("events", "合成展览旧记录", when=datetime(2024, 6, 1))
    wanted = service.store.append("events", "合成展览新记录", context=MemoryContext(
        event_time="2024-06", location="合成展馆", location_kind="event",
        emotion="期待", emotion_source="explicit"))
    service.store.append("events", "合成展览另一记录", context=MemoryContext(event_time="2025"))
    hits = service.recall("合成展览", date_from="2024", date_to="2024", location="展馆", emotion="期待")
    assert [hit.id for hit in hits] == [wanted.id]
    assert old.id in {hit.id for hit in service.recall("合成展览")}
    assert old.id not in {hit.id for hit in service.recall("合成展览", date_from="2024", date_to="2024")}


def test_inference_cli(service, monkeypatch):
    monkeypatch.setattr("quiet_memo.app.cli._service", lambda *args: service)
    source = service.note("合成事实")
    item = service.propose_inference("合成假设", [source.id])
    result = CliRunner().invoke(app, ["inferences", item.id, "--action", "confirm"])
    assert result.exit_code == 0, result.output
    assert "confirmed" in result.output


def test_inference_snapshot_without_final_newline(service):
    source = service.note("合成事实")
    item = service.propose_inference("合成假设", [source.id])
    path = service.inferences.path
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    before = path.read_bytes()
    service.review_inference(item.id, "confirm")
    assert path.read_bytes().startswith(before)
    assert service.list_inferences()[0].status == "confirmed"


def test_damaged_log_blocks_fact_mutation(service):
    source = service.note("合成事实")
    service.propose_inference("合成假设", [source.id])
    with service.inferences.path.open("ab") as handle:
        handle.write(b'{"broken":')
    before = service.inferences.path.read_bytes()
    with pytest.raises(ValueError):
        service.forget(source.id)
    assert service.store.find(source.id).status == "active"
    assert service.inferences.path.read_bytes() == before


def test_explicit_tail_recovery_preserves_bytes_and_allows_review(service):
    source = service.note("合成事实")
    item = service.propose_inference("合成假设", [source.id])
    with service.inferences.path.open("ab") as handle:
        handle.write(b'{"type":"revision","records":[')
    before = service.inferences.path.read_bytes()
    with pytest.raises(ValueError):
        service.list_inferences()
    assert service.recover_inference_tail()
    assert service.inferences.path.read_bytes().startswith(before)
    assert service.review_inference(item.id, "confirm").status == "confirmed"
    assert not service.recover_inference_tail()


def test_tail_recovery_rejects_middle_corruption(service):
    source = service.note("合成事实")
    service.propose_inference("合成假设", [source.id])
    with service.inferences.path.open("ab") as handle:
        handle.write(b'broken\n{"tail":')
    before = service.inferences.path.read_bytes()
    with pytest.raises(ValueError):
        service.recover_inference_tail()
    assert service.inferences.path.read_bytes() == before


@pytest.mark.parametrize("action", ["forget", "correct"])
def test_source_audit_survives_inference_append_failure(service, monkeypatch, action):
    import json

    source = service.note("合成事实")
    service.propose_inference("合成假设", [source.id])
    original = service.inferences.record
    def fail(*items):
        raise OSError("合成写入失败")
    monkeypatch.setattr(service.inferences, "record", fail)
    with pytest.raises(OSError):
        if action == "forget":
            service.forget(source.id)
        else:
            service.correct(source.id, "合成更正事实")
    lines = (service.config.vault / ".quiet-memo" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["action"] == action for line in lines)
    monkeypatch.setattr(service.inferences, "record", original)
    assert service.list_inferences()[0].status == "superseded"


@pytest.mark.agent
def test_agent_recall_passes_context_filters(service):
    from quiet_memo.agent.tools import build_tools
    from quiet_memo.agent.session import SessionStore
    service.config.search.enabled = False
    captured = {}
    def recall(query, limit, **filters):
        captured.update(filters)
        return []
    service.recall = recall
    tools = build_tools(service, SessionStore(service.config.vault).create())
    result = tools.execute("recall", {"query": "展览", "date_from": "2024", "location": "合成展馆"})
    assert result.ok
    assert captured == {"date_from": "2024", "location": "合成展馆"}


@pytest.mark.mcp
async def test_mcp_recall_passes_context_filters(service):
    from fastmcp import Client
    from quiet_memo.app.server import build_server
    service.config.search.enabled = False
    captured = {}
    def recall(query, limit, **filters):
        captured.update(filters)
        return []
    service.recall = recall
    async with Client(build_server(service=service)) as client:
        result = await client.call_tool("recall", {"query": "展览", "date_from": "2024", "location": "合成展馆"})
        assert result.data == []
    assert captured["date_from"] == "2024"
    assert captured["location"] == "合成展馆"
