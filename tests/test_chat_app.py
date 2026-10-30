from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

from textual.widgets import Input, Static

from quiet_memo.agent.runtime import AgentRuntime
from quiet_memo.agent.session import SessionStore, new_message_id
from quiet_memo.app.chat_app import ChatApp, SidebarToggle
from quiet_memo.app.chat_input import BarInput
from quiet_memo.app.command_catalog import SLASH_COMMANDS
from quiet_memo.infra.config import ChatConfig, Config, LLMConfig, RetrievalConfig, SearchConfig
from quiet_memo.infra.tokens import format_tokens
from quiet_memo.llm import AssistantTurn
from quiet_memo.memory.extractor import StaticExtractor
from quiet_memo.memory.models import ExtractionResult
from quiet_memo.memory.service import MemoryService


class FakeChatModel:
    def __init__(self, turns):
        self.turns = list(turns)

    def chat(self, messages, tools, on_delta=None):
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


@pytest.mark.parametrize("apply", [False, True])
async def test_import_command_uses_worker_and_releases_busy(tmp_path, monkeypatch, apply):
    from quiet_memo.memory import service as module
    from quiet_memo.memory.models import Candidate

    class Extractor:
        def __init__(self, *args, **kwargs):
            pass

        def extract(self, text):
            return ExtractionResult(memories=[Candidate(text="合成用户偏好茶饮", confidence=0.95)])

    monkeypatch.setattr(module, "ImportExtractor", Extractor)
    path = tmp_path / "synthetic conversation.txt"
    path.write_text("合成聊天记录", encoding="utf-8")
    app = build_app(tmp_path, [])
    async with app.run_test() as pilot:
        app._command(f'/import "{path}"' + (" --apply" if apply else ""))
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        assert not app.busy
        assert bool(app.service.store.entries()) is apply
        assert not app.query_one("#input", Input).disabled


async def test_import_error_releases_busy(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test() as pilot:
        app._command(f'/import "{tmp_path / "missing.pdf"}"')
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        assert not app.busy
        assert any("导入失败" in str(card.render()) for card in app.query(".card"))


def build_app(tmp_path, turns, extract_on_exit=False, search=None):
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        retrieval=RetrievalConfig(method="keyword"),
        search=search or SearchConfig(auto_start=False),
        chat=ChatConfig(context_window=32000, auto_recall=3, recall_rewrite=False, extract_on_exit=extract_on_exit),
    )
    service = MemoryService(config, extractor=StaticExtractor(ExtractionResult(memories=[])))
    service.init_vault()
    runtime = AgentRuntime(
        service,
        FakeChatModel(turns),
        SessionStore(config.vault),
        summarizer=FakeSummarizer(),
        config=config.chat,
    )
    runtime.start()
    return ChatApp(runtime, service, SessionStore(config.vault))


async def _wait_reply(app, pilot, limit=200):
    # press 后消息处理是异步的：必须等回复真正挂载，不能只看 busy 标志。
    for _ in range(limit):
        await pilot.pause()
        # 回复块在流式中途就挂载，侧栏刷新发生在收尾阶段；必须等 busy 释放再断言。
        if app.query(".reply") and not app.busy:
            return
    raise AssertionError("等待回复超时")


async def _wait_context_usage(app, pilot):
    for _ in range(100):
        await pilot.pause()
        if "计算中" not in str(app.query_one("#status", Static).content):
            return
    raise AssertionError("等待后台上下文统计超时")


async def test_chat_roundtrip(tmp_path):
    app = build_app(tmp_path, [AssistantTurn(content="收到，已了解。")])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "你好，记住我喜欢喝茶"
        await pilot.press("enter")
        await _wait_reply(app, pilot)

        assert app.query(".bubble")  # 用户气泡已渲染
        assert app.query(".reply")  # 助手 Markdown 回复已渲染
        assert app.runtime.has_session
        assert app.query("SessionItem")  # 侧栏出现当前会话
        assert "Context" in str(app.query_one("#status", Static).content)


async def test_undo_restores_last_message_for_editing(tmp_path):
    app = build_app(tmp_path, [AssistantTurn(content="旧回答"), AssistantTurn(content="新回答")])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "原始提问"
        await pilot.press("enter")
        await _wait_reply(app, pilot)

        widget.value = "/undo"
        await pilot.press("enter")
        await pilot.pause()

        assert widget.value == "原始提问"
        assert widget.cursor_position == len("原始提问")
        assert not app.busy
        assert not app.query(".bubble")
        assert app.runtime.current.user_texts() == []
        assert any(record.get("type") == "undo" for record in app.runtime.current.records)

        widget.value = "编辑后的提问"
        await pilot.press("enter")
        await _wait_reply(app, pilot)

        assert app.runtime.current.user_texts() == ["编辑后的提问"]
        assert [getattr(reply, "_markdown", "") for reply in app.query(".reply")] == ["新回答"]
        raw_texts = [
            record["content"]
            for record in app.runtime.current.records
            if record.get("type") == "message" and record.get("role") == "user"
        ]
        assert raw_texts == ["原始提问", "编辑后的提问"]


@pytest.mark.parametrize("action", ["new", "resume"])
async def test_session_switch_drops_previous_context_usage(tmp_path, action):
    app = build_app(tmp_path, [AssistantTurn(content="合成回复")])
    target = app.sessions.create()
    app.sessions.append(target, {
        "type": "message", "id": new_message_id(), "role": "user", "content": "历史合成提问",
    })
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "新合成提问"
        await pilot.press("enter")
        await _wait_reply(app, pilot)
        assert app.runtime.last_plan is not None
        if action == "new":
            app._new_session()
        else:
            app._switch_session(target.id)
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        assert not app.busy
        assert app.runtime.last_plan is None
        await _wait_context_usage(app, pilot)
        status = str(app.query_one("#status", Static).content)
        if action == "new":
            assert "0/32K" in status
        else:
            # 恢复会话后不能沿用上一个会话的用量，但要显示目标会话自身的占用。
            assert "0/32K" not in status
            assert f"{format_tokens(app.runtime.context_usage())}/32K" in status
        # 重建 transcript 后焦点恢复可能晚一帧，busy 释放不等于 focus 已处理。
        for _ in range(50):
            await pilot.pause()
            if widget.has_focus:
                break
        assert widget.has_focus, app.focused
        if action == "new":
            assert not app.runtime.has_session
        else:
            assert app.runtime.current.id == target.id


