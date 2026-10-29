from __future__ import annotations

from pathlib import Path
import re


TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".text", ".log", ".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".yaml", ".yml", ".xml"})
DOCUMENT_SUFFIXES = frozenset({".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".html", ".htm", ".epub", ".msg", ".ipynb"})
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | DOCUMENT_SUFFIXES


def document_paths(message: str) -> list[Path]:
    """只从当前用户消息识别显式文件路径；带空格路径需加引号。"""
    pattern = r'"([^"\n]+)"|\x27([^\x27\n]+)\x27|“([^”\n]+)”|(?<![\w:/\\])((?:[A-Za-z]:[\\/]|~/|/)[^\s"\x27<>]+)'
    paths: list[Path] = []
    for match in re.finditer(pattern, message):
        value = next(group for group in match.groups() if group is not None).rstrip("，。；,;")
        path = Path(value).expanduser()
        if path.is_absolute() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            path = path.resolve()
            if path not in paths:
                paths.append(path)
    return paths


class DocumentError(ValueError):
    """文件无法转换为可导入的文本。"""


def read_document(path: Path) -> str:
    """读取用户指定文件；复杂格式交给本地 MarkItDown，不启用插件或云端转换。"""
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise DocumentError(f"不支持的文件格式：{suffix or '无扩展名'}。请导出为 TXT、Markdown 或常见文档格式。")
    try:
        if path.stat().st_size > 20 * 1024 * 1024:
            raise DocumentError("文件超过 20 MiB，请拆分后导入。")
        if suffix in TEXT_SUFFIXES:
            raw = path.read_bytes()
            # 只凭 BOM 识别 UTF-16，避免把无 BOM 的普通文本误判成 UTF-16。
            encodings = ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
            text = None
            for encoding in encodings:
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeError:
                    continue
            if text is None or "\0" in text:
                raise DocumentError("无法解码文本，请转换为 UTF-8 后重试。")
        else:
            try:
                from markitdown import MarkItDown
            except ImportError as exc:
                raise DocumentError("文档解析依赖未安装：开发环境运行 uv sync --extra documents；工具安装使用 uv tool install 'quiet-memo[documents]'。") from exc
            text = MarkItDown(enable_plugins=False).convert(str(path)).text_content
    except DocumentError:
        raise
    except Exception as exc:
        # 不回显解析器异常里的文档片段或内部路径。
        raise DocumentError("文件读取或解析失败，请检查文件是否损坏、加密或缺少解析依赖。") from exc
    if not text.strip():
        raise DocumentError("未提取到文字；扫描件或图片需先做 OCR，再导入文本。")
    return text
