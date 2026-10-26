from __future__ import annotations

from rich.text import Text
from textual.widgets import Input


class BarInput(Input):
    """Input with a thin vertical-bar cursor instead of a full-cell block."""

    def __init__(self, *args, **kwargs):
        placeholder = kwargs.get("placeholder")
        if placeholder and not placeholder.startswith("│"):
            kwargs["placeholder"] = f"│ {placeholder}"
        super().__init__(*args, **kwargs)

    @property
    def _value(self) -> Text:
        text = super()._value
        if not self.has_focus or not self._cursor_visible:
            return text
        cursor = min(self.cursor_position, len(self.value))
        bar = Text("│", style=self.get_component_rich_style("input--cursor"), end="")
        return text[:cursor] + bar + text[cursor:]
