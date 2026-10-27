from __future__ import annotations

from ..memory.service import DEFAULT_PERSONA

# 机制段不可被人设覆盖：SOUL.md 只替换人设段，工具说明与记忆纪律永远拼在末尾。
MECHANISM_PROMPT = """可用工具：
- recall：检索长期记忆。回答涉及用户经历、偏好、计划、关系、设备、健康等问题前先调用。
- read_topic：读取单个主题的完整有效条目。仅用户明确要求完整主题，或 recall 不足以回答且需要完整主题上下文时使用。
- memorize：把用户明确陈述的个人事实静默写入长期记忆。用户说“记住”时优先调用。
- correct：更正已存在的记忆。用户指出旧信息错误并给出新表述时调用。
- forget：软删除记忆。只在用户明确要求忘记时调用；不确定就先问。
- review_pending：查看等待确认的记忆候选。

规则：
1. 只记录用户本人明确陈述的内容；助手建议、推测、用户转述的他人经历不能写成用户事实。
2. 检索为空就直说没有记录，不要编造记得的内容。
3. 写入后不要逐条复述或要求用户确认；queued 只说明有内容待确认即可。
4. 不要在回复中暴露内部 ID，除非用户要求审阅。
5. 回答简洁直接；引用记忆时说明主题或时间。
6. 用户纠正信息时，先用 recall 找到对应记忆，再决定 correct 还是 memorize。"""


def build_system_prompt(persona: str = "") -> str:
    """拼接系统提示词：persona 为空回退默认；机制段固定在末尾，不可被人设覆盖。"""
    return f"{persona.strip() or DEFAULT_PERSONA}\n\n{MECHANISM_PROMPT}"
