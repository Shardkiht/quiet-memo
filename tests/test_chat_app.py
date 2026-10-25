from __future__ import annotations

from textual.widgets import Input, Static

from quiet_memo.agent.runtime import AgentRuntime
from quiet_memo.agent.session import SessionStore
from quiet_memo.app.chat_app import SLASH_COMMANDS, ChatApp, SidebarToggle
from quiet_memo.infra.config import ChatConfig, Config, LLMConfig, RetrievalConfig
from quiet_memo.llm import AssistantTurn
from quiet_memo.memory.extractor import StaticExtractor
from quiet_memo.memory.models import ExtractionResult
from quiet_memo.memory.service import MemoryService


class FakeChatModel:
    def __init__(self, turns):
        self.turns = list(turns)

    def chat(self, messages, tools):
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class FakeSummarizer:
    def summarize(self, previous, transcript, max_chars=1200):
        return "会话摘要"


def build_app(tmp_path, turns):
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        retrieval=RetrievalConfig(method="keyword"),
        chat=ChatConfig(context_window=32000, auto_recall=3, recall_rewrite=False),
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
        if app.query(".reply"):
            return
    raise AssertionError("等待回复超时")


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
        assert "上下文" in str(app.query_one("#status", Static).content)


async def test_slash_help_card(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/help"
        await pilot.press("enter")
        await pilot.pause()

        cards = app.query(".card")
        assert cards
        assert "命令" in str(cards[0].content)


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
        cards = app.query(".card")
        assert cards  # /think 走选择弹窗前先渲染提示或弹窗——执行过即可
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
        assert "压缩失败" in str(app.query(".card")[-1].render())


async def test_session_switch_failure_releases_busy(tmp_path):
    # 回归：resume 抛异常（会话文件损坏等）时同样不能卡死输入框。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        def boom(session_id):
            raise KeyError("会话不存在：s-xxx")

        app.runtime.resume = boom
        app._switch_session("s-20260101-000000-dead")
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        assert app.busy is False
        assert app.query_one("#input", Input).disabled is False
        assert "会话切换失败" in str(app.query(".card")[-1].render())


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
        app._delete_session_result(row, True)
        await pilot.pause()

        assert not store.exists(session.id)
        assert store.exists(survivor.id)
        from quiet_memo.app.chat_app import SessionItem

        # 列表已刷新：被删条目消失，保留会话仍在侧栏。
        assert [item.row["id"] for item in app.query(SessionItem)] == [survivor.id]


async def test_context_custom_rejects_unicode_digits(tmp_path):
    # 回归："①".isdigit() 为 True 但 int() 抛错；现在应给出错误卡片而不是崩进事件循环。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._context_custom("①")
        await pilot.pause()
        assert "正整数" in str(app.query(".card")[-1].render())
        app._context_custom("16x")
        await pilot.pause()
        assert "正整数" in str(app.query(".card")[-1].render())


def test_slash_commands_cover_aliases():
    # 回归：面板必须列出 _command 实际支持的别名，/qu、/mo 前缀过滤要有提示。
    commands = {command for command, _, _ in SLASH_COMMANDS}
    assert "quit" in commands and "model" in commands
