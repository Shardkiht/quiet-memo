from __future__ import annotations

import pytest

from quiet_memo.agent.runtime import AgentRuntime
from quiet_memo.agent.session import SessionStore, new_message_id
from quiet_memo.infra.config import ChatConfig, Config, LLMConfig
from quiet_memo.llm import AssistantTurn, ChatModelError, ToolCall
from quiet_memo.memory.extractor import StaticExtractor
from quiet_memo.memory.models import Candidate, ExtractionResult
from quiet_memo.memory.service import MemoryService


class FakeChatModel:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def chat(self, messages, tools, on_delta=None):
        self.calls.append((messages, tools))
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if on_delta is not None and turn.content:
            half = max(1, len(turn.content) // 2)
            on_delta(turn.content[:half])
            on_delta(turn.content[half:])
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


def test_system_prompt_includes_current_date(tmp_path):
    from datetime import date

    runtime, _ = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="好")]))
    runtime.ask("今天几号")

    system_message = runtime.model.calls[0][0][0]["content"]
    assert date.today().isoformat() in system_message


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


def test_undo_excludes_original_turn_from_next_model_call(tmp_path):
    model = FakeChatModel([AssistantTurn(content="旧回答"), AssistantTurn(content="新回答")])
    runtime, _ = build_runtime(tmp_path, model)
    runtime.ask("原始提问")

    assert runtime.undo() == "原始提问"
    runtime.ask("编辑后的提问")

    contents = [message.get("content") for message in model.calls[1][0]]
    assert "编辑后的提问" in contents
    assert "原始提问" not in contents
    assert "旧回答" not in contents
    assert runtime.current.user_texts() == ["编辑后的提问"]
    assert any(record.get("type") == "undo" for record in runtime.current.records)


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
    assert any("Saved 1" in event for event in reply.events)
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
    assert any("Recalled" in event for event in reply.events)


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


def test_attribute_question_rewrites_with_synonyms(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from quiet_memo.agent.recall import RecallQueryBuilder
    from quiet_memo.memory.store import MemoryStore
    from quiet_memo.retrieval.keyword import KeywordRetriever

    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "test-key")

    def fake_complete(config, messages, **kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="姓名 名字 用户称呼")
                )
            ]
        )

    query = RecallQueryBuilder(LLMConfig(), completer=fake_complete).build(
        "我叫什么名字", []
    )

    assert query.rewritten is True
    assert "姓名" in query.text

    store = MemoryStore(tmp_path / "vault")
    store.append("01-identity", "姓名：张三")
    store.append("preferences", "喜欢清淡风味")
    store.append("devices", "使用便携键盘")
    hits = KeywordRetriever(store).search(query.text)
    assert hits and hits[0].topic == "01-identity"


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
    assert any("forced wrap-up" in event for event in reply.events)


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


def test_apply_model_updates_config_and_runtime(tmp_path, monkeypatch):
    from quiet_memo.app import actions

    runtime, service = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="ok")]))
    config_path = tmp_path / "quiet-memo.toml"
    service.config_path = config_path

    message = actions.apply_model(service, runtime, "deepseek", "deepseek-v4-pro")

    assert "deepseek/deepseek-v4-pro" in message
    assert service.config.llm.model == "deepseek-v4-pro"
    assert runtime.model_label == "deepseek/deepseek-v4-pro"
    assert "deepseek-v4-pro" in config_path.read_text(encoding="utf-8")


def test_model_options_lists_current_first(service):
    from quiet_memo.app import actions

    options = actions.model_options(service)

    assert options
    assert options[0].current
    # 未配置其他供应商的 Key 时只展示当前供应商。
    assert {option.provider for option in options} == {"deepseek"}


def test_model_options_uses_fetched_models(service):
    from quiet_memo.app import actions

    options = actions.model_options(service, ["future-model"])

    assert options[0].current
    assert "future-model" in {option.model for option in options}


def test_provider_options_mark_current_and_unconfigured(service):
    from quiet_memo.app import actions

    options = actions.provider_options(service)
    by_key = {value[0]: (label, current) for label, value, current in options}

    assert by_key["deepseek"][1] is True
    assert by_key["siliconflow"] == ("SiliconFlow · add provider", False)