async def test_status_bar_renders_context_usage(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        status = app.query_one("#status", Static)
        rendered = "".join(
            status.render_line(y).text for y in range(status.size.height)
        )

        assert "Context" in rendered
        assert "/32K" in rendered


async def test_resumed_session_shows_its_own_context_usage(tmp_path):
    app = build_app(tmp_path, [])
    target = app.sessions.create()
    app.sessions.append(
        target,
        {
            "type": "message",
            "id": new_message_id(),
            "role": "user",
            "content": "历史合成陈述" * 20,
        },
    )
    app.runtime.resume(target.id)

    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_context_usage(app, pilot)
        assert app.runtime.last_plan is None
        usage = app.runtime.context_usage()
        assert usage > 0
        assert f"{format_tokens(usage)}/32K" in str(
            app.query_one("#status", Static).content
        )


async def test_undo_recomputes_context_usage(tmp_path):
    app = build_app(tmp_path, [AssistantTurn(content="合成回复")])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "待撤销的合成陈述"
        await pilot.press("enter")
        await _wait_reply(app, pilot)
        assert app.runtime.last_plan is not None

        app.runtime.undo()
        app.update_status()
        await _wait_context_usage(app, pilot)

        assert app.runtime.last_plan is None
        usage = app.runtime.context_usage()
        assert usage > 0
        assert f"{format_tokens(usage)}/32K" in str(
            app.query_one("#status", Static).content
        )


async def test_status_context_usage_matches_last_plan(tmp_path):
    app = build_app(tmp_path, [AssistantTurn(content="合成回复")])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "合成提问"
        await pilot.press("enter")
        await _wait_reply(app, pilot)

        plan = app.runtime.last_plan
        assert plan is not None
        assert f"{format_tokens(plan.estimated_tokens)}/32K" in str(
            app.query_one("#status", Static).content
        )


async def test_history_pages_preserve_records_and_flat_reply_widgets(tmp_path):
    from quiet_memo.app.chat_widgets import ChatReply, HistoryMore, TranscriptView

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    for index in range(14):
        app.sessions.append(session, {"type": "message", "id": f"u{index}", "role": "user", "content": f"合成提问{index}"})
        app.sessions.append(session, {"type": "assistant", "id": f"a{index}", "content": f"# 合成回复{index}\n\n" + "- 合成条目\n" * 30})
    original = session.path.read_bytes()
    app.runtime.resume(session.id)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert len(app.query(ChatReply)) == TranscriptView.PAGE_SIZE // 2
        assert "合成提问0" not in str([widget.content for widget in app.query(".bubble")])
        while app.query(HistoryMore):
            app.query_one(HistoryMore).action_load()
            await pilot.pause()
        assert [str(widget.content) for widget in app.query(".bubble")] == [f"合成提问{i}" for i in range(14)]
        assert len(app.query(ChatReply)) == 14
        assert all(len(reply.query("*")) == 0 for reply in app.query(ChatReply))
    assert session.path.read_bytes() == original


@pytest.mark.parametrize("restored", [False, True])
async def test_assistant_markdown_can_be_selected_and_copied(tmp_path, restored):
    from quiet_memo.app.chat_widgets import ChatReply

    app = build_app(tmp_path, [AssistantTurn(content="**合成回复** sample")])
    if restored:
        session = app.sessions.create()
        app.sessions.append(session, {"type": "assistant", "content": "**合成回复** sample"})
        app.runtime.resume(session.id)
    async with app.run_test(size=(120, 40)) as pilot:
        if not restored:
            app.query_one("#input", Input).value = "合成提问"
            await pilot.press("enter")
            await _wait_reply(app, pilot)
        reply = app.query_one(ChatReply)
        await pilot.pause()
        await pilot.mouse_down(reply, offset=(0, 0))
        await pilot.hover(reply, offset=(7, 0))
        await pilot.mouse_up(reply, offset=(7, 0))
        await pilot.pause()
        assert app.screen.get_selected_text() == "合成回复 "
        await pilot.press("ctrl+c")
        assert app.clipboard == "合成回复 "
        assert not reply.query("*")


async def test_resume_session_does_not_show_success_notification(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    # 只观察恢复动作；启动补记线程可能稍后发出独立的自动提交通知。
    monkeypatch.setattr(app, "_run_catchup", lambda: None)
    session = app.sessions.create()
    app.sessions.append(session, {"type": "assistant", "content": "合成历史回复"})
    async with app.run_test(size=(120, 40)) as pilot:
        notifications = []
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append(message))
        app._switch_session(session.id)
        await _wait_reply(app, pilot)
        assert app.runtime.current.id == session.id
        assert not notifications
        assert not app.query_one("#input", Input).disabled


async def test_reply_selection_tracks_stream_updates_and_resize(tmp_path):
    from textual.selection import SELECT_ALL
    from quiet_memo.app.chat_widgets import ChatReply

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        app._stream_append("12:00", "**合成标题**\n\n")
        app._stream_flush()
        await pilot.pause()
        reply = app.query_one(ChatReply)
        assert "合成标题" in reply.get_selection(SELECT_ALL)[0]
        app._stream_append("12:00", "- 合成条目\n\n```python\nprint('sample')\n```\n\n" + "wrapped words " * 12)
        app._stream_flush()
        await pilot.pause()
        wide = reply.get_selection(SELECT_ALL)[0]
        assert "合成条目" in wide and "print('sample')" in wide
        assert "**" not in wide and "```" not in wide
        await pilot.resize_terminal(80, 40)
        await pilot.pause()
        narrow = reply.get_selection(SELECT_ALL)[0]
        assert narrow.count("wrapped") == 12
        assert len(narrow.splitlines()) > len(wide.splitlines())
        reply.text_select_all()
        await pilot.press("ctrl+c")
        assert app.clipboard == narrow


async def test_switch_clears_previous_history_page_state(tmp_path):
    from quiet_memo.app.chat_widgets import HistoryMore, TranscriptView

    app = build_app(tmp_path, [])
    first, second = app.sessions.create(), app.sessions.create()
    for index in range(22):
        app.sessions.append(first, {"type": "message", "id": f"m{index}", "role": "user", "content": f"旧会话{index}"})
    app.sessions.append(second, {"type": "message", "id": "new", "role": "user", "content": "新会话"})
    app.runtime.resume(first.id)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.query(HistoryMore)
        app._switch_session(second.id)
        for _ in range(100):
            await pilot.pause()
            if not app.busy and not app.query(HistoryMore):
                break
        assert not app.query(HistoryMore)
        app.query_one(TranscriptView).load_earlier()
        assert [str(widget.content) for widget in app.query(".bubble")] == ["新会话"]


async def test_context_estimation_uses_worker_cache_and_rejects_stale_results(tmp_path, monkeypatch):
    import asyncio
    import threading

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.append(session, {"type": "message", "id": "m1", "role": "user", "content": "合成陈述"})
    entered, release = threading.Event(), threading.Event()
    calls = []
    main_thread = threading.get_ident()

    def estimate(snapshot, prompt):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return 1234

    monkeypatch.setattr(app.runtime.context, "estimate_usage", estimate)
    async with app.run_test(size=(120, 40)) as pilot:
        app.runtime.resume(session.id)
        try:
            app.update_status()
            assert await asyncio.to_thread(entered.wait, 3)
            for _ in range(4):
                app.update_status()
            assert len(calls) == 1 and calls[0] != main_thread
            assert not app.query_one("#input", Input).disabled
            app.runtime.start()
            app.update_status()
        finally:
            release.set()
        await pilot.pause()
        assert "0/32K" in str(app.query_one("#status", Static).content)
        app.runtime.resume(session.id)
        app.update_status()
        for _ in range(50):
            await pilot.pause()
            if app._usage_request is None:
                break
        assert f"{format_tokens(1234)}/32K" in str(app.query_one("#status", Static).content)
        before = len(calls)
        for _ in range(4):
            app.update_status()
        await pilot.pause()
        assert len(calls) == before
        app.sessions.append(app.runtime.current, {"type": "undo", "message_id": "m1"})
        app.update_status()
        for _ in range(50):
            await pilot.pause()
            if len(calls) == before + 1 and app._usage_request is None:
                break
        assert len(calls) == before + 1


async def test_context_estimation_failure_keeps_input_enabled(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.append(session, {"type": "message", "id": "m1", "role": "user", "content": "合成陈述"})
    app.runtime.resume(session.id)
    calls = []

    def fail(*args):
        calls.append(1)
        raise RuntimeError("合成统计失败")

    monkeypatch.setattr(app.runtime.context, "estimate_usage", fail)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert "估算失败" in str(app.query_one("#status", Static).content)
        assert not app.query_one("#input", Input).disabled
        app.update_status()
        await pilot.pause()
        assert len(calls) == 1


async def test_slash_help_card(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/help"
        await pilot.press("enter")
        await pilot.pause()

        cards = app.query(".card")
        assert cards
        assert "Commands" in str(cards[-1].content)


async def test_command_palette_filter_navigate_execute(tmp_path):
    from quiet_memo.app.chat_app import CommandPalette

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/th"  # 触发 Changed -> 面板打开
        await pilot.pause()

        palette = app.query_one("#cmd-palette", CommandPalette)
        assert palette.display
        assert [m[0] for m in palette.matches] == ["think"]
        assert palette.current == "think"

        await pilot.press("enter")  # 执行高亮命令
        await pilot.pause()
        assert not palette.display
        # /think 要么弹出选择器，要么提示模型不支持；两者都说明命令已执行。
        assert app.query(".card") or app.screen.__class__.__name__ in {"PickScreen", "TextInputScreen"}
        assert widget.value == ""


async def test_command_palette_escape_closes(tmp_path):
    from quiet_memo.app.chat_app import CommandPalette

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/"
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        assert palette.display and len(palette.matches) == len(SLASH_COMMANDS)

        await pilot.press("escape")
        await pilot.pause()
        assert not palette.display


@pytest.mark.parametrize("prefix", ["/", "/rename"])
async def test_command_palette_sits_above_input(tmp_path, prefix):
    from quiet_memo.app.chat_app import CommandPalette

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = prefix
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        assert palette.display
        assert palette.region.bottom <= widget.region.y, (
            f"palette={palette.region} input={widget.region}"
        )
        assert palette.region.x == widget.region.x, (
            f"palette={palette.region} input={widget.region}"
        )


async def test_command_palette_columns_align(tmp_path):
    from quiet_memo.app.chat_app import CommandPalette
    from quiet_memo.app.chat_widgets import CommandRow

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#input", Input).value = "/"
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        rows = list(palette.query(CommandRow))
        assert len(rows) == len(SLASH_COMMANDS)

        starts = set()
        for row, (_, usage, description) in zip(rows, SLASH_COMMANDS):
            plain = row.render().plain
            # [refresh] 等方括号必须原样可见，不能被当成标记吞掉
            assert usage in plain
            starts.add(plain.index(description))
        assert len(starts) == 1


async def test_rename_command_prompts_for_title(tmp_path):
    from quiet_memo.app.chat_widgets import TextInputScreen

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.rename(session, "旧标题")
    app.runtime.resume(session.id)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", Input)
        widget.value = "/rename"
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, TextInputScreen):
                break
        assert isinstance(app.screen, TextInputScreen)
        title_input = app.screen.query_one("#pick-input", Input)
        assert title_input.value == "旧标题"

        title_input.value = "新标题"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if app.runtime.current.title() == "新标题":
                break
        assert app.runtime.current.title() == "新标题"
        assert app.sessions.load(session.id).title() == "新标题"


async def test_model_picker_can_configure_provider_key(tmp_path, monkeypatch):
    import sys
    import types

    from textual.widgets import Label, ListView

    from quiet_memo.app.chat_widgets import PickScreen

    keys: dict[str, str] = {}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service_name, name: keys.get(name)
    fake.set_password = lambda service_name, name, value: keys.__setitem__(name, value)
    monkeypatch.setitem(sys.modules, "keyring", fake)

    app = build_app(tmp_path, [])
    app.service.config_path = tmp_path / "quiet-memo.toml"
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/model"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            # 等模态挂载完成并拿到焦点，再改高亮；否则 on_mount 可能重置 index。
            if isinstance(app.screen, PickScreen) and app.screen.query("#pick-list"):
                if app.screen.query_one("#pick-search", Input).has_focus:
                    break

        model_picker = app.screen
        listing = model_picker.query_one("#pick-list", ListView)
        labels = [str(item.query_one(Label).render()) for item in listing.children]
        assert labels
        assert all(not item.value[0].startswith("__") for item in listing.children)
        assert "Ctrl+P" in str(model_picker.query_one(".pick-hint").render())
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert "供应商管理" in str(app.screen.query_one(".pick-title").render())
        listing = app.screen.query_one("#pick-list", ListView)
        assert [item.value for item in listing.children] == ["add", "update", "remove"]
        listing.index = 0
        listing.focus()
        await pilot.press("enter")
        # 第一个选择器 dismiss 与下一个 push 不在同一帧，等新屏幕出现再查询。
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, PickScreen) and app.screen is not model_picker:
                if app.screen.query(".pick-title"):
                    break
        assert "Add provider" in str(app.screen.query_one(".pick-title").render())

        providers = app.screen.query_one("#pick-list", ListView)
        labels = [str(item.query_one(Label).render()) for item in providers.children]
        providers.index = next(i for i, label in enumerate(labels) if label.startswith("SiliconFlow"))
        providers.focus()
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            if app.screen.__class__.__name__ == "TextInputScreen":
                break

        key_input = app.screen.query_one("#pick-input", Input)
        assert key_input.password
        key_input.value = "sk-test"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            if app.service.config.llm.provider == "siliconflow" and not app.busy:
                break

        assert keys["SILICONFLOW_API_KEY"] == "sk-test"
        assert app.service.config.llm.model == "deepseek-ai/DeepSeek-V3"
        assert not app.busy


async def test_model_picker_can_remove_provider_key(tmp_path, monkeypatch):
    import sys
    import types

    from textual.widgets import Label, ListView

    from quiet_memo.app.chat_widgets import PickScreen

    keys = {"SILICONFLOW_API_KEY": "sk-test"}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service_name, name: keys.get(name)
    fake.delete_password = lambda service_name, name: keys.pop(name, None)
    monkeypatch.setitem(sys.modules, "keyring", fake)

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._show_model_picker(app._model_entries())
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        listing = app.screen.query_one("#pick-list", ListView)
        listing.index = 2
        listing.focus()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, PickScreen) and "Remove provider" in str(
                app.screen.query_one(".pick-title").render()
            ):
                break

        providers = app.screen.query_one("#pick-list", ListView)
        labels = [str(item.query_one(Label).render()) for item in providers.children]
        providers.index = next(i for i, label in enumerate(labels) if label.startswith("SiliconFlow"))
        providers.focus()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, PickScreen) and "Remove SiliconFlow provider?" in str(
                app.screen.query_one(".pick-title").render()
            ):
                break

        confirm = app.screen.query_one("#pick-list", ListView)
        confirm.index = 0
        confirm.focus()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if app.query(".card"):
                break

        assert "SILICONFLOW_API_KEY" not in keys
        assert "provider removed" in str(app.query(".card")[-1].render())


