from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from quiet_memo.memory.models import Candidate, ExtractionResult, MemoryContext, ReflectionProposal
from quiet_memo.memory.extractor import StaticExtractor
from quiet_memo.memory.service import MemoryService

pytestmark = pytest.mark.memory


def proposal(mode, entries):
    return [ReflectionProposal(text="可能偏好安静的合成活动", source_ids=[entries[0].id])]


def test_dream_is_incremental_separate_and_restartable(service):
    source = service.note("合成阅读偏好", topic="preferences")
    first = service.reflection.run(model=proposal)
    assert first.requests == 1 and first.proposed == 1 and first.remaining == 0
    assert service.inferences.all()[0].kind == "reflection"
    assert service.store.entries() == [source]
    restarted = MemoryService(service.config)
    second = restarted.reflection.run(model=lambda *_: pytest.fail("不得重复请求"))
    assert second.requests == 0 and second.skipped == 1


def test_new_fact_does_not_restart_entire_reflection_corpus(service):
    for index in range(25):
        service.store.append(f"synthetic-{index}", f"合成事实 {index}")
    initial = service.reflection.run(max_requests=3, model=lambda *_: [])
    assert initial.remaining == 0
    added = service.note("新增合成事实")
    calls = []
    result = service.reflection.run(model=lambda mode, entries: calls.append(entries) or [])
    assert result.requests == 1 and result.remaining == 0 and result.skipped == 25
    assert len(calls[0]) <= 5
    assert added.id in {entry.id for entry in calls[0]}


def test_profile_rebuilds_from_sources_and_only_injects_confirmed_related_inferences(service):
    source = service.note("合成阅读偏好", topic="preferences")
    other = service.note("合成其他事实")
    result = service.reflection.run("profile", model=proposal)
    assert result.proposed == 1
    items = service.reflection.profile()
    assert {item.kind for item in items} == {"fact", "inference"}
    assert not service.reflection.context({source.id})
    inference = service.inferences.all()[0]
    service.review_inference(inference.id, "confirm")
    assert "推断" in service.reflection.context({source.id})
    assert not service.reflection.context({other.id})
    service.forget(source.id)
    assert service.reflection.profile() == []
    assert not service.reflection.context({source.id})


def test_manual_fact_edit_invalidates_profile_evidence(service):
    source = service.note("合成早期阅读偏好", topic="preferences")
    service.reflection.run("profile", model=proposal)
    path = service.store.topic_path("preferences")
    path.write_text(path.read_text(encoding="utf-8").replace("合成早期阅读偏好", "合成后期阅读偏好"), encoding="utf-8")
    assert service.store.find(source.id).status == "active"
    assert service.list_inferences()[0].status == "superseded"
    assert all(item.kind == "fact" for item in service.reflection.profile())


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_reflection_request_budget(service, budget):
    for index in range(25):
        service.store.append(f"synthetic-{index}", f"合成事实 {index}")
    calls = []
    result = service.reflection.run(max_requests=budget, model=lambda mode, entries: calls.append(entries) or [])
    assert result.requests == len(calls) == budget
    assert result.remaining == 3 - budget


@pytest.mark.parametrize("bad", ["source", "secret"])
def test_invalid_model_output_never_reaches_inference_log(service, bad):
    source = service.note("合成事实")
    def model(mode, entries):
        return [ReflectionProposal(text="password = synthetic-only" if bad == "secret" else "合成假设",
                                   source_ids=["missing"] if bad == "source" else [source.id])]
    result = service.reflection.run(model=model)
    assert result.error and result.requests == 1
    assert service.inferences.all() == []
    assert "synthetic-only" not in service.reflection.path.read_text(encoding="utf-8")


def test_cancel_keeps_chat_writes_available_and_drops_returned_candidates(service):
    service.note("合成事实")
    started, release = threading.Event(), threading.Event()
    def model(mode, entries):
        started.set()
        assert release.wait(5)
        return proposal(mode, entries)
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(service.reflection.run, model=model)
        try:
            assert started.wait(5)
            # 网络等待期间不持有写锁。
            service.note("回顾期间的新事实")
            with pytest.raises(ValueError, match="已有"):
                service.reflection.run(model=proposal)
            service.reflection.cancel()
        finally:
            release.set()
        result = pending.result(timeout=5)
    assert result.cancelled and not service.inferences.all()


