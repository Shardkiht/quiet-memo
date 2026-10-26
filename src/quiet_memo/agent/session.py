from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..infra.tokens import estimate_tokens

SESSION_ID_RE = re.compile(r"^s-\d{8}-\d{6}-[0-9a-f]{4}$")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_message_id() -> str:
    stamp = datetime.now()
    return f"m-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


@dataclass
class Session:
    id: str
    started_at: str
    path: Path
    records: list[dict] = field(default_factory=list)

    @property
    def messages(self) -> list[dict]:
        return [record for record in self.records if record.get("type") == "message"]

    @property
    def updated_at(self) -> str:
        stamps = [record["at"] for record in self.records if record.get("at")]
        return stamps[-1] if stamps else self.started_at

    def user_texts(self) -> list[str]:
        return [
            record["content"]
            for record in self.records
            if record.get("type") == "message" and record.get("role") == "user"
        ]

    def last_user_message_id(self) -> str | None:
        for record in reversed(self.records):
            if record.get("type") == "message" and record.get("role") == "user":
                return record.get("id")
        return None

    def title(self, width: int = 40) -> str:
        for record in reversed(self.records):
            if record.get("type") == "title":
                return record.get("title", "")[:width]
        texts = self.user_texts()
        if not texts:
            return "(空会话)"
        title = texts[0].replace("\n", " ").strip()
        return title[:width] + ("…" if len(title) > width else "")

    def latest_summary(self) -> dict | None:
        for record in reversed(self.records):
            if record.get("type") == "summary":
                return record
        return None

    def summary_text(self) -> str:
        summary = self.latest_summary()
        return summary.get("content", "") if summary else ""

    def covered_records(self) -> int:
        summary = self.latest_summary()
        return int(summary.get("covered_records", 0)) if summary else 0

    def records_after_summary(self) -> list[dict]:
        return [
            record
            for record in self.records[self.covered_records() :]
            if record.get("type") in ("message", "assistant", "tool")
        ]

    def needs_consolidation(self) -> bool:
        """以沉淀游标为准：游标后还有用户消息就需要补记。

        正常退出/补记成功会推进游标；异常退出、抽取失败或恢复后再次崩溃时
        游标不动，因此不依赖 closed/consolidated 标记的历史顺序。
        """
        return bool(self.user_texts_after_distilled())

    def user_texts_after_summary(self) -> list[str]:
        return [
            record["content"]
            for record in self.records_after_summary()
            if record.get("type") == "message" and record.get("role") == "user"
        ]

    def _distill_cursor_index(self) -> int:
        # 最后一次成功沉淀覆盖到的消息记录位置；从未沉淀过则回退到摘要边界。
        for index in range(len(self.records) - 1, -1, -1):
            record = self.records[index]
            if record.get("type") == "distilled":
                up_to = record.get("up_to")
                if up_to:
                    message_index = next(
                        (j for j, r in enumerate(self.records) if r.get("id") == up_to),
                        None,
                    )
                    if message_index is not None:
                        return message_index
        return self.covered_records() - 1

    def user_texts_after_distilled(self) -> list[str]:
        """自上次沉淀游标（或摘要边界）之后的用户消息——避免来回切换重复抽取。"""
        start = max(self._distill_cursor_index(), self.covered_records() - 1)
        return [
            record["content"]
            for record in self.records[start + 1 :]
            if record.get("type") == "message" and record.get("role") == "user"
        ]

    def estimated_tokens(self) -> int:
        return sum(
            estimate_tokens(str(record.get("content") or ""))
            for record in self.records
            if record.get("type") in ("message", "assistant", "summary")
        )


class SessionStore:
    def __init__(self, vault: Path):
        self.root = Path(vault) / "sessions"

    def create(self) -> Session:
        stamp = datetime.now()
        self.root.mkdir(parents=True, exist_ok=True)
        session_id = f"s-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        path = self.root / f"{session_id}.jsonl"
        # 同秒创建可能撞 16 位后缀：撞了重摇，不能覆盖已有会话文件。
        while path.exists():
            session_id = f"s-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
            path = self.root / f"{session_id}.jsonl"
        meta = {
            "type": "meta",
            "id": session_id,
            "started_at": stamp.isoformat(timespec="seconds"),
            "cwd": str(Path.cwd()),
        }
        path.write_text(json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8")
        return Session(id=session_id, started_at=meta["started_at"], path=path, records=[meta])

    def exists(self, session_id: str) -> bool:
        return self._path(session_id).exists()

    def load(self, session_id: str) -> Session:
        path = self._path(session_id)
        if not path.exists():
            raise KeyError(f"会话不存在：{session_id}")
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        meta = records[0] if records else {"id": session_id, "started_at": ""}
        return Session(
            id=meta.get("id", session_id),
            started_at=meta.get("started_at", ""),
            path=path,
            records=records,
        )

    def append(self, session: Session, record: dict) -> dict:
        record.setdefault("at", now_iso())
        session.records.append(record)
        with session.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def rename(self, session: Session, title: str) -> None:
        self.append(session, {"type": "title", "title": title.strip()[:80]})

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list(self, limit: int = 20, with_tokens: bool = True) -> list[dict]:
        if not self.root.exists():
            return []
        sessions = []
        for path in self.root.glob("*.jsonl"):
            try:
                session = self.load(path.stem)
            except (json.JSONDecodeError, KeyError):
                continue
            sessions.append(
                {
                    "id": session.id,
                    "started_at": session.started_at,
                    "updated_at": session.updated_at,
                    "messages": len(session.messages),
                    # token 估算要遍历全部记录，侧栏高频刷新时传 with_tokens=False 跳过。
                    "tokens": session.estimated_tokens() if with_tokens else 0,
                    "summaries": sum(1 for record in session.records if record.get("type") == "summary"),
                    "title": session.title(),
                }
            )
        sessions.sort(key=lambda item: item["updated_at"], reverse=True)
        return sessions[:limit]

    def unconsolidated(self, limit: int = 3, exclude: str | None = None) -> list[Session]:
        """按最近更新优先，找出漏沉淀的会话（异常终止或退出抽取失败）。"""
        if not self.root.exists():
            return []
        found: list[tuple[str, Session]] = []
        for path in self.root.glob("*.jsonl"):
            if exclude and path.stem == exclude:
                continue
            try:
                session = self.load(path.stem)
            except (json.JSONDecodeError, KeyError):
                continue
            if session.needs_consolidation():
                found.append((session.updated_at, session))
        found.sort(key=lambda item: item[0], reverse=True)
        return [session for _, session in found[:limit]]

    def _path(self, session_id: str) -> Path:
        if not SESSION_ID_RE.match(session_id):
            raise KeyError(f"非法会话 ID：{session_id}")
        return self.root / f"{session_id}.jsonl"


def record_label(record: dict) -> str:
    record_type = record.get("type")
    if record_type == "message":
        return f"用户：{record.get('content', '')}"
    if record_type == "assistant":
        content = (record.get("content") or "").strip()
        tools = [call.get("name", "") for call in record.get("tool_calls") or []]
        suffix = f"（调用工具：{', '.join(tools)}）" if tools else ""
        return f"助手：{content}{suffix}"
    if record_type == "tool":
        return f"工具 {record.get('name')}：{record.get('content', '')}"
    if record_type == "summary":
        return f"此前摘要：{record.get('content', '')}"
    return ""


def transcript(records: list[dict]) -> str:
    return "\n".join(label for label in map(record_label, records) if label)