async def test_model_picker_provider_menu_returns_to_models(tmp_path, monkeypatch):
    from textual.widgets import ListView
    from quiet_memo.app.chat_widgets import ModelPickerScreen

    monkeypatch.setattr("quiet_memo.app.chat_commands.fetch_provider_models", lambda *args, **kwargs: [])
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        app._show_model_picker(app._model_entries())
        await pilot.pause()
        picker = app.screen
        picker.query_one("#pick-search", Input).value = "no-matching-model"
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert not isinstance(app.screen, ModelPickerScreen)
        assert [item.value for item in app.screen.query_one("#pick-list", ListView).children] == ["add", "update", "remove"]
        app._apply_fetched_models(picker, [("late-model", ("deepseek", "late"), False)])
        assert [item.value for item in app.screen.query_one("#pick-list", ListView).children] == ["add", "update", "remove"]
        await pilot.press("escape")
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, ModelPickerScreen) and app.screen.query("#pick-search"):
                break
        assert isinstance(app.screen, ModelPickerScreen)
        assert not app.busy


@pytest.mark.parametrize("shortcut,sentinel", [("ctrl+r", "__refresh__"), ("ctrl+n", "__custom__")])
async def test_model_picker_shortcuts_work_from_search(tmp_path, monkeypatch, shortcut, sentinel):
    selected = []
    app = build_app(tmp_path, [])
    monkeypatch.setattr(app, "_model_picked", selected.append)
    async with app.run_test(size=(120, 40)) as pilot:
        app._show_model_picker(app._model_entries())
        await pilot.pause()
        assert app.screen.query_one("#pick-search", Input).has_focus
        await pilot.press(shortcut)
        await pilot.pause()
        assert selected == [(sentinel, "")]


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_model_picker_keeps_shortcut_hint_visible(tmp_path, size):
    app = build_app(tmp_path, [])
    entries = [(f"model-{i}", ("deepseek", f"model-{i}"), i == 0) for i in range(50)]
    async with app.run_test(size=size) as pilot:
        app._show_model_picker(entries)
        await pilot.pause()
        hint = app.screen.query_one(".pick-hint", Static)
        card = app.screen.query_one("#pick-card")
        assert hint.region.bottom <= card.content_region.bottom
        assert hint.region.height > 0
        assert "Ctrl+P" in str(hint.render())
        assert card.region.bottom <= app.screen.region.bottom


