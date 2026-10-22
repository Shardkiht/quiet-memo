from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime
from pathlib import Path

import yaml

from .models import Entry, TopicInfo

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
COMMENT_LINE_RE = re.compile(
    r"^- <!-- ([^>]+) --> (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) — (.*)$"
)
LEGACY_LINE_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) — (.*)$")
SLUG_RE = re.compile(r"[^\w\u4e00-\u9fff-]+")

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


class MemoryStore:
    def __init__(self, vault: Path):
        self.vault = Path(vault)
        self.root = self.vault / "memories"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def topic_path(self, topic: str) -> Path:
        return self.root / f"{slugify(topic)}.md"

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
        path.write_text(f"---\n{front}\n---\n\n{body.lstrip()}", encoding="utf-8")

    @staticmethod
    def new_entry_id(stamp: datetime | None = None) -> str:
        stamp = stamp or datetime.now()
        return f"mem-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"

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
    ) -> Entry:
        self.ensure()
        stamp = when or datetime.now()
        entry_id = entry_id or self.new_entry_id(stamp)
        path = self.topic_path(topic)
        meta, body = self._read(path, slugify(topic))
        if title and not path.exists():
            meta["title"] = title
        meta.setdefault("created", stamp.date())
        meta["updated"] = stamp.date()

        comment_fields = {"id": entry_id, "status": STATUS_ACTIVE}
        if supersedes:
            comment_fields["supersedes"] = supersedes
        comment = _render_comment(comment_fields)

        parts = [f"source: {source}", f"confidence: {confidence:.2f}"]
        if note:
            parts.append(note)
        for key, value in (extra or {}).items():
            if value:
                parts.append(f"{key}: {value}")
        line = (
            f"- <!-- {comment} --> {stamp.isoformat(timespec='seconds')} — {text}"
            f"  ({', '.join(parts)})"
        )

        if not path.exists():
            body = f"# {meta['title']}\n\n{line}\n"
        else:
            body = body.rstrip() + f"\n{line}\n"
        self._write(path, meta, body)

        metadata = {}
        for part in parts:
            if ": " in part:
                key, value = part.split(": ", 1)
                metadata[key] = value
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
        match = COMMENT_LINE_RE.match(line.strip())
        if match:
            fields = _parse_comment(match.group(1))
            text, metadata = _split_metadata(match.group(3))
            return Entry(
                id=fields.get("id") or self.new_entry_id(),
                topic=topic,
                text=text,
                timestamp=match.group(2),
                status=fields.get("status", STATUS_ACTIVE),
                supersedes=fields.get("supersedes"),
                metadata=metadata,
            )
        legacy = LEGACY_LINE_RE.match(line.strip())
        if legacy:
            text, metadata = _split_metadata(legacy.group(2))
            digest = hashlib.sha1(f"{legacy.group(1)}{text}".encode("utf-8")).hexdigest()[:8]
            return Entry(
                id=f"mem-legacy-{digest}",
                topic=topic,
                text=text,
                timestamp=legacy.group(1),
                status=STATUS_ACTIVE,
                metadata=metadata,
            )
        return None

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
            _, body = self._read(path, path.stem)
            lines = body.splitlines()
            changed = False
            for index, line in enumerate(lines):
                match = COMMENT_LINE_RE.match(line.strip())
                if match:
                    fields = _parse_comment(match.group(1))
                    if fields.get("id") != entry_id:
                        continue
                    fields["status"] = status
                    lines[index] = (
                        f"- <!-- {_render_comment(fields)} --> {match.group(2)} — {match.group(3)}"
                    )
                    changed = True
                    break
                legacy = LEGACY_LINE_RE.match(line.strip())
                if legacy:
                    text, _ = _split_metadata(legacy.group(2))
                    digest = hashlib.sha1(
                        f"{legacy.group(1)}{text}".encode("utf-8")
                    ).hexdigest()[:8]
                    if f"mem-legacy-{digest}" != entry_id:
                        continue
                    comment = _render_comment({"id": entry_id, "status": status})
                    lines[index] = f"- <!-- {comment} --> {legacy.group(1)} — {legacy.group(2)}"
                    changed = True
                    break
            if changed:
                meta, _ = self._read(path, path.stem)
                self._write(path, meta, "\n".join(lines) + "\n")
                return True
        return False

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
