from __future__ import annotations

import shutil


def terminal_width() -> int:
    return shutil.get_terminal_size((100, 30)).columns


def widths(cols: int | None = None) -> tuple[int, int, int]:
    """返回（左边距, 内容宽度, 右边距）：内容约占窗口 36%，水平居中。"""
    cols = cols or terminal_width()
    width = max(40, min(cols - 4, round(cols * 0.36)))
    left = max(0, (cols - width) // 2)
    right = max(0, cols - width - left)
    return left, width, right


def indent(cols: int | None = None) -> str:
    return " " * widths(cols)[0]