async def test_ctrl_p_outside_model_picker_keeps_command_palette(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "CommandPalette"


async def test_model_picker_filters_by_search(tmp_path):
    from textual.widgets import ListView

    from quiet_memo.app.chat_widgets import PickScreen

    app = build_app(tmp_path, [])
    entries = [
        ("DeepSeek · DeepSeek Flash", ("deepseek", "deepseek-flash"), True),
        ("SiliconFlow · DeepSeek V3", ("siliconflow", "deepseek-ai/DeepSeek-V3"), False),
        ("Spark · Spark X2.6", ("spark", "spark-x2.6"), False),
    ]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._show_model_picker(entries)
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, PickScreen)
        search = picker.query_one("#pick-search", Input)
        assert search.has_focus

        search.value = "silicon"
        await pilot.pause()

        items = list(picker.query("#pick-list OptionItem"))
        assert [item.label for item in items] == ["SiliconFlow · DeepSeek V3"]
        listing = picker.query_one("#pick-list", ListView)
        assert listing.index == 0

        await pilot.press("down")
        await pilot.pause()
        assert listing.has_focus


async def test_model_picker_uses_cache_and_refreshes_in_background(tmp_path, monkeypatch):
    import threading

    from quiet_memo.app.chat_widgets import PickScreen

    release = threading.Event()

    def fake_fetch(config, refresh=False):
        release.wait(5)
        return ["fresh-model"]

    monkeypatch.setattr(
        "quiet_memo.app.chat_commands.cached_provider_models",
        lambda config: ["cached-model"],
    )
    monkeypatch.setattr(
        "quiet_memo.app.chat_commands.fetch_provider_models", fake_fetch
    )

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._open_models()
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, PickScreen):
                break

        picker = app.screen
        assert isinstance(picker, PickScreen)
        assert not app.busy
        for _ in range(50):
            await pilot.pause()
            labels = [item.label for item in picker.query("#pick-list OptionItem")]
            if labels:
                break
        assert any("cached-model" in label for label in labels)
        assert not any("fresh-model" in label for label in labels)

        release.set()
        for _ in range(100):
            await pilot.pause()
            labels = [item.label for item in picker.query("#pick-list OptionItem")]
            if any("fresh-model" in label for label in labels):
                break
        assert any("fresh-model" in label for label in labels)


async def test_ctrl_b_toggles_sidebar(tmp_path):
    from textual.containers import Vertical

    from quiet_memo.app.chat_app import SidebarToggle

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        sidebar = app.query_one("#sidebar", Vertical)
        expand = app.query_one("#show-sidebar", SidebarToggle)
        assert sidebar.display and not expand.display
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert not sidebar.display and expand.display
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert sidebar.display and not expand.display

        # 点侧栏右上角 « 隐藏；对话区左上角 » 恢复
        await pilot.click("#hide-sidebar")
        await pilot.pause()
        assert not sidebar.display and expand.display
        await pilot.click("#show-sidebar")
        await pilot.pause()
        assert sidebar.display and not expand.display


async def test_main_focus_stays_on_chat_input(tmp_path):
    from textual.containers import Vertical
    from textual.widgets import ListView

    app = build_app(tmp_path, [])
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", Input)
        sessions = app.query_one("#sessions", ListView)
        assert sessions.can_focus is False
        assert widget.has_focus

        await pilot.press("tab")
        await pilot.pause()
        assert widget.has_focus

        await pilot.press("shift+tab")
        await pilot.pause()
        assert widget.has_focus

        app._set_sidebar_visible(False)
        app.show_sidebar()
        await pilot.pause()
        assert widget.has_focus
        assert app.query_one("#sidebar", Vertical).display


async def test_clicking_chat_keeps_input_focus(tmp_path):
    from textual.containers import VerticalScroll

    app = build_app(tmp_path, [])
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.add_assistant_turn("点击正文不应该抢走输入焦点", "12:00")
        await pilot.pause()
        widget = app.query_one("#input", Input)
        chat = app.query_one("#chat", VerticalScroll)
        assert chat.can_focus is False
        assert widget.has_focus

        await pilot.click(chat)
        await pilot.pause()
        assert widget.has_focus

        await pilot.click(".reply")
        await pilot.pause()
        assert widget.has_focus


async def test_sidebar_rows_align_with_new_session(tmp_path):
    # 回归：New session 与会话列表必须同宽同起点；列表滚动时也不能缩窄。
    app = build_app(tmp_path, [])
    store = app.sessions
    for index in range(12):
        session = store.create()
        store.rename(session, f"会话 {index}")
        store.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": f"内容 {index}"},
        )
    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(60):
            await pilot.pause()
            if app.query("#sessions > ListItem"):
                break
        new_session = app.query_one("#new-session").region
        rows = [
            item.region
            for item in app.query("#sessions > ListItem")
            if hasattr(item, "row")
        ]
        assert rows
        assert all(row.x == new_session.x and row.width == new_session.width for row in rows)


async def test_sidebar_items_are_compact_and_clickable(tmp_path):
    from quiet_memo.agent.session import new_message_id
    from quiet_memo.app.chat_app import SessionItem

    app = build_app(tmp_path, [])
    ids = []
    for title in ("会话甲", "会话乙"):
        session = app.sessions.create()
        app.sessions.rename(session, title)
        app.sessions.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": title},
        )
        ids.append(session.id)
    async with app.run_test(size=(100, 30)) as pilot:
        # 等启动补沉淀（含自动提交）结束再操作列表，避免列表刷新吞掉点击
        for _ in range(200):
            await pilot.pause()
            if not app.busy:
                break
        await pilot.pause()
        items = list(app.query(SessionItem))
        assert len(items) >= 2
        # 回归：ListItem 内的 Vertical 默认 1fr 会把条目撑满列表导致命中错乱
        assert all(item.region.height <= 3 for item in items)

        second = items[1]
        await pilot.click(second)
        for _ in range(100):
            await pilot.pause()
            if not app.busy and app.runtime.has_session:
                break
        assert app.runtime.current.id == second.row["id"]
        assert app.query_one("#input", Input).has_focus


