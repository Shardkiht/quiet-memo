from __future__ import annotations

import json
import re
from typing import Protocol

from ..infra.config import LLMConfig
from ..llm.client import complete
from .models import Candidate, ExtractionResult


class ExtractorError(RuntimeError):
    pass


class Extractor(Protocol):
    def extract(self, text: str, context: str = "") -> ExtractionResult: ...


SYSTEM_PROMPT = """你是一个个人记忆抽取器。从对话内容中抽取用户本人明确陈述、值得长期保留的信息。

规则：
1. 只记录用户本人明确说出的事实、偏好、计划、经历、关系、设备、日期、预算等；不要把助手的建议、推测、提问当作本人事实。
2. 用户转述他人的经历时，不是本人事实，除非与本人明确相关，否则不要抽取。
3. 每条记忆是一个独立、可检索的短句，保留具体型号、数字、日期、预算。
4. topic 用英文小写 kebab-case，如 preferences、career、health、devices、projects、housing、food、people、schedule、learning。
5. 与用户本人无关的寒暄、临时指令不要抽取。
6. 新信息可能与既有记忆冲突、或涉及健康/财务/隐私等敏感内容时，设置 kind=conflict/update 或 sensitive=true。
7. 不确定时降低 confidence（0 到 1），不要编造。
8. 既有记忆仅用于判断新增、更新还是冲突，不要重复输出已有内容。

输出 JSON，格式：
{"memories":[{"text":"...","topic":"...","title":"中文主题名","confidence":0.9,"sensitive":false,"kind":"add","reason":""}]}

kind 取 add（新增）、update（更新既有事实）、conflict（与既有事实冲突）。没有可记内容返回 {"memories":[]}。"""


def _strip_fences(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    return content.strip()


def _extract_json_block(content: str) -> str:
    content = _strip_fences(content)
    if content.startswith("["):
        end = content.rfind("]")
        if end == -1:
            raise ExtractorError(f"LLM 未返回完整 JSON：{content[:200]}")
        return content[: end + 1]
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ExtractorError(f"LLM 未返回 JSON：{content[:200]}")
    return content[start : end + 1]


def parse_extraction(content: str) -> ExtractionResult:
    payload = json.loads(_extract_json_block(content))
    if isinstance(payload, list):
        payload = {"memories": payload}
    if not isinstance(payload, dict):
        raise ExtractorError("LLM 返回结构不是对象")
    candidates = []
    for item in payload.get("memories", []):
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            continue
        item.setdefault("topic", "uncategorized")
        candidates.append(Candidate.model_validate(item))
    return ExtractionResult(memories=candidates)


class LLMExtractor:
    def __init__(self, config: LLMConfig):
        self.config = config

    def extract(self, text: str, context: str = "") -> ExtractionResult:
        if self.config.requires_api_key() and not self.config.api_key():
            raise ExtractorError(
                f"未配置 LLM API Key，请设置环境变量 {self.config.api_key_env}，"
                "或运行 qm setup 保存到系统凭据。"
            )
        user_content = f"既有记忆：\n{context or '（暂无）'}\n\n对话内容：\n{text}"
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        try:
            response = complete(
                self.config,
                messages,
                temperature=self.config.temperature,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            message = str(exc).lower()
            if not any(
                token in message
                for token in ("response_format", "json_object", "json mode", "unsupported", "invalid parameter")
            ):
                raise ExtractorError(f"LLM 调用失败：{exc}") from exc
            # 部分 OpenAI 兼容服务不支持 JSON mode，退回普通对话。
            response = complete(self.config, messages, temperature=self.config.temperature)
        content = response.choices[0].message.content or ""
        return parse_extraction(content)


class StaticExtractor:
    """测试与离线演示用的固定抽取器。"""

    def __init__(self, result: ExtractionResult):
        self.result = result

    def extract(self, text: str, context: str = "") -> ExtractionResult:
        return self.result
