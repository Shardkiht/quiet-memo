from __future__ import annotations

import json
import re
from typing import Protocol

from pydantic import ValidationError

from ..infra.config import LLMConfig
from ..llm.client import complete
from .models import Candidate, ExtractionResult


class ExtractorError(RuntimeError):
    pass


class Extractor(Protocol):
    def extract(self, text: str, context: str = "") -> ExtractionResult: ...


CONTEXT_PROMPT = """

可选上下文 context（没有依据时省略整个字段或相应子字段）：
- event_time 是事件/事实发生日期，valid_from / valid_to 是明确提供的生效起止日期。
  日期只接受 YYYY、YYYY-MM、YYYY-MM-DD，保留原有精度，不能补猜月日。
  time_text 保留时间原话。没有可靠的原始陈述日期时，“去年”“现在”等只写 time_text，
  不填 event_time / valid_from / valid_to；禁止用抽取或写入当天代替原始陈述时间。
  日期沿用原始表述的日历日期，不做时区换算；未给结束时间不代表永远有效。
- location 仅取用户明确提供或可靠上下文中的地点；同时提供 location_kind：
  event（事件地点）或 residence（居住地）。事件地点不能默认成当前住址，不采集设备位置。
- emotion 只取用户明确表达的情绪原话，必须标记 emotion_source="explicit"；
  emotion_at="event" 表示事件当时，"expression" 表示如今谈起的感受，无法区分时用 expression。
  反讽、玩笑、模糊语气与模型推断不写情绪字段，不推断心理疾病或敏感人格标签。
- 上下文是补充，正文仍须保留日期、地点、情绪及不确定性的限定，不能靠元数据改变原意。
例如："context":{"event_time":"2024-06","time_text":"2024年6月","location":"合成展馆","location_kind":"event"}。
只输出这些已定义字段，不能在 context 中写置信度、推断标签或其他新字段。
"""


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


SYSTEM_PROMPT += CONTEXT_PROMPT


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


IMPORT_PROMPT = """你是个人记忆导入器。把给定的档案或笔记内容整理成规范的记忆条目。

规则：
1. 一条 = 一个原子事实，改写成自包含短句，脱离原文也能读懂；不保留“本节”“上文”这类指代。
2. 保留全部数字、日期、型号、价格、预算原文，不改写、不四舍五入。
3. 保留确定性边界：原文的“当时记录、未验证”“两条口径矛盾都保留”“未形成决定”“仅询问过”等标注必须体现在条目中，不得抹掉或合并。
4. 相互矛盾的记录各自成条，不合并、不替档案主人裁决。
5. 只整理档案主人本人的事实、偏好、计划、经历、设备、健康等；维护说明、导航索引、拆分注记、纯标题行不要输出。
6. topic 用英文小写 kebab-case 规范名，如 preferences、career、health、devices、housing、food、people、schedule、learning、projects、identity。
7. 明确事实 confidence 给 0.9 左右；带“未验证、存疑、矛盾、仅提及”边界的给 0.5—0.7，留给人工确认。
8. 正文里不要保留小节编号或标题前缀（如“01 基本身份｜”），主题信息由 topic/title 承担；超短条目（≤12 字）尤其要保持正文就是事实本身，前缀会稀释向量语义导致检索不到。

输出 JSON，格式：
{"memories":[{"text":"...","topic":"preferences","title":"中文主题名","confidence":0.9}]}

没有可整理内容返回 {"memories":[]}。"""


IMPORT_PROMPT += CONTEXT_PROMPT


def parse_import(content: str) -> ExtractionResult:
    """严格校验导入输出：任一条目不合法即抛错，由上层回传 AI 重出。"""
    payload = json.loads(_extract_json_block(content))
    if isinstance(payload, list):
        payload = {"memories": payload}
    items = payload.get("memories") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ExtractorError("顶层缺少 memories 数组")
    candidates = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            raise ExtractorError(f"第 {index + 1} 条缺少 text")
        item.setdefault("topic", "uncategorized")
        try:
            candidates.append(Candidate.model_validate(item))
        except ValidationError as exc:
            reason = exc.errors()[0]["msg"]
            raise ExtractorError(f"第 {index + 1} 条字段不合法：{reason}") from exc
    return ExtractionResult(memories=candidates)


class ImportExtractor:
    """导入抽取：输出必须通过严格校验，失败带着错误回传重出，连续失败整块放弃。"""

    def __init__(self, config: LLMConfig, completer=complete, max_attempts: int = 3, topics: list[str] | None = None):
        self.config = config
        self.completer = completer
        self.max_attempts = max_attempts
        # vault 已有主题：新条目优先归入既有主题，避免增量导入产生碎片主题。
        self.topics = [topic for topic in (topics or []) if topic]

    def extract(self, text: str) -> ExtractionResult:
        if self.config.requires_api_key() and not self.config.api_key():
            raise ExtractorError(
                f"未配置 LLM API Key，请设置环境变量 {self.config.api_key_env}，"
                "或运行 qm setup 保存到系统凭据。"
            )
        topic_hint = (
            f"\n\nvault 已有主题（新条目优先归入这里的既有主题，确属新领域才新建）：\n{'、'.join(self.topics)}"
            if self.topics
            else ""
        )
        user_content = f"待整理内容：\n{text}{topic_hint}"
        feedback = ""
        for _ in range(self.max_attempts):
            content = user_content if not feedback else f"{user_content}\n\n上一轮输出被拒绝：{feedback}\n请修正后重新输出完整 JSON。"
            response = self._complete(content)
            try:
                return parse_import(response.choices[0].message.content or "")
            except (ExtractorError, json.JSONDecodeError) as exc:
                feedback = str(exc)[:300]
        raise ExtractorError(f"导入抽取连续 {self.max_attempts} 次未通过格式校验：{feedback}")

    def _complete(self, content: str):
        messages = [
            {"role": "system", "content": IMPORT_PROMPT},
            {"role": "user", "content": content},
        ]
        try:
            return self.completer(
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
            return self.completer(self.config, messages, temperature=self.config.temperature)


def split_chunks(text: str, max_chars: int = 6000) -> list[str]:
    """按空行段落切块；只控制大小，语义整理交给导入抽取器。"""
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        while len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:]
        if not paragraph.strip():
            continue
        if current and len(current) + len(paragraph) + 2 > max_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current.strip():
        chunks.append(current)
    return chunks