async def test_compact_failure_releases_busy(tmp_path):
    # 回归：force_compact 抛异常时输入框必须恢复可用，而不是永久禁用。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        def boom():
            raise RuntimeError("persona broken")

        app.runtime.force_compact = boom
        app._run_compact()
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        assert app.busy is False
        input_widget = app.query_one("#input", Input)
        assert input_widget.disabled is False
        assert "Compaction failed" in str(app.query(".card")[-1].render())


async def test_session_switch_failure_releases_busy(tmp_path):
    # 回归：resume 抛异常（会话文件损坏等）时同样不能卡死输入框。
    app = build_app(tmp_path, [])
    app.runtime.config.extract_on_exit = True
    session = app.runtime.ensure_session()
    app.runtime.sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "合成陈述"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        def boom(session_id, **kwargs):
            raise KeyError("会话不存在：s-xxx")

        app.runtime.resume = boom
        app._switch_session("s-20260101-000000-dead")
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        assert app.busy is False
        assert app.query_one("#input", Input).disabled is False
        assert "Session switch failed" in str(app.query(".card")[-1].render())
        assert app.runtime.current is session
        app.runtime.close()
        assert any(record.get("type") == "distilled" for record in session.records)


async def test_resuming_torn_session_shows_recovery_notice_and_continues(tmp_path):
    app = build_app(tmp_path, [AssistantTurn(content="接续成功")])
    session = app.runtime.ensure_session()
    app.runtime.sessions.append(session, {
        "type": "message", "id": new_message_id(), "role": "user", "content": "合成历史",
    })
    with session.path.open("ab") as handle:
        handle.write(b'{"type":"assistant","content":"')
    original = session.path.read_bytes()
    app.runtime.resume(session.id)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert any("会话末行不完整" in str(card.render()) for card in app.query(".card"))
        widget = app.query_one("#input", Input)
        widget.value = "接续提问"
        await pilot.press("enter")
        await _wait_reply(app, pilot)
        assert session.path.read_bytes().startswith(original)
        assert app.sessions.load(session.id).user_texts() == ["合成历史", "接续提问"]


async def test_session_switch_updates_sidebar_without_reload(tmp_path, monkeypatch):
    from quiet_memo.app.chat_app import SessionItem

    app = build_app(tmp_path, [])
    store = app.sessions
    ids = []
    for title in ("会话甲", "会话乙"):
        session = store.create()
        store.rename(session, title)
        store.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": title},
        )
        ids.append(session.id)

    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(100):
            await pilot.pause()
            if len(list(app.query(SessionItem))) >= 2:
                break

        calls = []
        original_list = store.list

        def counted_list(*args, **kwargs):
            calls.append(1)
            return original_list(*args, **kwargs)

        monkeypatch.setattr(store, "list", counted_list)
        app._switch_session(ids[0])
        for _ in range(100):
            await pilot.pause()
            if not app.busy and app.runtime.has_session:
                break
        await pilot.pause()

        assert calls == []
        current = [item for item in app.query(SessionItem) if item.has_class("current")]
        assert [item.row["id"] for item in current] == [ids[0]]


async def test_delete_session_uses_captured_row(tmp_path):
    # 回归：确认弹窗期间列表被刷新时，仍删除弹窗捕获的那条会话（不读高亮项）。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from quiet_memo.agent.session import new_message_id

        # 待删目标是非当前会话；再留一个有消息的会话撑住列表渲染。
        store = app.sessions
        session = store.create()
        store.rename(session, "待删除会话")
        store.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": "待删除会话"},
        )
        survivor = store.create()
        store.rename(survivor, "保留会话")
        store.append(
            survivor,
            {"type": "message", "id": new_message_id(), "role": "user", "content": "保留会话"},
        )
        await pilot.pause()
        app.refresh_sessions()
        row = {"id": session.id, "title": "待删除会话"}

        app.confirm_delete_session(row)
        await pilot.pause()
        # 模态打开期间模拟列表刷新（如后台回复完成后的 refresh_sessions）。
        app.refresh_sessions()
        # 走真实模态交互：高亮默认在第一项"确认删除"上，Enter 触发回调。
        assert "cannot be undone" in str(app.screen.query_one(".pick-title").render())
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            if not store.exists(row["id"]):
                break

        assert not store.exists(session.id)
        assert store.exists(survivor.id)
        from quiet_memo.app.chat_app import SessionItem

        # 文件删除先于 DOM 异步清理完成；等到旧节点移除、列表收敛后再断言。
        remaining = []
        for _ in range(100):
            await pilot.pause()
            remaining = [item.row["id"] for item in app.query(SessionItem)]
            if remaining == [survivor.id]:
                break
        assert remaining == [survivor.id]


async def test_delete_confirmation_rechecks_current_session(tmp_path):
    # 回归：弹窗期间目标变成当前会话时，确认也不能删除它。
    app = build_app(tmp_path, [])
    store = app.sessions
    session = store.create()
    store.rename(session, "目标会话")
    store.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "内容"},
    )
    row = {"id": session.id, "title": "目标会话"}

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.confirm_delete_session(row)
        await pilot.pause()
        app.runtime.resume(session.id)
        app._delete_session_result(row, True)
        await pilot.pause()

        assert store.exists(session.id)


async def test_model_picker_drains_pending_session_intent(tmp_path):
    # 回归：模型列表加载期间排队的会话意图，在释放 busy 时必须接续。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._pending_new = True
        app._show_model_picker([])
        await pilot.pause()

        assert app._pending_new is False


async def test_context_custom_rejects_unicode_digits(tmp_path):
    # 回归："①".isdigit() 为 True 但 int() 抛错；现在应给出错误卡片而不是崩进事件循环。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._context_custom("①")
        await pilot.pause()
        assert "positive integer" in str(app.query(".card")[-1].render())
        app._context_custom("16x")
        await pilot.pause()
        assert "positive integer" in str(app.query(".card")[-1].render())


async def test_context_custom_does_not_send_value_as_chat_message(tmp_path):
    from textual.widgets import Label, ListView

    from quiet_memo.app.chat_widgets import PickScreen, TextInputScreen

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/context"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, PickScreen):
                break

        listing = app.screen.query_one(ListView)
        labels = [str(item.query_one(Label).render()) for item in listing.children]
        listing.index = labels.index("Custom…")
        listing.focus()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, TextInputScreen):
                break

        field = app.screen.query_one("#pick-input", Input)
        field.value = "400000"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            if not isinstance(app.screen, TextInputScreen) and not app.busy:
                break

        assert app.service.config.chat.context_window == 400_000
        assert app.runtime.has_session is False
        assert not app.query(".bubble")


def test_slash_commands_cover_aliases():
    # 回归：面板必须列出 _command 实际支持的别名，/qu、/mo 前缀过滤要有提示。
    commands = {command for command, _, _ in SLASH_COMMANDS}
    assert "quit" in commands and "model" in commands


