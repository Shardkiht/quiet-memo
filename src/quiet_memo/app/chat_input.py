from __future__ import annotations

from rich.text import Text
from textual.strip import Strip
from textual.widgets import Input


class BarInput(Input):
    """竖线光标输入框：Textual 默认把光标渲染成整格反色块，中文下显得很粗。"""

    def render_line(self, y: int) -> Strip:
        # 基于 Textual 8.x Input.render_line；升级 Textual 时需同步这段渲染逻辑。
        if y != 0:
            return Strip.blank(self.size.width, self.rich_style)

        console = self.app.console
        console_options = self.app.console_options
        max_content_width = self.scrollable_content_region.width
        cursor_style = self.get_component_rich_style("input--cursor")

        if not self.value:
            placeholder = Text(self.placeholder, justify="left", end="")
            placeholder.stylize(self.get_component_rich_style("input--placeholder"))
            if self.has_focus and self._cursor_visible:
                placeholder = Text("│", style=cursor_style, end="") + placeholder
            strip = Strip(
                console.render(
                    placeholder, console_options.update_width(max_content_width + 1)
                )
            )
        else:
            result = self._value
            value = self.value
            value_length = len(value)
            suggestion = self._suggestion
            show_suggestion = len(suggestion) > value_length and self.has_focus
            if show_suggestion:
                result += Text(
                    suggestion[value_length:],
                    self.get_component_rich_style("input--suggestion"),
                    end="",
                )

            if self.has_focus:
                if not self.selection.is_empty:
                    start, end = sorted(self.selection)
                    result.stylize_before(
                        self.get_component_rich_style("input--selection"), start, end
                    )
                if self._cursor_visible:
                    cursor = self.cursor_position
                    if not show_suggestion and self.cursor_at_end:
                        result.pad_right(1)
                    result = (
                        result[:cursor]
                        + Text("│", style=cursor_style, end="")
                        + result[cursor:]
                    )

            segments = list(
                console.render(
                    result, console_options.update_width(max_content_width + 1)
                )
            )
            strip = Strip(segments)
            scroll_x, _ = self.scroll_offset
            strip = strip.crop(scroll_x, scroll_x + max_content_width + 1)
            strip = strip.extend_cell_length(max_content_width + 1)

        return strip.apply_style(self.rich_style)
