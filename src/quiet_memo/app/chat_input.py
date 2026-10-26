from __future__ import annotations

from rich.text import Text
from textual.widgets import Input


class BarInput(Input):
    """竖线光标输入框：Textual 默认把光标渲染成整格反色块，中文下显得很粗。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._hint = self.placeholder or ""

    def on_focus(self) -> None:
        # 空输入聚焦时用“竖线 + 提示语”占位；失焦后恢复原提示。
        self._hint = self.placeholder or self._hint
        self.placeholder = f"│ {self._hint}" if self._hint else "│"

    def on_blur(self) -> None:
        self.placeholder = self._hint

    @property
    def _value(self) -> Text:
        text = super()._value
        if not self.has_focus or not self._cursor_visible:
            return text
        cursor = min(self.cursor_position, len(self.value))
        bar = Text("│", style=self.get_component_rich_style("input--cursor"), end="")
        return text[:cursor] + bar + text[cursor:]