def test_configure_provider_saves_key_and_switches_model(tmp_path, monkeypatch):
    import sys
    import types

    from quiet_memo.app import actions

    runtime, service = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="ok")]))
    config_path = tmp_path / "quiet-memo.toml"
    service.config_path = config_path
    keys: dict[str, str] = {}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service_name, name: keys.get(name)
    fake.set_password = lambda service_name, name, value: keys.__setitem__(name, value)
    monkeypatch.setitem(sys.modules, "keyring", fake)

    message = actions.configure_provider(service, runtime, "siliconflow", "sk-test")

    assert "SiliconFlow" in message
    assert keys["SILICONFLOW_API_KEY"] == "sk-test"
    assert service.config.llm.provider == "siliconflow"
    assert service.config.llm.model == "deepseek-ai/DeepSeek-V3"
    assert runtime.model_label == "siliconflow/deepseek-ai/DeepSeek-V3"
    config_text = config_path.read_text(encoding="utf-8")
    assert 'provider = "siliconflow"' in config_text
    assert "sk-test" not in config_text


def test_configure_provider_keeps_state_when_keyring_fails(tmp_path, monkeypatch):
    import sys
    import types

    from quiet_memo.app import actions

    runtime, service = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="ok")]))
    config_path = tmp_path / "quiet-memo.toml"
    service.config_path = config_path
    fake = types.ModuleType("keyring")

    def locked(service_name, name):
        return None

    def fail(service_name, name, value):
        raise RuntimeError("keyring locked")

    fake.get_password = locked
    fake.set_password = fail
    monkeypatch.setitem(sys.modules, "keyring", fake)

    message = actions.configure_provider(service, runtime, "siliconflow", "sk-test")

    assert "Could not save" in message and "SILICONFLOW_API_KEY" in message
    assert service.config.llm.provider == "deepseek"
    assert not config_path.exists()


def test_update_provider_key_keeps_current_model(tmp_path, monkeypatch):
    import sys
    import types

    from quiet_memo.app import actions

    runtime, service = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="ok")]))
    keys: dict[str, str] = {}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service_name, name: keys.get(name)
    fake.set_password = lambda service_name, name, value: keys.__setitem__(name, value)
    monkeypatch.setitem(sys.modules, "keyring", fake)

    message = actions.update_provider_key(service, "siliconflow", "sk-updated")

    assert "API key updated" in message
    assert keys["SILICONFLOW_API_KEY"] == "sk-updated"
    assert service.config.llm.provider == "deepseek"
    assert runtime.model_label.startswith("deepseek/")


def test_remove_provider_deletes_saved_key(tmp_path, monkeypatch):
    import sys
    import types

    from quiet_memo.app import actions

    runtime, service = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="ok")]))
    keys = {"SILICONFLOW_API_KEY": "sk-test"}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service_name, name: keys.get(name)
    fake.delete_password = lambda service_name, name: keys.pop(name, None)
    monkeypatch.setitem(sys.modules, "keyring", fake)

    message = actions.remove_provider(service, "siliconflow")

    assert "SiliconFlow provider removed" in message
    assert "SILICONFLOW_API_KEY" not in keys
    assert service.config.llm.provider == "deepseek"


def test_remove_provider_rejects_current_provider(service):
    from quiet_memo.app import actions

    message = actions.remove_provider(service, "deepseek")

    assert "current provider" in message
    assert service.config.llm.provider == "deepseek"


def test_remove_provider_defers_to_environment_key(monkeypatch, service):
    from quiet_memo.app import actions

    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-env")

    message = actions.remove_provider(service, "siliconflow")

    assert "provided by SILICONFLOW_API_KEY" in message


