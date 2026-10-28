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


def format_tokens(value: int) -> str:
    """把 token 数格式化为 K/M 单位，如 64000 → 64K、1000000 → 1M。"""
    if value >= 1_000_000:
        return f"{value / 1_000_000:g}M"
    if value >= 1000:
        return f"{value / 1000:g}K"
    return str(value)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    suffix = "\n…（已截断）"
    # 截断标记也占预算：先预留，否则返回值必然超过 max_tokens。
    budget = max_tokens - estimate_tokens(suffix)
    if budget < 1:
        suffix = ""
        budget = max_tokens
    encoding = _encoding()
    if encoding is not None:
        try:
            tokens = encoding.encode(text, disallowed_special=())
            prefix = encoding.decode(tokens[:budget], errors="ignore")
            while prefix and estimate_tokens(prefix + suffix) > max_tokens:
                prefix = prefix[:-1]
            return prefix + suffix
        except Exception:
            pass
    # 字符估算含固定开销，极小预算可能连一个汉字也放不下。
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle] + suffix) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low] + suffix
