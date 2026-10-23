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
