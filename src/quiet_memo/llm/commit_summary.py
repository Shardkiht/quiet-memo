from __future__ import annotations

import re

from ..infra.config import LLMConfig
from .client import complete


SUMMARY_PROMPT = """你是个人记忆库的提交总结器。根据 git diff 生成给本人审阅的中文摘要。

要求：
- 2 到 4 条短句，说明这次新增、更新或删除了哪些记忆、涉及哪些主题。
- 只依据 diff 内容，不要推测动机或编造。
- 不要输出 Markdown 列表符号或标题，每行一条。
- 如果只有格式或元数据变化，直接说明。"""


def fallback_summary(staged_stat: str) -> str:
    files: list[str] = []
    total = ""
    for line in staged_stat.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            files.append(line.split("|", 1)[0].strip())
        elif "changed" in line:
            total = line
    if not files:
        return "memory: 更新记忆库"
    listed = "、".join(files[:6]) + ("等" if len(files) > 6 else "")
    head = f"memory: 更新 {listed}"
    return f"{head}\n（{total}）" if total else head


def _clean(text: str) -> str:
    lines = []
    for line in text.strip().splitlines():
        line = re.sub(r"^[-*•\d.、\s]+", "", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines[:4])


def summarize_diff(diff: str, config: LLMConfig, staged_stat: str = "") -> str:
    fallback = fallback_summary(staged_stat)
    if not diff.strip() or (config.requires_api_key() and not config.api_key()):
        return fallback
    try:
        response = complete(
            config,
            [
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": diff},
            ],
            temperature=0.2,
        )
        content = _clean(response.choices[0].message.content or "")
        return content or fallback
    except Exception:
        return fallback
