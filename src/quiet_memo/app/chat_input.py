from __future__ import annotations

from rich.text import Text
from textual.widgets import Input


class BarInput(Input):
    """Input with a thin vertical-bar cursor instead of a full-cell block."""

    _hint = ""

    def __init__(self, *args, **kwargs) -> None:
        # 提示词放边框：中文输入法组字期间 Textual 仍认为值为空，留在文字区会和组字重叠。
        # 空值只保留占位竖线，组字内容覆盖它不会留下残影。
        hint = kwargs.pop("placeholder", "")
        kwargs["placeholder"] = "│" if hint else ""
        super().__init__(*args, **kwargs)
        self._hint = hint
        self.border_title = hint or None

    def _watch_value(self, value: str) -> None:
        super()._watch_value(value)
        self.border_title = None if value else (self._hint or None)

    @property
    def _value(self) -> Text:
        text = super()._value
        if not self.has_focus or not self._cursor_visible:
            return text
        cursor = min(self.cursor_position, len(self.value))
        bar = Text("│", style=self.get_component_rich_style("input--cursor"), end="")
        return text[:cursor] + bar + text[cursor:]
