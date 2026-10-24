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



def test_format_tokens_uses_km_units():
    from quiet_memo.infra.tokens import format_tokens

    assert format_tokens(800) == "800"
    assert format_tokens(16000) == "16K"
    assert format_tokens(64000) == "64K"
    assert format_tokens(1000000) == "1M"
    assert format_tokens(1500000) == "1.5M"


def test_layout_widths_wide_left():
    from quiet_memo.infra.layout import widths

    for cols in (80, 120, 200):
        left, width, right = widths(cols)
        assert left + width + right == cols
        # 宽幅左对齐：固定小左边距，内容约占 90% 且随窗口伸缩、不设上限。
        assert left == 2
        assert width == max(40, min(cols - 4, round(cols * 0.9)))
    assert widths(200)[1] == 180


def test_padded_leaves_right_slack():
    # 对话列不铺满终端：整行恰好占满时，窗口拉伸/收窄会让行尾回绕。
    from rich.console import Console
    from rich.text import Text

    from quiet_memo.app.ui import padded

    console = Console(width=100)
    with console.capture() as capture:
        console.print(padded(Text("─" * 90), 100))
        console.print(padded("长" * 120, 100))
    lines = capture.get().splitlines()
    assert lines
    assert all(len(line) <= 98 for line in lines)
    assert max(len(line) for line in lines) >= 90  # 内容仍占宽幅主列


def test_escape_cancels_questionary_prompt():
    import questionary
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from quiet_memo.infra.prompts import ask

    with create_pipe_input() as inp:
        question = questionary.text("name", input=inp, output=DummyOutput())
        inp.send_text("\x1b")
        assert ask(question) is None






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


def test_import_without_api_key_reports_hint(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config = tmp_path / "quiet-memo.toml"
    vault = tmp_path / "vault"
    invoke(["init", "--vault", str(vault), "--config", str(config)])
    source = tmp_path / "notes.md"
    source.write_text("# 档案\n\n- 咖啡偏好清淡风味。\n", encoding="utf-8")

    result = invoke(["import", str(source), "--config", str(config)])

    assert result.exit_code == 1
    assert "DEEPSEEK_API_KEY" in result.output


def test_import_dry_run_then_apply(tmp_path, monkeypatch):
    from quiet_memo.memory import service as service_module
    from quiet_memo.memory.models import Candidate, ExtractionResult

    class FakeImportExtractor:
        def __init__(self, config):
            pass

        def extract(self, text):
            return ExtractionResult(
                memories=[Candidate(text="咖啡偏好清淡风味", topic="preferences", confidence=0.95)]
            )

    monkeypatch.setattr(service_module, "ImportExtractor", FakeImportExtractor)
    config = tmp_path / "quiet-memo.toml"
    vault = tmp_path / "vault"
    invoke(["init", "--vault", str(vault), "--config", str(config)])
    source = tmp_path / "notes.md"
    source.write_text("# 档案\n\n- 咖啡偏好清淡风味。\n", encoding="utf-8")

    result = invoke(["import", str(source), "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "直写 1" in result.output
    assert not (vault / "memories" / "preferences.md").exists()

    result = invoke(["import", str(source), "--apply", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "已写入" in result.output
    assert (vault / "memories" / "preferences.md").exists()
