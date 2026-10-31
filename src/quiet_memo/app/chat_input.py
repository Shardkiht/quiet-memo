from __future__ import annotations

import os
import sys

from rich.cells import cell_len
from rich.segment import Segment
from textual.geometry import Offset
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
        self._native_cursor_active = False
        self._hint = hint
        self.border_title = hint or None

    @staticmethod
    def _terminal_supports_native_cursor() -> bool:
        # conhost 不识别 DECSCUSR：转义序列会被画出来或光标错置。
        # Windows Terminal（WT_*）与 VS Code 等（TERM_PROGRAM）支持，其余退回自绘光标。
        return bool(
            os.environ.get("WT_SESSION")
            or os.environ.get("WT_PROFILE_ID")
            or os.environ.get("TERM_PROGRAM")
        )

    @property
    def _uses_native_cursor(self) -> bool:
        driver = self.app._driver
        return (
            sys.platform == "win32"
            and driver is not None
            and not driver.is_headless
            and self._terminal_supports_native_cursor()
        )

    @property
    def cursor_screen_offset(self) -> Offset:
        # Input adds an end-of-value cell for scroll visibility. The terminal
        # caret must instead sit at the actual insertion point (also when empty).
        offset = super().cursor_screen_offset
        return offset - Offset(1, 0) if self.cursor_at_end else offset

    def on_resize(self) -> None:
        # 折叠侧栏/窗口缩放会移动输入框，但 App 每帧只回放上次记录的光标坐标；
        # 不及时刷新硬件光标就会落在旧位置（看起来停在文字中间）。
        if self._native_cursor_active and self.has_focus:
            self.app.cursor_position = self.cursor_screen_offset

    def _restart_blink(self) -> None:
        if self._uses_native_cursor:
            self._pause_blink()
            self._cursor_visible = False
        else:
            super()._restart_blink()

    def _toggle_cursor(self) -> None:
        if not self._uses_native_cursor:
            super()._toggle_cursor()

    def on_focus(self) -> None:
        self.call_after_refresh(self._show_native_cursor)

    def _show_native_cursor(self) -> None:
        if self._uses_native_cursor and self.has_focus and not self.disabled:
            # IME preedit belongs to the terminal; a painted glyph cannot follow it.
            self.app._driver.write("\x1b[5 q\x1b[?25h")
            self._native_cursor_active = True

    def _hide_native_cursor(self) -> None:
        if self._native_cursor_active:
            if self.app._driver is not None:
                self.app._driver.write("\x1b[?25l\x1b[0 q")
            self._native_cursor_active = False

    def on_blur(self) -> None:
        self._hide_native_cursor()

    def on_unmount(self) -> None:
        self._hide_native_cursor()

    def set_hint(self, hint: str) -> None:
        self._hint = hint
        self.border_title = None if self.value else (hint or None)

    def _watch_value(self, value: str) -> None:
        super()._watch_value(value)
        self.border_title = None if value else (self._hint or None)

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        if self._uses_native_cursor or y != 0 or not self.has_focus or not self._cursor_visible:
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