async def test_session_picker_focus_bar_follows_selection(tmp_path):
    # 回归：左侧竖线要跟着焦点走，同时所有行文字保持同一列。
    from textual.color import Color
    from textual.widgets import ListView

    from quiet_memo.app.chat_widgets import SessionPickerScreen

    app = build_app(tmp_path, [])
    store = app.sessions
    ids = []
    for title in ("当前会话", "另一条会话"):
        session = store.create()
        store.rename(session, title)
        store.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": title},
        )
        ids.append(session.id)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.runtime.resume(ids[0])
        app.open_session_picker()
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, SessionPickerScreen)
        rows = list(picker.query("#picker-list SessionItem"))
        assert len(rows) == 2
        current = next(row for row in rows if row.has_class("current"))
        other = next(row for row in rows if not row.has_class("current"))

        listing = picker.query_one("#picker-list", ListView)
        listing.index = rows.index(other)
        await pilot.pause()

        assert other.has_class("-highlight")
        assert other.styles.border_left[1] == Color.parse("#d2d3d7")  # 焦点强调色
        assert current.styles.border_left[1] == Color.parse("#96979c")  # 当前会话弱色
        assert len({row.query_one(".session-title").region.x for row in rows}) == 1


async def test_startup_commits_dirty_vault_without_crashed_sessions(tmp_path):
    # 回归：没有漏沉淀会话时，启动检查仍应提交未保存的 vault 变更。
    import asyncio

    app = build_app(tmp_path, [])
    app.service.note("启动前未提交的变更", topic="misc")
    async with app.run_test(size=(120, 40)) as pilot:
        # 先等待启动 worker 完成，再验证落盘结果，避免 Git 用时随负载波动。
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert app.busy is False
        assert not app.service.repo.has_changes()


async def test_chat_scrollbar_is_thin(tmp_path):
    from textual.containers import Vertical, VerticalScroll

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        chat = app.query_one("#chat", VerticalScroll)
        assert chat.styles.scrollbar_size_vertical == 1
        # 滚动条要贴着窗口右缘，而不是停在对话区右侧留白里。
        assert chat.region.right == app.query_one("#main", Vertical).region.right
        assert chat.styles.padding.right == 0


async def test_input_hint_uses_border_not_text(tmp_path):
    # 回归：提示词留在文字区会和中文输入法的组字内容重叠。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", BarInput)
        assert widget.placeholder == ""
        assert widget.border_title == "Message or /command"
        assert "Message or /command" not in widget.render_line(0).text

        widget.value = "你好"
        await pilot.pause()
        assert widget.border_title is None
        assert "Message or /command" not in widget.render_line(0).text

        widget.value = ""
        await pilot.pause()
        assert widget.border_title == "Message or /command"


async def test_input_cursor_is_vertical_bar(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", Input)
        assert isinstance(widget, BarInput)
        assert "│" in widget.render_line(0).text
        assert not widget.get_component_rich_style("input--cursor").underline

        widget.value = "测试"
        await pilot.pause()
        rendered = widget.render_line(0).text
        assert "│" in rendered
        assert "测试" in rendered
        # 光标只在渲染层插入；写进 _value 会让 Textual 把输入背景刷到左侧。
        assert "│" not in widget._value.plain

        widget.blur()
        await pilot.pause()
        assert "│" not in widget.render_line(0).text


async def test_auto_start_warms_search_in_background(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "quiet_memo.app.chat_app.ensure_search_daemon",
        lambda base_url: calls.append(base_url) or "running",
    )
    app = build_app(
        tmp_path,
        [],
        search=SearchConfig(enabled=True, auto_start=True),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(100):
            await pilot.pause()
            if calls:
                break
        assert calls == ["http://127.0.0.1:3000"]
        assert app.busy is False


async def test_new_session_click_queues_while_busy(tmp_path):
    # 回归：忙碌期间点击新会话不能被静默丢弃。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._set_busy(True)
        await pilot.click("#new-session")

        assert app._pending_new is True

        app._set_busy(False)
        app._drain_pending_intent()
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        assert app._pending_new is False
        assert app.busy is False


async def test_startup_catchup_shows_card_for_crashed_session(tmp_path):
    # 回归：启动检查发现异常终止的会话时，补记并显示卡片；无欠账时保持沉默。
    import asyncio

    app = build_app(tmp_path, [AssistantTurn(content="好")], extract_on_exit=True)
    crashed = app.sessions.create()
    app.sessions.append(
        crashed,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "中断前说的话"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert app.busy is False
        assert app.query_one("#input", Input).disabled is False
        cards = [str(widget.render()) for widget in app.query(".card")]
        assert any("Consolidated 1 unfinished session(s)" in card for card in cards)
        assert app.runtime.consolidate_pending()["scanned"] == 0  # 已标记，不会重复


async def test_pending_interactive_approve(tmp_path):
    # 回归：/pending 必须能在界面内完成确认闭环（issue #1）。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from quiet_memo.memory.models import Candidate, PendingItem

        item = PendingItem.from_candidate(
            Candidate(text="低置信的偏好内容", topic="preferences", confidence=0.5), "chat", "low confidence"
        )
        app.service.queue.add(item)
        app.refresh_pending()

        widget = app.query_one("#input", Input)
        widget.value = "/pending"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if app.screen.__class__.__name__ == "PickScreen":
                break
        await pilot.press("enter")  # 选中第一条 → 动作菜单
        for _ in range(50):
            await pilot.pause()
            if len(app.screen_stack) >= 2 and app.screen.__class__.__name__ == "PickScreen":
                break
        await pilot.press("enter")  # 高亮"确认写入"
        for _ in range(100):
            await pilot.pause()
            if not app.service.pending():
                break
        assert not app.service.pending()
        assert app.service.store.has_text("低置信的偏好内容")
        cards = [str(w.render()) for w in app.query(".card")]
        assert any("Saved" in c for c in cards)


async def test_pending_interactive_edit_approves_only_final_text(tmp_path):
    from textual.widgets import ListView

    from quiet_memo.app.chat_widgets import PickScreen, TextInputScreen
    from quiet_memo.memory.models import Candidate, PendingItem

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        item = PendingItem.from_candidate(
            Candidate(text="可能喜欢浅烘", topic="preferences", confidence=0.5),
            "chat", "low confidence",
        )
        app.service.queue.add(item)
        app.refresh_pending()

        widget = app.query_one("#input", Input)
        widget.value = "/pending"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, PickScreen) and app.screen.query_one(ListView).has_focus:
                break
        picker = app.screen
        assert isinstance(picker, PickScreen)
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, PickScreen) and app.screen is not picker:
                if app.screen.query_one(ListView).has_focus:
                    break
        assert isinstance(app.screen, PickScreen) and app.screen is not picker
        await pilot.press("down")
        listing = app.screen.query_one(ListView)
        assert listing.highlighted_child.label == "Edit and approve"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, TextInputScreen):
                if app.screen.query_one("#pick-input", Input).has_focus:
                    break
        assert isinstance(app.screen, TextInputScreen)
        field = app.screen.query_one("#pick-input", Input)
        assert field.value == item.text
        field.value = "喜欢中烘咖啡"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            if not app.service.pending() and widget.has_focus and not app.busy:
                break

        entries = app.service.store.entries(include_inactive=True)
        assert [(entry.text, entry.status, entry.supersedes) for entry in entries] == [
            ("喜欢中烘咖啡", "active", None)
        ]
        assert app.service.pending() == []
        assert app.runtime.has_session is False
        assert not app.query(".bubble")
        assert not app.query(".reply")
        assert not app.busy
        assert widget.value == ""
        assert widget.has_focus
        cards = [str(card.render()) for card in app.query(".card")]
        assert any("Saved" in card and "喜欢中烘咖啡" in card for card in cards)