def test_apply_reasoning_updates_config_hot(tmp_path, monkeypatch, service):
    from quiet_memo.app import actions

    config_path = tmp_path / "quiet-memo.toml"
    service.config_path = config_path
    monkeypatch.setattr(actions, "supported_params", lambda model: ("reasoning_effort",))

    assert actions.reasoning_options(service)
    message = actions.apply_reasoning(service, "high")

    assert "high" in message
    assert service.config.llm.reasoning_effort == "high"
    assert 'reasoning_effort = "high"' in config_path.read_text(encoding="utf-8")


def test_reasoning_options_empty_when_unsupported(service, monkeypatch):
    from quiet_memo.app import actions

    monkeypatch.setattr(actions, "supported_params", lambda model: ())
    assert actions.reasoning_options(service) == []


def test_context_options_and_apply(tmp_path, monkeypatch, service):
    from quiet_memo.app import actions

    config_path = tmp_path / "quiet-memo.toml"
    service.config_path = config_path

    options = actions.context_options(service)
    assert 16000 in options
    assert "16K" in actions.apply_context(service, 16000)
    assert service.config.chat.context_window == 16000
    assert (tmp_path / "quiet-memo.toml").exists()
    assert "at least" in actions.apply_context(service, 1000)




def test_ask_streams_events_to_callback(tmp_path):
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

    received = []
    reply = runtime.ask("我喜欢什么风味？", on_event=received.append)

    assert any("Recalled" in event for event in received)
    assert received == reply.events


def _config_with_vault(tmp_path_factory=None):
    import tempfile, pathlib
    from quiet_memo.infra.config import Config, LLMConfig, RetrievalConfig
    tmp = pathlib.Path(tempfile.mkdtemp())
    return Config(vault=tmp / "vault", llm=LLMConfig(), retrieval=RetrievalConfig(method="keyword"))


def test_recall_tool_guides_query_with_synonyms():
    """身份类查询召回：自然问句还要带常见同义词/字段名，避免“名字”漏掉“姓名”。"""
    from quiet_memo.agent.tools import build_tools
    from quiet_memo.agent.session import SessionStore
    from quiet_memo.memory.service import MemoryService

    config = _config_with_vault()
    service = MemoryService(config)
    service.init_vault()
    session = SessionStore(config.vault).create()
    spec = build_tools(service, session)._tools["recall"]
    assert "同义词" in spec.description
    assert "自然问句" in spec.parameters["properties"]["query"]["description"]
    assert "姓名" in spec.parameters["properties"]["query"]["description"]
    assert "不要拼凑关键词" not in spec.description


def test_import_prompt_forbids_section_prefix():
    """身份类查询召回缺口的修复之二：导入条目不带小节前缀，超短条目正文即事实。"""
    from quiet_memo.memory.extractor import IMPORT_PROMPT

    assert "小节编号或标题前缀" in IMPORT_PROMPT
    assert "稀释向量语义" in IMPORT_PROMPT


def test_system_prompt_default_without_soul(tmp_path):
    model = FakeChatModel([AssistantTurn(content="收到")])
    runtime, _ = build_runtime(tmp_path, model)

    runtime.ask("你好")

    content = model.calls[0][0][0]["content"]
    assert "个人记忆助手" in content
    assert "可用工具：" in content


def test_soul_md_replaces_persona_keeps_mechanism(tmp_path):
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "SOUL.md").write_text("你是阿澈，语气简洁带冷幽默。", encoding="utf-8")
    model = FakeChatModel([AssistantTurn(content="收到")])
    runtime, _ = build_runtime(tmp_path, model)

    runtime.ask("你好")

    content = model.calls[0][0][0]["content"]
    assert "阿澈" in content
    assert "个人记忆助手" not in content
    assert content.index("阿澈") < content.index("规则：")


def test_soul_md_hot_reload_next_turn(tmp_path):
    model = FakeChatModel([AssistantTurn(content="一"), AssistantTurn(content="二")])
    runtime, _ = build_runtime(tmp_path, model)
    runtime.ask("第一句")

    (tmp_path / "vault" / "SOUL.md").write_text("你是测试人设B。", encoding="utf-8")
    runtime.ask("第二句")

    assert "测试人设B" not in model.calls[0][0][0]["content"]
    assert "测试人设B" in model.calls[1][0][0]["content"]


