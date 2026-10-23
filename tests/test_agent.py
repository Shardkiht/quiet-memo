from __future__ import annotations

from quiet_memo.agent.runtime import AgentRuntime
from quiet_memo.agent.session import SessionStore
from quiet_memo.infra.config import ChatConfig, Config, LLMConfig
from quiet_memo.llm import AssistantTurn, ToolCall
from quiet_memo.memory.extractor import StaticExtractor
from quiet_memo.memory.models import Candidate, ExtractionResult
from quiet_memo.memory.service import MemoryService


class FakeChatModel:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append((messages, tools))
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class FakeSummarizer:
    def summarize(self, previous, transcript, max_chars=1200):
        return "会话摘要"


def build_runtime(tmp_path, model, candidates=None, *, extract_on_exit=False, max_steps=4):
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        chat=ChatConfig(
            context_window=32000,
            max_tool_steps=max_steps,
            auto_recall=3,
            extract_on_exit=extract_on_exit,
        ),
    )
    service = MemoryService(
        config,
        extractor=StaticExtractor(ExtractionResult(memories=candidates or [])),
    )
    service.init_vault()
    runtime = AgentRuntime(
        service,
        model,
        SessionStore(config.vault),
        summarizer=FakeSummarizer(),
        config=config.chat,
    )
    runtime.start()
    return runtime, service


def test_start_defers_session_creation(tmp_path):
    runtime, service = build_runtime(
        tmp_path, FakeChatModel([AssistantTurn(content="收到")])
    )

    assert runtime.session is None
    store = SessionStore(service.config.vault)
    assert store.list() == []

    runtime.ask("你好")

    assert runtime.has_session
    assert len(store.list()) == 1


def test_close_without_session_is_noop(tmp_path):
    runtime, _ = build_runtime(tmp_path, FakeChatModel([]))

    assert runtime.close()["admitted"] == 0


def test_resume_restores_prior_messages_for_model(tmp_path):
    runtime, service = build_runtime(
        tmp_path, FakeChatModel([AssistantTurn(content="第一次回答")])
    )
    runtime.ask("第一问")
    session_id = runtime.current.id
    runtime.close()

    model = FakeChatModel([AssistantTurn(content="第二次回答")])
    resumed = AgentRuntime(
        service,
        model,
        SessionStore(service.config.vault),
        summarizer=FakeSummarizer(),
        config=service.config.chat,
    )
    resumed.resume(session_id)
    resumed.ask("第二问")

    contents = [message.get("content") for message in model.calls[0][0]]
    assert "第一问" in contents
    assert "第一次回答" in contents
    assert "第二问" in contents


def test_memorize_tool_writes_with_session_provenance(tmp_path):
    model = FakeChatModel(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="memorize",
                        arguments={"text": "我住在市区"},
                    )
                ]
            ),
            AssistantTurn(content="记下了。"),
        ]
    )
    candidate = Candidate(text="住在市区", topic="housing", confidence=0.95)
    runtime, service = build_runtime(tmp_path, model, [candidate])

    reply = runtime.ask("我住在市区")

    assert reply.content == "记下了。"
    assert reply.steps == 2
    assert any("写入 1 条" in event for event in reply.events)
    entries = service.store.entries()
    assert entries[0].text == "住在市区"
    assert entries[0].metadata["source"] == f"session:{runtime.current.id}"
    assert entries[0].metadata["session"] == runtime.current.id
    tool_records = [record for record in runtime.current.records if record.get("type") == "tool"]
    assert tool_records[0]["name"] == "memorize"


def test_recall_tool_result_reaches_model(tmp_path):
    model = FakeChatModel(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="c1", name="recall", arguments={"query": "清淡"})]
            ),
            AssistantTurn(content="你喜欢清淡风味。"),
        ]
    )
    runtime, service = build_runtime(tmp_path, model)
    service.note("喜欢清淡风味", topic="preferences")

    reply = runtime.ask("我喜欢什么风味？")

    assert reply.content == "你喜欢清淡风味。"
    second_call_messages = model.calls[1][0]
    tool_messages = [message for message in second_call_messages if message["role"] == "tool"]
    assert tool_messages and "清淡" in tool_messages[0]["content"]
    assert any("检索" in event for event in reply.events)


def test_auto_recall_injects_long_term_memory(tmp_path):
    model = FakeChatModel([AssistantTurn(content="记得。")])
    runtime, service = build_runtime(tmp_path, model)
    service.note("喜欢清淡风味", topic="preferences")

    runtime.ask("清淡")

    system_message = model.calls[0][0][0]
    assert "<long_term_memory>" in system_message["content"]
    assert "清淡" in system_message["content"]


