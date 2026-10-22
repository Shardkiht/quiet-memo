from __future__ import annotations

import re

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_ENCODING = None


def _encoding():
    """优先用 tiktoken；首次使用时下载编码表失败则退回字符估算。"""
    global _ENCODING
    if _ENCODING is None:
        try:
            import tiktoken

            _ENCODING = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENCODING = False
    return _ENCODING or None


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    encoding = _encoding()
    if encoding is not None:
        try:
            return len(encoding.encode(text, disallowed_special=()))
        except Exception:
            pass
    cjk = len(CJK_RE.findall(text))
    return int(cjk + (len(text) - cjk) / 4) + 1


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        total += 4
        total += estimate_tokens(str(message.get("content") or ""))
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            total += estimate_tokens(str(function.get("name", "")))
            total += estimate_tokens(str(function.get("arguments", "")))
    return total


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    encoding = _encoding()
    if encoding is not None:
        try:
            tokens = encoding.encode(text, disallowed_special=())
            return encoding.decode(tokens[:max_tokens]) + "\n…（已截断）"
        except Exception:
            pass
    limit = max(1, int(len(text) * max_tokens / max(estimate_tokens(text), 1)))
    return text[:limit] + "\n…（已截断）"
