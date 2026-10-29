from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from quiet_memo.infra.documents import DocumentError, read_document

pytestmark = pytest.mark.cli


@pytest.mark.parametrize("suffix", [".MD", ".txt", ".json", ".jsonl", ".csv", ".yaml", ".xml"])
@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "gb18030"])
def test_text_import_preserves_roles_and_dates(tmp_path, suffix, encoding):
    path = tmp_path / ("conversation" + suffix)
    text = '2024-06-01\n{"role":"user","content":"我喜欢合成茶饮"}\n{"role":"assistant","content":"仅为建议"}'
    path.write_bytes(text.encode(encoding))
    assert read_document(path) == text


@pytest.mark.parametrize("suffix", [".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".html", ".epub", ".msg", ".ipynb"])
def test_document_delegates_to_local_converter(tmp_path, monkeypatch, suffix):
    path = tmp_path / ("synthetic" + suffix)
    path.write_bytes(b"synthetic")
    calls = []

    class Converter:
        def __init__(self, **kwargs):
            assert kwargs == {"enable_plugins": False}

        def convert(self, filename):
            calls.append(filename)
            return SimpleNamespace(text_content="# 合成资料\n正文")

    monkeypatch.setitem(sys.modules, "markitdown", SimpleNamespace(MarkItDown=Converter))
    assert "合成资料" in read_document(path)
    assert calls == [str(path)]


def test_missing_converter_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "markitdown", None)
    (tmp_path / "synthetic.pdf").write_bytes(b"synthetic")
    with pytest.raises(DocumentError, match="uv sync --extra documents"):
        read_document(tmp_path / "synthetic.pdf")


@pytest.mark.parametrize("content", [b"", b" \n", b"abc\x00def", b"\xff"])
def test_unreadable_or_empty_text_is_rejected(tmp_path, content):
    path = tmp_path / "synthetic.txt"
    path.write_bytes(content)
    with pytest.raises(DocumentError):
        read_document(path)


def test_unsupported_format_is_not_sent_as_text(tmp_path):
    with pytest.raises(DocumentError, match="不支持"):
        read_document(tmp_path / "synthetic.exe")


def test_conversion_errors_do_not_echo_document_content(tmp_path, monkeypatch):
    (tmp_path / "synthetic.pdf").write_bytes(b"synthetic")
    class Converter:
        def __init__(self, **kwargs):
            pass

        def convert(self, filename):
            raise ValueError("synthetic-private-fragment")

    monkeypatch.setitem(sys.modules, "markitdown", SimpleNamespace(MarkItDown=Converter))
    with pytest.raises(DocumentError) as error:
        read_document(tmp_path / "synthetic.pdf")
    assert "synthetic-private-fragment" not in str(error.value)


def test_real_html_conversion(tmp_path):
    pytest.importorskip("markitdown")
    path = tmp_path / "synthetic.html"
    path.write_text("<html><body><h1>合成笔记</h1><p>用户明确表示偏好茶饮。</p></body></html>", encoding="utf-8")
    text = read_document(path)
    assert "合成笔记" in text
    assert "偏好茶饮" in text
    assert "<body>" not in text


def test_real_docx_conversion(tmp_path):
    pytest.importorskip("markitdown")
    pytest.importorskip("mammoth")
    from zipfile import ZipFile

    path = tmp_path / "synthetic.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>合成用户偏好茶饮</w:t></w:r></w:p></w:body></w:document>')
    assert "合成用户偏好茶饮" in read_document(path)


def test_real_xlsx_conversion_preserves_sheet_and_cells(tmp_path):
    pytest.importorskip("markitdown")
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    workbook.active.title = "合成资料"
    workbook.active.append(["日期", "偏好"])
    workbook.active.append(["2024-06", "合成茶饮"])
    path = tmp_path / "synthetic.xlsx"
    workbook.save(path)
    workbook.close()
    text = read_document(path)
    assert "合成资料" in text
    assert "2024-06" in text
    assert "合成茶饮" in text


def test_real_pptx_conversion_preserves_slide_text(tmp_path):
    pytest.importorskip("markitdown")
    pptx = pytest.importorskip("pptx")
    presentation = pptx.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "合成资料"
    slide.placeholders[1].text = "用户偏好合成茶饮"
    path = tmp_path / "synthetic.pptx"
    presentation.save(path)
    assert "用户偏好合成茶饮" in read_document(path)
