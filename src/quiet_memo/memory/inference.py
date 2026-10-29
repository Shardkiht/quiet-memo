from __future__ import annotations

from pathlib import Path
import json

from .models import Inference


class InferenceStore:
    """仅追加的推断状态快照；更正的两个状态在同一事件内提交。"""

    def __init__(self, vault: Path) -> None:
        self.path = Path(vault) / ".quiet-memo" / "inferences.jsonl"

    def all(self) -> list[Inference]:
        if not self.path.exists():
            return []
        return self._project(self.path.read_bytes().splitlines())

    @staticmethod
    def _project(lines: list[bytes]) -> list[Inference]:
        records: dict[str, Inference] = {}
        payloads = []
        errors: dict[int, bytes] = {}
        recovered: set[int] = set()
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                errors[number] = line
                continue
            if not isinstance(payload, dict):
                Inference.model_validate(payload)
            if payload.get("type") == "recovery":
                ignored = payload.get("ignored_line")
                if type(ignored) is not int or ignored not in errors or ignored >= number:
                    raise ValueError("推断恢复标记无效。")
                recovered.add(ignored)
            else:
                payloads.append(payload)
        for number, line in errors.items():
            if number not in recovered:
                Inference.model_validate_json(line)
        for payload in payloads:
            snapshots = payload["records"] if payload.get("type") == "revision" else [payload]
            if not isinstance(snapshots, list) or not snapshots:
                raise ValueError("推断状态事件不能为空。")
            for snapshot in snapshots:
                item = Inference.model_validate(snapshot)
                records[item.id] = item
        return list(records.values())

    def recover_tail(self) -> bool:
        """显式恢复截断尾行，追加标记保留原始字节；中间损坏不能恢复。"""
        if not self.path.exists():
            return False
        lines = self.path.read_bytes().splitlines()
        if not lines:
            return False
        if not lines[-1].strip():
            self.all()
            return False
        try:
            json.loads(lines[-1])
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._project(lines[:-1])
            self._append({"type": "recovery", "ignored_line": len(lines)})
            return True
        self.all()
        return False

    def _append(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell():
                handle.seek(-1, 2)
                if handle.read(1) != b"\n":
                    handle.write(b"\n")
            handle.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))

    def add(self, item: Inference) -> Inference:
        existing = next((record for record in self.all() if record.id == item.id), None)
        if existing is not None:
            return existing
        self._append(item.model_dump())
        return item

    def record(self, *items: Inference) -> None:
        # 先完整读取验证，不能在损坏日志后继续写入并掩盖错误。
        self.all()
        payload = {"type": "revision", "records": [item.model_dump() for item in items]}
        self._append(payload)
