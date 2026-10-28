from __future__ import annotations

import hashlib
import os
import re
import secrets
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

import yaml

from .models import Entry, MemoryContext, TopicInfo

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
ENTRY_LINE_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) — (.*)$")
COMMENT_LINE_RE = re.compile(
    r"^- <!-- ([^>]+) --> (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) — (.*)$"
)
SLUG_RE = re.compile(r"[^\w\u4e00-\u9fff-]+")
TAG_AT_END_RE = re.compile(r"\s+`([A-Za-z_][\w-]*)=([^`]*)`$")
KNOWN_TAG_KEYS = frozenset(
    {"id", "status", "supersedes", "source", "confidence", "note", "session", "message", "context"}
)
ESCAPED_TAG_PREFIX = "~qm-escaped~"

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_DELETED = "deleted"


def slugify(value: str) -> str:
    slug = SLUG_RE.sub("-", value.strip().lower()).replace("_", "-")
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "uncategorized"


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text).strip().lower()


def _split_metadata(rest: str) -> tuple[str, dict[str, str]]:
    if rest.endswith(")") and "  (" in rest:
        idx = rest.rfind("  (")
        text = rest[:idx].rstrip()
        raw = rest[idx + 3 : -1]
        metadata: dict[str, str] = {}
        for part in raw.split(", "):
            if not part:
                continue
            if ": " in part:
                key, value = part.split(": ", 1)
                metadata[key.strip()] = value.strip()
            else:
                metadata[part.strip()] = "true"
        return text, metadata
    return rest, {}


