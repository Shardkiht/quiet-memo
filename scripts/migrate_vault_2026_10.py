"""一次性迁移：vault 条目去小节前缀 + 碎片主题归并（issue #2）。

仅针对本地 vault 的一次性操作，不随包发布。默认 dry-run 只出报告；
--apply 落盘并自动验证（ID 集合一致、条数不变、无空正文）后提交留回滚点。

用法：
    uv run python scripts/migrate_vault_2026_10.py            # dry-run
    uv run python scripts/migrate_vault_2026_10.py --apply    # 实际迁移
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from quiet_memo.infra.config import load_config
from quiet_memo.infra.gitops import GitRepo
from quiet_memo.memory.service import MemoryService

# 行首小节前缀：1-2 位数字 + 空格 + 含 CJK 的短标签 + ｜（只剥第一段）。
# 日期开头（"2026-10-24 …"）因第 3 位非空格不会误中；无 ｜ 的正文不会误中。
PREFIX_RE = re.compile(r"^\d{1,2}\s+(?=[^｜]*[\u4e00-\u9fff])[^｜]{1,80}｜\s*")
ENTRY_RE = re.compile(r"^(- \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} — )(.*)$")
COMMENT_RE = re.compile(r"^(- <!-- .*? --> \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} — )(.*)$")

TOPIC_MOVES = {
    "food": "08-coffee-food-routine",
    "gaming": "10-entertainment",
    "preferences": "10-entertainment",
    "learning": "04-learning-projects",
    "projects": "04-learning-projects",
}
CANONICAL_TOPICS = {
    "01-identity", "02-collaboration", "03-career", "04-learning-projects",
    "05-devices-software", "06-peripherals-audio", "07-housing-life",
    "08-coffee-food-routine", "09-health", "10-entertainment",
    "11-private-purchases", "12-growth-people",
}


def strip_prefix(text: str) -> str:
    return PREFIX_RE.sub("", text, count=1)


def transform_body(body: str) -> tuple[str, list[tuple[str, str]]]:
    """返回（新正文, [(原文, 去前缀后)]）。仅改正文段，标签原样保留。"""
    samples: list[tuple[str, str]] = []
    out_lines = []
    for line in body.splitlines():
        m = ENTRY_RE.match(line) or COMMENT_RE.match(line)
        if m and PREFIX_RE.match(m.group(2)):
            new_text = strip_prefix(m.group(2))
            samples.append((m.group(2), new_text))
            out_lines.append(m.group(1) + new_text)
        else:
            out_lines.append(line)
    return "\n".join(out_lines) + ("\n" if body.endswith("\n") else ""), samples


def entry_ids(service: MemoryService) -> set[str]:
    return {entry.id for entry in service.store.entries(include_inactive=True)}


def main() -> None:
    parser = argparse.ArgumentParser(description="vault 一次性迁移（去前缀 + 主题归并）")
    parser.add_argument("--apply", action="store_true", help="实际落盘；默认 dry-run")
    args = parser.parse_args()

    config = load_config()
    service = MemoryService(config)
    vault: Path = config.vault
    if not (vault / ".git").exists():
        raise SystemExit("vault 不是 git 仓库，拒绝迁移。")

    before_ids = entry_ids(service)
    before_count = len(service.store.entries(include_inactive=True))

    # ---- 动作一：去前缀 ----
    stripped_total = 0
    stripped_samples: list[tuple[str, str, str]] = []
    for path in sorted((vault / "memories").glob("*.md")):
        meta, body = service.store._read(path, path.stem)
        new_body, samples = transform_body(body)
        if not samples:
            continue
        stripped_total += len(samples)
        for old, new in samples[:3]:
            stripped_samples.append((path.stem, old[:48], new[:48]))
        if args.apply:
            service.store._write(path, meta, new_body)

    # ---- 动作二：主题归并 ----
    moved: dict[str, int] = {}
    if args.apply:
        service.store.ensure()
    for src, dst in TOPIC_MOVES.items():
        src_path = service.store.topic_path(src)
        if not src_path.exists():
            continue
        meta, body = service.store._read(src_path, src)
        entry_lines = [line for line in body.splitlines() if line.startswith("- ")]
        if not entry_lines:
            continue
        moved[src] = len(entry_lines)
        if args.apply:
            dst_path = service.store.topic_path(dst)
            dst_meta, dst_body = service.store._read(dst_path, dst)
            dst_meta["updated"] = meta.get("updated") or dst_meta.get("updated")
            from datetime import date

            dst_meta["updated"] = date.today().isoformat()
            service.store._write(dst_path, dst_meta, dst_body.rstrip() + "\n" + "\n".join(entry_lines) + "\n")
            src_path.unlink()

    # ---- pending.jsonl 的 topic 同步映射 ----
    pending_path = vault / ".quiet-memo" / "pending.jsonl"
    pending_remapped = 0
    if pending_path.exists() and TOPIC_MOVES:
        lines = []
        for line in pending_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("topic") in TOPIC_MOVES:
                record["topic"] = TOPIC_MOVES[record["topic"]]
                pending_remapped += 1
            lines.append(json.dumps(record, ensure_ascii=False))
        if args.apply and pending_remapped:
            pending_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- 验证 ----
    after_ids = entry_ids(service)
    after_count = len(service.store.entries(include_inactive=True))
    topics_now = {path.stem for path in (vault / "memories").glob("*.md")}
    checks = {
        "ID 集合一致": before_ids == after_ids,
        "条目总数不变": before_count == after_count,
    }
    if args.apply:
        # 归并类检查只对落盘后的状态有意义；dry-run 必然未清。
        checks["碎片主题已清"] = not (topics_now & set(TOPIC_MOVES))
        checks["主题收敛 canonical"] = topics_now <= CANONICAL_TOPICS
    failed = [name for name, ok in checks.items() if not ok]

    # ---- 报告 ----
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] vault={vault}")
    print(f"去前缀：{stripped_total} 条")
    for topic, old, new in stripped_samples:
        print(f"  [{topic}] {old}…  →  {new}…")
    print(f"归并：{sum(moved.values())} 条  {moved}")
    print(f"pending topic 重映射：{pending_remapped} 条")
    print(f"验证：{'全部通过' if not failed else '失败 → ' + '；'.join(failed)}")
    if args.apply and failed:
        print("验证未过，已停止提交；可用 git -C vault 检查或回滚。")
        raise SystemExit(1)
    if args.apply:
        repo = GitRepo(vault)
        repo.stage_all()
        commit = repo.commit(
            f"migrate: strip section prefixes ({stripped_total}), consolidate topics ({sum(moved.values())})"
        )
        print(f"已提交回滚点：{commit}")
    else:
        print("dry-run 未落盘；确认无误后加 --apply。")


if __name__ == "__main__":
    main()
