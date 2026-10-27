from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..infra.config import LLMConfig
from ..llm.client import complete

MEMORY_CUE_RE = re.compile(
    r"你还记得|记得|之前|上次|我说过|我提过|以前|曾经|刚才说|"
    r"remember|previously|last time|i told you|i mentioned",
    re.IGNORECASE,
)
FOLLOWUP_RE = re.compile(
    r"^(那|这|它|他|她|他们|还有|然后|所以|附近|呢|吗|怎么|多少)|"
    r"^(and|what about|how about|then)\b",
    re.IGNORECASE,
)
ATTRIBUTE_RE = re.compile(
    r"叫什么|名字|姓名|怎么称呼|我多大|年龄|家乡|老家|住哪|住处|住址",
    re.IGNORECASE,
)
REWRITE_PROMPT = """把当前问题和最近对话合并成一个用于检索个人记忆的独立查询。
要求：
1. 保留人物、地点、时间、数字、型号和主题词。
2. 补充同义词和常见说法，例如“找房子”可以补充“租房、房源”。
3. 非中文的问题，先补上对应的中文说法（记忆库以中文为主）。
4. 不要回答问题，不要解释，只输出一行查询。"""


@dataclass(slots=True)
class RecallQuery:
    text: str
    cue: bool = False
    rewritten: bool = False


Completer = Callable[..., object]


class RecallQueryBuilder:
    """把当前消息补成可检索的 query；线索词或追问时结合最近用户消息。"""

    def __init__(
        self,
        llm: LLMConfig,
        *,
        rewrite: bool = True,
        context_turns: int = 2,
        completer: Completer = complete,
    ):
        self.llm = llm
        self.rewrite = rewrite
        self.context_turns = max(1, context_turns)
        self.completer = completer

    def build(self, text: str, recent_texts: list[str]) -> RecallQuery:
        text = text.strip()
        recent = [
            item.strip()
            for item in recent_texts
            if item.strip()
        ][-self.context_turns :]
        cue = bool(MEMORY_CUE_RE.search(text))
        followup = bool(FOLLOWUP_RE.search(text))
        attribute = bool(ATTRIBUTE_RE.search(text))
        needs_context = bool(recent) and (cue or followup or attribute)
        # 名字/年龄等属性问题即使没有“记得”线索也需要同义词改写。
        if not needs_context and not attribute:
            return RecallQuery(text, cue=cue)

        combined = " ".join([*recent, text])[-400:]
        if not self.rewrite or (self.llm.requires_api_key() and not self.llm.api_key()):
            return RecallQuery(combined, cue=cue)

        recent_block = "\n".join(f"- {item}" for item in recent)
        try:
            response = self.completer(
                self.llm,
                [
                    {"role": "system", "content": REWRITE_PROMPT},
                    {
                        "role": "user",
                        "content": f"最近对话：\n{recent_block}\n\n当前问题：\n{text}",
                    },
                ],
                temperature=0.0,
                max_tokens=120,
            )
            content = (response.choices[0].message.content or "").strip()
            query = content.splitlines()[0].strip().strip("\"'“”")
            if query:
                return RecallQuery(query[:200], cue=cue, rewritten=True)
        except Exception:
            pass
        return RecallQuery(combined, cue=cue)
