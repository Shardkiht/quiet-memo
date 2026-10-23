from __future__ import annotations

from typer.testing import CliRunner

from quiet_memo.app.cli import app

runner = CliRunner()


def invoke(args):
    return runner.invoke(app, args)


def test_bare_invocation_falls_back_to_help_without_tty():
    result = invoke([])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_slash_completer_lists_and_filters_commands():
    from prompt_toolkit.document import Document

    from quiet_memo.app.ui import SLASH_COMMANDS, slash_completer

    completer = slash_completer()
    completions = list(completer.get_completions(Document("/", 1), None))
    assert {item.text for item in completions} == {f"/{name}" for name, _, _ in SLASH_COMMANDS}

    filtered = list(completer.get_completions(Document("/mo", 3), None))
    assert [item.text for item in filtered] == ["/models"]

    assert list(completer.get_completions(Document("/models re", 10), None)) == []
    models = next(item for item in completions if item.text == "/models")
    assert "切换模型" in models.display_meta_text


def test_escape_cancels_questionary_prompt():
    import questionary
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from quiet_memo.infra.prompts import ask

    with create_pipe_input() as inp:
        question = questionary.text("name", input=inp, output=DummyOutput())
        inp.send_text("\x1b")
        assert ask(question) is None


def test_session_select_delete_returns_sentinel():
    import questionary
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from quiet_memo.app import session_ui

    with create_pipe_input() as inp:
        question = questionary.select(
            "pick",
            choices=[questionary.Choice("A", value="a")],
            input=inp,
            output=DummyOutput(),
        )
        inp.send_text("\x1b[3~")
        result = session_ui._session_select(question)

    assert result == (session_ui.DELETE, "a")


def test_session_choices_are_grouped_by_date():
    import questionary

    from quiet_memo.app.session_ui import session_choices, visible_sessions

    rows = [
        {
            "id": "s-2",
            "title": "新会话",
            "messages": 3,
            "summaries": 0,
            "updated_at": "2026-10-22T10:00:00",
        },
        {
            "id": "s-1",
            "title": "旧会话",
            "messages": 1,
            "summaries": 1,
            "updated_at": "2026-10-21T09:00:00",
        },
        {
            "id": "s-empty",
            "title": "(空会话)",
            "messages": 0,
            "summaries": 0,
            "updated_at": "2026-10-20T09:00:00",
        },
    ]

    visible = visible_sessions(rows)
    choices = session_choices(visible, current_id="s-2")
    separators = [item for item in choices if isinstance(item, questionary.Separator)]
    values = [
        item.value
        for item in choices
        if isinstance(item, questionary.Choice) and not isinstance(item, questionary.Separator)
    ]

    assert [row["id"] for row in visible] == ["s-2", "s-1"]
    assert [item.title for item in separators] == ["2026-10-22", "2026-10-21"]
    assert values == ["s-2", "s-1"]
    assert "← 当前" in next(
        item.title
        for item in choices
        if getattr(item, "value", None) == "s-2" and not isinstance(item, questionary.Separator)
    )


def test_session_manager_returns_session_id_not_first_character(
    tmp_path, monkeypatch, patch_questionary
):
    from quiet_memo.agent.session import SessionStore, new_message_id
    from quiet_memo.app import session_ui

    patch_questionary(session_ui)
    store = SessionStore(tmp_path)
    session = store.create()
    store.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "测试会话"},
    )

    answers = [session.id]
    monkeypatch.setattr(session_ui, "ask", lambda question, **kwargs: answers.pop(0))

    assert session_ui.session_manager(store, current_id=None) == session.id


def test_session_manager_delete_key_asks_before_deleting(
    tmp_path, monkeypatch, patch_questionary
):
    from quiet_memo.agent.session import SessionStore, new_message_id
    from quiet_memo.app import session_ui

    patch_questionary(session_ui)
    store = SessionStore(tmp_path)
    session = store.create()
    store.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "待删除"},
    )

    answers = [(session_ui.DELETE, session.id), False, None]
    monkeypatch.setattr(session_ui, "ask", lambda question, **kwargs: answers.pop(0))

    assert session_ui.session_manager(store, current_id=None) is None
    assert store.exists(session.id)

    answers = [(session_ui.DELETE, session.id), True, None]
    assert session_ui.session_manager(store, current_id=None) is None
    assert not store.exists(session.id)


def test_init_note_recall_commit(tmp_path):
    config = tmp_path / "quiet-memo.toml"
    vault = tmp_path / "vault"

    result = invoke(["init", "--vault", str(vault), "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert config.exists()
    assert (vault / ".git").is_dir()

    result = invoke(
        ["note", "喝咖啡偏好清淡风味", "--topic", "preferences", "--title", "偏好", "--config", str(config)]
    )
    assert result.exit_code == 0, result.output
    assert "已记录" in result.output

    result = invoke(["recall", "清淡", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "喝咖啡偏好清淡风味" in result.output

    result = invoke(["topics", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "preferences" in result.output

    result = invoke(["pending", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "没有待确认项" in result.output

    result = invoke(["status", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "条目 1" in result.output

    result = invoke(["commit", "--config", str(config), "--no-push"])
    assert result.exit_code == 0, result.output
    assert "本次提交总结" in result.output

    result = invoke(["history", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "memory:" in result.output


def test_note_duplicate_fails(tmp_path):
    config = tmp_path / "quiet-memo.toml"
    vault = tmp_path / "vault"
    invoke(["init", "--vault", str(vault), "--config", str(config)])
    first = invoke(["note", "同一条事实", "--config", str(config)])
    assert first.exit_code == 0
    second = invoke(["note", "同一条事实", "--config", str(config)])
    assert second.exit_code == 1
    assert "已存在" in second.output


def test_memorize_without_api_key_reports_hint(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config = tmp_path / "quiet-memo.toml"
    vault = tmp_path / "vault"
    invoke(["init", "--vault", str(vault), "--config", str(config)])

    result = invoke(["memorize", "一段对话", "--config", str(config)])
    assert result.exit_code == 1
    assert "DEEPSEEK_API_KEY" in result.output