def test_soul_md_truncated_by_tokens(tmp_path):
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "SOUL.md").write_text("长" * 8000, encoding="utf-8")
    model = FakeChatModel([AssistantTurn(content="收到")])
    runtime, _ = build_runtime(tmp_path, model)

    runtime.ask("你好")

    content = model.calls[0][0][0]["content"]
    assert len(content) < 9000
    assert "规则：" in content


def test_init_vault_seeds_default_soul(tmp_path):
    runtime, service = build_runtime(tmp_path, FakeChatModel([]))

    soul = service.config.vault / "SOUL.md"
    assert "个人记忆助手" in soul.read_text(encoding="utf-8")


def test_consolidate_pending_catches_crashed_session(tmp_path):
    model = FakeChatModel([])
    runtime, service = build_runtime(
        tmp_path,
        model,
        [Candidate(text="崩溃会话里说的偏好", topic="preferences", confidence=0.95)],
        extract_on_exit=True,
    )
    store = runtime.sessions
    # 模拟意外退出：会话有用户消息但没有 closed 记录。
    session = store.create()
    store.append(session, {"type": "message", "id": new_message_id(), "role": "user", "content": "我喜欢清淡风味"})

    result = runtime.consolidate_pending()

    assert result["consolidated"] == 1
    assert result["admitted"] == 1
    assert service.store.has_text("崩溃会话里说的偏好")
    types = [record["type"] for record in store.load(session.id).records]
    assert "consolidated" in types
    # 已打标记的会话不会被重复处理。
    assert runtime.consolidate_pending()["scanned"] == 0


def test_catchup_covers_messages_added_after_resume_then_crash(tmp_path):
    runtime, service = build_runtime(tmp_path, FakeChatModel([]), extract_on_exit=True)
    store = runtime.sessions
    session = store.create()
    store.append(session, {"type": "message", "id": new_message_id(), "role": "user", "content": "第一轮内容"})
    runtime.resume(session.id)
    runtime.close()  # 正常退出：游标推进并留下 closed

    runtime.resume(session.id)
    store.append(
        runtime.current,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "恢复后补充的新事实"},
    )
    # 模拟再次异常退出：没有 close()，直接以新进程姿态扫描。
    runtime.start()
    service.extractor = StaticExtractor(
        ExtractionResult(memories=[Candidate(text="恢复后补充的事实", topic="misc", confidence=0.95)])
    )

    assert store.load(session.id).needs_consolidation()
    result = runtime.consolidate_pending()
    assert result["consolidated"] == 1
    assert service.store.has_text("恢复后补充的事实")


def test_close_distills_all_chunks_instead_of_truncating(tmp_path):
    runtime, service = build_runtime(tmp_path, FakeChatModel([]), extract_on_exit=True)

    class RecordingExtractor:
        def __init__(self):
            self.texts = []

        def extract(self, text, context=""):
            self.texts.append(text)
            return ExtractionResult(memories=[])

    extractor = RecordingExtractor()
    service.extractor = extractor
    session = runtime.sessions.create()
    contents = [f"第{index}段" + "长" * 8000 for index in range(3)]
    for content in contents:
        runtime.sessions.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": content},
        )
    runtime.resume(session.id)
    runtime.close()

    joined = "\n---\n".join(contents)
    assert len(extractor.texts) >= 2
    assert "".join(extractor.texts) == joined
    types = [record["type"] for record in runtime.sessions.load(session.id).records]
    assert "distilled" in types


def test_consolidate_ignores_session_without_user_messages(tmp_path):
    runtime, service = build_runtime(tmp_path, FakeChatModel([]), extract_on_exit=True)
    session = runtime.sessions.create()

    result = runtime.consolidate_pending()

    # 没有用户消息的会话没有可沉淀内容，游标判空后直接跳过，无需再写收尾标记。
    assert result["scanned"] == 0
    assert result["consolidated"] == 0
    assert not runtime.sessions.load(session.id).needs_consolidation()
    assert service.store.entries() == []


