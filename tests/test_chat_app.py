from __future__ import annotations

from textual.widgets import Input, Static

from quiet_memo.agent.runtime import AgentRuntime
from quiet_memo.agent.session import SessionStore, new_message_id
from quiet_memo.app.chat_app import ChatApp, SidebarToggle
from quiet_memo.app.chat_input import BarInput
from quiet_memo.app.chat_widgets import SLASH_COMMANDS
from quiet_memo.infra.config import ChatConfig, Config, LLMConfig, RetrievalConfig, SearchConfig
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
                if app.screen.query_one("#pick-list", ListView).has_focus:
                    break

        model_picker = app.screen
        listing = model_picker.query_one("#pick-list", ListView)
        labels = [str(item.query_one(Label).render()) for item in listing.children]
        configure_index = labels.index("Configure provider…")
        listing.index = configure_index
        await pilot.pause()
        assert listing.index == configure_index
        await pilot.press("enter")
        # 第一个选择器 dismiss 与下一个 push 不在同一帧，等新屏幕出现再查询。
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, PickScreen) and app.screen is not model_picker:
                if app.screen.query(".pick-title"):
                    break
        assert "Configure provider" in str(app.screen.query_one(".pick-title").render())

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
        assert "Session switch failed" in str(app.query(".card")[-1].render())


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

        # 列表已刷新：被删条目消失，保留会话仍在侧栏。
        assert [item.row["id"] for item in app.query(SessionItem)] == [survivor.id]


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
    import time

    app = build_app(tmp_path, [])
    app.service.note("启动前未提交的变更", topic="misc")
    async with app.run_test(size=(120, 40)) as pilot:
        # 后台 git 提交耗时受机器负载影响，不能只等固定帧数。
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            await pilot.pause()
            if not app.service.repo.has_changes():
                break
            await asyncio.sleep(0.02)
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
        assert widget.placeholder == "│"
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
    app = build_app(tmp_path, [AssistantTurn(content="好")], extract_on_exit=True)
    crashed = app.sessions.create()
    app.sessions.append(
        crashed,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "中断前说的话"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(300):
            await pilot.pause()
            if any("Consolidated 1" in str(widget.render()) for widget in app.query(".card")):
                break
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
