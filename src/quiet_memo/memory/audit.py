from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class AuditLog:
    def __init__(self, vault: Path):
        self.path = Path(vault) / ".quiet-memo" / "audit.jsonl"

    def write(self, action: str, **fields) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"at": datetime.now().isoformat(timespec="seconds"), "action": action, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