def test_consolidate_pending_skips_current_and_respects_flag(tmp_path):
    runtime, _ = build_runtime(tmp_path, FakeChatModel([]))
    store = runtime.sessions
    session = store.create()
    store.append(session, {"type": "message", "id": new_message_id(), "role": "user", "content": "当前会话内容"})

    # 当前活跃会话不参与（其内容由 close() 沉淀）。
    runtime.resume(session.id)
    assert runtime.consolidate_pending()["scanned"] == 0

    # extract_on_exit=False 时整个补记关闭。
    runtime.start()
    runtime.config.extract_on_exit = False
    assert runtime.consolidate_pending()["scanned"] == 0


def test_close_failure_marks_extraction_error_and_catchup_recovers(tmp_path):
    runtime, service = build_runtime(
        tmp_path,
        FakeChatModel([]),
        [Candidate(text="抽取失败后又补记的内容", topic="misc", confidence=0.95)],
        extract_on_exit=True,
    )
    store = runtime.sessions
    session = store.create()
    store.append(session, {"type": "message", "id": new_message_id(), "role": "user", "content": "退出时该被抽取的话"})

    original = service.memorize

    def boom(*args, **kwargs):
        raise RuntimeError("API 挂了")

    service.memorize = boom
    runtime.resume(session.id)
    stats = runtime.close()
    assert stats["error"]
    types = [record["type"] for record in store.load(session.id).records]
    assert "extraction_error" in types and "closed" in types

    service.memorize = original
    runtime.start()
    result = runtime.consolidate_pending()
    assert result["admitted"] == 1
    assert service.store.has_text("抽取失败后又补记的内容")


def test_ask_streams_reply_deltas(tmp_path):
    model = FakeChatModel([AssistantTurn(content="你好世界")])
    runtime, _ = build_runtime(tmp_path, model)
    deltas: list[str] = []

    reply = runtime.ask("测试", on_delta=deltas.append)

    assert deltas == ["你好", "世界"]
    assert reply.content == "你好世界"
    # 会话记录仍是完整内容
    session = runtime.current
    assert any(
        record.get("type") == "assistant" and record.get("content") == "你好世界"
        for record in session.records
    )


def test_streaming_failure_does_not_retry_or_duplicate(tmp_path):
    # 回归：流中断后循环级重试会让界面收到重复 delta，必须单次直败。

    class StreamBoom(FakeChatModel):
        def chat(self, messages, tools, on_delta=None):
            self.calls.append((messages, tools))
            if on_delta is not None:
                on_delta("半截")
            raise RuntimeError("连接中断")

    model = StreamBoom([])
    runtime, _ = build_runtime(tmp_path, model)
    deltas: list[str] = []

    with pytest.raises(ChatModelError):
        runtime.ask("测试", on_delta=deltas.append)

    assert len(model.calls) == 1  # 未重试
    assert deltas == ["半截"]  # 已发内容不重复


def test_close_deferred_marks_session_for_catchup(tmp_path):
    # 切换会话走快速路径：不内联抽取，打 deferred 标记，由补沉淀拾取。
    model = FakeChatModel([])
    runtime, service = build_runtime(
        tmp_path,
        model,
        [Candidate(text="延后沉淀的内容", topic="misc", confidence=0.95)],
        extract_on_exit=True,
    )
    session = runtime.sessions.create()
    runtime.sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "说点要记住的"},
    )
    runtime.resume(session.id)

    runtime.close(distill=False)

    types = {record["type"] for record in runtime.sessions.load(session.id).records}
    assert "distill_deferred" in types and "closed" in types
    assert session.needs_consolidation()
    assert not service.store.has_text("延后沉淀的内容")  # 未内联抽取

    # 补沉淀排除当前活跃会话：脱离会话（如已切走/退出）后才拾取。
    runtime.start()
    result = runtime.consolidate_pending()
    assert result["admitted"] == 1
    assert service.store.has_text("延后沉淀的内容")

    # 游标回归：再次切走（无新消息）不重复打标记、不重复抽取
    session = runtime.resume(session.id)
    runtime.close(distill=False)
    assert not runtime.sessions.load(session.id).needs_consolidation()
    assert runtime.consolidate_pending()["scanned"] == 0


