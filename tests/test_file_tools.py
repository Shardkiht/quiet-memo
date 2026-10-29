from __future__ import annotations

import json

import pytest

from quiet_memo.agent.file_tools import file_handlers
from quiet_memo.agent.session import SessionStore
from quiet_memo.agent.tools import build_tools
from quiet_memo.infra.documents import document_paths
from quiet_memo.infra.tokens import estimate_tokens
from quiet_memo.memory.models import Candidate, ExtractionResult

pytestmark = pytest.mark.agent


def test_paths_require_explicit_absolute_path_and_preserve_spaces(tmp_path):
    path = tmp_path / "synthetic conversation.JSONL"
    assert document_paths(f'分析 "{path}"，只读不写') == [path.resolve()]
    assert document_paths('分析 relative.txt 和 https://example.invalid/file.pdf') == []


def test_file_reads_are_paged_losslessly_and_scoped(service, tmp_path):
    path = tmp_path / "synthetic.txt"
    text = "合成个人资料。" * 3000
    path.write_text(text, encoding="utf-8")
    handlers = file_handlers(service, [path])
    with pytest.raises(ValueError, match="本条用户消息"):
        handlers["read_file"]({"path": str(tmp_path / "other.txt")})
    pieces = []
    offset = 0
    while offset is not None:
        result = handlers["read_file"]({"path": str(path), "offset": offset})
        assert estimate_tokens(json.dumps(result, ensure_ascii=False)) < 2000
        pieces.append(result["text"])
        offset = result["next_offset"]
    assert "".join(pieces) == text


def test_file_read_redacts_before_tool_output(service, tmp_path):
    path = tmp_path / "synthetic.txt"
    path.write_text("密码是 SYNTHETIC-ONLY-12345", encoding="utf-8")
    result = file_handlers(service, [path])["read_file"]({"path": str(path)})
    assert "SYNTHETIC-ONLY-12345" not in str(result)


def test_import_is_preview_by_default_and_reports_remaining_chunks(service, tmp_path, monkeypatch):
    path = tmp_path / "synthetic.txt"
    path.write_text("合成笔记" * 2000, encoding="utf-8")
    calls = []

    def extract(text, **kwargs):
        calls.append(kwargs)
        return service._adjudicate([Candidate(text="合成用户偏好茶饮", confidence=0.95)], kwargs["source"], dry_run=kwargs["dry_run"])

    monkeypatch.setattr(service, "import_text", extract)
    handler = file_handlers(service, [path])["import_file"]
    result = handler({"path": str(path)})
    assert result["next_chunk"] == 2
    assert service.store.entries() == []
    assert calls[0]["dry_run"] is True
    result = handler({"path": str(path), "apply": True})
    assert result["mode"] == "applied"
    assert service.store.entries()[0].metadata["source"].endswith("#chunk:1")


def test_tool_registration_is_scoped_to_current_turn(service, tmp_path):
    session = SessionStore(service.config.vault).create()
    assert "read_file" not in build_tools(service, session)._tools
    tools = build_tools(service, session, file_paths=[tmp_path / "synthetic.txt"])
    assert tools._tools["import_file"].mutating
    assert not tools.execute("read_file", {"path": str(tmp_path / "not-granted.txt")}).ok
