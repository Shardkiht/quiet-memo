from __future__ import annotations

from pathlib import Path

from .models import Inference


class InferenceStore:
    """仅追加的独立推断记录；状态确认及来源失效处理由后续流程实现。"""

    def __init__(self, vault: Path) -> None:
        self.path = Path(vault) / ".quiet-memo" / "inferences.jsonl"

    def all(self) -> list[Inference]:
        if not self.path.exists():
            return []
        records: dict[str, Inference] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = Inference.model_validate_json(line)
                records[item.id] = item
        return list(records.values())

    def add(self, item: Inference) -> Inference:
        existing = next((record for record in self.all() if record.id == item.id), None)
        if existing is not None:
            return existing
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(item.model_dump_json() + "\n")
        return item
