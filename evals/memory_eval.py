from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from quiet_memo.infra.config import Config, RetrievalConfig
from quiet_memo.memory.extractor import StaticExtractor, parse_extraction
from quiet_memo.memory.service import MemoryService


@dataclass(frozen=True)
class Case:
    name: str
    text: str = "合成事实"
    expected: str = "admitted"
    confidence: float = 0.95
    sensitive: bool = False
    kind: str = "add"
    context: dict | None = None
    seed: str | None = None


# 固定模型输出，评估解析、存储与裁决；不冒充真实模型的语义抽取准确率。
CASES = [
    Case("明确事实"),
    Case("低置信候选", expected="queued", confidence=0.5),
    Case("敏感候选", expected="queued", sensitive=True),
    Case("更新候选", expected="queued", kind="update"),
    Case("冲突候选", expected="queued", kind="conflict"),
    Case("完全重复", text="喜欢纸质阅读", seed="喜欢纸质阅读", expected="duplicates"),
    Case("空白重复", text="喜欢 纸质 阅读", seed="喜欢纸质阅读", expected="duplicates"),
    Case("数字冲突", text="每周阅读 5 次", seed="每周阅读 2 次", expected="queued"),
    Case("否定冲突", text="不喜欢纸质阅读", seed="喜欢纸质阅读", expected="queued"),
    Case("凭据拦截", text="密码是 synthetic-only", expected="blocked"),
    Case("年份精度", context={"event_time": "2024"}),
    Case("月份精度", context={"event_time": "2024-06"}),
    Case("日期精度", context={"event_time": "2024-02-29"}),
    Case("无效日期", context={"event_time": "2023-02-29"}, expected="invalid"),
    Case("未知相对时间", context={"time_text": "去年"}),
    Case("生效区间", context={"valid_from": "2024-06", "valid_to": "2025"}),
    Case("倒置区间", context={"valid_from": "2025", "valid_to": "2024"}, expected="invalid"),
    Case("事件地点", context={"location": "合成展馆", "location_kind": "event"}),
    Case("居住地点", context={"location": "合成居所", "location_kind": "residence"}),
    Case("缺少地点类型", context={"location": "合成展馆"}, expected="invalid"),
    Case("当时情绪", context={"emotion": "当时开心", "emotion_at": "event", "emotion_source": "explicit"}),
    Case("当前表达", context={"emotion": "如今想起感到遗憾", "emotion_at": "expression", "emotion_source": "explicit"}),
    Case("推断情绪拒绝", context={"emotion": "似乎不开心", "emotion_source": "inferred"}, expected="invalid"),
    Case("上下文凭据拦截", context={"location": "密码是 synthetic-only", "location_kind": "event"}, expected="blocked"),
]


def run_case(case: Case, vault: Path) -> str | None:
    candidate = {
        "text": case.text, "topic": "synthetic", "confidence": case.confidence,
        "kind": case.kind, "sensitive": case.sensitive, "context": case.context,
    }
    try:
        extraction = parse_extraction(json.dumps({"memories": [candidate]}, ensure_ascii=False))
    except ValueError as exc:
        return None if case.expected == "invalid" else f"解析失败：{exc}"
    if case.expected == "invalid":
        return "应拒绝无效上下文，但解析成功"
    service = MemoryService(
        Config(vault=vault, retrieval=RetrievalConfig(method="keyword")),
        extractor=StaticExtractor(extraction),
    )
    seed = service.store.append("synthetic", case.seed) if case.seed else None
    result = service.memorize(case.text, source="synthetic-eval")
    counts = {key: len(getattr(result, key)) for key in ("admitted", "queued", "duplicates", "blocked")}
    if counts.get(case.expected) != 1 or sum(counts.values()) != 1:
        return f"预期 {case.expected}，实际 {counts}"
    if seed and case.expected == "queued" and result.queued[0].conflict_with != seed.id:
        return "冲突来源 ID 丢失"
    if result.admitted:
        entry = service.store.find(result.admitted[0].id)
        if entry.context != extraction.memories[0].context:
            return "上下文落盘后发生变化"
    if case.expected == "blocked" and service.store.entries():
        return "被拦截内容仍然落盘"
    return None


def main() -> int:
    failures = []
    with TemporaryDirectory(prefix="qm-memory-eval-") as directory:
        for index, case in enumerate(CASES):
            error = run_case(case, Path(directory) / str(index))
            if error:
                failures.append((case.name, error))
        metrics = run_contextual_eval(Path(directory) / "contextual")
    print(f"合成解析与裁决回归：{len(CASES) - len(failures)}/{len(CASES)} 通过")
    for name, error in failures:
        print(f"失败：{name}：{error}")
    print("固定模型输出；不调用真实模型，不衡量自然语言语义抽取效果。")
    print(json.dumps(metrics, ensure_ascii=False))
    return 1 if failures else 0


def run_contextual_eval(vault: Path) -> dict:
    """小型合成对照，比较不带上下文的关键词检索与明确时间/地点/情绪条件。"""
    from quiet_memo.memory.models import MemoryContext

    service = MemoryService(Config(vault=vault, retrieval=RetrievalConfig(method="keyword")))
    old = service.store.append("preferences", "合成偏好早期状态", context=MemoryContext(valid_from="2020-01-01"))
    new = service.evolve(old.id, "合成偏好后期状态", "2024-01-01")
    target = service.store.append("events", "合成展览活动", context=MemoryContext(
        event_time="2024-06", location="合成展馆甲", location_kind="event",
        emotion="期待", emotion_source="explicit"))
    service.store.append("events", "合成展览活动", context=MemoryContext(
        event_time="2025-06", location="合成展馆乙", location_kind="event",
        emotion="遗憾", emotion_source="explicit"))
    cases = [
        ("合成偏好", {"as_of": "2022-01-01"}, old.id),
        ("合成偏好", {}, new.id),
        ("合成展览", {"date_from": "2024", "date_to": "2024"}, target.id),
        ("合成展览", {"location": "合成展馆甲"}, target.id),
        ("合成展览", {"emotion": "期待"}, target.id),
    ]
    result = {"cases": len(cases)}
    for name in ("baseline", "contextual"):
        recall, reciprocal = 0.0, 0.0
        for query, filters, expected in cases:
            hits = service.retriever.search(query, 5) if name == "baseline" else service.recall(query, 5, **filters)
            ids = [hit.id for hit in hits]
            recall += float(bool(ids) and ids[0] == expected)
            reciprocal += 1 / (ids.index(expected) + 1) if expected in ids else 0
        result[name] = {"Recall@1": recall / len(cases), "MRR@5": reciprocal / len(cases)}
    return result


if __name__ == "__main__":
    raise SystemExit(main())