def _parse_comment(comment: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in comment.split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()
    return fields


def _render_comment(fields: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in fields.items())


def _render_tags(fields: dict[str, str]) -> str:
    def escape(value: str) -> str:
        if any(char in value for char in "`\r\n") or value.startswith(ESCAPED_TAG_PREFIX):
            return ESCAPED_TAG_PREFIX + quote(value, safe="")
        return value

    return " ".join(f"`{key}={escape(value)}`" for key, value in fields.items())


def _split_tags(rest: str) -> tuple[str, dict[str, str]]:
    # 只从行尾逐个剥离已知键；生成的标签以 id 开头，剥到 id 即停，
    # 否则正文末尾同形的 `status=...`/`id=...` 会被当成元数据覆盖真实值。
    tags: dict[str, str] = {}
    text = rest.rstrip()
    while (match := TAG_AT_END_RE.search(text)) and match.group(1) in KNOWN_TAG_KEYS:
        key = match.group(1)
        value = match.group(2)
        tags[key] = (
            unquote(value[len(ESCAPED_TAG_PREFIX):])
            if value.startswith(ESCAPED_TAG_PREFIX) else value
        )
        text = text[: match.start()].rstrip()
        if key == "id":
            break
    return text, dict(reversed(list(tags.items())))


class MemoryStore:
    def __init__(self, vault: Path):
        self.vault = Path(vault)
        self.root = self.vault / "memories"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def topic_path(self, topic: str) -> Path:
        return self.root / f"{slugify(topic)}.md"

    def fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        """按文件名排序返回 (文件名, 纳秒修改时间, 字节数)，供派生缓存失效判断。"""
        if not self.root.exists():
            return ()
        items: list[tuple[str, int, int]] = []
        for path in sorted(self.root.glob("*.md")):
            stat = path.stat()
            items.append((path.name, stat.st_mtime_ns, stat.st_size))
        return tuple(items)

    def _read(self, path: Path, topic: str) -> tuple[dict, str]:
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        match = FRONTMATTER_RE.match(raw)
        if match:
            meta = yaml.safe_load(match.group(1)) or {}
            body = raw[match.end() :]
        else:
            meta, body = {}, raw
        meta.setdefault("topic", topic)
        meta.setdefault("title", topic)
        return meta, body

    def _write(self, path: Path, meta: dict, body: str) -> None:
        front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
        temporary: Path | None = None
        try:
            # 同目录保证 replace 不跨文件系统；关闭后替换以兼容 Windows。
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(f"---\n{front}\n---\n\n{body.lstrip()}")
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def new_entry_id(stamp: datetime | None = None) -> str:
        stamp = stamp or datetime.now()
        return f"mem-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"

    def append(
        self,
        topic: str,
        text: str,
        *,
        source: str = "chat",
        confidence: float = 1.0,
        note: str | None = None,
        when: datetime | None = None,
        title: str | None = None,
        extra: dict[str, str] | None = None,
        entry_id: str | None = None,
        supersedes: str | None = None,
        known_ids: set[str] | None = None,
        context: MemoryContext | None = None,
    ) -> Entry:
        """追加条目；批量调用可传入含停用条目的完整 ID 集合，由调用方增量维护。"""
        self.ensure()
        # 条目格式是单行 bullet；多行文本压成一行，避免正文行在解析时被静默丢弃。
        text = re.sub(r"\s*\n\s*", " ", text).strip()
        if not text:
            raise ValueError("条目内容为空。")
        stamp = when or datetime.now()
        # ID 必须全局唯一：批量导入可能挤在同一秒，只靠随机数会生日碰撞。
        if known_ids is None:
            known_ids = {entry.id for entry in self.entries(include_inactive=True)}
        entry_id = entry_id or self.new_entry_id(stamp)
        while entry_id in known_ids:
            entry_id = self.new_entry_id(stamp)
        path = self.topic_path(topic)
        meta, body = self._read(path, slugify(topic))
        if title and not path.exists():
            meta["title"] = title
        meta.setdefault("created", stamp.date())
        meta["updated"] = stamp.date()

        tags = {"id": entry_id, "status": STATUS_ACTIVE}
        if supersedes:
            tags["supersedes"] = supersedes
        tags["source"] = source
        tags["confidence"] = f"{confidence:.2f}"
        if note:
            tags["note"] = note
        for key, value in (extra or {}).items():
            if value:
                tags[key] = value
        if context is not None and context.model_dump(exclude_none=True):
            tags["context"] = context.model_dump_json(exclude_none=True)
        line = (
            f"- {stamp.isoformat(timespec='seconds')} — {text}"
            f"  {_render_tags(tags)}"
        )

        if not path.exists():
            body = f"# {meta['title']}\n\n{line}\n"
        else:
            body = body.rstrip() + f"\n{line}\n"
        self._write(path, meta, body)

        metadata = {
            key: value
            for key, value in tags.items()
            if key not in ("id", "status", "supersedes")
        }
        return Entry(
            id=entry_id,
            topic=slugify(topic),
            text=text,
            timestamp=stamp.isoformat(timespec="seconds"),
            status=STATUS_ACTIVE,
            supersedes=supersedes,
            metadata=metadata,
        )

    def _parse_line(self, line: str, topic: str) -> Entry | None:
        stripped = line.strip()
        comment = COMMENT_LINE_RE.match(stripped)
        if comment:
            fields = _parse_comment(comment.group(1))
            text, metadata = _split_metadata(comment.group(3))
            return Entry(
                id=fields.get("id") or self._legacy_id(comment.group(2), text),
                topic=topic,
                text=text,
                timestamp=comment.group(2),
                status=fields.get("status", STATUS_ACTIVE),
                supersedes=fields.get("supersedes"),
                metadata=metadata,
            )
        match = ENTRY_LINE_RE.match(stripped)
        if match is None:
            return None
        text, tags = _split_tags(match.group(2))
        if tags:
            metadata = {
                key: value
                for key, value in tags.items()
                if key not in ("id", "status", "supersedes")
            }
            entry_id = tags.get("id") or self._legacy_id(match.group(1), text)
            return Entry(
                id=entry_id,
                topic=topic,
                text=text,
                timestamp=match.group(1),
                status=tags.get("status", STATUS_ACTIVE),
                supersedes=tags.get("supersedes"),
                metadata=metadata,
            )
        text, metadata = _split_metadata(text)
        return Entry(
            id=self._legacy_id(match.group(1), text),
            topic=topic,
            text=text,
            timestamp=match.group(1),
            status=STATUS_ACTIVE,
            metadata=metadata,
        )

    @staticmethod
    def _legacy_id(timestamp: str, text: str) -> str:
        digest = hashlib.sha1(f"{timestamp}{text}".encode("utf-8")).hexdigest()[:8]
        return f"mem-legacy-{digest}"

    def entries(self, topic: str | None = None, include_inactive: bool = False) -> list[Entry]:
        if not self.root.exists():
            return []
        paths = [self.topic_path(topic)] if topic else sorted(self.root.glob("*.md"))
        result: list[Entry] = []
        for path in paths:
            if not path.exists():
                continue
            _, body = self._read(path, path.stem)
            for line in body.splitlines():
                entry = self._parse_line(line, path.stem)
                if entry is None:
                    continue
                if include_inactive or entry.status == STATUS_ACTIVE:
                    result.append(entry)
        return result

    def find(self, entry_id: str) -> Entry | None:
        return next(
            (entry for entry in self.entries(include_inactive=True) if entry.id == entry_id),
            None,
        )

    def set_status(self, entry_id: str, status: str) -> bool:
        if not self.root.exists():
            return False
        for path in sorted(self.root.glob("*.md")):
            meta, body = self._read(path, path.stem)
            lines = body.splitlines()
            for index, line in enumerate(lines):
                entry = self._parse_line(line, path.stem)
                if entry is None or entry.id != entry_id:
                    continue
                lines[index] = self._rewrite_status(line, entry, status)
                meta["updated"] = datetime.now().date()
                self._write(path, meta, "\n".join(lines) + "\n")
                return True
        return False

    @staticmethod
    def _rewrite_status(line: str, entry: Entry, status: str) -> str:
        stripped = line.strip()
        comment = COMMENT_LINE_RE.match(stripped)
        if comment:
            fields = _parse_comment(comment.group(1))
            fields["status"] = status
            return (
                f"- <!-- {_render_comment(fields)} --> "
                f"{comment.group(2)} — {comment.group(3)}"
            )
        match = ENTRY_LINE_RE.match(stripped)
        if match is None:
            return line
        text, tags = _split_tags(match.group(2))
        if not tags:
            text, tags = _split_metadata(text)
        rest = {
            key: value
            for key, value in tags.items()
            if key not in ("id", "status") and re.fullmatch(r"[A-Za-z_][\w-]*", key)
        }
        rebuilt = {"id": tags.get("id", entry.id), "status": status}
        rebuilt.update(rest)
        return f"- {match.group(1)} — {text}  {_render_tags(rebuilt)}"

    def topics(self) -> list[TopicInfo]:
        if not self.root.exists():
            return []
        result: list[TopicInfo] = []
        for path in sorted(self.root.glob("*.md")):
            meta, body = self._read(path, path.stem)
            count = sum(
                1
                for line in body.splitlines()
                if (entry := self._parse_line(line, path.stem)) is not None
                and entry.status == STATUS_ACTIVE
            )
            result.append(
                TopicInfo(
                    topic=path.stem,
                    title=str(meta.get("title", path.stem)),
                    entries=count,
                    updated=str(meta.get("updated", "")),
                )
            )
        return result

    def has_text(self, text: str) -> bool:
        target = normalize_text(text)
        return any(normalize_text(entry.text) == target for entry in self.entries())

    def recent_context(self, limit: int = 60) -> str:
        entries = sorted(self.entries(), key=lambda entry: entry.timestamp, reverse=True)[:limit]
        lines = [f"[{entry.topic}] {entry.timestamp} {entry.text}" for entry in reversed(entries)]
        return "\n".join(lines)