def test_auto_recall_uses_recent_context_for_followup(tmp_path, monkeypatch):
    model = FakeChatModel([AssistantTurn(content="第一次"), AssistantTurn(content="第二次")])
    runtime, service = build_runtime(tmp_path, model)
    queries = []
    recall = service.recall

    def capture(query, limit=10):
        queries.append(query)
        return recall(query, limit)

    monkeypatch.setattr(service, "recall", capture)

    runtime.ask("我在看滨江的房子")
    runtime.ask("那附近便宜吗")

    assert "滨江" in queries[1]


def test_memory_cue_increases_auto_recall_limit(tmp_path, monkeypatch):
    model = FakeChatModel([AssistantTurn(content="记得。")])
    runtime, service = build_runtime(tmp_path, model)
    limits = []

    def capture(query, limit=10):
        limits.append(limit)
        return []

    monkeypatch.setattr(service, "recall", capture)

    runtime.ask("你还记得我的咖啡偏好吗")

    assert limits == [8]


def test_force_conclude_after_max_steps(tmp_path):
    class LoopModel:
        def __init__(self):
            self.tool_rounds = []

        def chat(self, messages, tools):
            self.tool_rounds.append(bool(tools))
            if tools:
                return AssistantTurn(
                    tool_calls=[
                        ToolCall(id="c1", name="memorize", arguments={"text": "不断调用"})
                    ]
                )
            return AssistantTurn(content="收敛回答")

    model = LoopModel()
    runtime, _ = build_runtime(tmp_path, model, max_steps=1)

    reply = runtime.ask("开始")

    assert reply.content == "收敛回答"
    assert model.tool_rounds == [True, False]
    assert any("强制收敛" in event for event in reply.events)


def test_retries_transient_model_failure(tmp_path):
    class FlakyModel:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary")
            return AssistantTurn(content="恢复了")

    model = FlakyModel()
    runtime, _ = build_runtime(tmp_path, model)

    reply = runtime.ask("你好")

    assert reply.content == "恢复了"
    assert model.calls == 2


def test_close_extracts_memories_once(tmp_path):
    model = FakeChatModel([AssistantTurn(content="好的。")])
    candidate = Candidate(text="退出时沉淀的事实", topic="misc", confidence=0.95)
    runtime, service = build_runtime(
        tmp_path, model, [candidate], extract_on_exit=True
    )

    runtime.ask("这是一条值得记住的事实")
    stats = runtime.close()
    again = runtime.close()

    assert stats["admitted"] == 1
    assert again["admitted"] == 0
    entries = service.store.entries()
    assert len(entries) == 1
    assert entries[0].metadata["source"] == f"session:{runtime.current.id}:exit"


def test_reload_llm_updates_model_and_extractor(tmp_path, monkeypatch):
    runtime, service = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="ok")]))
    new_llm = LLMConfig(
        provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        model="deepseek-ai/DeepSeek-V3",
    )
    monkeypatch.setattr(
        "quiet_memo.infra.config.load_config",
        lambda *args, **kwargs: Config(vault=service.config.vault, llm=new_llm),
    )

    runtime.reload_llm()

    assert runtime.model_label == "siliconflow/deepseek-ai/DeepSeek-V3"
    assert service.config.llm is new_llm
    assert runtime.context.config is service.config.chat


def test_switch_model_updates_config_and_runtime(
    tmp_path, monkeypatch, patch_questionary
):
    from quiet_memo.app import model_ui

    patch_questionary(model_ui)
    runtime, service = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="ok")]))
    config_path = tmp_path / "quiet-memo.toml"

    monkeypatch.setattr(
        model_ui,
        "ask",
        lambda question: ("deepseek", "deepseek-v4-pro"),
    )
    monkeypatch.setattr(
        "quiet_memo.infra.providers.resolve_config_path",
        lambda path=None: config_path,
    )
    monkeypatch.setattr(
        "quiet_memo.infra.config.load_config",
        lambda *args, **kwargs: service.config,
    )

    model_ui.switch_model(runtime, service)

    assert service.config.llm.model == "deepseek-v4-pro"
    assert runtime.model_label == "deepseek/deepseek-v4-pro"
    assert config_path.exists()
    assert "deepseek-v4-pro" in config_path.read_text(encoding="utf-8")