def test_cancel_before_worker_starts_never_calls_model(service):
    service.note("合成事实")
    token = threading.Event()
    token.set()
    result = service.reflection.run(model=lambda *_: pytest.fail("不得请求"), cancel_event=token)
    assert result.cancelled and result.requests == 0


def test_source_change_during_model_call_invalidates_result(service):
    source = service.note("合成事实")
    def model(mode, entries):
        service.forget(source.id)
        return proposal(mode, entries)
    result = service.reflection.run(model=model)
    assert result.error and service.inferences.all() == []


def test_prepared_result_resumes_without_another_model_request(service, monkeypatch):
    service.note("合成事实")
    original = service._propose_inference_locked
    def fail(*args):
        raise OSError("合成写入失败")
    monkeypatch.setattr(service, "_propose_inference_locked", fail)
    assert service.reflection.run(model=proposal).error
    monkeypatch.setattr(service, "_propose_inference_locked", original)
    result = service.reflection.run(max_requests=0, model=lambda *_: pytest.fail("应复用已验证结果"))
    assert result.requests == 0 and result.proposed == 1 and result.remaining == 0


def test_second_batch_cached_result_survives_completed_first_batch(service, monkeypatch):
    for index in range(13):
        service.store.append(f"synthetic-{index}", f"合成事实 {index}")
    original = service._propose_inference_locked
    count = 0
    def fail_second(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("合成写入失败")
        return original(*args)
    monkeypatch.setattr(service, "_propose_inference_locked", fail_second)
    first = service.reflection.run(max_requests=2, model=proposal)
    assert first.error and first.requests == 2
    monkeypatch.setattr(service, "_propose_inference_locked", original)
    result = service.reflection.run(max_requests=0, model=lambda *_: pytest.fail("不得重复发送第二批"))
    assert result.requests == 0 and result.proposed == 1 and result.remaining == 0


def test_job_tail_recovery_preserves_all_bytes(service):
    service.note("合成事实")
    service.reflection.path.parent.mkdir(parents=True, exist_ok=True)
    service.reflection.path.write_bytes(b'{"status":')
    before = service.reflection.path.read_bytes()
    result = service.reflection.run(model=proposal)
    assert result.proposed == 1
    assert service.reflection.path.read_bytes().startswith(before)
    assert service.reflection._events()[1] is None


def test_current_and_historical_state_views(service):
    old = service.store.append("preferences", "合成偏好早期状态", context=MemoryContext(valid_from="2020-01-01"))
    new = service.evolve(old.id, "合成偏好后期状态", "2024-01-01")
    assert [hit.id for hit in service.recall("合成偏好")] == [new.id]
    assert [hit.id for hit in service.recall("合成偏好", as_of="2022-01-01")] == [old.id]
    assert {hit.id for hit in service.recall("合成偏好", view="history")} == {old.id, new.id}
    service.forget(new.id)
    assert not service.recall("合成偏好", view="history")


def test_correction_does_not_resurrect_known_wrong_fact_as_history(service):
    old = service.store.append("preferences", "合成错误偏好", context=MemoryContext(valid_from="2020-01-01"))
    service.correct(old.id, "合成正确偏好")
    assert old.id not in {hit.id for hit in service.recall("合成偏好", view="history")}


def test_current_view_excludes_expired_and_future_states_but_keeps_undated_facts(service):
    old = service.store.append("goals", "合成目标旧状态", context=MemoryContext(valid_to="2000"))
    future = service.store.append("goals", "合成目标未来状态", context=MemoryContext(valid_from="9999"))
    current = service.note("合成目标未标日期", topic="goals")
    assert {hit.id for hit in service.recall("合成目标")} == {current.id}
    assert {hit.id for hit in service.recall("合成目标", view="history")} == {old.id, future.id, current.id}


def test_disjoint_periods_are_not_conflicts(service):
    service.store.append("preferences", "合成预算为1000", context=MemoryContext(valid_from="2020", valid_to="2021"))
    service.extractor = StaticExtractor(ExtractionResult(memories=[Candidate(text="合成预算为2000",
        topic="preferences", confidence=1, context=MemoryContext(valid_from="2024"))]))
    result = service.memorize("合成陈述")
    assert len(result.admitted) == 1 and not result.queued


def test_same_fact_can_recur_in_disjoint_periods(service):
    service.store.append("preferences", "合成阅读偏好", context=MemoryContext(valid_from="2020", valid_to="2021"))
    service.extractor = StaticExtractor(ExtractionResult(memories=[Candidate(text="合成阅读偏好",
        topic="preferences", kind="update", confidence=1, context=MemoryContext(valid_from="2024"))]))
    result = service.memorize("合成陈述")
    assert len(result.admitted) == 1 and not result.duplicates and not result.queued
    assert len(service.store.entries()) == 2


def test_overlapping_periods_still_require_confirmation(service):
    service.store.append("preferences", "合成预算为1000", context=MemoryContext(valid_from="2020", valid_to="2025"))
    service.extractor = StaticExtractor(ExtractionResult(memories=[Candidate(text="合成预算为2000",
        topic="preferences", confidence=1, context=MemoryContext(valid_from="2024"))]))
    result = service.memorize("合成陈述")
    assert result.queued and not result.admitted


def test_historical_conflicts_do_not_hide_current_conflict(service):
    for index in range(3):
        service.store.append("preferences", f"合成预算为{1000 + index}", context=MemoryContext(valid_to="2021"))
    current = service.store.append("preferences", "合成预算为1500", context=MemoryContext(valid_from="2023"))
    service.extractor = StaticExtractor(ExtractionResult(memories=[Candidate(text="合成预算为2000",
        topic="preferences", confidence=1, context=MemoryContext(valid_from="2024"))]))
    result = service.memorize("合成陈述")
    assert result.queued[0].conflict_with == current.id


def test_reflection_model_disables_retries_and_limits_output(service, monkeypatch):
    from types import SimpleNamespace
    from quiet_memo.infra.config import LLMConfig
    from quiet_memo.memory import reflection as module

    service.config.llm = LLMConfig(provider="local", num_retries=7, fallbacks=["unused"])
    source = service.note("合成事实")
    captured = {}
    def complete(config, messages, **kwargs):
        captured.update(retries=config.num_retries, fallbacks=config.fallbacks, **kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"proposals":[]}'))])
    monkeypatch.setattr(module, "complete", complete)
    assert service.reflection._model("dream", [source]) == []
    assert captured["retries"] == 0 and captured["fallbacks"] == []
    assert captured["max_tokens"] <= 1800


@pytest.mark.cli
def test_profile_cli_is_read_only_and_evolve_cli_preserves_history(service, monkeypatch):
    from typer.testing import CliRunner
    from quiet_memo.app.cli import app

    monkeypatch.setattr("quiet_memo.app.cli._service", lambda *args: service)
    old = service.store.append("preferences", "合成早期偏好", context=MemoryContext(valid_from="2020-01-01"))
    result = CliRunner().invoke(app, ["evolve", old.id, "合成后期偏好", "--from", "2024-01-01"])
    assert result.exit_code == 0, result.output
    monkeypatch.setattr(service.reflection, "_model", lambda *_: pytest.fail("只读画像不得调用模型"))
    result = CliRunner().invoke(app, ["profile"])
    assert result.exit_code == 0 and "合成后期偏好" in result.output
    result = CliRunner().invoke(app, ["recall", "合成早期偏好", "--as-of", "2022-01-01"])
    assert result.exit_code == 0 and "合成早期偏好" in result.output


@pytest.mark.mcp
async def test_mcp_profile_and_temporal_evolution(service):
    from fastmcp import Client
    from quiet_memo.app.server import build_server

    old = service.store.append("preferences", "合成早期偏好", context=MemoryContext(valid_from="2020-01-01"))
    service.config.search.enabled = False
    async with Client(build_server(service=service)) as client:
        result = await client.call_tool("evolve", {"entry_id": old.id, "text": "合成后期偏好", "effective_from": "2024-01-01"})
        new_id = result.data["id"]
        profile = await client.call_tool("profile", {})
        assert [item["id"] for item in profile.data] == [new_id]
        history = await client.call_tool("recall", {"query": "合成偏好", "as_of": "2022-01-01"})
        assert [item["id"] for item in history.data] == [old.id]


def test_missing_proposals_is_not_a_successful_empty_batch():
    from pydantic import ValidationError
    from quiet_memo.memory.models import ReflectionResponse
    with pytest.raises(ValidationError):
        ReflectionResponse.model_validate({})