def test_consolidate_threshold_and_cursor_partition(tmp_path):
    # 回归（频繁切换场景）：小尾巴攒批不烧调用；重复关闭只抽取游标之后的新内容。
    calls: list[str] = []
    # 候选文本必须互斥：同主题 bigram 相似度过高会被裁决为冲突进队列
    words = ("oranges imported", "bananas yellow", "cherries red")

    class CountingExtractor:
        def extract(self, text, context=""):
            calls.append(text)
            from quiet_memo.memory.models import ExtractionResult

            return ExtractionResult(
                memories=[Candidate(text=words[len(calls) - 1], topic="misc", confidence=0.95)]
            )

    model = FakeChatModel([])
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        chat=ChatConfig(context_window=32000, extract_on_exit=True),
    )
    service = MemoryService(config, extractor=CountingExtractor())
    service.init_vault()
    runtime = AgentRuntime(service, model, SessionStore(config.vault), config=config.chat)
    runtime.start()
    session = runtime.sessions.create()
    for text in ("第一条", "第二条"):
        runtime.sessions.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": text},
        )
    session = runtime.resume(session.id)

    # 两条新消息 < 阈值 3：跳过、标记保留、零调用
    runtime.close(distill=False)
    assert session.needs_consolidation()
    runtime.start()
    assert runtime.consolidate_pending(min_new_messages=3)["consolidated"] == 0
    assert calls == []

    # 启动补记（阈值 1）：两条消息合并进同一次抽取调用
    assert runtime.consolidate_pending()["admitted"] == 1
    assert len(calls) == 1
    assert "第一条" in calls[0] and "第二条" in calls[0]

    # 游标回归：再新增一条后关闭，只抽新内容，不含已沉淀的旧消息
    session = runtime.resume(session.id)  # 重取活跃对象，避免过期引用
    runtime.sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "第三条"},
    )
    runtime.close(distill=False)
    runtime.start()
    assert runtime.consolidate_pending()["admitted"] == 1
    assert "第三条" in calls[1]
    assert "第一条" not in calls[1] and "第二条" not in calls[1]


def test_compact_tool_outputs_budget_ignores_system_and_history():
    from quiet_memo.agent.tools import compact_tool_outputs

    messages = [
        {"role": "system", "content": "系统提示" * 5000},
        {"role": "tool", "content": "a" * 400},
        {"role": "tool", "content": "b" * 400},
        {"role": "tool", "content": "c" * 400},
    ]

    # 系统提示再长也不该触发工具结果压缩；只有工具输出本身超预算才压缩。
    assert compact_tool_outputs(messages, max_tokens=1000) == 0
    assert compact_tool_outputs(messages, max_tokens=100) == 1
    assert "较早工具结果已压缩" in messages[1]["content"]


def test_tool_output_budget_scales_with_context_window(tmp_path, monkeypatch):
    captured = {}

    def fake_compact(messages, max_tokens=8000):
        captured["budget"] = max_tokens
        return 0

    monkeypatch.setattr("quiet_memo.agent.runtime.compact_tool_outputs", fake_compact)
    model = FakeChatModel(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="c1", name="recall", arguments={"query": "清淡"})]
            ),
            AssistantTurn(content="好"),
        ]
    )
    runtime, _ = build_runtime(tmp_path, model)
    runtime.config.context_window = 200000

    runtime.ask("测试")

    assert captured["budget"] == 50000


def test_consolidate_skips_active_session_even_when_scan_includes_it(tmp_path, monkeypatch):
    runtime, _ = build_runtime(tmp_path, FakeChatModel([]), extract_on_exit=True)
    store = runtime.sessions
    session = store.create()
    store.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "当前会话内容"},
    )
    runtime.resume(session.id)
    # 模拟启动扫描已经拿到候选，随后用户才切进该会话；补记不能在活跃会话上写入。
    monkeypatch.setattr(store, "unconsolidated", lambda *args, **kwargs: [session])

    result = runtime.consolidate_pending()

    assert result["scanned"] == 0
    assert result["consolidated"] == 0
