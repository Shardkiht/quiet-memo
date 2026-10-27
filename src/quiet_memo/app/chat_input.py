from __future__ import annotations

from rich.cells import cell_len
from rich.segment import Segment
from textual.strip import Strip
from textual.widgets import Input


class BarInput(Input):
    """Input with a thin vertical-bar cursor instead of a full-cell block."""

    _hint = ""

    def __init__(self, *args, **kwargs) -> None:
        # 提示词放边框：中文输入法组字期间 Textual 仍认为值为空，留在文字区会和组字重叠。
        hint = kwargs.pop("placeholder", "")
        kwargs["placeholder"] = ""
        super().__init__(*args, **kwargs)
        self._hint = hint
        self.border_title = hint or None

    def _watch_value(self, value: str) -> None:
        super()._watch_value(value)
        self.border_title = None if value else (self._hint or None)

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        if y != 0 or not self.has_focus or not self._cursor_visible:
            return strip

        # 在渲染结果里插竖线，不能改 _value：额外字符会让 Textual 的输入框重绘
        # 把背景刷到左侧，形成横贯侧栏的黑条。
        cursor_cell = cell_len(self.value[: self.cursor_position]) - self.scroll_offset.x
        if not 0 <= cursor_cell < strip.cell_length:
            return strip
        prefix = strip.crop(0, cursor_cell)
        rest = strip.crop(cursor_cell, strip.cell_length)
        # 插一格就要从行尾裁掉一格，保持整行宽度不变。
        tail = rest.crop(0, max(0, rest.cell_length - 1))
        bar = Strip([Segment("│", self.get_component_rich_style("input--cursor"))])
        return prefix + bar + tail