async def test_topics_command_lists_and_drills(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.service.note("喝咖啡偏好清淡", topic="preferences", title="偏好")
        app._show_topics()
        await pilot.pause()
        cards = [str(w.render()) for w in app.query(".card")]
        assert any("preferences" in c and "Memory topics" in c for c in cards)
        app._show_topics("preferences")
        await pilot.pause()
        cards = [str(w.render()) for w in app.query(".card")]
        assert any("喝咖啡偏好清淡" in c for c in cards)


async def test_streaming_roundtrip_renders_single_reply(tmp_path):
    # 回归（issue #4）：流式增量渲染只挂载一个回复块，完成后不重复。
    app = build_app(tmp_path, [AssistantTurn(content="流式回复全文")])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "测流式"
        await pilot.press("enter")
        for _ in range(200):
            await pilot.pause()
            if not app.busy:
                break
        assert app._stream_widget is None  # 收尾已清
        assert len(app.query(".reply")) == 1
        assert len(app.query(".meta")) == 1
        session = app.runtime.current
        assert any(
            record.get("type") == "assistant" and record.get("content") == "流式回复全文"
            for record in session.records
        )


async def test_streaming_keeps_text_before_tool_calls(tmp_path):
    # 回归：工具调用前的流式正文不能被最终回复覆盖，事件顺序保持生成顺序。
    from quiet_memo.llm import ToolCall

    app = build_app(
        tmp_path,
        [
            AssistantTurn(
                content="我先查一下",
                tool_calls=[ToolCall(id="c1", name="recall", arguments={"query": "清淡"})],
            ),
            AssistantTurn(content="最终回答"),
        ],
    )
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "测试工具前文本"
        await pilot.press("enter")
        for _ in range(300):
            await pilot.pause()
            if not app.busy:
                break

        children = list(app._chat().children)
        contents = [getattr(child, "_markdown", "") for child in children]
        first = next(i for i, text in enumerate(contents) if "我先查一下" in text)
        final = next(i for i, text in enumerate(contents) if "最终回答" in text)
        event = next(i for i, child in enumerate(children) if "event-card" in child.classes)
        assert first < event < final


async def test_tool_event_rows_use_single_border_and_assistant_is_qmo(tmp_path):
    # 事件行由 CSS 左边框画一条竖线，正文里不再拼前缀；助手标签统一为 qmo。
    from quiet_memo.llm import ToolCall

    app = build_app(
        tmp_path,
        [
            AssistantTurn(
                content="",
                tool_calls=[ToolCall(id="c1", name="recall", arguments={"query": "清淡"})],
            ),
            AssistantTurn(content="最终回答"),
        ],
    )
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "测试事件样式"
        await pilot.press("enter")
        for _ in range(300):
            await pilot.pause()
            if not app.busy:
                break

        events = list(app.query(".event-card"))
        assert events
        assert all("┃" not in str(event.render()) for event in events)
        labels = [str(label.render()) for label in app.query(".who-ai")]
        assert labels and all(label.startswith("qmo") for label in labels)


async def test_streaming_error_marks_interruption(tmp_path):
    # 回归（issue #4）：中途断流保留半截内容并显式标注，不静默吞掉。
    app = build_app(tmp_path, [])
    from quiet_memo.llm import ChatModelError

    class BoomModel:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, tools, on_delta=None):
            self.calls += 1
            if on_delta is not None:
                on_delta("生成到一半")
            raise ChatModelError("连接中断")

    app.runtime.model = BoomModel()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        widget = app.query_one("#input", Input)
        widget.value = "测试中断"
        await pilot.press("enter")
        for _ in range(200):
            await pilot.pause()
            if not app.busy:
                break
        assert app.query(".reply")
        cards = [str(w.render()) for w in app.query(".card")]
        assert any("连接中断" in c for c in cards)
        assert app.query_one("#input", Input).disabled is False


async def test_switch_defers_distill_then_catches_up_in_background(tmp_path):
    # 回归：切换会话不做内联抽取（不卡顿），deferred 标记由后台补沉淀消化。
    app = build_app(tmp_path, [], extract_on_exit=True)
    from quiet_memo.memory.models import Candidate

    class CountingExtractor:
        def __init__(self):
            self.calls = 0

        def extract(self, text, context=""):
            self.calls += 1
            from quiet_memo.memory.models import ExtractionResult

            return ExtractionResult(
                memories=[Candidate(text="后台补记的内容", topic="misc", confidence=0.95)]
            )

    app.service.extractor = counting = CountingExtractor()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        first = app.sessions.create()
        # 3 条用户消息：达到后台补沉淀阈值（min_new_messages=3）
        for text in ("第一场对话一", "第一场对话二", "第一场对话三"):
            app.sessions.append(
                first,
                {"type": "message", "id": new_message_id(), "role": "user", "content": text},
            )
        second = app.sessions.create()
        app.sessions.append(
            second,
            {"type": "message", "id": new_message_id(), "role": "user", "content": "第二场对话内容"},
        )
        app.refresh_sessions()
        app.runtime.resume(first.id)  # 活跃会话才有 close(distill=False) 的 deferred 标记

        app._switch_session(second.id)
        for _ in range(300):
            await pilot.pause()
            if not app.busy and app.runtime.current.id == second.id:
                break
        assert app.runtime.current.id == second.id
        types = {r["type"] for r in app.sessions.load(first.id).records}
        assert "distill_deferred" in types
        for _ in range(300):
            await pilot.pause()
            if app.service.store.has_text("后台补记的内容"):
                break
        assert counting.calls >= 1  # 后台补沉淀已执行
        assert app.service.store.has_text("后台补记的内容")


async def test_session_picker_search_resume_and_delete(tmp_path):
    # 回归（issue 反馈）：/sessions 打开 OpenCode 式选择器——搜索/恢复/删除。
    app = build_app(tmp_path, [])
    from textual.widgets import Input

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from quiet_memo.agent.session import new_message_id
        from quiet_memo.app.chat_widgets import SessionPickerScreen

        store = app.sessions
        s1 = store.create()
        store.rename(s1, "Java 并发讨论")
        store.append(s1, {"type": "message", "id": new_message_id(), "role": "user", "content": "Java 并发讨论"})
        s2 = store.create()
        store.rename(s2, "租房预算讨论")
        store.append(s2, {"type": "message", "id": new_message_id(), "role": "user", "content": "租房预算讨论"})

        widget = app.query_one("#input", Input)
        widget.value = "/sessions"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, SessionPickerScreen):
                break
        picker = app.screen
        assert isinstance(picker, SessionPickerScreen)
        assert len(list(picker.query("#picker-list ListItem"))) == 2
        # 回归：SessionItem 内层 Vertical 不能撑满列表，否则高亮变成整块、底部提示被挤掉。
        assert all(item.size.height <= 3 for item in picker.query("#picker-list ListItem"))
        hint = picker.query_one(".pick-hint", Static)
        assert hint.region.bottom <= picker.query_one("#pick-card").region.bottom

        picker.query_one("#picker-input", Input).value = "java"
        await pilot.pause()
        picker.on_input_changed(Input.Changed(picker.query_one("#picker-input", Input), "java"))
        await pilot.pause()
        assert len(list(picker.query("#picker-list ListItem"))) == 1

        await pilot.press("enter")
        for _ in range(200):
            await pilot.pause()
            if not app.busy and app.runtime.has_session:
                break
        assert app.runtime.current.id == s1.id

        # 再次打开：过滤锁定另一条会话，Del 删除，确认后列表同步刷新
        widget.value = "/sessions"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, SessionPickerScreen):
                break
        picker = app.screen
        picker.query_one("#picker-input", Input).value = "租房"
        picker.on_input_changed(Input.Changed(picker.query_one("#picker-input", Input), "租房"))
        await pilot.pause()
        await pilot.press("down")  # 焦点从搜索框移交列表（搜索框的 Del 是编辑按键）
        await pilot.press("delete")
        await pilot.pause()
        assert "PickScreen" in type(app.screen).__name__  # 删除确认弹窗已弹出
        await pilot.press("enter")  # 确认删除
        for _ in range(100):
            await pilot.pause()
            if not store.exists(s2.id):
                break
        assert not store.exists(s2.id)
        assert store.exists(s1.id)
        # 过滤结果也要同步：已删会话不能继续留在过滤后的列表里。
        assert len(list(picker.query("#picker-list ListItem"))) == 0


