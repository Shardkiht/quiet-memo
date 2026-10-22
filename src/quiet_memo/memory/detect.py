from __future__ import annotations

import re

from .models import Candidate, Entry

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
NEGATION_RE = re.compile(r"(不|没|无|非|未|别|勿)")
SECRET_RE = re.compile(
    r"(api[_ -]?key|access[_ -]?token|secret|password|passwd|密码|口令|验证码|私钥)"
    r"\s*(是|为|[:：=])?\s*\S{4,}",
    re.I,
)
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"\s+", "", text.lower())
    chars = CJK_RE.findall(cleaned)
    if len(chars) < 2:
        return set(chars)
    return {chars[index] + chars[index + 1] for index in range(len(chars) - 1)}


def similarity(left: str, right: str) -> float:
    left_set, right_set = _bigrams(left), _bigrams(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def contains_secret(text: str) -> bool:
    return bool(SECRET_RE.search(text))


def find_conflicts(candidate: Candidate, entries: list[Entry]) -> list[Entry]:
    conflicts: list[Entry] = []
    candidate_numbers = set(NUMBER_RE.findall(candidate.text))
    for entry in entries:
        score = similarity(candidate.text, entry.text)
        same_topic = entry.topic == candidate.topic
        if score >= 0.85:
            conflicts.append(entry)
            continue
        entry_numbers = set(NUMBER_RE.findall(entry.text))
        numbers_differ = bool(candidate_numbers and entry_numbers and candidate_numbers != entry_numbers)
        negation_differs = bool(NEGATION_RE.search(candidate.text)) != bool(
            NEGATION_RE.search(entry.text)
        )
        if same_topic and numbers_differ and score >= 0.35:
            conflicts.append(entry)
        elif same_topic and negation_differs and score >= 0.45:
            conflicts.append(entry)
    return conflicts[:3]
