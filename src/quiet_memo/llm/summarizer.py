from __future__ import annotations

from ..infra.config import LLMConfig
from .client import complete


class Summarizer:
    def summarize(self, previous: str, transcript: str, max_chars: int = 1200) -> str: ...


SUMMARY_PROMPT = """你是会话压缩器。把较早的对话压缩成中文摘要，供同一个助手继续对话。

必须保留：
- 用户明确陈述的事实、偏好、计划、决定、约束。
- 尚未解决的问题和用户明确提出的要求。
- 工具查证后得到的结论（不要保留工具调用过程）。

不要编造；不要记录助手自己的建议为用户事实。已有摘要可以合并进来。
只输出摘要正文，不要标题或 Markdown 代码块。"""


def fallback_summary(previous: str, transcript: str, max_chars: int = 1200) -> str:
    pieces = []
    if previous.strip():
        pieces.append(previous.strip())
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("用户："):
            pieces.append(line)
        elif line.startswith("助手："):
            pieces.append(line)
        elif line.startswith("工具 "):
            pieces.append(line[:160])
    text = "\n".join(pieces)
    return text[:max_chars] + ("…" if len(text) > max_chars else "")


class LLMSummarizer:
    def __init__(self, config: LLMConfig):
        self.config = config

    def summarize(self, previous: str, transcript: str, max_chars: int = 1200) -> str:
        if self.config.requires_api_key() and not self.config.api_key():
            return fallback_summary(previous, transcript, max_chars)
        try:
            user_content = (
                f"已有摘要：\n{previous or '（无）'}\n\n"
                f"需要压缩的对话：\n{transcript[:16000]}\n\n"
                f"输出不超过 {max_chars} 字。"
            )
            response = complete(
                self.config,
                [
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.2,
            )
            content = (response.choices[0].message.content or "").strip()
            return (
                content[:max_chars]
                if content
                else fallback_summary(previous, transcript, max_chars)
            )
        except Exception:
            return fallback_summary(previous, transcript, max_chars)


class SessionSummarizer(Summarizer):  # pragma: no cover - 兼容旧名
    pass