async def test_startup_catchup_does_not_block_switching(tmp_path):
    # 回归：启动补沉淀在后台执行，不能再把输入和会话切换一起锁住。
    import threading

    app = build_app(tmp_path, [], extract_on_exit=True)
    crashed = app.sessions.create()
    app.sessions.append(
        crashed,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "中断前说的话"},
    )

    class BlockingExtractor:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def extract(self, text, context=None):
            self.started.set()
            assert self.release.wait(timeout=10)
            return ExtractionResult(memories=[])

    blocker = BlockingExtractor()
    app.service.extractor = blocker
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(200):
            await pilot.pause()
            if blocker.started.is_set():
                break
        assert blocker.started.is_set()
        assert app.busy is False
        assert app.query_one("#input", Input).disabled is False

        app._switch_session(crashed.id)
        for _ in range(200):
            await pilot.pause()
            if app.runtime.has_session and app.runtime.current.id == crashed.id and not app.busy:
                break
        assert app.runtime.current.id == crashed.id

        blocker.release.set()
        for _ in range(300):
            await pilot.pause()
            if any("Consolidated" in str(widget.render()) for widget in app.query(".card")):
                break
        assert app.busy is False
        assert any("Consolidated 1" in str(widget.render()) for widget in app.query(".card"))


async def test_transcript_replays_tool_events(tmp_path):
    # 回归：旧会话不仅回放问答正文，也要能看到当时的工具事件。
    import json

    from quiet_memo.agent.tools import SEARCH_NOTICE

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.append(
        session, {"type": "message", "id": "m1", "role": "user", "content": "查 GLM"}
    )
    app.sessions.append(
        session,
        {
            "type": "assistant",
            "id": "a1",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "search", "arguments": {"query": "GLM-5.3"}}],
        },
    )
    payload = {
        "results": [
            {"title": "t", "url": "https://example.com", "snippet": "", "published": None}
        ],
        "provider": "open_websearch",
        "failed": [],
    }
    app.sessions.append(
        session,
        {
            "type": "tool",
            "id": "t1",
            "tool_call_id": "c1",
            "name": "search",
            "ok": True,
            "duration_ms": 10,
            "content": SEARCH_NOTICE + "\n" + json.dumps(payload, ensure_ascii=False),
        },
    )
    app.sessions.append(session, {"type": "assistant", "id": "a2", "content": "最终回答"})
    app.runtime.resume(session.id)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        events = [str(widget.render()) for widget in app.query(".event-card")]
        assert any('Search "GLM-5.3": 1 results' in event for event in events)
        replies = [getattr(widget, "_markdown", "") for widget in app.query(".reply")]
        assert any("最终回答" in reply for reply in replies)


async def test_modal_lists_share_chat_scrollbar_style(tmp_path):
    from quiet_memo.app.chat_widgets import PickScreen, SessionPickerScreen

    def scrollbar_style(widget):
        return (
            widget.styles.scrollbar_size_vertical,
            widget.styles.scrollbar_background,
            widget.styles.scrollbar_color,
            widget.styles.scrollbar_color_hover,
            widget.styles.scrollbar_color_active,
        )

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "会话"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        chat_style = scrollbar_style(app.query_one("#chat"))

        app._show_model_picker([])
        await pilot.pause()
        assert isinstance(app.screen, PickScreen)
        model_list = app.screen.query_one("#pick-list")
        assert model_list.has_class("thin-scroll")
        assert scrollbar_style(model_list) == chat_style
        await pilot.press("escape")
        await pilot.pause()

        app.open_session_picker()
        await pilot.pause()
        assert isinstance(app.screen, SessionPickerScreen)
        session_list = app.screen.query_one("#picker-list")
        assert session_list.has_class("thin-scroll")
        assert scrollbar_style(session_list) == chat_style


async def test_session_picker_is_centered(tmp_path):
    from quiet_memo.app.chat_widgets import SessionPickerScreen

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "会话"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.open_session_picker()
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, SessionPickerScreen)
        card = picker.query_one("#pick-card")
        # 卡片应水平、垂直居中，而不是贴在左上角。
        assert abs(card.region.x - (picker.region.width - card.region.width) / 2) <= 1
        assert abs(card.region.y - (picker.region.height - card.region.height) / 2) <= 1
        assert "Ctrl+R" in str(picker.query_one(".pick-hint", Static).content)


async def test_dialog_inputs_share_neutral_focus_style(tmp_path):
    from textual.color import Color

    from quiet_memo.app.chat_widgets import SessionPickerScreen

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "会话"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        main_input = app.query_one("#input", Input)
        assert main_input.styles.background == Color.parse("#131417")
        assert main_input.styles.border_top[1] == Color.parse("#56575f")
        main_input.blur()
        await pilot.pause()
        assert main_input.styles.background == Color.parse("#131417")
        main_input.focus()

        app.open_session_picker()
        await pilot.pause()
        assert isinstance(app.screen, SessionPickerScreen)
        picker_input = app.screen.query_one("#picker-input", Input)
        assert picker_input.styles.background == Color.parse("#191a1f")
        assert picker_input.styles.border_top[1] == main_input.styles.border_top[1]


async def test_session_picker_ctrl_r_renames_session(tmp_path):
    from quiet_memo.app.chat_widgets import SessionPickerScreen, TextInputScreen

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.rename(session, "旧标题")
    app.sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "旧标题"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.open_session_picker()
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, TextInputScreen)
        rename_input = app.screen.query_one("#pick-input", Input)
        assert rename_input.value == "旧标题"

        rename_input.value = "新标题"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, SessionPickerScreen):
                break
        assert app.sessions.load(session.id).title() == "新标题"
        titles = [item.row["title"] for item in app.screen.query("#picker-list SessionItem")]
        assert "新标题" in titles


async def test_session_picker_rename_current_updates_runtime(tmp_path):
    from quiet_memo.app.chat_widgets import SessionPickerScreen, TextInputScreen

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.rename(session, "旧标题")
    app.sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "旧标题"},
    )
    app.runtime.resume(session.id)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.open_session_picker()
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, TextInputScreen)
        app.screen.query_one("#pick-input", Input).value = "当前新标题"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, SessionPickerScreen):
                break
        assert app.runtime.current.title() == "当前新标题"


async def test_removed_shortcuts_are_not_available(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    assert not {"resume", "recall", "remember"}.intersection(item[0] for item in SLASH_COMMANDS)
    cards = []
    monkeypatch.setattr(app, "add_card", lambda message, *args: cards.append(message))
    for command in ("/resume", "/recall 合成查询", "/remember 合成事实"):
        app._command(command)
        assert "Unknown command" in cards[-1]
    opened = []
    monkeypatch.setattr(app, "open_session_picker", lambda: opened.append(True))
    app._command("/sessions")
    assert opened == [True]
